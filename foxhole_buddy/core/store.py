from __future__ import annotations

import json
import secrets
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import fields
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable, Iterator, Type

# Models and pure helpers live in models.py; re-exported here so existing
# `from foxhole_buddy.core.store import Stockpile, ...` imports keep working.
from foxhole_buddy.core.models import (  # noqa: F401
    EXPIRY_HOURS,
    WARNING_THRESHOLDS,
    URGENT_WARNING,
    utc_now,
    parse_dt,
    dt_to_str,
    slugify,
    Stockpile,
    remaining_time,
    warning_due,
    mark_warning_sent,
    format_remaining,
    canonicalize_material_name,
    OffsiteLocation,
    LOGI_OPEN,
    LOGI_CLAIMED,
    LOGI_DELIVERED,
    LogisticsRequest,
    make_line,
    derive_logi_status,
    logi_counts,
    FactoryAlarm,
    OP_SCHEDULED,
    OP_IN_PROGRESS,
    OP_COMPLETED,
    OP_CANCELLED,
    OP_OPEN_STATUSES,
    make_squad,
    Operation,
)


# Fields requiring conversion between Python objects and SQLite storage.
# Booleans are stored as 0/1 INTEGER; list/dict fields are stored as JSON TEXT.
_BOOL_FIELDS = {
    "single_ping", "warned_before", "warned_exact", "warned_after", "completed",
    "warned_30m", "warned_start", "track_expiry",
}
_JSON_FIELDS: set[str] = {
    "reminders_sent", "squads", "going", "tentative", "not_available", "items",
    "duty_user_ids",
}
# JSON fields whose natural empty value is a dict ({}), not a list ([]).
_JSON_DICT_FIELDS: set[str] = {"participant_meta"}


# ── Schema ──────────────────────────────────────────────────────────────────────

_SCHEMA = """
CREATE TABLE IF NOT EXISTS guild_config (
    guild_id             INTEGER PRIMARY KEY,
    channel_id           INTEGER,
    urgent_role_id       INTEGER,
    faction              TEXT,
    ops_channel_id       INTEGER,
    stockpile_channel_id INTEGER,
    relay_channel_id     INTEGER
);

CREATE TABLE IF NOT EXISTS stockpiles (
    id                        TEXT PRIMARY KEY,
    guild_id                  INTEGER NOT NULL,
    channel_id                INTEGER,
    message_id                INTEGER,
    name                      TEXT,
    location                  TEXT,
    type                      TEXT,
    created_by_user_id        INTEGER,
    last_refreshed_at         TEXT,
    expires_at                TEXT,
    last_refreshed_by_user_id INTEGER,
    reminders_sent            TEXT,
    created_at                TEXT,
    updated_at                TEXT,
    duty_user_ids             TEXT,
    track_expiry              INTEGER,
    thread_id                 INTEGER
);
CREATE INDEX IF NOT EXISTS idx_stockpiles_guild ON stockpiles(guild_id);

CREATE TABLE IF NOT EXISTS logistics_requests (
    id                   TEXT PRIMARY KEY,
    guild_id             INTEGER NOT NULL,
    channel_id           INTEGER,
    message_id           INTEGER,
    category             TEXT,
    subcategory          TEXT,
    item                 TEXT,
    quantity             INTEGER,
    requested_by_user_id INTEGER,
    status               TEXT,
    claimed_by_user_id   INTEGER,
    op_id                TEXT,
    notes                TEXT,
    created_at           TEXT,
    updated_at           TEXT,
    items                TEXT
);
CREATE INDEX IF NOT EXISTS idx_logistics_requests_guild ON logistics_requests(guild_id);
CREATE INDEX IF NOT EXISTS idx_logistics_requests_op ON logistics_requests(op_id);

CREATE TABLE IF NOT EXISTS base_inventory (
    guild_id INTEGER NOT NULL,
    material TEXT NOT NULL,
    amount   REAL NOT NULL,
    PRIMARY KEY (guild_id, material)
);

CREATE TABLE IF NOT EXISTS stockpile_inventory (
    stockpile_id TEXT NOT NULL,
    guild_id     INTEGER NOT NULL,
    material     TEXT NOT NULL,
    amount       REAL NOT NULL,
    PRIMARY KEY (stockpile_id, material)
);
CREATE INDEX IF NOT EXISTS idx_stockpile_inventory_guild ON stockpile_inventory(guild_id);

CREATE TABLE IF NOT EXISTS offsite_locations (
    id                 TEXT PRIMARY KEY,
    guild_id           INTEGER NOT NULL,
    name               TEXT,
    location           TEXT,
    type               TEXT,
    created_by_user_id INTEGER,
    created_at         TEXT,
    updated_at         TEXT
);
CREATE INDEX IF NOT EXISTS idx_offsite_locations_guild ON offsite_locations(guild_id);

CREATE TABLE IF NOT EXISTS offsite_inventory (
    location_id TEXT NOT NULL,
    guild_id    INTEGER NOT NULL,
    material    TEXT NOT NULL,
    amount      REAL NOT NULL,
    PRIMARY KEY (location_id, material)
);
CREATE INDEX IF NOT EXISTS idx_offsite_inventory_guild ON offsite_inventory(guild_id);

CREATE TABLE IF NOT EXISTS factory_alarms (
    id                 TEXT PRIMARY KEY,
    guild_id           INTEGER NOT NULL,
    channel_id         INTEGER,
    message_id         INTEGER,
    facility_name      TEXT,
    created_by_user_id INTEGER,
    end_time           TEXT,
    single_ping        INTEGER,
    warned_before      INTEGER,
    warned_exact       INTEGER,
    warned_after       INTEGER,
    completed          INTEGER
);
CREATE INDEX IF NOT EXISTS idx_factory_alarms_guild ON factory_alarms(guild_id);

CREATE TABLE IF NOT EXISTS operations (
    id              TEXT PRIMARY KEY,
    op_number       INTEGER,
    guild_id        INTEGER NOT NULL,
    channel_id      INTEGER,
    message_id      INTEGER,
    name            TEXT,
    description     TEXT,
    location        TEXT,
    war_number      INTEGER,
    scheduled_at    TEXT,
    leader_user_id  INTEGER,
    status          TEXT,
    squads          TEXT,
    going           TEXT,
    tentative       TEXT,
    not_available   TEXT,
    warned_30m      INTEGER,
    warned_start    INTEGER,
    created_at      TEXT,
    ally_room       TEXT,
    participant_meta TEXT,
    thread_id       INTEGER
);
CREATE INDEX IF NOT EXISTS idx_operations_guild ON operations(guild_id);

CREATE TABLE IF NOT EXISTS operation_mirrors (
    op_id      TEXT NOT NULL,
    guild_id   INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    message_id INTEGER,
    PRIMARY KEY (op_id, guild_id)
);
CREATE INDEX IF NOT EXISTS idx_operation_mirrors_op ON operation_mirrors(op_id);
CREATE INDEX IF NOT EXISTS idx_operation_mirrors_guild ON operation_mirrors(guild_id);

CREATE TABLE IF NOT EXISTS ally_memberships (
    room_code  TEXT NOT NULL,
    guild_id   INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    created_at TEXT,
    PRIMARY KEY (room_code, guild_id)
);
CREATE INDEX IF NOT EXISTS idx_ally_memberships_guild ON ally_memberships(guild_id);
"""

