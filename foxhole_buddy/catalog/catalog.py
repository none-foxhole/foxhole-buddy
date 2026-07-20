"""Read-only access to the item catalog.

The catalog is loaded from the runtime cache written by the wiki sync
(``data/catalog.json``); if that is missing or unreadable we fall back to the
seed snapshot committed inside the package, so the bot always has a usable
catalog even on first boot or when the wiki is unreachable.
"""

from __future__ import annotations

import difflib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

_SEED_PATH = Path(__file__).with_name("seed_catalog.json")

# Foxhole community slang → official catalog item names, so "bmats" finds
# "Basic Materials". Matched the same way as the real name (exact/prefix/etc).
_ITEM_ALIASES: dict[str, list[str]] = {
    "Basic Materials": ["bmat", "bmats", "basic mats"],
    "Refined Materials": ["rmat", "rmats", "refined mats"],
    "Construction Materials": ["cmat", "cmats", "con mats", "construction mats"],
    "Processed Construction Materials": ["pcmat", "pcmats", "pcons", "pcon mats"],
    "Steel Construction Materials": ["scmat", "scmats"],
    "Concrete Materials": ["concrete", "concrete mats"],
    "Explosive Powder": ["emat", "emats", "explosive mats", "epowder"],
    "Heavy Explosive Powder": ["hemat", "hemats", "heavy explosive mats"],
    "Rare Materials": ["rare mats", "raremats"],
    "Relic Materials": ["relic mats", "relicmats"],
    "Assembly Materials I": ["amat1", "amats1", "amat i", "asmat1"],
    "Assembly Materials II": ["amat2", "amats2", "amat ii"],
    "Assembly Materials III": ["amat3", "amats3", "amat iii"],
    "Assembly Materials IV": ["amat4", "amats4", "amat iv"],
    "Assembly Materials V": ["amat5", "amats5", "amat v"],
    "Components": ["comp", "comps"],
    "Damaged Components": ["dcomp", "dcomps", "damaged comp"],
    "Sulfur": ["sulf"],
    "Salvage": ["salv", "scrap"],
}


def _match_rank(text: str, query: str) -> int | None:
    """Rank how well ``text`` matches ``query`` (lower is better), or None."""
    if text == query:
        return 0
    if text.startswith(query):
        return 1
    if any(word.startswith(query) for word in text.split()):
        return 2
    if query in text:
        return 3
    return None


@dataclass(frozen=True)
class CatalogItem:
    name: str
    faction: list[str]
    codename: str | None
    crate_amount: int | None


