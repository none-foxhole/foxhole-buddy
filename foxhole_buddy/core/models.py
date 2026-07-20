"""Domain models, lifecycle constants, and pure (storage-agnostic) helpers.

This module describes *what the data is*; ``store.py`` handles *how it's
persisted*. Keeping them separate makes the models easy to reuse and test
without touching SQLite.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone


EXPIRY_HOURS = 48
# Graduated stockpile reminders, ordered longest → shortest. Each fires once;
# which have fired is tracked per-stockpile in `reminders_sent`.
WARNING_THRESHOLDS = (
    ("12h", timedelta(hours=12)),
    ("6h", timedelta(hours=6)),
    ("1h", timedelta(hours=1)),
    ("30m", timedelta(minutes=30)),
)
# The shortest interval triggers the urgent-role ping.
URGENT_WARNING = "30m"


# ── Time helpers ──────────────────────────────────────────────────────────────────

def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


def dt_to_str(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def slugify(value: str) -> str:
    """Lowercase alphanumeric slug used for stable squad keys."""
    out: list[str] = []
    for ch in value.strip().lower():
        if ch.isalnum():
            out.append(ch)
        elif out and out[-1] != "_":
            out.append("_")
    return "".join(out).strip("_") or "squad"


# ── Off-site inventories ────────────────────────────────────────────────────────

@dataclass
class OffsiteLocation:
    """A named off-site place (a forward base, storage depot, or seaport) that
    holds its own material inventory, mirroring the guild's base inventory."""
    id: str
    guild_id: int
    name: str
    location: str
    type: str                     # 'base' | 'storage_depot' | 'seaport'
    created_by_user_id: int
    created_at: str
    updated_at: str


# ── Stockpiles ────────────────────────────────────────────────────────────────────

@dataclass
class Stockpile:
    id: str
    guild_id: int
    channel_id: int
    message_id: int | None
    name: str
    location: str
    type: str
    created_by_user_id: int
    last_refreshed_at: str
    expires_at: str
    last_refreshed_by_user_id: int
    reminders_sent: list          # list[str] — which thresholds have pinged (e.g. ["12h","6h"])
    created_at: str
    updated_at: str
    duty_user_ids: list = field(default_factory=list)  # members pinged when this stockpile runs low
    # False ⇒ inventory-only container (no 48h timer, no reminders).
    track_expiry: bool = True
    # Set when the stockpile lives in its own forum post (mirrors Operation).
    thread_id: int | None = None

    @property
    def expires_datetime(self) -> datetime:
        return parse_dt(self.expires_at)

    @property
    def last_refreshed_datetime(self) -> datetime:
        return parse_dt(self.last_refreshed_at)


def remaining_time(stockpile: Stockpile, now: datetime | None = None) -> timedelta:
    return stockpile.expires_datetime - (now or utc_now())


def warning_due(stockpile: Stockpile, now: datetime | None = None) -> str | None:
    """Return the most urgent crossed-but-unsent reminder key, or None.

    Fires one reminder per check; on restarts it catches up any missed
    intervals over successive ticks (most urgent first).
    """
    remaining = remaining_time(stockpile, now)
    if remaining <= timedelta(seconds=0):
        return "expired" if "expired" not in stockpile.reminders_sent else None
    # Shortest threshold first → most urgent unsent reminder wins.
    for key, threshold in sorted(WARNING_THRESHOLDS, key=lambda kv: kv[1]):
        if remaining <= threshold and key not in stockpile.reminders_sent:
            return key
    return None


def mark_warning_sent(stockpile: Stockpile, warning: str) -> Stockpile:
    valid = {key for key, _ in WARNING_THRESHOLDS} | {"expired"}
    if warning not in valid:
        raise ValueError(f"Unknown warning: {warning}")
    if warning not in stockpile.reminders_sent:
        stockpile.reminders_sent.append(warning)
    return stockpile