def _schema_columns(schema: str) -> dict[str, list[tuple[str, str]]]:
    """Parse `CREATE TABLE` blocks → {table: [(column, alter_safe_definition)]}.

    Used to add columns missing from tables created by older versions. Constraint
    lines (PRIMARY KEY (...), etc.) and PK/NOT NULL/UNIQUE modifiers are stripped
    so the definitions are valid in `ALTER TABLE ADD COLUMN`.
    """
    import re

    tables: dict[str, list[tuple[str, str]]] = {}
    for match in re.finditer(
        r"CREATE TABLE IF NOT EXISTS (\w+)\s*\((.*?)\);", schema, re.S
    ):
        name = match.group(1)
        cols: list[tuple[str, str]] = []
        for raw in match.group(2).split("\n"):
            line = raw.strip().rstrip(",").strip()
            if not line:
                continue
            if line.upper().startswith(("PRIMARY KEY", "FOREIGN KEY", "UNIQUE", "CHECK", "CONSTRAINT")):
                continue
            parts = line.split(None, 1)
            colname = parts[0]
            coltype = parts[1] if len(parts) > 1 else "TEXT"
            for token in ("PRIMARY KEY", "NOT NULL", "UNIQUE"):
                coltype = re.sub(token, "", coltype, flags=re.IGNORECASE)
            cols.append((colname, f"{colname} {' '.join(coltype.split())}".strip()))
        tables[name] = cols
    return tables


# Tables that hold per-guild rows, used by purge_guild() / known_guild_ids().
_GUILD_TABLES = (
    "guild_config",
    "stockpiles",
    "logistics_requests",
    "base_inventory",
    "offsite_locations",
    "offsite_inventory",
    "factory_alarms",
    "operations",
    "operation_mirrors",
    "ally_memberships",
)


