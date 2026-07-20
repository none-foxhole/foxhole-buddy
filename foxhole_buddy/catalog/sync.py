"""Wiki-synced item catalog.

The catalog of in-game items is pulled from the Foxhole community wiki's Cargo
database (``itemdata`` table), which exposes clean, structured, English item
data keyed by category/type/faction. We reshape it into a three-level
hierarchy — Category -> Subcategory -> Item — that the logistics request
wizard navigates with dropdowns.

The wiki requires a descriptive User-Agent; the default urllib/aiohttp agent
gets a 403.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Iterable

WIKI_API = "https://foxhole.wiki.gg/api.php"
USER_AGENT = "FoxholeBuddyBot/1.0 (https://github.com/; Discord logistics bot catalog sync)"
_FIELDS = "name,category,type,faction,codename,crate_amount"
_PAGE_SIZE = 500

# Wiki faction codes -> our normalized faction list.
_FACTION_MAP = {
    "Both": ["colonial", "warden"],
    "Col": ["colonial"],
    "War": ["warden"],
    "Warden": ["warden"],
    "Colonial": ["colonial"],
    "": ["colonial", "warden"],
}


def _slug(value: str) -> str:
    out = []
    for ch in value.strip().lower():
        if ch.isalnum():
            out.append(ch)
        elif out and out[-1] != "_":
            out.append("_")
    return "".join(out).strip("_") or "general"


def _norm_faction(value: str | None) -> list[str]:
    return _FACTION_MAP.get((value or "").strip(), ["colonial", "warden"])


def _label_category(raw: str) -> str:
    # The wiki has one oddball lowercase category ("aircraft parts").
    return raw.strip() if raw.strip() and raw[0].isupper() else raw.strip().title()


def build_catalog(rows: Iterable[dict], *, fetched_at: datetime) -> dict:
    """Reshape raw wiki rows into the hierarchical catalog document.

    Items with no ``type`` are bucketed under a "General" subcategory so every
    item is reachable through the same Category -> Subcategory -> Item flow.
    """
    # category_key -> {"label", "subs": {sub_key -> {"label", "items": [...]}}}
    categories: dict[str, dict] = {}

    for row in rows:
        name = (row.get("name") or "").strip()
        cat_raw = (row.get("category") or "").strip()
        if not name or not cat_raw:
            continue
        type_raw = (row.get("type") or "").strip()
        sub_label = type_raw or "General"

        cat_key = _slug(cat_raw)
        sub_key = _slug(sub_label)

        cat = categories.setdefault(cat_key, {"label": _label_category(cat_raw), "subs": {}})
        sub = cat["subs"].setdefault(sub_key, {"label": sub_label, "items": []})

        crate = row.get("crate_amount") or row.get("crate amount")
        try:
            crate = int(crate) if crate not in (None, "") else None
        except (TypeError, ValueError):
            crate = None

        sub["items"].append({
            "name": name,
            "faction": _norm_faction(row.get("faction")),
            "codename": (row.get("codename") or "").strip() or None,
            "crate_amount": crate,
        })

    # Materialize to sorted lists for stable ordering.
    out_categories = []
    item_count = 0
    for cat_key in sorted(categories, key=lambda k: categories[k]["label"].lower()):
        cat = categories[cat_key]
        subs = []
        for sub_key in sorted(cat["subs"], key=lambda k: cat["subs"][k]["label"].lower()):
            sub = cat["subs"][sub_key]
            items = sorted(sub["items"], key=lambda it: it["name"].lower())
            item_count += len(items)
            subs.append({"key": sub_key, "label": sub["label"], "items": items})
        out_categories.append({"key": cat_key, "label": cat["label"], "subcategories": subs})

    return {
        "source": "foxhole.wiki.gg (itemdata)",
        "fetched_at": fetched_at.isoformat(),
        "item_count": item_count,
        "categories": out_categories,
    }


async def _fetch_rows(session) -> list[dict]:
    """Page through the Cargo ``itemdata`` table and return raw item rows."""
    rows: list[dict] = []
    offset = 0
    while True:
        params = {
            "action": "cargoquery",
            "format": "json",
            "tables": "itemdata",
            "fields": _FIELDS,
            "where": "name IS NOT NULL AND category IS NOT NULL",
            "limit": str(_PAGE_SIZE),
            "offset": str(offset),
        }
        async with session.get(
            WIKI_API, params=params, headers={"User-Agent": USER_AGENT}
        ) as resp:
            resp.raise_for_status()
            payload = await resp.json()
        if "error" in payload:
            raise RuntimeError(f"Wiki API error: {payload['error']}")
        batch = [entry["title"] for entry in payload.get("cargoquery", [])]
        rows.extend(batch)
        if len(batch) < _PAGE_SIZE:
            break
        offset += _PAGE_SIZE
    return rows


def _atomic_write(path: Path, document: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(document, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    tmp.replace(path)


async def refresh_catalog(dest: str | Path, *, fetched_at: datetime) -> dict:
    """Fetch the catalog from the wiki and write it to ``dest`` atomically.

    Raises on network/parse failure; the caller is responsible for keeping the
    previous cache when that happens.
    """
    import aiohttp

    async with aiohttp.ClientSession() as session:
        rows = await _fetch_rows(session)
    document = build_catalog(rows, fetched_at=fetched_at)
    if not document["categories"]:
        raise RuntimeError("Wiki returned an empty catalog; refusing to overwrite cache.")
    _atomic_write(Path(dest), document)
    return document