def format_remaining(delta: timedelta) -> str:
    total_seconds = int(delta.total_seconds())
    if total_seconds <= 0:
        return "expired"
    hours, remainder = divmod(total_seconds, 3600)
    minutes = remainder // 60
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def canonicalize_material_name(raw: str) -> str:
    """Normalise a base-inventory material name for storage and dedup.

    Case-insensitive dedup without breaking Foxhole names that carry numbers:
    a token containing a digit is lowercased (``12.7mm``, ``9mm``, ``.44``);
    a pure-alpha token is title-cased (``bayonet crates`` → ``Bayonet Crates``).
    A trailing parenthetical note is dropped and trailing punctuation trimmed,
    but a leading ``.`` is preserved so ``.44 Mag`` survives.
    """
    name = (raw or "").strip()
    # A parenthetical is a clarification, not part of the name (handles an
    # unclosed paren too, e.g. "(armored variant of javelin").
    if "(" in name:
        name = name[: name.index("(")].strip()
    name = name.rstrip(" \t.,;:)")  # trailing junk only — keep a leading dot
    tokens = name.split()
    cased = [t.lower() if any(c.isdigit() for c in t) else t.title() for t in tokens]
    return " ".join(cased)


# ── Logistics requests ────────────────────────────────────────────────────────────

LOGI_OPEN = "open"
LOGI_CLAIMED = "claimed"
LOGI_DELIVERED = "delivered"


def make_line(category: str, subcategory: str, item: str, quantity: int) -> dict:
    """Build one line item for a (possibly multi-item) logistics request.

    Each line tracks its own status + driver so a shopping list can be split
    across several drivers, or claimed/validated as a whole.
    """
    return {
        "lid": uuid.uuid4().hex[:6],
        "category": category,
        "subcategory": subcategory,
        "item": item,
        "quantity": max(1, int(quantity)),
        "status": LOGI_OPEN,
        "claimed_by_user_id": None,
    }


def derive_logi_status(lines: list[dict]) -> str:
    """Roll up per-line statuses into the request's overall status.

    DELIVERED when every line is delivered; OPEN when nothing is claimed yet;
    otherwise CLAIMED (in progress / partially fulfilled).
    """
    if not lines:
        return LOGI_OPEN
    if all(line["status"] == LOGI_DELIVERED for line in lines):
        return LOGI_DELIVERED
    if all(line["status"] == LOGI_OPEN for line in lines):
        return LOGI_OPEN
    return LOGI_CLAIMED


def logi_counts(lines: list[dict]) -> dict[str, int]:
    """Tally how many lines are open / claimed / delivered (for the embed)."""
    counts = {LOGI_OPEN: 0, LOGI_CLAIMED: 0, LOGI_DELIVERED: 0}
    for line in lines:
        counts[line.get("status", LOGI_OPEN)] = counts.get(line.get("status", LOGI_OPEN), 0) + 1
    return counts


@dataclass
class LogisticsRequest:
    """A structured supply request (a shopping list) picked from the catalog.

    A request holds one or more ``items`` (line items, see ``make_line``). Each
    line has its own ``open -> claimed -> delivered`` lifecycle, so a list can be
    split across drivers or handled as a whole. The request's ``status`` is the
    rolled-up ``derive_logi_status`` of its lines. ``op_id`` optionally links the
    request to an Operation.

    The scalar ``category``/``subcategory``/``item``/``quantity`` fields mirror
    the first line and are kept for backward compatibility with older rows.
    """
    id: str
    guild_id: int
    channel_id: int
    message_id: int | None
    category: str              # display label of the first line, e.g. "Utility"
    subcategory: str           # display label of the first line, e.g. "Field Tool"
    item: str                  # exact in-game item name of the first line
    quantity: int
    requested_by_user_id: int
    status: str                # LOGI_OPEN | LOGI_CLAIMED | LOGI_DELIVERED (derived)
    claimed_by_user_id: int | None
    op_id: str | None
    notes: str
    created_at: str
    updated_at: str
    items: list = field(default_factory=list)   # list[dict] (see make_line)

    def line_items(self) -> list[dict]:
        """Return the line items, synthesizing one from the legacy scalar
        fields for older single-item rows that predate the ``items`` list."""
        if self.items:
            return self.items
        return [{
            "lid": "legacy",
            "category": self.category,
            "subcategory": self.subcategory,
            "item": self.item,
            "quantity": self.quantity,
            "status": self.status,
            "claimed_by_user_id": self.claimed_by_user_id,
        }]

    def find_line(self, lid: str) -> dict | None:
        return next((line for line in self.line_items() if line["lid"] == lid), None)

    def item_count(self) -> int:
        return len(self.line_items())


