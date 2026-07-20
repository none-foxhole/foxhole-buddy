"""Foxhole War API client (live war data) with a polite on-disk cache.

The war number only changes when a new war starts (roughly weekly), so we cache
it and refresh at most once a day — this is respectful to Foxhole's public API.
Every failure path degrades gracefully: if the API can't be reached the cached
value (possibly None) is kept, and features simply omit live data.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

# Foxhole runs multiple live shards; the primary live shard is the sensible
# default for a regiment's "current war".
SHARD_BASES = (
    "https://war-service-live.foxholeservices.com/api",
    "https://war-service-live-2.foxholeservices.com/api",
    "https://war-service-live-3.foxholeservices.com/api",
)
_USER_AGENT = "FoxholeBuddyBot/1.0 (Discord regiment bot; war data)"
CACHE_TTL_SECONDS = 24 * 3600


async def _get_json(path: str):
    """GET a worldconquest path across shards; return parsed JSON or None."""
    import aiohttp

    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for base in SHARD_BASES:
            try:
                async with session.get(
                    f"{base}{path}", headers={"User-Agent": _USER_AGENT}
                ) as resp:
                    if resp.status != 200:
                        continue
                    return await resp.json(content_type=None)
            except Exception:  # noqa: BLE001 — try the next shard on any error
                continue
    return None


async def fetch_war_number() -> int | None:
    data = await _get_json("/worldconquest/war")
    if isinstance(data, dict) and isinstance(data.get("warNumber"), int):
        return data["warNumber"]
    return None


async def fetch_war_status() -> dict | None:
    """Full /war payload: warNumber, winner, conquestStartTime, etc."""
    data = await _get_json("/worldconquest/war")
    return data if isinstance(data, dict) else None


async def fetch_maps() -> list[str]:
    data = await _get_json("/worldconquest/maps")
    return [m for m in data if isinstance(m, str)] if isinstance(data, list) else []


async def fetch_war_report(map_name: str) -> dict | None:
    data = await _get_json(f"/worldconquest/warReport/{map_name}")
    return data if isinstance(data, dict) else None


def prettify_map(name: str) -> str:
    """'TheFingersHex' -> 'The Fingers'; 'DeadLandsHex' -> 'Dead Lands'."""
    stripped = name[:-3] if name.endswith("Hex") else name
    spaced = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", stripped)
    return spaced.strip() or name


# ── On-disk cache (war number) ────────────────────────────────────────────────────

def load_cache(path: str | Path) -> tuple[int | None, datetime | None]:
    """Return (war_number, fetched_at) from the cache file, or (None, None)."""
    p = Path(path)
    if not p.exists():
        return None, None
    try:
        with p.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        number = data.get("war_number")
        fetched_raw = data.get("fetched_at")
        fetched = datetime.fromisoformat(fetched_raw) if fetched_raw else None
        return (number if isinstance(number, int) else None), fetched
    except (json.JSONDecodeError, OSError, ValueError):
        return None, None


def save_cache(path: str | Path, war_number: int | None, fetched_at: datetime) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump({"war_number": war_number, "fetched_at": fetched_at.isoformat()}, handle)
        handle.write("\n")
    tmp.replace(p)


def cache_is_stale(fetched_at: datetime | None, now: datetime | None = None) -> bool:
    if fetched_at is None:
        return True
    now = now or datetime.now(timezone.utc)
    return (now - fetched_at).total_seconds() >= CACHE_TTL_SECONDS
