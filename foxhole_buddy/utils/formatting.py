from foxhole_buddy.core.store import Stockpile, EXPIRY_HOURS, remaining_time
from foxhole_buddy.theme import Color

def unix_ts(value) -> int:
    return int(value.timestamp())

def stockpile_type_label(stockpile: Stockpile) -> str:
    return "Storage Depot" if stockpile.type == "storage_depot" else "Seaport"

# Off-site inventory location types (a forward base, a depot, or a seaport).
_OFFSITE_TYPES = {
    "base": ("Base", "🏭"),
    "storage_depot": ("Storage Depot", "🏬"),
    "seaport": ("Seaport", "⚓"),
}

def offsite_type_label(location_type: str) -> str:
    return _OFFSITE_TYPES.get(location_type, ("Off-Site", "📦"))[0]

def offsite_type_emoji(location_type: str) -> str:
    return _OFFSITE_TYPES.get(location_type, ("Off-Site", "📦"))[1]

def stockpile_status(stockpile: Stockpile) -> tuple[str, int]:
    remaining = remaining_time(stockpile)
    seconds_left = remaining.total_seconds()
    if seconds_left <= 0:
        return "PUBLIC RISK", Color.RED
    if seconds_left <= 1 * 3600:
        return "CRITICAL", Color.CRITICAL
    if seconds_left <= 6 * 3600:
        return "URGENT", Color.AMBER
    if seconds_left <= 12 * 3600:
        return "WATCH", Color.YELLOW
    return "SECURE", Color.BRAND

def progress_bar(stockpile: Stockpile) -> str:
    total_seconds = EXPIRY_HOURS * 3600
    seconds_left = max(0, int(remaining_time(stockpile).total_seconds()))
    filled = round((seconds_left / total_seconds) * 10)
    filled = max(0, min(10, filled))
    return f"{'█' * filled}{'░' * (10 - filled)}"

# Discord caps a webhook's username at 80 characters.
_WEBHOOK_NAME_LIMIT = 80
# Faction badge appended to a relayed sender's name on the global regi net.
_FACTION_TAG = {"warden": "🔵 Warden", "colonial": "🟢 Colonial"}

def relay_display_name(author: str, regiment: str, faction: str | None = None) -> str:
    """Webhook username for a relayed regi-net message:
    ``"Author • Regiment · 🔵 Warden"`` (faction badge only if known).

    Kept within Discord's 80-char webhook-username limit by trimming the
    regiment first (the faction badge and author are preserved).
    """
    author = (author or "").strip() or "Unknown"
    regiment = (regiment or "").strip()
    badge = _FACTION_TAG.get(faction or "", "")
    sep, join = " • ", " · "

    if not regiment and not badge:
        return author[:_WEBHOOK_NAME_LIMIT]
    if regiment and badge:
        room = _WEBHOOK_NAME_LIMIT - (len(author) + len(sep) + len(join) + len(badge))
        if room < 1:  # author + badge already fill the budget — drop regiment.
            return f"{author}{sep}{badge}"[:_WEBHOOK_NAME_LIMIT]
        if len(regiment) > room:
            regiment = regiment[:room]
        return f"{author}{sep}{regiment}{join}{badge}"
    # Exactly one of regiment / badge present.
    tail = regiment or badge
    room = _WEBHOOK_NAME_LIMIT - (len(author) + len(sep))
    if room < 1:
        return author[:_WEBHOOK_NAME_LIMIT]
    return f"{author}{sep}{tail[:room]}"