# ── Factory alarms ────────────────────────────────────────────────────────────────

@dataclass
class FactoryAlarm:
    id: str
    guild_id: int
    channel_id: int
    message_id: int | None
    facility_name: str
    created_by_user_id: int
    end_time: str
    single_ping: bool
    warned_before: bool
    warned_exact: bool
    warned_after: bool
    completed: bool

    @property
    def end_datetime(self) -> datetime:
        return parse_dt(self.end_time)


# ── Operations ────────────────────────────────────────────────────────────────────

OP_SCHEDULED = "scheduled"
OP_IN_PROGRESS = "in_progress"
OP_COMPLETED = "completed"
OP_CANCELLED = "cancelled"
OP_OPEN_STATUSES = (OP_SCHEDULED, OP_IN_PROGRESS)


def make_squad(name: str, capacity: int = 0) -> dict:
    """Build an empty squad. capacity 0 means unlimited."""
    return {
        "key": slugify(name),
        "name": name.strip(),
        "capacity": max(0, int(capacity)),
        "lead_user_id": None,
        "members": [],
        "waitlist": [],
    }


@dataclass
class Operation:
    """A scheduled operation.

    Squads are optional — an op with no squads is a simple RSVP event. When
    squads are present, players sign up for a specific squad (with capacity +
    waitlist + an optional lead). ``tentative`` / ``not_available`` are the
    roleless RSVP buckets that work with or without squads.
    """
    id: str
    op_number: int
    guild_id: int
    channel_id: int
    message_id: int | None
    name: str
    description: str
    location: str
    war_number: int | None
    scheduled_at: str            # ISO datetime (UTC)
    leader_user_id: int
    status: str                  # OP_SCHEDULED | OP_IN_PROGRESS | OP_COMPLETED | OP_CANCELLED
    squads: list                 # list[dict] (see make_squad)
    going: list                  # list[int] — committed but not in a squad
    tentative: list              # list[int]
    not_available: list          # list[int]
    warned_30m: bool
    warned_start: bool
    created_at: str
    # Allied ops: when set, this op is shared across an ally room and mirrored
    # into every member server's channel. NULL ⇒ a normal, single-server op.
    ally_room: str | None = None
    # {"<user_id>": {"name": str, "faction": str|None, "guild_id": int}} — lets
    # cross-server cards/reminders render attendees by name + origin, since a
    # raw <@id> mention doesn't resolve (or ping) outside the user's home server.
    participant_meta: dict = field(default_factory=dict)
    # When set, this local op has its own thread (``channel_id`` is that thread).
    # NULL ⇒ the card sits directly in the channel. Allied ops never use threads.
    thread_id: int | None = None

    @property
    def scheduled_datetime(self) -> datetime:
        return parse_dt(self.scheduled_at)

    @property
    def is_allied(self) -> bool:
        return bool(self.ally_room)

    def find_squad(self, key: str) -> dict | None:
        return next((s for s in self.squads if s["key"] == key), None)

    def participant_ids(self) -> list[int]:
        """Everyone committed (squad members + unassigned 'going'), excluding
        waitlists and tentatives — i.e. who to ping for reminders."""
        ids: list[int] = list(self.going)
        for squad in self.squads:
            ids.extend(squad["members"])
        return ids

    def going_count(self) -> int:
        return len(self.participant_ids())
