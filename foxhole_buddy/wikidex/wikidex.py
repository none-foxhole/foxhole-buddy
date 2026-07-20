"""Read-only lookup over the wikidex cache for the ``/s`` command.

Loaded from the runtime cache written by the weekly wiki sync
(``data/wikidex.json``); falls back to the seed snapshot committed inside the
package so ``/s`` works on first boot or when the wiki is unreachable.
"""

from __future__ import annotations

import difflib
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

# Shared ranking + community slang aliases ("bmats" -> Basic Materials).
from foxhole_buddy.catalog.catalog import _ITEM_ALIASES, _match_rank
from foxhole_buddy.wikidex.sync import SCHEMA_VERSION

_SEED_PATH = Path(__file__).with_name("seed_wikidex.json")

WIKI_BASE = "https://foxhole.wiki.gg"


def wiki_page_url(name: str) -> str:
    return f"{WIKI_BASE}/wiki/{quote(name.replace(' ', '_'))}"


def wiki_image_url(filename: str) -> str:
    """Direct URL for a wiki-hosted file via MediaWiki's md5 hash layout —
    computable offline, no API round-trip."""
    normalized = filename.strip().replace(" ", "_")
    normalized = normalized[:1].upper() + normalized[1:]
    digest = hashlib.md5(normalized.encode("utf-8")).hexdigest()
    return f"{WIKI_BASE}/images/{digest[0]}/{digest[:2]}/{quote(normalized)}"


class WikiDex:
    """Flat name/alias search over every wiki item, vehicle, and structure."""

    def __init__(self, document: dict):
        self._doc = document
        self._entries: list[dict] = document.get("entries", [])
        # name (lowercased) -> entry; on the rare cross-kind name collision the
        # first (item > vehicle > structure, the build order) wins.
        self._by_name: dict[str, dict] = {}
        kind_rank = {"item": 0, "vehicle": 1, "structure": 2}
        for entry in sorted(self._entries, key=lambda e: kind_rank.get(e["kind"], 9)):
            self._by_name.setdefault(entry["name"].lower(), entry)

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------

    @classmethod
    def load(cls, cache_path: str | Path | None = None) -> "WikiDex":
        for candidate in (cache_path, _SEED_PATH):
            if candidate is None:
                continue
            path = Path(candidate)
            if not path.exists():
                continue
            try:
                with path.open("r", encoding="utf-8") as handle:
                    document = json.load(handle)
                # A cache written by an older bot version (schema mismatch) is
                # skipped so a deploy shows the new data immediately via the
                # seed instead of serving stale-shaped entries for days.
                if document.get("entries") and document.get("schema") == SCHEMA_VERSION:
                    return cls(document)
            except (json.JSONDecodeError, OSError):
                continue
        # Last resort: an empty dex rather than crashing.
        return cls({"entries": [], "entry_count": 0})

    # ------------------------------------------------------------------
    # Metadata
    # ------------------------------------------------------------------

    @property
    def entry_count(self) -> int:
        return self._doc.get("entry_count", len(self._entries))

    @property
    def fetched_at(self) -> datetime | None:
        raw = self._doc.get("fetched_at")
        if not raw:
            return None
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            return None

    def age_seconds(self, now: datetime | None = None) -> float | None:
        fetched = self.fetched_at
        if fetched is None:
            return None
        now = now or datetime.now(timezone.utc)
        return (now - fetched).total_seconds()

    def is_empty(self) -> bool:
        return not self._entries

    # ------------------------------------------------------------------
    # Lookup
    # ------------------------------------------------------------------

    def get(self, name: str) -> dict | None:
        """Exact (case-insensitive) name lookup — the autocomplete round-trip."""
        return self._by_name.get((name or "").strip().lower())

    @staticmethod
    def _entry_aliases(entry: dict) -> list[str]:
        return entry.get("aliases", []) + _ITEM_ALIASES.get(entry["name"], [])

    def search(self, query: str, limit: int = 25) -> list[dict]:
        """Ranked search over names + wiki aliases + community slang.

        Same ranking as the catalog: exact > prefix > word-start > substring,
        then alphabetical.
        """
        q = query.strip().lower()
        if not q:
            return []
        scored: list[tuple[int, str, dict]] = []
        for entry in self._entries:
            name_low = entry["name"].lower()
            rank = _match_rank(name_low, q)
            for alias in self._entry_aliases(entry):
                r = _match_rank(alias.lower(), q)
                if r is not None and (rank is None or r < rank):
                    rank = r
            if rank is None:
                continue
            scored.append((rank, name_low, entry))
        scored.sort(key=lambda t: (t[0], t[1]))
        return [entry for _, _, entry in scored[:limit]]

    def suggest(self, query: str, limit: int = 5) -> list[dict]:
        """Fuzzy "did you mean?" fallback for when ``search`` finds nothing."""
        q = query.strip().lower()
        if not q:
            return []
        close = difflib.get_close_matches(q, list(self._by_name), n=limit, cutoff=0.6)
        return [self._by_name[name] for name in close]