class StockpileStore:
    """SQLite-backed persistence for all bot domains.

    Data is partitioned by ``guild_id`` (multi-tenancy) and every per-guild
    table is indexed on it, so per-guild reads and full-guild purges are cheap.
    """

    def __init__(self, path: str | Path, *, migrate_from: str | Path | None = None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()
        self._maybe_migrate_legacy_json(migrate_from)
        self.migrate_inventory_to_stockpiles()

    # ------------------------------------------------------------------
    # Connection / schema
    # ------------------------------------------------------------------

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._db() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)
            self._ensure_columns(conn)

    def migrate_inventory_to_stockpiles(self) -> None:
        """One-time, idempotent unification of the old inventory features into
        stockpiles: every off-site location becomes a timer-less stockpile
        carrying its items, and each guild's base inventory becomes a single
        timer-less "Base Inventory" stockpile. Old rows are deleted as they are
        migrated, so once done there is nothing left to re-run.

        Also backfills ``track_expiry`` for stockpiles predating that column
        (NULL would otherwise decode as False, silently disabling their timers).
        """
        now = dt_to_str(utc_now())
        # Timer-less stockpiles must never trip the expiry path, so their
        # expires_at sits far in the future (the reminder loop also skips them
        # on ``track_expiry``, but this is defence-in-depth against any check).
        never = "2099-01-01T00:00:00+00:00"

        def _insert_timerless(conn, sp_id, guild_id, name, location, sp_type, creator):
            conn.execute(
                "INSERT OR IGNORE INTO stockpiles "
                "(id, guild_id, channel_id, message_id, name, location, type, "
                " created_by_user_id, last_refreshed_at, expires_at, "
                " last_refreshed_by_user_id, reminders_sent, created_at, updated_at, "
                " duty_user_ids, track_expiry, thread_id) "
                "VALUES (?, ?, NULL, NULL, ?, ?, ?, ?, ?, ?, ?, '[]', ?, ?, '[]', 0, NULL)",
                (sp_id, guild_id, name, location, sp_type, creator,
                 now, never, creator, now, now),
            )

        with self._db() as conn:
            # Pre-feature stockpiles are all real timers.
            conn.execute(
                "UPDATE stockpiles SET track_expiry = 1 WHERE track_expiry IS NULL"
            )

            # Off-site locations → timer-less stockpiles (reuse the id so the
            # migration is naturally idempotent and inventory keys line up).
            locations = conn.execute("SELECT * FROM offsite_locations").fetchall()
            for loc in locations:
                _insert_timerless(
                    conn, loc["id"], loc["guild_id"], loc["name"],
                    loc["location"] or "", loc["type"] or "storage_depot",
                    loc["created_by_user_id"],
                )
                conn.execute(
                    "INSERT OR IGNORE INTO stockpile_inventory "
                    "(stockpile_id, guild_id, material, amount) "
                    "SELECT location_id, guild_id, material, amount FROM offsite_inventory "
                    "WHERE location_id = ?",
                    (loc["id"],),
                )
                conn.execute("DELETE FROM offsite_inventory WHERE location_id = ?", (loc["id"],))
                conn.execute("DELETE FROM offsite_locations WHERE id = ?", (loc["id"],))

            # Base inventory → one timer-less "Base Inventory" stockpile per guild
            # (deterministic id so a re-run finds nothing to migrate).
            guild_rows = conn.execute(
                "SELECT DISTINCT guild_id FROM base_inventory"
            ).fetchall()
            for row in guild_rows:
                guild_id = row["guild_id"]
                sp_id = f"base-{guild_id}"
                _insert_timerless(conn, sp_id, guild_id, "Base Inventory", "", "storage_depot", 0)
                conn.execute(
                    "INSERT OR IGNORE INTO stockpile_inventory "
                    "(stockpile_id, guild_id, material, amount) "
                    "SELECT ?, guild_id, material, amount FROM base_inventory "
                    "WHERE guild_id = ?",
                    (sp_id, guild_id),
                )
                conn.execute("DELETE FROM base_inventory WHERE guild_id = ?", (guild_id,))

            # Recover any orphaned inventory: rows whose parent stockpile is gone
            # (e.g. a base stockpile deleted by an over-eager expiry before the
            # timer-less skip existed). Rebuild a timer-less parent so the items
            # become visible again instead of being stranded.
            orphans = conn.execute(
                "SELECT DISTINCT si.stockpile_id, si.guild_id FROM stockpile_inventory si "
                "LEFT JOIN stockpiles s ON s.id = si.stockpile_id WHERE s.id IS NULL"
            ).fetchall()
            for orphan in orphans:
                oid = orphan["stockpile_id"]
                name = "Base Inventory" if str(oid).startswith("base-") else "Recovered Inventory"
                _insert_timerless(conn, oid, orphan["guild_id"], name, "", "storage_depot", 0)

    def _ensure_columns(self, conn: sqlite3.Connection) -> None:
        """Add any columns missing from existing tables (lightweight migration).

        ``CREATE TABLE IF NOT EXISTS`` won't alter a table created by an older
        version, so we reconcile each table's columns against the schema and
        ``ALTER TABLE ADD COLUMN`` whatever is missing (always nullable).
        """
        for table, columns in _schema_columns(_SCHEMA).items():
            existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
            if not existing:
                continue  # table was just created with the full, current schema
            for name, definition in columns:
                if name not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")

    # ------------------------------------------------------------------
    # Object <-> row mapping
    # ------------------------------------------------------------------

    @staticmethod
    def _encode_row(obj) -> dict:
        out: dict = {}
        for f in fields(obj):
            value = getattr(obj, f.name)
            if f.name in _JSON_FIELDS or f.name in _JSON_DICT_FIELDS:
                value = json.dumps(value)
            elif f.name in _BOOL_FIELDS:
                value = 1 if value else 0
            out[f.name] = value
        return out

    @staticmethod
    def _decode_row(cls: Type, row: sqlite3.Row):
        kwargs: dict = {}
        for f in fields(cls):
            value = row[f.name]
            if f.name in _JSON_DICT_FIELDS:
                value = json.loads(value) if value is not None else {}
            elif f.name in _JSON_FIELDS:
                value = json.loads(value) if value is not None else []
            elif f.name in _BOOL_FIELDS:
                value = bool(value)
            kwargs[f.name] = value
        return cls(**kwargs)

    def _upsert(self, conn: sqlite3.Connection, table: str, obj) -> None:
        data = self._encode_row(obj)
        cols = list(data)
        placeholders = ", ".join("?" for _ in cols)
        updates = ", ".join(f"{c}=excluded.{c}" for c in cols if c != "id")
        conn.execute(
            f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders}) "
            f"ON CONFLICT(id) DO UPDATE SET {updates}",
            [data[c] for c in cols],
        )

    def _exists(self, conn: sqlite3.Connection, table: str, row_id: str) -> bool:
        cur = conn.execute(f"SELECT 1 FROM {table} WHERE id = ?", (row_id,))
        return cur.fetchone() is not None

    # ------------------------------------------------------------------
    # Guild configuration
    # ------------------------------------------------------------------

    _CONFIG_FIELDS = (
        "channel_id", "urgent_role_id", "faction", "ops_channel_id", "stockpile_channel_id",
        "relay_channel_id",
    )

    def _guild_config_row(self, guild_id: int) -> sqlite3.Row | None:
        with self._db() as conn:
            return conn.execute(
                "SELECT * FROM guild_config WHERE guild_id = ?", (guild_id,)
            ).fetchone()

    def get_guild_config(self, guild_id: int) -> dict:
        """All config columns for a guild as a dict (missing → None)."""
        row = self._guild_config_row(guild_id)
        base = {col: None for col in self._CONFIG_FIELDS}
        if row is not None:
            base.update({col: row[col] for col in self._CONFIG_FIELDS})
        return base

    def get_guild_channel(self, guild_id: int) -> int | None:
        row = self._guild_config_row(guild_id)
        return row["channel_id"] if row else None

    def get_guild_urgent_role(self, guild_id: int) -> int | None:
        row = self._guild_config_row(guild_id)
        return row["urgent_role_id"] if row else None

    def get_guild_faction(self, guild_id: int) -> str | None:
        row = self._guild_config_row(guild_id)
        return row["faction"] if row else None

    def get_relay_channel(self, guild_id: int) -> int | None:
        """The guild's regiment-chat relay channel, or None if it hasn't joined."""
        row = self._guild_config_row(guild_id)
        return row["relay_channel_id"] if row else None

    def relay_channels(self) -> list[tuple[int, int]]:
        """(guild_id, channel_id) for every guild that has joined the regi-chat
        lobby — i.e. set a relay channel. This is the relay's fan-out list."""
        with self._db() as conn:
            rows = conn.execute(
                "SELECT guild_id, relay_channel_id FROM guild_config "
                "WHERE relay_channel_id IS NOT NULL"
            ).fetchall()
        return [(row["guild_id"], row["relay_channel_id"]) for row in rows]

    # ------------------------------------------------------------------
    # Ally chat — private invite-code rooms (multiple per guild)
    # ------------------------------------------------------------------

    # Unambiguous alphabet for room codes (no O/0, I/1, etc.).
    _ALLY_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"

    def _ally_code_exists(self, conn: sqlite3.Connection, code: str) -> bool:
        return conn.execute(
            "SELECT 1 FROM ally_memberships WHERE room_code = ? LIMIT 1", (code,)
        ).fetchone() is not None

    def create_ally_room(self, guild_id: int, channel_id: int) -> str:
        """Create a new ally room with a fresh unique code, joining ``guild_id``
        via ``channel_id``. Returns the shareable code."""
        with self._db() as conn:
            while True:
                code = "ALLY-" + "".join(secrets.choice(self._ALLY_ALPHABET) for _ in range(6))
                if not self._ally_code_exists(conn, code):
                    break
            conn.execute(
                "INSERT INTO ally_memberships (room_code, guild_id, channel_id, created_at) "
                "VALUES (?, ?, ?, ?)",
                (code, guild_id, channel_id, dt_to_str(utc_now())),
            )
        return code

    def join_ally_room(self, guild_id: int, channel_id: int, room_code: str) -> str:
        """Join an existing ally room. Returns a status string: ``ok``,
        ``not_found``, ``channel_in_use``, or ``already_member``."""
        code = (room_code or "").strip().upper()
        with self._db() as conn:
            if not self._ally_code_exists(conn, code):
                return "not_found"
            if conn.execute(
                "SELECT 1 FROM ally_memberships WHERE room_code = ? AND guild_id = ?",
                (code, guild_id),
            ).fetchone():
                return "already_member"
            if conn.execute(
                "SELECT 1 FROM ally_memberships WHERE guild_id = ? AND channel_id = ?",
                (guild_id, channel_id),
            ).fetchone():
                return "channel_in_use"
            conn.execute(
                "INSERT INTO ally_memberships (room_code, guild_id, channel_id, created_at) "
                "VALUES (?, ?, ?, ?)",
                (code, guild_id, channel_id, dt_to_str(utc_now())),
            )
        return "ok"

    def leave_ally_room(self, guild_id: int, room_code: str) -> None:
        with self._db() as conn:
            conn.execute(
                "DELETE FROM ally_memberships WHERE guild_id = ? AND room_code = ?",
                (guild_id, room_code),
            )

    def ally_rooms_for_guild(self, guild_id: int) -> list[dict]:
        """[{room_code, channel_id, members}] for one guild's ally rooms."""
        with self._db() as conn:
            rows = conn.execute(
                "SELECT room_code, channel_id FROM ally_memberships WHERE guild_id = ? "
                "ORDER BY created_at",
                (guild_id,),
            ).fetchall()
            result = []
            for row in rows:
                members = conn.execute(
                    "SELECT COUNT(*) AS n FROM ally_memberships WHERE room_code = ?",
                    (row["room_code"],),
                ).fetchone()["n"]
                result.append({
                    "room_code": row["room_code"],
                    "channel_id": row["channel_id"],
                    "members": members,
                })
        return result

    def ally_room_by_channel(self, guild_id: int, channel_id: int) -> str | None:
        """The ally room a channel is bound to, or None."""
        with self._db() as conn:
            row = conn.execute(
                "SELECT room_code FROM ally_memberships WHERE guild_id = ? AND channel_id = ?",
                (guild_id, channel_id),
            ).fetchone()
        return row["room_code"] if row else None

    def ally_members(self, room_code: str) -> list[tuple[int, int]]:
        """(guild_id, channel_id) for every member of an ally room — fan-out list."""
        with self._db() as conn:
            rows = conn.execute(
                "SELECT guild_id, channel_id FROM ally_memberships WHERE room_code = ?",
                (room_code,),
            ).fetchall()
        return [(row["guild_id"], row["channel_id"]) for row in rows]

    def get_alert_channel(self, guild_id: int, kind: str) -> int | None:
        """Channel for an alert ``kind`` ("stockpile"/"ops"), falling back to the
        main configured channel."""
        row = self._guild_config_row(guild_id)
        if row is None:
            return None
        override = {"stockpile": "stockpile_channel_id", "ops": "ops_channel_id"}.get(kind)
        if override and row[override]:
            return row[override]
        return row["channel_id"]

    def update_guild_config(self, guild_id: int, **fields) -> None:
        """Partial config update — only the provided columns are changed."""
        fields = {k: v for k, v in fields.items() if k in self._CONFIG_FIELDS}
        with self._db() as conn:
            conn.execute("INSERT OR IGNORE INTO guild_config (guild_id) VALUES (?)", (guild_id,))
            if fields:
                assignments = ", ".join(f"{col} = ?" for col in fields)
                conn.execute(
                    f"UPDATE guild_config SET {assignments} WHERE guild_id = ?",
                    [*fields.values(), guild_id],
                )

    def set_guild_config(
        self, guild_id: int, channel_id: int, urgent_role_id: int | None = None
    ) -> None:
        self.update_guild_config(
            guild_id, channel_id=channel_id, urgent_role_id=urgent_role_id
        )

    # ------------------------------------------------------------------
    # Stockpile CRUD
    # ------------------------------------------------------------------

    def all(self, guild_id: int | None = None) -> list[Stockpile]:
        with self._db() as conn:
            if guild_id is not None:
                rows = conn.execute(
                    "SELECT * FROM stockpiles WHERE guild_id = ?", (guild_id,)
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM stockpiles").fetchall()
        return [self._decode_row(Stockpile, r) for r in rows]

    def save_all(self, stockpiles: Iterable[Stockpile]) -> None:
        with self._db() as conn:
            conn.execute("DELETE FROM stockpiles")
            for stockpile in stockpiles:
                self._upsert(conn, "stockpiles", stockpile)

    def get(self, stockpile_id: str, guild_id: int | None = None) -> Stockpile | None:
        with self._db() as conn:
            row = conn.execute(
                "SELECT * FROM stockpiles WHERE id = ?", (stockpile_id,)
            ).fetchone()
        if row is None:
            return None
        stockpile = self._decode_row(Stockpile, row)
        if guild_id is not None and stockpile.guild_id != guild_id:
            return None
        return stockpile

    def create(
        self,
        *,
        guild_id: int,
        channel_id: int,
        name: str,
        location: str,
        stockpile_type: str,
        user_id: int,
        track_expiry: bool = True,
        now: datetime | None = None,
    ) -> Stockpile:
        current = now or utc_now()
        current_str = dt_to_str(current)
        stockpile = Stockpile(
            id=uuid.uuid4().hex[:8],
            guild_id=guild_id,
            channel_id=channel_id,
            message_id=None,
            name=name.strip(),
            location=location.strip(),
            type=stockpile_type,
            created_by_user_id=user_id,
            last_refreshed_at=current_str,
            expires_at=dt_to_str(current + timedelta(hours=EXPIRY_HOURS)),
            last_refreshed_by_user_id=user_id,
            reminders_sent=[],
            created_at=current_str,
            updated_at=current_str,
            duty_user_ids=[],
            track_expiry=track_expiry,
        )
        with self._db() as conn:
            self._upsert(conn, "stockpiles", stockpile)
        return stockpile

    def update(self, updated: Stockpile) -> Stockpile:
        with self._db() as conn:
            if not self._exists(conn, "stockpiles", updated.id):
                raise KeyError(f"Unknown stockpile id: {updated.id}")
            updated.updated_at = dt_to_str(utc_now())
            self._upsert(conn, "stockpiles", updated)
        return updated

    def set_message_id(self, stockpile_id: str, message_id: int) -> Stockpile:
        stockpile = self.get(stockpile_id)
        if stockpile is None:
            raise KeyError(f"Unknown stockpile id: {stockpile_id}")
        stockpile.message_id = message_id
        return self.update(stockpile)

    def attach_stockpile_thread(
        self, stockpile_id: str, *, thread_id: int, message_id: int
    ) -> Stockpile:
        """Bind a freshly created per-stockpile forum post: the card now lives in
        the thread, so ``channel_id`` points at it and ``thread_id`` marks the
        stockpile for thread-aware cleanup (mirrors ``attach_operation_thread``)."""
        stockpile = self.get(stockpile_id)
        if stockpile is None:
            raise KeyError(f"Unknown stockpile id: {stockpile_id}")
        stockpile.thread_id = thread_id
        stockpile.channel_id = thread_id
        stockpile.message_id = message_id
        return self.update(stockpile)

    def refresh(
        self,
        stockpile_id: str,
        *,
        user_id: int,
        guild_id: int | None = None,
        now: datetime | None = None,
    ) -> Stockpile:
        stockpile = self.get(stockpile_id, guild_id=guild_id)
        if stockpile is None:
            raise KeyError(f"Unknown stockpile id: {stockpile_id}")

        current = now or utc_now()
        stockpile.last_refreshed_at = dt_to_str(current)
        stockpile.expires_at = dt_to_str(current + timedelta(hours=EXPIRY_HOURS))
        stockpile.last_refreshed_by_user_id = user_id
        stockpile.reminders_sent = []
        return self.update(stockpile)

    def delete(self, stockpile_id: str, guild_id: int | None = None) -> bool:
        if self.get(stockpile_id, guild_id=guild_id) is None:
            return False
        with self._db() as conn:
            conn.execute("DELETE FROM stockpiles WHERE id = ?", (stockpile_id,))
            # A stockpile's inventory is meaningless without it.
            conn.execute(
                "DELETE FROM stockpile_inventory WHERE stockpile_id = ?", (stockpile_id,)
            )
        return True

    # ------------------------------------------------------------------
    # Per-stockpile inventory (the items stored in a stockpile)
    # ------------------------------------------------------------------

    def get_stockpile_inventory(self, stockpile_id: str) -> dict[str, float]:
        with self._db() as conn:
            rows = conn.execute(
                "SELECT material, amount FROM stockpile_inventory WHERE stockpile_id = ?",
                (stockpile_id,),
            ).fetchall()
        return {r["material"]: r["amount"] for r in rows}

    def add_to_stockpile_inventory(
        self, guild_id: int, stockpile_id: str, material: str, amount: float
    ) -> dict[str, float]:
        if amount <= 0:
            raise ValueError("Amount to add must be greater than zero.")
        material = canonicalize_material_name(material)
        with self._db() as conn:
            conn.execute(
                "INSERT INTO stockpile_inventory (stockpile_id, guild_id, material, amount) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(stockpile_id, material) DO UPDATE SET "
                "amount = amount + excluded.amount",
                (stockpile_id, guild_id, material, amount),
            )
        return self.get_stockpile_inventory(stockpile_id)

    def remove_from_stockpile_inventory(
        self, stockpile_id: str, material: str, amount: float
    ) -> dict[str, float]:
        if amount <= 0:
            raise ValueError("Amount to remove must be greater than zero.")
        material = canonicalize_material_name(material)
        with self._db() as conn:
            row = conn.execute(
                "SELECT amount FROM stockpile_inventory WHERE stockpile_id = ? AND material = ?",
                (stockpile_id, material),
            ).fetchone()
            if row is None:
                raise KeyError(f"'{material}' is not in this stockpile.")
            current = row["amount"]
            if amount > current:
                raise ValueError(
                    f"Cannot remove {amount} {material}. Only {current} available."
                )
            new_amount = current - amount
            if new_amount <= 0:
                conn.execute(
                    "DELETE FROM stockpile_inventory WHERE stockpile_id = ? AND material = ?",
                    (stockpile_id, material),
                )
            else:
                conn.execute(
                    "UPDATE stockpile_inventory SET amount = ? "
                    "WHERE stockpile_id = ? AND material = ?",
                    (new_amount, stockpile_id, material),
                )
        return self.get_stockpile_inventory(stockpile_id)

    def clear_stockpile_inventory(self, stockpile_id: str) -> None:
        with self._db() as conn:
            conn.execute(
                "DELETE FROM stockpile_inventory WHERE stockpile_id = ?", (stockpile_id,)
            )

    def set_stockpile_duty(
        self, stockpile_id: str, user_ids: Iterable[int], guild_id: int | None = None
    ) -> Stockpile:
        """Replace a stockpile's refresh-duty roster (order-preserving, deduped).

        These are the members pinged when the stockpile runs low, instead of
        blasting the whole channel. Raises ``KeyError`` if the id is unknown.
        """
        stockpile = self.get(stockpile_id, guild_id=guild_id)
        if stockpile is None:
            raise KeyError(f"Unknown stockpile id: {stockpile_id}")
        seen: set[int] = set()
        roster: list[int] = []
        for uid in user_ids:
            uid = int(uid)
            if uid not in seen:
                seen.add(uid)
                roster.append(uid)
        stockpile.duty_user_ids = roster
        return self.update(stockpile)

    # ------------------------------------------------------------------
    # Logistics requests
    # ------------------------------------------------------------------

    def get_logistics_requests(
        self, guild_id: int | None = None, *, include_delivered: bool = True
    ) -> list[LogisticsRequest]:
        query = "SELECT * FROM logistics_requests"
        params: list = []
        clauses = []
        if guild_id is not None:
            clauses.append("guild_id = ?")
            params.append(guild_id)
        if not include_delivered:
            clauses.append("status != ?")
            params.append(LOGI_DELIVERED)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at"
        with self._db() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._decode_row(LogisticsRequest, r) for r in rows]

    def get_logistics_request(
        self, request_id: str, guild_id: int | None = None
    ) -> LogisticsRequest | None:
        with self._db() as conn:
            row = conn.execute(
                "SELECT * FROM logistics_requests WHERE id = ?", (request_id,)
            ).fetchone()
        if row is None:
            return None
        request = self._decode_row(LogisticsRequest, row)
        if guild_id is not None and request.guild_id != guild_id:
            return None
        return request

    def create_logistics_request(
        self,
        *,
        guild_id: int,
        channel_id: int,
        items: list[dict],
        user_id: int,
        notes: str = "",
        op_id: str | None = None,
    ) -> LogisticsRequest:
        """Create a request from one or more line items (see ``make_line``).

        The scalar ``category``/``subcategory``/``item``/``quantity`` fields
        mirror the first line for backward compatibility.
        """
        if not items:
            raise ValueError("A logistics request needs at least one item.")
        now = dt_to_str(utc_now())
        first = items[0]
        request = LogisticsRequest(
            id=uuid.uuid4().hex[:8],
            guild_id=guild_id,
            channel_id=channel_id,
            message_id=None,
            category=first["category"],
            subcategory=first["subcategory"],
            item=first["item"],
            quantity=first["quantity"],
            requested_by_user_id=user_id,
            status=derive_logi_status(items),
            claimed_by_user_id=None,
            op_id=op_id,
            notes=notes.strip(),
            created_at=now,
            updated_at=now,
            items=items,
        )
        with self._db() as conn:
            self._upsert(conn, "logistics_requests", request)
        return request

    def update_logistics_request(self, updated: LogisticsRequest) -> LogisticsRequest:
        with self._db() as conn:
            if not self._exists(conn, "logistics_requests", updated.id):
                raise KeyError(f"Unknown logistics request id: {updated.id}")
            updated.updated_at = dt_to_str(utc_now())
            self._upsert(conn, "logistics_requests", updated)
        return updated

    def set_logistics_request_message_id(
        self, request_id: str, message_id: int
    ) -> LogisticsRequest:
        request = self.get_logistics_request(request_id)
        if request is None:
            raise KeyError(request_id)
        request.message_id = message_id
        return self.update_logistics_request(request)

    def _save_logistics_lines(
        self, request: LogisticsRequest, lines: list[dict]
    ) -> LogisticsRequest:
        """Persist mutated line items and re-derive the rolled-up fields."""
        request.items = lines
        request.status = derive_logi_status(lines)
        first = lines[0] if lines else None
        if first:
            request.category = first["category"]
            request.subcategory = first["subcategory"]
            request.item = first["item"]
            request.quantity = first["quantity"]
        # The request-level driver only means "one person took the whole list";
        # clear it once the lines have mixed/distinct drivers.
        claimers = {ln["claimed_by_user_id"] for ln in lines if ln["status"] == LOGI_CLAIMED}
        request.claimed_by_user_id = next(iter(claimers)) if len(claimers) == 1 else None
        return self.update_logistics_request(request)

    # -- per-line actions --

    def claim_logistics_line(
        self, request_id: str, lid: str, *, user_id: int, guild_id: int | None = None
    ) -> LogisticsRequest:
        """A driver claims a single open line item."""
        request = self.get_logistics_request(request_id, guild_id)
        if request is None:
            raise KeyError(request_id)
        lines = request.line_items()
        for line in lines:
            if line["lid"] == lid and line["status"] == LOGI_OPEN:
                line["status"] = LOGI_CLAIMED
                line["claimed_by_user_id"] = user_id
        return self._save_logistics_lines(request, lines)

    def unclaim_logistics_line(
        self, request_id: str, lid: str, *, guild_id: int | None = None
    ) -> LogisticsRequest:
        """Release a single claimed line back to open."""
        request = self.get_logistics_request(request_id, guild_id)
        if request is None:
            raise KeyError(request_id)
        lines = request.line_items()
        for line in lines:
            if line["lid"] == lid and line["status"] == LOGI_CLAIMED:
                line["status"] = LOGI_OPEN
                line["claimed_by_user_id"] = None
        return self._save_logistics_lines(request, lines)

    def validate_logistics_line(
        self, request_id: str, lid: str, *, guild_id: int | None = None
    ) -> LogisticsRequest:
        """Mark a single claimed line as delivered."""
        request = self.get_logistics_request(request_id, guild_id)
        if request is None:
            raise KeyError(request_id)
        lines = request.line_items()
        for line in lines:
            if line["lid"] == lid and line["status"] == LOGI_CLAIMED:
                line["status"] = LOGI_DELIVERED
        return self._save_logistics_lines(request, lines)

    # -- whole-list actions --

    def claim_all_logistics(
        self, request_id: str, *, user_id: int, guild_id: int | None = None
    ) -> LogisticsRequest:
        """One driver claims every still-open line in the request."""
        request = self.get_logistics_request(request_id, guild_id)
        if request is None:
            raise KeyError(request_id)
        lines = request.line_items()
        for line in lines:
            if line["status"] == LOGI_OPEN:
                line["status"] = LOGI_CLAIMED
                line["claimed_by_user_id"] = user_id
        return self._save_logistics_lines(request, lines)

    def validate_all_logistics(
        self, request_id: str, *, user_id: int, is_manager: bool = False,
        guild_id: int | None = None,
    ) -> LogisticsRequest:
        """Deliver every claimed line the caller may close (their own lines, or
        all of them with Manage Server)."""
        request = self.get_logistics_request(request_id, guild_id)
        if request is None:
            raise KeyError(request_id)
        lines = request.line_items()
        for line in lines:
            if line["status"] == LOGI_CLAIMED and (
                is_manager or line["claimed_by_user_id"] == user_id
            ):
                line["status"] = LOGI_DELIVERED
        return self._save_logistics_lines(request, lines)

    def revoke_logistics(
        self, request_id: str, *, user_id: int, is_manager: bool = False,
        guild_id: int | None = None,
    ) -> LogisticsRequest:
        """Release claimed lines back to open (the caller's lines, or all with
        Manage Server)."""
        request = self.get_logistics_request(request_id, guild_id)
        if request is None:
            raise KeyError(request_id)
        lines = request.line_items()
        for line in lines:
            if line["status"] == LOGI_CLAIMED and (
                is_manager or line["claimed_by_user_id"] == user_id
            ):
                line["status"] = LOGI_OPEN
                line["claimed_by_user_id"] = None
        return self._save_logistics_lines(request, lines)

    def set_logistics_op(
        self, request_id: str, op_id: str | None, guild_id: int | None = None
    ) -> LogisticsRequest:
        """Link (or unlink with op_id=None) a request to an operation."""
        request = self.get_logistics_request(request_id, guild_id)
        if request is None:
            raise KeyError(request_id)
        request.op_id = op_id
        return self.update_logistics_request(request)

    def get_logistics_requests_for_op(
        self, op_id: str, guild_id: int | None = None
    ) -> list[LogisticsRequest]:
        query = "SELECT * FROM logistics_requests WHERE op_id = ?"
        params: list = [op_id]
        if guild_id is not None:
            query += " AND guild_id = ?"
            params.append(guild_id)
        query += " ORDER BY created_at"
        with self._db() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._decode_row(LogisticsRequest, r) for r in rows]

    def delete_logistics_request(
        self, request_id: str, guild_id: int | None = None
    ) -> bool:
        if self.get_logistics_request(request_id, guild_id) is None:
            return False
        with self._db() as conn:
            conn.execute("DELETE FROM logistics_requests WHERE id = ?", (request_id,))
        return True

    # ------------------------------------------------------------------
    # Base inventory
    # ------------------------------------------------------------------

    def get_base_inventory(self, guild_id: int) -> dict[str, float]:
        with self._db() as conn:
            rows = conn.execute(
                "SELECT material, amount FROM base_inventory WHERE guild_id = ?",
                (guild_id,),
            ).fetchall()
        return {r["material"]: r["amount"] for r in rows}

    def add_to_base_inventory(self, guild_id: int, material: str, amount: float) -> dict[str, float]:
        if amount <= 0:
            raise ValueError("Amount to add must be greater than zero.")
        material = canonicalize_material_name(material)
        with self._db() as conn:
            conn.execute(
                "INSERT INTO base_inventory (guild_id, material, amount) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT(guild_id, material) DO UPDATE SET "
                "amount = amount + excluded.amount",
                (guild_id, material, amount),
            )
        return self.get_base_inventory(guild_id)

    def remove_from_base_inventory(self, guild_id: int, material: str, amount: float) -> dict[str, float]:
        if amount <= 0:
            raise ValueError("Amount to remove must be greater than zero.")
        material = canonicalize_material_name(material)
        with self._db() as conn:
            row = conn.execute(
                "SELECT amount FROM base_inventory WHERE guild_id = ? AND material = ?",
                (guild_id, material),
            ).fetchone()
            if row is None:
                raise KeyError(f"'{material}' is not in the base inventory.")
            current = row["amount"]
            if amount > current:
                raise ValueError(
                    f"Cannot remove {amount} {material}. Only {current} available."
                )
            new_amount = current - amount
            if new_amount <= 0:
                conn.execute(
                    "DELETE FROM base_inventory WHERE guild_id = ? AND material = ?",
                    (guild_id, material),
                )
            else:
                conn.execute(
                    "UPDATE base_inventory SET amount = ? WHERE guild_id = ? AND material = ?",
                    (new_amount, guild_id, material),
                )
        return self.get_base_inventory(guild_id)

    # ------------------------------------------------------------------
    # Off-site inventories (named locations + their materials)
    # ------------------------------------------------------------------

    def list_offsite_locations(self, guild_id: int) -> list[OffsiteLocation]:
        with self._db() as conn:
            rows = conn.execute(
                "SELECT * FROM offsite_locations WHERE guild_id = ? "
                "ORDER BY name COLLATE NOCASE",
                (guild_id,),
            ).fetchall()
        return [self._decode_row(OffsiteLocation, r) for r in rows]

    def create_offsite_location(
        self,
        *,
        guild_id: int,
        name: str,
        location: str,
        type: str,
        user_id: int,
        now: datetime | None = None,
    ) -> OffsiteLocation:
        current_str = dt_to_str(now or utc_now())
        loc = OffsiteLocation(
            id=uuid.uuid4().hex[:8],
            guild_id=guild_id,
            name=name.strip(),
            location=location.strip(),
            type=type,
            created_by_user_id=user_id,
            created_at=current_str,
            updated_at=current_str,
        )
        with self._db() as conn:
            self._upsert(conn, "offsite_locations", loc)
        return loc

    def get_offsite_location(
        self, location_id: str, guild_id: int | None = None
    ) -> OffsiteLocation | None:
        with self._db() as conn:
            row = conn.execute(
                "SELECT * FROM offsite_locations WHERE id = ?", (location_id,)
            ).fetchone()
        if row is None:
            return None
        loc = self._decode_row(OffsiteLocation, row)
        if guild_id is not None and loc.guild_id != guild_id:
            return None
        return loc

    def update_offsite_location(self, updated: OffsiteLocation) -> OffsiteLocation:
        with self._db() as conn:
            if not self._exists(conn, "offsite_locations", updated.id):
                raise KeyError(f"Unknown off-site location id: {updated.id}")
            updated.updated_at = dt_to_str(utc_now())
            self._upsert(conn, "offsite_locations", updated)
        return updated

    def delete_offsite_location(
        self, location_id: str, guild_id: int | None = None
    ) -> bool:
        if self.get_offsite_location(location_id, guild_id=guild_id) is None:
            return False
        with self._db() as conn:
            conn.execute(
                "DELETE FROM offsite_inventory WHERE location_id = ?", (location_id,)
            )
            conn.execute(
                "DELETE FROM offsite_locations WHERE id = ?", (location_id,)
            )
        return True

    def get_offsite_inventory(self, location_id: str) -> dict[str, float]:
        with self._db() as conn:
            rows = conn.execute(
                "SELECT material, amount FROM offsite_inventory WHERE location_id = ?",
                (location_id,),
            ).fetchall()
        return {r["material"]: r["amount"] for r in rows}

    def add_to_offsite_inventory(
        self, guild_id: int, location_id: str, material: str, amount: float
    ) -> dict[str, float]:
        if amount <= 0:
            raise ValueError("Amount to add must be greater than zero.")
        material = canonicalize_material_name(material)
        with self._db() as conn:
            conn.execute(
                "INSERT INTO offsite_inventory (location_id, guild_id, material, amount) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(location_id, material) DO UPDATE SET "
                "amount = amount + excluded.amount",
                (location_id, guild_id, material, amount),
            )
        return self.get_offsite_inventory(location_id)

    def remove_from_offsite_inventory(
        self, location_id: str, material: str, amount: float
    ) -> dict[str, float]:
        if amount <= 0:
            raise ValueError("Amount to remove must be greater than zero.")
        material = canonicalize_material_name(material)
        with self._db() as conn:
            row = conn.execute(
                "SELECT amount FROM offsite_inventory WHERE location_id = ? AND material = ?",
                (location_id, material),
            ).fetchone()
            if row is None:
                raise KeyError(f"'{material}' is not in this off-site inventory.")
            current = row["amount"]
            if amount > current:
                raise ValueError(
                    f"Cannot remove {amount} {material}. Only {current} available."
                )
            new_amount = current - amount
            if new_amount <= 0:
                conn.execute(
                    "DELETE FROM offsite_inventory WHERE location_id = ? AND material = ?",
                    (location_id, material),
                )
            else:
                conn.execute(
                    "UPDATE offsite_inventory SET amount = ? "
                    "WHERE location_id = ? AND material = ?",
                    (new_amount, location_id, material),
                )
        return self.get_offsite_inventory(location_id)

    def clear_offsite_inventory(self, location_id: str) -> None:
        with self._db() as conn:
            conn.execute(
                "DELETE FROM offsite_inventory WHERE location_id = ?", (location_id,)
            )

    # ------------------------------------------------------------------
    # Factory alarms
    # ------------------------------------------------------------------

    def get_factory_alarms(self, guild_id: int | None = None) -> list[FactoryAlarm]:
        with self._db() as conn:
            if guild_id is not None:
                rows = conn.execute(
                    "SELECT * FROM factory_alarms WHERE guild_id = ?", (guild_id,)
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM factory_alarms").fetchall()
        return [self._decode_row(FactoryAlarm, r) for r in rows]

    def get_factory_alarm(
        self, alarm_id: str, guild_id: int | None = None
    ) -> FactoryAlarm | None:
        with self._db() as conn:
            row = conn.execute(
                "SELECT * FROM factory_alarms WHERE id = ?", (alarm_id,)
            ).fetchone()
        if row is None:
            return None
        alarm = self._decode_row(FactoryAlarm, row)
        if guild_id is not None and alarm.guild_id != guild_id:
            return None
        return alarm

    def create_factory_alarm(
        self, *, guild_id: int, channel_id: int, facility_name: str,
        duration_minutes: int, single_ping: bool, user_id: int,
    ) -> FactoryAlarm:
        now = utc_now()
        alarm = FactoryAlarm(
            id=uuid.uuid4().hex[:8],
            guild_id=guild_id,
            channel_id=channel_id,
            message_id=None,
            facility_name=facility_name.strip(),
            created_by_user_id=user_id,
            end_time=dt_to_str(now + timedelta(minutes=duration_minutes)),
            single_ping=single_ping,
            warned_before=False,
            warned_exact=False,
            warned_after=False,
            completed=False,
        )
        with self._db() as conn:
            self._upsert(conn, "factory_alarms", alarm)
        return alarm

    def update_factory_alarm(self, updated: FactoryAlarm) -> FactoryAlarm:
        with self._db() as conn:
            if not self._exists(conn, "factory_alarms", updated.id):
                raise KeyError(f"Unknown factory alarm id: {updated.id}")
            self._upsert(conn, "factory_alarms", updated)
        return updated

    def set_factory_alarm_message_id(self, alarm_id: str, message_id: int) -> FactoryAlarm:
        alarm = self.get_factory_alarm(alarm_id)
        if alarm is None:
            raise KeyError(alarm_id)
        alarm.message_id = message_id
        return self.update_factory_alarm(alarm)

    def delete_factory_alarm(self, alarm_id: str, guild_id: int | None = None) -> bool:
        if self.get_factory_alarm(alarm_id, guild_id) is None:
            return False
        with self._db() as conn:
            conn.execute("DELETE FROM factory_alarms WHERE id = ?", (alarm_id,))
        return True

    def mark_factory_alarm_warned(self, alarm_id: str, warn_type: str) -> FactoryAlarm:
        alarm = self.get_factory_alarm(alarm_id)
        if alarm is None:
            raise KeyError(alarm_id)
        if warn_type == "before":
            alarm.warned_before = True
        elif warn_type == "exact":
            alarm.warned_exact = True
        elif warn_type == "after":
            alarm.warned_after = True
        elif warn_type == "completed":
            alarm.completed = True
        return self.update_factory_alarm(alarm)

    # ------------------------------------------------------------------
    # Operations
    # ------------------------------------------------------------------

    def get_operations(
        self, guild_id: int | None = None, *, open_only: bool = False
    ) -> list[Operation]:
        query = "SELECT * FROM operations"
        params: list = []
        clauses = []
        if guild_id is not None:
            clauses.append("guild_id = ?")
            params.append(guild_id)
        if open_only:
            placeholders = ", ".join("?" for _ in OP_OPEN_STATUSES)
            clauses.append(f"status IN ({placeholders})")
            params.extend(OP_OPEN_STATUSES)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY scheduled_at"
        with self._db() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._decode_row(Operation, r) for r in rows]

    def get_operation(self, op_id: str, guild_id: int | None = None) -> Operation | None:
        with self._db() as conn:
            row = conn.execute("SELECT * FROM operations WHERE id = ?", (op_id,)).fetchone()
        if row is None:
            return None
        op = self._decode_row(Operation, row)
        if guild_id is not None and op.guild_id != guild_id:
            return None
        return op

    def get_next_op_number(self, guild_id: int) -> int:
        with self._db() as conn:
            row = conn.execute(
                "SELECT MAX(op_number) AS m FROM operations WHERE guild_id = ?", (guild_id,)
            ).fetchone()
        return (row["m"] or 0) + 1

    def create_operation(
        self,
        *,
        guild_id: int,
        channel_id: int,
        name: str,
        scheduled_at: datetime,
        leader_user_id: int,
        description: str = "",
        location: str = "",
        war_number: int | None = None,
        squads: list | None = None,
        ally_room: str | None = None,
    ) -> Operation:
        now = utc_now()
        op = Operation(
            id=uuid.uuid4().hex[:8],
            op_number=self.get_next_op_number(guild_id),
            guild_id=guild_id,
            channel_id=channel_id,
            message_id=None,
            name=name.strip(),
            description=description.strip(),
            location=location.strip(),
            war_number=war_number,
            scheduled_at=dt_to_str(scheduled_at),
            leader_user_id=leader_user_id,
            status=OP_SCHEDULED,
            squads=squads or [],
            going=[],
            tentative=[],
            not_available=[],
            warned_30m=False,
            warned_start=False,
            created_at=dt_to_str(now),
            ally_room=ally_room,
            participant_meta={},
        )
        with self._db() as conn:
            self._upsert(conn, "operations", op)
        return op

    def update_operation(self, updated: Operation) -> Operation:
        with self._db() as conn:
            if not self._exists(conn, "operations", updated.id):
                raise KeyError(f"Unknown operation id: {updated.id}")
            self._upsert(conn, "operations", updated)
        return updated

    def set_operation_message_id(self, op_id: str, message_id: int) -> Operation:
        op = self.get_operation(op_id)
        if op is None:
            raise KeyError(op_id)
        op.message_id = message_id
        return self.update_operation(op)

    def attach_operation_thread(
        self, op_id: str, *, thread_id: int, message_id: int
    ) -> Operation:
        """Bind a freshly created per-op thread to the op: the card now lives in
        the thread, so ``channel_id`` (used for edits/reminders) points at it and
        ``thread_id`` marks the op for thread-aware cleanup."""
        op = self.get_operation(op_id)
        if op is None:
            raise KeyError(op_id)
        op.thread_id = thread_id
        op.channel_id = thread_id
        op.message_id = message_id
        return self.update_operation(op)

    def set_operation_status(self, op_id: str, status: str, guild_id: int | None = None) -> Operation:
        op = self.get_operation(op_id, guild_id)
        if op is None:
            raise KeyError(op_id)
        op.status = status
        return self.update_operation(op)

    def delete_operation(self, op_id: str, guild_id: int | None = None) -> bool:
        if self.get_operation(op_id, guild_id) is None:
            return False
        with self._db() as conn:
            conn.execute("DELETE FROM operations WHERE id = ?", (op_id,))
            # Mirrors are meaningless without their op; linked supply requests
            # must not dangle on a now-deleted op.
            conn.execute("DELETE FROM operation_mirrors WHERE op_id = ?", (op_id,))
            conn.execute("UPDATE logistics_requests SET op_id = NULL WHERE op_id = ?", (op_id,))
        return True

    @staticmethod
    def _op_remove_user(op: Operation, user_id: int) -> None:
        """Remove a user from every squad/waitlist/RSVP bucket, promoting the
        first waitlister into any squad slot the user vacates."""
        for squad in op.squads:
            if user_id in squad["members"]:
                squad["members"].remove(user_id)
                if squad["waitlist"]:
                    squad["members"].append(squad["waitlist"].pop(0))
            if user_id in squad["waitlist"]:
                squad["waitlist"].remove(user_id)
            if squad.get("lead_user_id") == user_id:
                squad["lead_user_id"] = None
        for bucket in (op.going, op.tentative, op.not_available):
            if user_id in bucket:
                bucket.remove(user_id)

    def signup_squad(
        self, op_id: str, *, user_id: int, squad_key: str, guild_id: int | None = None
    ) -> tuple[Operation, str]:
        """Sign a user up to a squad. Returns (op, "member" | "waitlist")."""
        op = self.get_operation(op_id, guild_id)
        if op is None:
            raise KeyError(op_id)
        squad = op.find_squad(squad_key)
        if squad is None:
            raise KeyError(f"Unknown squad: {squad_key}")
        self._op_remove_user(op, user_id)
        capacity = squad["capacity"]
        if capacity and len(squad["members"]) >= capacity:
            squad["waitlist"].append(user_id)
            outcome = "waitlist"
        else:
            squad["members"].append(user_id)
            outcome = "member"
        self.update_operation(op)
        return op, outcome

    def set_rsvp(
        self, op_id: str, *, user_id: int, state: str, guild_id: int | None = None
    ) -> Operation:
        """Set a roleless RSVP. state in {"going", "tentative", "not_available"}."""
        op = self.get_operation(op_id, guild_id)
        if op is None:
            raise KeyError(op_id)
        self._op_remove_user(op, user_id)
        bucket = {"going": op.going, "tentative": op.tentative, "not_available": op.not_available}.get(state)
        if bucket is None:
            raise ValueError(f"Unknown RSVP state: {state}")
        bucket.append(user_id)
        return self.update_operation(op)

    def withdraw(self, op_id: str, *, user_id: int, guild_id: int | None = None) -> Operation:
        op = self.get_operation(op_id, guild_id)
        if op is None:
            raise KeyError(op_id)
        self._op_remove_user(op, user_id)
        op.participant_meta.pop(str(user_id), None)
        return self.update_operation(op)

    def set_squad_lead(
        self, op_id: str, *, squad_key: str, user_id: int | None, guild_id: int | None = None
    ) -> Operation:
        op = self.get_operation(op_id, guild_id)
        if op is None:
            raise KeyError(op_id)
        squad = op.find_squad(squad_key)
        if squad is None:
            raise KeyError(f"Unknown squad: {squad_key}")
        squad["lead_user_id"] = user_id
        return self.update_operation(op)

    def set_squads(
        self, op_id: str, *, squad_defs: list[tuple[str, int]], guild_id: int | None = None
    ) -> Operation:
        """Replace an op's squads from (name, capacity) defs, preserving the
        members/waitlist/lead of any squad whose name (slug) survives."""
        op = self.get_operation(op_id, guild_id)
        if op is None:
            raise KeyError(op_id)
        existing = {s["key"]: s for s in op.squads}
        new_squads = []
        for name, capacity in squad_defs:
            squad = make_squad(name, capacity)
            prior = existing.get(squad["key"])
            if prior:
                squad["members"] = prior["members"]
                squad["waitlist"] = prior["waitlist"]
                squad["lead_user_id"] = prior["lead_user_id"]
            new_squads.append(squad)
        op.squads = new_squads
        return self.update_operation(op)

    def mark_operation_warned(self, op_id: str, warn_type: str) -> Operation:
        op = self.get_operation(op_id)
        if op is None:
            raise KeyError(op_id)
        if warn_type == "30m":
            op.warned_30m = True
        elif warn_type == "start":
            op.warned_start = True
        else:
            raise ValueError(f"Unknown warn type: {warn_type}")
        return self.update_operation(op)

    # ------------------------------------------------------------------
    # Allied operations — cross-server mirrors + participant identity
    # ------------------------------------------------------------------

    def set_participant_meta(
        self, op_id: str, user_id: int, *, name: str, faction: str | None,
        guild_id: int, server: str | None = None,
    ) -> Operation:
        """Record who a participant is (name + faction + home server) so a
        cross-server card can render them even where a raw mention won't resolve.
        Called whenever a user RSVPs / signs up to an allied op."""
        op = self.get_operation(op_id)
        if op is None:
            raise KeyError(op_id)
        op.participant_meta[str(user_id)] = {
            "name": name, "faction": faction, "guild_id": guild_id, "server": server,
        }
        return self.update_operation(op)

    def add_operation_mirror(
        self, op_id: str, guild_id: int, channel_id: int, message_id: int | None = None
    ) -> None:
        """Record (or update) a server's copy of an allied op."""
        with self._db() as conn:
            conn.execute(
                "INSERT INTO operation_mirrors (op_id, guild_id, channel_id, message_id) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(op_id, guild_id) DO UPDATE SET "
                "channel_id = excluded.channel_id, message_id = excluded.message_id",
                (op_id, guild_id, channel_id, message_id),
            )

    def set_mirror_message_id(self, op_id: str, guild_id: int, message_id: int) -> None:
        with self._db() as conn:
            conn.execute(
                "UPDATE operation_mirrors SET message_id = ? WHERE op_id = ? AND guild_id = ?",
                (message_id, op_id, guild_id),
            )

    def operation_mirrors(self, op_id: str) -> list[dict]:
        """[{guild_id, channel_id, message_id}] — every server's copy of an op."""
        with self._db() as conn:
            rows = conn.execute(
                "SELECT guild_id, channel_id, message_id FROM operation_mirrors WHERE op_id = ?",
                (op_id,),
            ).fetchall()
        return [
            {"guild_id": r["guild_id"], "channel_id": r["channel_id"], "message_id": r["message_id"]}
            for r in rows
        ]

    def all_operation_mirrors(self, *, open_only: bool = False) -> list[dict]:
        """[{op_id, guild_id, channel_id, message_id}] across all ops — used at
        startup to re-attach a persistent view to every mirror message."""
        query = "SELECT m.op_id, m.guild_id, m.channel_id, m.message_id FROM operation_mirrors m"
        params: list = []
        if open_only:
            placeholders = ", ".join("?" for _ in OP_OPEN_STATUSES)
            query += f" JOIN operations o ON o.id = m.op_id WHERE o.status IN ({placeholders})"
            params.extend(OP_OPEN_STATUSES)
        with self._db() as conn:
            rows = conn.execute(query, params).fetchall()
        return [
            {"op_id": r["op_id"], "guild_id": r["guild_id"],
             "channel_id": r["channel_id"], "message_id": r["message_id"]}
            for r in rows
        ]

    def operations_for_member_guild(
        self, guild_id: int, *, open_only: bool = False
    ) -> list[Operation]:
        """Allied ops a guild is a *member* of (has a mirror) but does not host."""
        query = (
            "SELECT o.* FROM operations o JOIN operation_mirrors m ON m.op_id = o.id "
            "WHERE m.guild_id = ? AND o.guild_id != ?"
        )
        params: list = [guild_id, guild_id]
        if open_only:
            placeholders = ", ".join("?" for _ in OP_OPEN_STATUSES)
            query += f" AND o.status IN ({placeholders})"
            params.extend(OP_OPEN_STATUSES)
        query += " ORDER BY o.scheduled_at"
        with self._db() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._decode_row(Operation, r) for r in rows]

    # ------------------------------------------------------------------
    # Maintenance / self-cleaning
    # ------------------------------------------------------------------

    def purge_guild(self, guild_id: int) -> int:
        """Delete all data belonging to a guild. Returns rows removed."""
        removed = 0
        with self._db() as conn:
            for table in _GUILD_TABLES:
                cur = conn.execute(
                    f"DELETE FROM {table} WHERE guild_id = ?", (guild_id,)
                )
                removed += cur.rowcount
            # Drop mirror rows orphaned by deleting a host guild's operations.
            conn.execute(
                "DELETE FROM operation_mirrors WHERE op_id NOT IN (SELECT id FROM operations)"
            )
        return removed

    def known_guild_ids(self) -> set[int]:
        """Every guild_id that currently has data in any table."""
        ids: set[int] = set()
        with self._db() as conn:
            for table in _GUILD_TABLES:
                for row in conn.execute(f"SELECT DISTINCT guild_id FROM {table}"):
                    ids.add(row["guild_id"])
        return ids

    # ------------------------------------------------------------------
    # Legacy JSON migration
    # ------------------------------------------------------------------

    def _is_empty(self) -> bool:
        with self._db() as conn:
            for table in _GUILD_TABLES:
                if conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone() is not None:
                    return False
        return True

    def _maybe_migrate_legacy_json(self, migrate_from: str | Path | None) -> None:
        """One-time import of the old flat-JSON store into SQLite.

        Runs only when the database is empty and a legacy file is present, then
        renames the legacy file so the import never repeats. Keys belonging to
        removed features (e.g. operations) are ignored.
        """
        if migrate_from is None:
            return
        legacy = Path(migrate_from)
        if legacy.resolve() == self.path.resolve():
            return  # legacy source and DB are the same file — nothing to import
        if not legacy.exists() or not self._is_empty():
            return

        with legacy.open("r", encoding="utf-8") as handle:
            raw = json.load(handle)

        with self._db() as conn:
            for gid, cfg in raw.get("guild_channels", {}).items():
                conn.execute(
                    "INSERT OR REPLACE INTO guild_config (guild_id, channel_id, urgent_role_id) "
                    "VALUES (?, ?, ?)",
                    (int(gid), cfg.get("channel_id"), cfg.get("urgent_role_id")),
                )
            for item in raw.get("stockpiles", []):
                self._upsert(conn, "stockpiles", _stockpile_from_legacy(item))
            # Note: legacy "resource_needs" (the old farming board) are intentionally
            # dropped — that feature was replaced by the logistics request system.
            for item in raw.get("factory_alarms", []):
                self._upsert(conn, "factory_alarms", FactoryAlarm(**item))
            for gid, inv in raw.get("base_inventory", {}).items():
                for material, amount in inv.items():
                    conn.execute(
                        "INSERT OR REPLACE INTO base_inventory (guild_id, material, amount) "
                        "VALUES (?, ?, ?)",
                        (int(gid), material, amount),
                    )

        legacy.rename(legacy.with_suffix(legacy.suffix + ".migrated"))


def _stockpile_from_legacy(item: dict) -> Stockpile:
    """Build a Stockpile from a legacy JSON record, mapping the old per-interval
    warned_* booleans onto the new reminders_sent list."""
    item = dict(item)
    if "reminders_sent" not in item:
        sent: list[str] = []
        if item.pop("expired_notified", False):
            sent.append("expired")
        # Old intervals (24h/6h/2h) don't map cleanly to the new ones; we only
        # preserve the terminal "expired" state and drop the rest so active
        # stockpiles simply re-arm on the new schedule.
        for old in ("warned_24h", "warned_6h", "warned_2h"):
            item.pop(old, None)
        item["reminders_sent"] = sent
    return Stockpile(**item)