class Catalog:
    """Hierarchical Category -> Subcategory -> Item lookup over a catalog doc."""

    def __init__(self, document: dict):
        self._doc = document
        self._categories = document.get("categories", [])
        # Index by key for O(1) traversal.
        self._by_cat = {c["key"]: c for c in self._categories}
        # Flat index for name search, built lazily on first search().
        self._flat: list[dict] | None = None

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------

    @classmethod
    def load(cls, cache_path: str | Path | None = None) -> "Catalog":
        for candidate in (cache_path, _SEED_PATH):
            if candidate is None:
                continue
            path = Path(candidate)
            if not path.exists():
                continue
            try:
                with path.open("r", encoding="utf-8") as handle:
                    document = json.load(handle)
                if document.get("categories"):
                    return cls(document)
            except (json.JSONDecodeError, OSError):
                continue
        # Last resort: an empty catalog rather than crashing.
        return cls({"categories": [], "item_count": 0})

    # ------------------------------------------------------------------
    # Metadata
    # ------------------------------------------------------------------

    @property
    def source(self) -> str:
        return self._doc.get("source", "unknown")

    @property
    def item_count(self) -> int:
        return self._doc.get("item_count", 0)

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
        return not self._categories

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------

    @staticmethod
    def _item_matches(item: dict, faction: str | None) -> bool:
        return faction is None or faction in item.get("faction", ["colonial", "warden"])

    def categories(self, faction: str | None = None) -> list[tuple[str, str]]:
        """[(key, label)] for categories that have ≥1 item for the faction."""
        result = []
        for cat in self._categories:
            if any(
                self._item_matches(it, faction)
                for sub in cat["subcategories"] for it in sub["items"]
            ):
                result.append((cat["key"], cat["label"]))
        return result

    def subcategories(self, category_key: str, faction: str | None = None) -> list[tuple[str, str]]:
        cat = self._by_cat.get(category_key)
        if not cat:
            return []
        return [
            (s["key"], s["label"])
            for s in cat["subcategories"]
            if any(self._item_matches(it, faction) for it in s["items"])
        ]

    def items(
        self, category_key: str, subcategory_key: str, faction: str | None = None
    ) -> list[CatalogItem]:
        cat = self._by_cat.get(category_key)
        if not cat:
            return []
        for sub in cat["subcategories"]:
            if sub["key"] == subcategory_key:
                return [
                    CatalogItem(
                        name=it["name"],
                        faction=it.get("faction", ["colonial", "warden"]),
                        codename=it.get("codename"),
                        crate_amount=it.get("crate_amount"),
                    )
                    for it in sub["items"]
                    if self._item_matches(it, faction)
                ]
        return []

    def category_label(self, category_key: str) -> str | None:
        cat = self._by_cat.get(category_key)
        return cat["label"] if cat else None

    def subcategory_label(self, category_key: str, subcategory_key: str) -> str | None:
        cat = self._by_cat.get(category_key)
        if not cat:
            return None
        for sub in cat["subcategories"]:
            if sub["key"] == subcategory_key:
                return sub["label"]
        return None

    def find_item(
        self, category_key: str, subcategory_key: str, item_name: str
    ) -> CatalogItem | None:
        for item in self.items(category_key, subcategory_key):
            if item.name == item_name:
                return item
        return None

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def _flat_index(self) -> list[dict]:
        """Build (and cache) a flat list of every item with its full path, so a
        name search doesn't have to walk the hierarchy each call."""
        if self._flat is None:
            flat: list[dict] = []
            for cat in self._categories:
                for sub in cat["subcategories"]:
                    for it in sub["items"]:
                        flat.append({
                            "name": it["name"],
                            "faction": it.get("faction", ["colonial", "warden"]),
                            "crate_amount": it.get("crate_amount"),
                            "category_key": cat["key"],
                            "subcategory_key": sub["key"],
                            "category_label": cat["label"],
                            "subcategory_label": sub["label"],
                            "aliases": _ITEM_ALIASES.get(it["name"], []),
                        })
            self._flat = flat
        return self._flat

    def search(
        self, query: str, faction: str | None = None, limit: int = 25
    ) -> list[dict]:
        """Case-insensitive item-name search, faction-filtered.

        Results are ranked exact > prefix > word-start > substring, then
        alphabetically. Each result is a dict carrying enough to disambiguate
        and to build a request line (name, crate_amount, category/subcategory
        keys + labels). The lone duplicate name ("Salvage") returns both, told
        apart by their subcategory label.
        """
        q = query.strip().lower()
        if not q:
            return []
        scored: list[tuple[int, str, dict]] = []
        for entry in self._flat_index():
            if faction is not None and faction not in entry["faction"]:
                continue
            name_low = entry["name"].lower()
            # Best rank across the real name and any slang aliases.
            rank = _match_rank(name_low, q)
            for alias in entry["aliases"]:
                r = _match_rank(alias.lower(), q)
                if r is not None and (rank is None or r < rank):
                    rank = r
            if rank is None:
                continue
            scored.append((rank, name_low, entry))
        scored.sort(key=lambda t: (t[0], t[1]))
        return [entry for _, _, entry in scored[:limit]]

    def suggest(self, query: str, faction: str | None = None, limit: int = 10) -> list[dict]:
        """Fuzzy "did you mean?" fallback for when ``search`` finds nothing.

        Uses difflib similarity over item names so typos still surface the
        intended item. Returns the same dict shape as ``search``.
        """
        q = query.strip().lower()
        if not q:
            return []
        by_name: dict[str, dict] = {}
        for entry in self._flat_index():
            if faction is not None and faction not in entry["faction"]:
                continue
            by_name.setdefault(entry["name"].lower(), entry)
        close = difflib.get_close_matches(q, list(by_name), n=limit, cutoff=0.6)
        return [by_name[name] for name in close]

    def best_match(self, query: str, faction: str | None = None, cutoff: float = 0.82) -> str | None:
        """Confidently resolve free text to a canonical item name, or ``None``.

        Used by bulk inventory paste to snap a typed name to the official one
        only when the match is safe: an exact name/alias hit, or a close
        difflib match above ``cutoff`` (catches plurals like ``smelters`` →
        ``Smelter`` and small typos). Anything looser stays unmatched so the
        caller can keep it as a custom entry.
        """
        q = query.strip().lower()
        if not q:
            return None
        names: list[str] = []
        for entry in self._flat_index():
            if faction is not None and faction not in entry["faction"]:
                continue
            if entry["name"].lower() == q or any(a.lower() == q for a in entry["aliases"]):
                return entry["name"]
            names.append(entry["name"])
        close = difflib.get_close_matches(query.strip(), names, n=1, cutoff=cutoff)
        return close[0] if close else None

    def find(self, name: str, faction: str | None = None) -> dict | None:
        """Return the flat index entry for an exact (case-insensitive) name, or
        ``None`` — lets a snapped name map back to a real catalog line."""
        q = (name or "").strip().lower()
        if not q:
            return None
        for entry in self._flat_index():
            if faction is not None and faction not in entry["faction"]:
                continue
            if entry["name"].lower() == q:
                return entry
        return None
