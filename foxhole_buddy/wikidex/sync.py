"""Wiki-synced item/vehicle/structure encyclopedia ("wikidex").

Bulk-downloads the Foxhole wiki's Cargo tables (``itemdata``, ``vehicles``,
``structures``) plus production recipes (``productionmerged3``) and reshapes
them into one flat searchable document for the ``/s`` lookup command. Only the
fields the info embed actually shows are kept — no gallery/version/history
bloat — so the whole cache stays well under 1 MB and is refreshed weekly.

The wiki requires a descriptive User-Agent; the default urllib/aiohttp agent
gets a 403.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

WIKI_API = "https://foxhole.wiki.gg/api.php"
# Bump whenever the document shape / kept fields change: caches written by an
# older bot version are then ignored on load (seed fallback) instead of being
# served for up to a week until the age-gated sync replaces them.
SCHEMA_VERSION = 2
USER_AGENT = "FoxholeBuddyBot/1.0 (https://github.com/; Discord logistics bot wikidex sync)"
_PAGE_SIZE = 500

# Wiki faction codes -> our normalized faction list (same map as catalog sync).
_FACTION_MAP = {
    "Both": ["colonial", "warden"],
    "Col": ["colonial"],
    "War": ["warden"],
    "Warden": ["warden"],
    "Colonial": ["colonial"],
    "": ["colonial", "warden"],
}

# Per-table field lists: (kind, cargo table, fields kept in the entry's stats).
# ``name``/``aliases``/``faction``/``image`` are handled separately for every
# table; everything listed here lands in the entry's ``stats`` dict verbatim
# (empty values dropped). The lists are each table's FULL schema minus pure
# display internals (version, gallery, icon overrides) and the per-slot
# ``A1_``–``A4_`` armament columns, which the wiki never populates — weapon
# data comes from the dedicated ``armament2`` table instead.
_TABLES: list[tuple[str, str, list[str]]] = [
    (
        "item",
        "itemdata",
        [
            "codename", "category", "type", "ChassisName", "EquipmentSlot",
            "ItemCategory", "ItemFlags", "ItemProfileType", "TechID",
            "uses", "variants",
            "damage", "damage2", "damage_no_bug", "damage_rng", "damage_type",
            "damage_multiplier", "damage_multiplier2", "AddedBurning",
            "BreachingModifier", "bIgnoreBreachesBunkersThreshold",
            "bApplyTankArmourMechanics", "degrades_armour",
            "ArmourDamageModifier", "TankArmourPenetrationFactor",
            "is_mounted", "explosion_radius_outer", "explosion_radius_inner",
            "fire_rate", "fire_rate2", "firing_time", "firing_mode", "fuze",
            "range_effective", "range_effective2", "range_max", "range_max2",
            "HalfAngleMin", "HalfAngleMax", "HalfAngleMin2", "HalfAngleMax2",
            "StabilityCostPerShot", "StabilityCostPerShot2", "Agility",
            "StabilityFloorFromMovement", "StabilityFloorFromMovement2",
            "StabilityGainRate", "magazine", "reload",
            "ammo", "ammo2", "ammo3", "ammo4",
            "encumbrance", "encumbrance_worn", "encumbrance_bonus",
            "slot", "volume", "intel_range", "packable",
            "crate_amount", "pallet_amount", "status",
        ],
    ),
    (
        "vehicle",
        "vehicles",
        [
            "codename", "type", "chassis",
            "vehicle_hp", "armour_type", "armour_hp",
            "min_pen_chance", "max_pen_chance",
            "disable_chance_tracks", "disable_chance_fueltank",
            "disable_chance_turret", "disable_chance_turret2", "disable",
            "crew", "passengers", "slots",
            "speed", "offspeed", "waterspeed", "airspeed",
            "boostspeed", "boostspeed_off", "boostspeed_water", "mobility",
            "snow_immune", "zero_encumbrance_speed_mod",
            "max_encumbrance_speed_mod",
            "fuelcap", "fueltype", "fuelrate", "fuelrate_water",
            "fuelrate_boost", "fuelrate_boost_water",
            "towing_power", "towed_weight", "trigger_mines",
            "intel_range", "intel_range_anchored", "intel_type",
            "shippable_size", "storable", "crate_amount",
            "build_location", "tier_cost", "variants", "repair", "status",
        ],
    ),
    (
        "structure",
        "structures",
        [
            "codename", "construction_type", "type", "ChassisName",
            "structure_hp", "structure_hp_entrenched", "armour_type",
            "husk_hp", "husk_armour_type", "husk_decay_start",
            "husk_decay_duration",
            "built_with", "build_material", "build_amount", "repair",
            "maintenance_amount", "slots", "wrenchable", "base_tier",
            "facility",
            "decay_start", "decay_duration", "decay_RDZ_immune",
            "ai_range", "retaliation_range", "firing_range_inac",
            "fuelcap", "fuelduration", "fuelrate", "fueltype",
            "intel_range", "intel_type", "crate_amount", "status",
        ],
    ),
]

# Vehicle/structure weapons live in their own table, joined by parent_name.
_ARMAMENT_FIELDS = [
    "parent_name", "ArmamentIndex", "ArmamentName",
    "AmmoName1", "AmmoName2", "AmmoName3", "AmmoName4", "AmmoName5",
    "ReloadTime", "ReloadAllAtOnce", "FiringTime",
    "RangeMax", "RangeEffective", "FireRate", "MagazineSize",
    "FiringArc", "Traverse", "RotationSpeed", "VelocityMod",
    "ArtyAccMin", "ArtyAccMax", "HalfAngleMin", "HalfAngleMax",
    "StabilityCostPerShot", "Agility", "StabilityFloorFromMovement",
    "StabilityGainRate", "SecondaryMode",
]

_PRODUCTION_FIELDS = [
    "Output", "Source",
    "InputItem1", "InputItem1Amount", "InputItem2", "InputItem2Amount",
    "InputItem3", "InputItem3Amount", "InputItem4", "InputItem4Amount",
    "InputItem5", "InputItem5Amount", "InputItem6", "InputItem6Amount",
    "InputVehicle", "InputPower",
    "OutputAmount", "OutputUnit", "ProductionTime",
    "Faction", "IsMPFable", "IsCrateOutput",
]

# [[Page|opts|label]] / [[Page]] wikitext links -> the last segment, which is
# the display text (also handles [[File:...|24x24px|link=X|alt=X|Label]]).
_WIKILINK_RE = re.compile(r"\[\[(?:[^\]|]*\|)*([^\]|]*)\]\]")
# {{Disp|pcmats}} and friends -> keep the template's last argument as text.
_TEMPLATE_RE = re.compile(r"\{\{(?:[^{}|]*\|)*([^{}|]*)\}\}")


def _norm_faction(value: str | None) -> list[str]:
    return _FACTION_MAP.get((value or "").strip(), ["colonial", "warden"])


def _norm_keys(row: dict) -> dict:
    """Cargo returns field names with underscores replaced by spaces; undo it."""
    return {k.replace(" ", "_"): v for k, v in row.items()}


def _strip_wikitext(value: str) -> str:
    value = _WIKILINK_RE.sub(r"\1", value)
    prev = None
    while prev != value:  # peel nested templates innermost-first
        prev = value
        value = _TEMPLATE_RE.sub(r"\1", value)
    return value.replace("'''", "").replace("''", "").strip()


def _split_list(value: str | None) -> list[str]:
    """Cargo list-fields arrive as a comma-joined string."""
    if not value:
        return []
    return [part.strip() for part in str(value).split(",") if part.strip()]


async def _fetch_rows(session, table: str, fields: list[str]) -> list[dict]:
    """Page through one Cargo table and return raw rows (keys normalized)."""
    rows: list[dict] = []
    offset = 0
    while True:
        params = {
            "action": "cargoquery",
            "format": "json",
            "tables": table,
            "fields": ",".join(fields),
            "limit": str(_PAGE_SIZE),
            "offset": str(offset),
        }
        async with session.get(
            WIKI_API, params=params, headers={"User-Agent": USER_AGENT}
        ) as resp:
            resp.raise_for_status()
            payload = await resp.json()
        if "error" in payload:
            raise RuntimeError(f"Wiki API error ({table}): {payload['error']}")
        batch = [_norm_keys(entry["title"]) for entry in payload.get("cargoquery", [])]
        rows.extend(batch)
        if len(batch) < _PAGE_SIZE:
            break
        offset += _PAGE_SIZE
    return rows


def _build_recipes(rows: list[dict]) -> dict[str, list[dict]]:
    """productionmerged3 rows -> {output item name: [recipe, ...]}."""
    recipes: dict[str, list[dict]] = {}
    for row in rows:
        output = (row.get("Output") or "").strip()
        if not output:
            continue
        inputs: list[list] = []
        for i in range(1, 7):
            item = (row.get(f"InputItem{i}") or "").strip()
            amount = (row.get(f"InputItem{i}Amount") or "").strip()
            if not item:
                continue
            try:
                num = float(amount)
                amount_val = int(num) if num == int(num) else num
            except (TypeError, ValueError):
                amount_val = None
            inputs.append([item, amount_val])
        if not inputs:
            continue
        recipe = {
            "source": (row.get("Source") or "").strip() or None,
            "inputs": inputs,
            "vehicle": (row.get("InputVehicle") or "").strip() or None,
            "power": (row.get("InputPower") or "").strip() or None,
            "output_amount": (row.get("OutputAmount") or "").strip() or None,
            "output_unit": (row.get("OutputUnit") or "").strip() or None,
            "time": (row.get("ProductionTime") or "").strip() or None,
            "mpf": (row.get("IsMPFable") or "").strip() in ("1", "Yes", "true", "True"),
        }
        recipes.setdefault(output, []).append(recipe)
    return recipes


def _build_armaments(rows: list[dict]) -> dict[str, list[dict]]:
    """armament2 rows -> {parent vehicle/structure name: [weapon, ...]}."""
    armaments: dict[str, list[dict]] = {}
    for row in rows:
        row = _norm_keys(row)
        parent = (row.get("parent_name") or "").strip()
        name = (row.get("ArmamentName") or "").strip()
        if not parent or not name:
            continue
        weapon = {"name": name}
        for key in _ARMAMENT_FIELDS:
            if key in ("parent_name", "ArmamentIndex", "ArmamentName"):
                continue
            value = str(row.get(key) or "").strip()
            if value:
                weapon[key] = value
        armaments.setdefault(parent, []).append(weapon)
    return armaments


def build_wikidex(
    table_rows: dict[str, list[dict]],
    production_rows: list[dict],
    *,
    fetched_at: datetime,
    armament_rows: list[dict] | None = None,
) -> dict:
    """Reshape raw Cargo rows into the flat wikidex document.

    ``table_rows`` maps kind ("item"/"vehicle"/"structure") to that table's
    raw rows. Stats keep only non-empty values so the embed builder can simply
    show what exists.
    """
    recipes = _build_recipes(production_rows)
    armaments = _build_armaments(armament_rows or [])

    entries: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for kind, _table, stat_fields in _TABLES:
        for row in table_rows.get(kind, []):
            row = _norm_keys(row)
            name = (row.get("name") or "").strip()
            if not name:
                continue
            key = (kind, name.lower())
            if key in seen:  # the wiki has occasional duplicate rows per variant
                continue
            seen.add(key)

            stats: dict = {}
            for field in stat_fields:
                value = row.get(field)
                if value is None:
                    continue
                value = _strip_wikitext(str(value))
                if value == "":
                    continue
                stats[field] = value

            entry = {
                "name": name,
                "kind": kind,
                "faction": _norm_faction(row.get("faction")),
                "stats": stats,
            }
            aliases = _split_list(row.get("aliases"))
            if aliases:
                entry["aliases"] = aliases
            image = (row.get("image") or "").strip()
            if image:
                entry["image"] = image
            if name in recipes:
                entry["production"] = recipes[name]
            if name in armaments:
                entry["armament"] = armaments[name]
            entries.append(entry)

    entries.sort(key=lambda e: e["name"].lower())
    return {
        "schema": SCHEMA_VERSION,
        "source": "foxhole.wiki.gg (itemdata/vehicles/structures/productionmerged3)",
        "fetched_at": fetched_at.isoformat(),
        "entry_count": len(entries),
        "entries": entries,
    }


def _atomic_write(path: Path, document: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(document, handle, indent=1, ensure_ascii=False)
        handle.write("\n")
    tmp.replace(path)


async def refresh_wikidex(dest: str | Path, *, fetched_at: datetime) -> dict:
    """Fetch all wikidex tables and write the cache to ``dest`` atomically.

    Raises on network/parse failure; the caller is responsible for keeping the
    previous cache when that happens.
    """
    import aiohttp

    base_fields = ["name", "aliases", "faction", "image"]
    async with aiohttp.ClientSession() as session:
        table_rows: dict[str, list[dict]] = {}
        for kind, table, stat_fields in _TABLES:
            table_rows[kind] = await _fetch_rows(session, table, base_fields + stat_fields)
        production_rows = await _fetch_rows(session, "productionmerged3", _PRODUCTION_FIELDS)
        armament_rows = await _fetch_rows(session, "armament2", _ARMAMENT_FIELDS)

    document = build_wikidex(
        table_rows, production_rows, fetched_at=fetched_at, armament_rows=armament_rows
    )
    if not document["entries"]:
        raise RuntimeError("Wiki returned an empty wikidex; refusing to overwrite cache.")
    _atomic_write(Path(dest), document)
    return document
