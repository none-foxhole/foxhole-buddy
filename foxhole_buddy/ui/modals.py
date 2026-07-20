import logging
import re
import discord
from typing import TYPE_CHECKING
from foxhole_buddy.theme import Color
from foxhole_buddy.utils.formatting import unix_ts
from foxhole_buddy.ui.embeds import stockpile_embed, factory_alarm_embed

if TYPE_CHECKING:
    from foxhole_buddy.core.bot import StockpileBot
    from foxhole_buddy.ui.views import StockpileView

log = logging.getLogger("foxhole_buddy.modals")

class AddStockpileModal(discord.ui.Modal, title="Add Stockpile"):
    name_input = discord.ui.TextInput(
        label="Stockpile Name",
        placeholder='e.g. "Bmats Reserve"',
        max_length=100,
    )
    location_input = discord.ui.TextInput(
        label="Location",
        placeholder='e.g. "Callahan\'s Passage"',
        required=False,
        max_length=100,
    )

    def __init__(self, bot: "StockpileBot", stockpile_type: str, track_expiry: bool = True):
        super().__init__()
        self.bot = bot
        self.stockpile_type = stockpile_type
        self.track_expiry = track_expiry
        if not track_expiry:
            self.title = "Add Inventory"

    async def on_submit(self, interaction: discord.Interaction) -> None:
        stockpile = self.bot.store.create(
            guild_id=interaction.guild_id or 0,
            channel_id=interaction.channel_id,
            name=self.name_input.value,
            location=self.location_input.value or "",
            stockpile_type=self.stockpile_type,
            user_id=interaction.user.id,
            track_expiry=self.track_expiry,
        )
        # Post through the destination helper: a configured forum channel gets one
        # post per stockpile, a text channel gets the card, else the current channel.
        await interaction.response.defer(ephemeral=True)
        thread = await self.bot.post_stockpile_card(stockpile, interaction.channel)
        where = thread.mention if thread is not None else "this channel"
        kind = "Stockpile" if self.track_expiry else "Inventory"
        await interaction.followup.send(
            f"📦 **{kind}** `{stockpile.name}` created in {where}.", ephemeral=True
        )


class RefreshStockpileModal(discord.ui.Modal, title="Refresh Stockpile"):
    stockpile_id_input = discord.ui.TextInput(
        label="Stockpile ID",
        placeholder="8-character ID shown on the stockpile card",
        min_length=8,
        max_length=8,
    )

    def __init__(self, bot: "StockpileBot"):
        super().__init__()
        self.bot = bot

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            stockpile = self.bot.store.refresh(
                self.stockpile_id_input.value.strip(),
                user_id=interaction.user.id,
                guild_id=interaction.guild_id,
            )
        except KeyError:
            await interaction.response.send_message(
                "Unknown stockpile ID. Use the **List** button to find IDs.", ephemeral=True
            )
            return
        await self.bot.update_stockpile_message(stockpile)
        await interaction.response.send_message(
            f"✅ Refreshed `{stockpile.name}`. Expires <t:{unix_ts(stockpile.expires_datetime)}:R>.",
            ephemeral=True,
        )


class DeleteStockpileModal(discord.ui.Modal, title="Delete Stockpile"):
    stockpile_id_input = discord.ui.TextInput(
        label="Stockpile ID",
        placeholder="8-character ID shown on the stockpile card",
        min_length=8,
        max_length=8,
    )

    def __init__(self, bot: "StockpileBot"):
        super().__init__()
        self.bot = bot

    async def on_submit(self, interaction: discord.Interaction) -> None:
        sid = self.stockpile_id_input.value.strip()
        stockpile = self.bot.store.get(sid, guild_id=interaction.guild_id)
        if stockpile is None:
            await interaction.response.send_message(
                "Unknown stockpile ID. Use the **List** button to find IDs.", ephemeral=True
            )
            return
        await interaction.response.send_message(f"🗑️ Removed stockpile `{sid}`.", ephemeral=True)
        # A forum stockpile keeps its post as a record (archived + locked); a
        # plain card is deleted outright.
        if stockpile.thread_id:
            await self.bot.archive_thread(
                stockpile.channel_id, stockpile.message_id, f"🗑️ {stockpile.name}"
            )
        else:
            await self.bot.delete_card_message(stockpile.channel_id, stockpile.message_id)
        self.bot.store.delete(sid, guild_id=interaction.guild_id)


def _parse_quantity(raw: str) -> int | None:
    """Parse a positive whole-number quantity, tolerating commas/whitespace."""
    try:
        quantity = int((raw or "").replace(",", "").strip())
    except (ValueError, AttributeError):
        return None
    return quantity if quantity > 0 else None


async def _rerender_cart(interaction: discord.Interaction, bot, draft, hint=None) -> None:
    """Edit the originating ephemeral message back to the cart view."""
    from foxhole_buddy.ui.embeds import cart_embed
    from foxhole_buddy.ui.views.logistics import LogisticsCartView

    await interaction.response.edit_message(
        embed=cart_embed(draft, hint=hint), view=LogisticsCartView(bot, draft)
    )


class LineQuantityModal(discord.ui.Modal, title="Add Item"):
    """Capture a quantity for a browsed item and add it to the request cart."""

    quantity_input = discord.ui.TextInput(
        label="Quantity",
        placeholder="e.g. 5 (crates or units)",
        max_length=9,
    )

    def __init__(self, bot: "StockpileBot", draft, category: str, subcategory: str, item: str):
        super().__init__()
        self.bot = bot
        self.draft = draft
        self.category = category
        self.subcategory = subcategory
        self.item = item
        self.title = f"Add: {item}"[:45]  # modal titles cap at 45 chars

    async def on_submit(self, interaction: discord.Interaction) -> None:
        quantity = _parse_quantity(self.quantity_input.value)
        if quantity is None:
            await interaction.response.send_message(
                "Quantity must be a positive whole number.", ephemeral=True
            )
            return
        from foxhole_buddy.core.models import make_line
        self.draft.lines.append(make_line(self.category, self.subcategory, self.item, quantity))
        await _rerender_cart(interaction, self.bot, self.draft)


class SearchItemModal(discord.ui.Modal, title="Add by Name"):
    """Type an item name + quantity; fuzzy-match the catalog and add to the cart."""

    name_input = discord.ui.TextInput(
        label="Item name",
        placeholder="e.g. bandages, grenade, 7.62, materials",
        max_length=100,
    )
    quantity_input = discord.ui.TextInput(
        label="Quantity",
        placeholder="e.g. 5 (crates or units)",
        max_length=9,
    )

    def __init__(self, bot: "StockpileBot", draft):
        super().__init__()
        self.bot = bot
        self.draft = draft

    async def on_submit(self, interaction: discord.Interaction) -> None:
        quantity = _parse_quantity(self.quantity_input.value)
        if quantity is None:
            await interaction.response.send_message(
                "Quantity must be a positive whole number.", ephemeral=True
            )
            return
        faction = self.bot.store.get_guild_faction(interaction.guild_id)
        # Fetch beyond one dropdown page — SearchResultView paginates the rest.
        matches = self.bot.catalog.search(self.name_input.value, faction, limit=100)
        query = self.name_input.value.strip()
        if not matches:
            # No catalog hit → offer "did you mean?" suggestions and the option
            # to add the typed text as a custom (off-catalog) item.
            from foxhole_buddy.ui.embeds import cart_embed
            from foxhole_buddy.ui.views.logistics import NoMatchView
            suggestions = self.bot.catalog.suggest(query, faction)
            hint = (
                f'No catalog match for "{query}". Did you mean one of these — '
                "or add it as a custom item?"
                if suggestions
                else f'No catalog match for "{query}". Add it as a custom item, or use Browse.'
            )
            await interaction.response.edit_message(
                embed=cart_embed(self.draft, hint=hint),
                view=NoMatchView(self.bot, self.draft, query, quantity, suggestions),
            )
            return
        if len(matches) == 1:
            from foxhole_buddy.core.models import make_line
            m = matches[0]
            self.draft.lines.append(
                make_line(m["category_label"], m["subcategory_label"], m["name"], quantity)
            )
            await _rerender_cart(interaction, self.bot, self.draft)
            return
        # Multiple hits → let them disambiguate, carrying the typed quantity.
        from foxhole_buddy.ui.embeds import cart_embed
        from foxhole_buddy.ui.views.logistics import SearchResultView
        await interaction.response.edit_message(
            embed=cart_embed(self.draft, hint=f'Multiple matches for "{query}" — pick one:'),
            view=SearchResultView(self.bot, self.draft, matches, quantity),
        )


class LogisticsNotesModal(discord.ui.Modal, title="Delivery Notes"):
    """Set/replace the delivery notes for the whole request."""

    def __init__(self, bot: "StockpileBot", draft):
        super().__init__()
        self.bot = bot
        self.draft = draft
        self.notes_input = discord.ui.TextInput(
            label="Notes (optional)",
            placeholder="e.g. drop at the frontline bunker base",
            style=discord.TextStyle.paragraph,
            required=False,
            max_length=300,
            default=draft.notes or None,
        )
        self.add_item(self.notes_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        self.draft.notes = self.notes_input.value or ""
        await _rerender_cart(interaction, self.bot, self.draft)


_ITEM_SEP = re.compile(r",\s+|,(?=\D)")
# A whole token that is purely a number (optional thousands commas / decimal),
# maybe glued to an "x" multiplier (10x, x10). Calibers like "12.7mm" or ".44"
# are NOT whole numbers, so they stay part of the name.
_QTY_TOKEN = re.compile(r"^x?(\d[\d,]*(?:\.\d+)?)x?$", re.IGNORECASE)


def _as_quantity(token: str) -> float | None:
    m = _QTY_TOKEN.match(token)
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


def parse_quantity_list(text: str) -> tuple[list[tuple[str, float]], list[str]]:
    """Parse a pasted "<qty> <item>" list into (items, skipped).

    Entries are separated by newlines and by commas, so several items can share
    a line (``10 Cartena, 20 Fuscina``). Within an entry the quantity is the
    first token if it is purely a number, else the last such token — so a
    caliber embedded in the name (``65 12.7mm Ammunition``, ``9mm 8``) is never
    mistaken for the count. Names are normalised via
    :func:`canonicalize_material_name` and duplicates summed in first-seen order.
    Segments with no quantity or no name are returned in ``skipped`` (verbatim).
    """
    from foxhole_buddy.core.models import canonicalize_material_name

    items: dict[str, list] = {}
    order: list[str] = []
    skipped: list[str] = []

    segments = [seg for line in text.splitlines() for seg in _ITEM_SEP.split(line)]
    for raw in segments:
        segment = raw.strip()
        if not segment:
            continue

        tokens = segment.split()
        if _as_quantity(tokens[0]) is not None:
            qty, name_tokens = _as_quantity(tokens[0]), tokens[1:]
        elif len(tokens) > 1 and _as_quantity(tokens[-1]) is not None:
            qty, name_tokens = _as_quantity(tokens[-1]), tokens[:-1]
        else:
            skipped.append(segment)
            continue

        name = canonicalize_material_name(" ".join(name_tokens))
        if not name or qty is None or qty <= 0:
            skipped.append(segment)
            continue

        if name not in items:
            items[name] = [name, 0.0]
            order.append(name)
        items[name][1] += qty

    return [(items[k][0], items[k][1]) for k in order], skipped


def plan_removals(inventory, items, resolve):
    """Partition a parsed removal list against current stock (pure, testable).

    ``inventory`` maps material -> amount; ``items`` is ``[(name, qty), …]`` from
    :func:`parse_quantity_list`; ``resolve(name)`` maps a typed name to the
    inventory key it should hit. Returns three lists:

    - ``to_remove`` — ``[(key, qty)]`` that fit within stock (apply directly);
    - ``not_found`` — typed names with no stock at all;
    - ``over``      — ``[(key, requested, available)]`` asking for more than in
      stock (caller confirms before zeroing these).
    """
    to_remove: list[tuple[str, float]] = []
    not_found: list[str] = []
    over: list[tuple[str, float, float]] = []
    for name, qty in items:
        key = resolve(name)
        available = inventory.get(key, 0)
        if available <= 0:
            not_found.append(name)
        elif qty <= available:
            to_remove.append((key, qty))
        else:
            over.append((key, qty, available))
    return to_remove, not_found, over


def _qty_str(qty: float) -> str:
    return f"{int(qty)}" if float(qty).is_integer() else f"{qty:.2f}"


# ── Off-site inventory (named locations) ──────────────────────────────────────
# These mirror the base-inventory modals but thread a ``location_id`` through and
# call the ``*_offsite_*`` store methods, reusing the same parsing/planning helpers.


# ── Per-stockpile inventory ───────────────────────────────────────────────────
# Mirror the off-site modals, but bound to a stockpile and refreshing its card
# (the posted stockpile message shows its inventory) after every change.


async def _refresh_stockpile_card(bot: "StockpileBot", stockpile_id: str) -> None:
    stockpile = bot.store.get(stockpile_id)
    if stockpile is not None:
        try:
            await bot.update_stockpile_message(stockpile)
        except Exception:  # noqa: BLE001 — card refresh is best-effort
            pass


class AddStockpileItemModal(discord.ui.Modal, title="Add to Stockpile"):
    material_input = discord.ui.TextInput(
        label="Material Name", placeholder='e.g. "Bmats" or "Diesel"', max_length=100,
    )
    amount_input = discord.ui.TextInput(
        label="Amount", placeholder="e.g. 10.5 or 500", max_length=20,
    )

    def __init__(self, bot: "StockpileBot", stockpile_id: str):
        super().__init__()
        self.bot = bot
        self.stockpile_id = stockpile_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            amount = float(self.amount_input.value.replace(",", "").strip())
            if amount <= 0:
                raise ValueError
        except ValueError:
            await interaction.response.send_message("Amount must be a number greater than 0.", ephemeral=True)
            return
        guild_id = interaction.guild_id or 0
        material = self.material_input.value
        catalog = getattr(self.bot, "catalog", None)
        stored = (catalog.best_match(material) if catalog else None) or material
        self.bot.store.add_to_stockpile_inventory(guild_id, self.stockpile_id, stored, amount)
        await interaction.response.send_message(
            f"✅ Added `{_qty_str(amount)}` of **{stored}** to this stockpile.", ephemeral=True
        )
        await _refresh_stockpile_card(self.bot, self.stockpile_id)


class RemoveStockpileItemModal(discord.ui.Modal, title="Remove from Stockpile"):
    material_input = discord.ui.TextInput(
        label="Material Name", placeholder='e.g. "Bmats" or "Diesel"', max_length=100,
    )
    amount_input = discord.ui.TextInput(
        label="Amount", placeholder="e.g. 10.5 or 500", max_length=20,
    )

    def __init__(self, bot: "StockpileBot", stockpile_id: str):
        super().__init__()
        self.bot = bot
        self.stockpile_id = stockpile_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        from foxhole_buddy.core.models import canonicalize_material_name

        try:
            amount = float(self.amount_input.value.replace(",", "").strip())
            if amount <= 0:
                raise ValueError
        except ValueError:
            await interaction.response.send_message("Amount must be a number greater than 0.", ephemeral=True)
            return
        material = self.material_input.value
        catalog = getattr(self.bot, "catalog", None)
        key = canonicalize_material_name((catalog.best_match(material) if catalog else None) or material)
        try:
            self.bot.store.remove_from_stockpile_inventory(self.stockpile_id, key, amount)
            await interaction.response.send_message(
                f"➖ Removed `{_qty_str(amount)}` of **{key}** from this stockpile.", ephemeral=True
            )
            await _refresh_stockpile_card(self.bot, self.stockpile_id)
        except KeyError:
            await interaction.response.send_message(
                f"❌ **{key}** is not in this stockpile.", ephemeral=True
            )
        except ValueError as e:
            await interaction.response.send_message(f"❌ {str(e)}", ephemeral=True)


class BulkAddStockpileModal(discord.ui.Modal, title="Add List to Stockpile"):
    list_input = discord.ui.TextInput(
        label="Items — one per line: <qty> <name>",
        style=discord.TextStyle.paragraph,
        placeholder="120 Bmats\n40 Diesel\n10 Rifles",
        max_length=2000,
    )

    def __init__(self, bot: "StockpileBot", stockpile_id: str):
        super().__init__()
        self.bot = bot
        self.stockpile_id = stockpile_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        items, skipped = parse_quantity_list(self.list_input.value)
        if not items:
            await interaction.response.send_message(
                "❌ Couldn't read any `<quantity> <item>` lines. Example: `120 Bmats`",
                ephemeral=True,
            )
            return
        guild_id = interaction.guild_id or 0
        catalog = getattr(self.bot, "catalog", None)
        added_lines = []
        for name, qty in items:
            match = catalog.best_match(name) if catalog else None
            stored = match or name
            self.bot.store.add_to_stockpile_inventory(guild_id, self.stockpile_id, stored, qty)
            tag = "" if match else "  *(custom)*"
            added_lines.append(f"• **{stored}** ×{_qty_str(qty)}{tag}")
        embed = discord.Embed(
            title="✅ Added to Stockpile", description="\n".join(added_lines), color=Color.SLATE,
        )
        if skipped:
            embed.add_field(
                name=f"⚠️ Skipped {len(skipped)} line(s)",
                value="\n".join(f"`{s[:60]}`" for s in skipped[:10]),
                inline=False,
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        await _refresh_stockpile_card(self.bot, self.stockpile_id)


class BulkRemoveStockpileModal(discord.ui.Modal, title="Remove List from Stockpile"):
    list_input = discord.ui.TextInput(
        label="Items — one per line: <qty> <name>",
        style=discord.TextStyle.paragraph,
        placeholder="120 Bmats\n40 Diesel\n10 Rifles",
        max_length=2000,
    )

    def __init__(self, bot: "StockpileBot", stockpile_id: str):
        super().__init__()
        self.bot = bot
        self.stockpile_id = stockpile_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        from foxhole_buddy.core.models import canonicalize_material_name

        items, skipped = parse_quantity_list(self.list_input.value)
        if not items:
            await interaction.response.send_message(
                "❌ Couldn't read any `<quantity> <item>` lines. Example: `120 Bmats`",
                ephemeral=True,
            )
            return
        catalog = getattr(self.bot, "catalog", None)

        def resolve(name: str) -> str:
            match = catalog.best_match(name) if catalog else None
            return canonicalize_material_name(match or name)

        inventory = self.bot.store.get_stockpile_inventory(self.stockpile_id)
        to_remove, not_found, over = plan_removals(inventory, items, resolve)

        removed_lines = []
        for key, qty in to_remove:
            self.bot.store.remove_from_stockpile_inventory(self.stockpile_id, key, qty)
            removed_lines.append(f"• **{key}** ×{_qty_str(qty)}")
        embed = discord.Embed(
            title="➖ Removed from Stockpile",
            description="\n".join(removed_lines) or "*Nothing removed yet.*",
            color=Color.SLATE,
        )
        misses = not_found + [f"{s}" for s in skipped]
        if misses:
            embed.add_field(
                name=f"⚠️ Not in inventory ({len(misses)})",
                value="\n".join(f"`{m[:60]}`" for m in misses[:10]),
                inline=False,
            )
        if over:
            embed.add_field(
                name="❓ More than in stock",
                value="\n".join(
                    f"**{key}** — asked {_qty_str(req)}, only {_qty_str(avail)} in stock"
                    for key, req, avail in over[:10]
                )
                + "\n\nZero these out, or leave them as-is?",
                inline=False,
            )
            await interaction.response.send_message(
                embed=embed,
                view=BulkRemoveStockpileConfirmView(self.bot, self.stockpile_id, over),
                ephemeral=True,
            )
            await _refresh_stockpile_card(self.bot, self.stockpile_id)
            return
        await interaction.response.send_message(embed=embed, ephemeral=True)
        await _refresh_stockpile_card(self.bot, self.stockpile_id)


class BulkRemoveStockpileConfirmView(discord.ui.View):
    """Confirms zeroing over-stock items in a stockpile (stockpile twin of the
    off-site confirm view)."""

    def __init__(self, bot: "StockpileBot", stockpile_id: str, over: list[tuple[str, float, float]]):
        super().__init__(timeout=120)
        self.bot = bot
        self.stockpile_id = stockpile_id
        self.over = over

    @discord.ui.button(label="Zero them out", style=discord.ButtonStyle.danger, emoji="✅")
    async def zero_out(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        cleared = []
        for key, _req, avail in self.over:
            try:
                self.bot.store.remove_from_stockpile_inventory(self.stockpile_id, key, avail)
                cleared.append(f"• **{key}** → 0")
            except (KeyError, ValueError):
                continue
        embed = discord.Embed(
            title="➖ Zeroed Out",
            description="\n".join(cleared) or "*Nothing to clear.*",
            color=Color.SLATE,
        )
        await interaction.response.edit_message(embed=embed, view=None)
        await _refresh_stockpile_card(self.bot, self.stockpile_id)

    @discord.ui.button(label="Leave them", style=discord.ButtonStyle.secondary, emoji="❌")
    async def leave(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        embed = discord.Embed(
            title="Left unchanged",
            description="\n".join(f"• **{key}** ×{_qty_str(avail)}" for key, _req, avail in self.over),
            color=Color.GRAY,
        )
        await interaction.response.edit_message(embed=embed, view=None)


class AddFactoryAlarmModal(discord.ui.Modal, title="Set Factory Alarm"):
    facility_input = discord.ui.TextInput(
        label="Facility Name",
        placeholder='e.g. "Coke Refinery" or "Blast Furnace"',
        max_length=100,
    )
    duration_input = discord.ui.TextInput(
        label="Duration (in minutes)",
        placeholder="e.g. 60 for 1h (Rounded to nearest 5m)",
        max_length=40,
    )

    def __init__(self, bot: "StockpileBot", single_ping: bool):
        super().__init__()
        self.bot = bot
        self.single_ping = single_ping

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            duration = int(self.duration_input.value.strip())
            if duration < 5:
                raise ValueError("Duration must be at least 5 minutes.")
        except ValueError:
            await interaction.response.send_message("Please enter a valid number of minutes (minimum 5).", ephemeral=True)
            return

        # Round to nearest 5
        remainder = duration % 5
        if remainder > 0:
            if remainder >= 3:
                duration += (5 - remainder)
            else:
                duration -= remainder

        alarm = self.bot.store.create_factory_alarm(
            guild_id=interaction.guild_id or 0,
            channel_id=interaction.channel_id,
            facility_name=self.facility_input.value,
            duration_minutes=duration,
            single_ping=self.single_ping,
            user_id=interaction.user.id,
        )

        from foxhole_buddy.ui.views import FactoryAlarmCardView
        await interaction.response.send_message(
            embed=factory_alarm_embed(alarm),
            view=FactoryAlarmCardView(self.bot, alarm.id),
        )
        message = await interaction.original_response()
        self.bot.store.set_factory_alarm_message_id(alarm.id, message.id)


# ── Operations ───────────────────────────────────────────────────────────────────

import re
from datetime import datetime, timezone
from foxhole_buddy.core.store import make_squad
from foxhole_buddy.ui.embeds import operation_card_embed

_DATE_FORMATS = ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%d/%m/%Y %H:%M")


def parse_schedule(raw: str) -> datetime | None:
    """Parse an operator-entered UTC date/time into an aware UTC datetime."""
    raw = raw.strip().replace("T", " ")
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def parse_squads(raw: str) -> list[dict]:
    """One squad per line; an optional trailing 'xN' / '×N' sets capacity."""
    squads: list[dict] = []
    seen: set[str] = set()
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        capacity = 0
        match = re.search(r"[x×]\s*(\d+)\s*$", line, re.IGNORECASE)
        if match:
            capacity = int(match.group(1))
            line = line[: match.start()].strip()
        if not line:
            continue
        squad = make_squad(line, capacity)
        if squad["key"] in seen:
            continue
        seen.add(squad["key"])
        squads.append(squad)
    return squads


def squads_to_text(squads: list[dict]) -> str:
    lines = []
    for squad in squads:
        if squad["capacity"]:
            lines.append(f"{squad['name']} x{squad['capacity']}")
        else:
            lines.append(squad["name"])
    return "\n".join(lines)


class CreateOperationModal(discord.ui.Modal, title="Schedule Operation"):
    name_input = discord.ui.TextInput(label="Operation Name", max_length=100)
    schedule_input = discord.ui.TextInput(
        label="Date & Time — UTC",
        placeholder="YYYY-MM-DD HH:MM   e.g. 2026-06-28 23:30",
        max_length=20,
    )
    location_input = discord.ui.TextInput(
        label="Location (optional)", required=False, max_length=100,
        placeholder="e.g. Kuoppa Seaport",
    )
    description_input = discord.ui.TextInput(
        label="Briefing (optional)", required=False, style=discord.TextStyle.paragraph,
        max_length=1000,
    )
    squads_input = discord.ui.TextInput(
        label="Squads (optional) — one per line",
        required=False, style=discord.TextStyle.paragraph, max_length=500,
        placeholder="Flame Tank Crew x6\nTremola Squad x3\nInfantry",
    )

    def __init__(self, bot: "StockpileBot", ally_room: str | None = None):
        super().__init__()
        self.bot = bot
        # When set, the op is shared across this ally room and mirrored into every
        # member server's channel instead of being posted in the current channel.
        self.ally_room = ally_room

    async def on_submit(self, interaction: discord.Interaction) -> None:
        when = parse_schedule(self.schedule_input.value)
        if when is None:
            await interaction.response.send_message(
                "Couldn't read that date/time. Use **UTC** like `2026-06-28 23:30`.",
                ephemeral=True,
            )
            return

        if self.ally_room:
            await self._submit_allied(interaction, when)
            return

        op = self.bot.store.create_operation(
            guild_id=interaction.guild_id or 0,
            channel_id=interaction.channel_id,
            name=self.name_input.value,
            scheduled_at=when,
            leader_user_id=interaction.user.id,
            description=self.description_input.value or "",
            location=self.location_input.value or "",
            war_number=getattr(self.bot, "war_number", None),
            squads=parse_squads(self.squads_input.value or ""),
        )

        from foxhole_buddy.ui.views import OperationCardView

        # When the ops channel is a forum, each op is its own forum post (thread);
        # a plain text ops channel keeps the card inline as before.
        ops_id = self.bot.store.get_alert_channel(interaction.guild_id, "ops")
        parent = self.bot.get_channel(ops_id) if ops_id else None
        if parent is None and ops_id:
            try:
                parent = await self.bot.fetch_channel(ops_id)
            except Exception:  # noqa: BLE001 — treat an unreachable channel as "not a forum"
                parent = None
        if isinstance(parent, discord.ForumChannel):
            await self._submit_forum_post(interaction, op, parent)
            return

        await interaction.response.send_message(
            embed=operation_card_embed(op),
            view=OperationCardView(self.bot, op.id),
        )
        message = await interaction.original_response()
        self.bot.store.set_operation_message_id(op.id, message.id)

    async def _submit_forum_post(
        self, interaction: discord.Interaction, op, forum: discord.ForumChannel
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            thread, message = await self.bot.open_operation_thread(op, forum)
        except Exception as exc:  # noqa: BLE001 — perms / required forum tags / etc.
            log.warning("Op forum post creation failed (%r); rolling back op.", exc)
            self.bot.store.delete_operation(op.id)
            await interaction.followup.send(
                f"⚠️ I couldn't create the op post in {forum.mention}. Check my "
                "**Create Posts / Send Messages** there — and if the forum requires a "
                "tag, ops posts can't set one, so use a plain text ops channel instead.",
                ephemeral=True,
            )
            return
        self.bot.store.attach_operation_thread(op.id, thread_id=thread.id, message_id=message.id)
        await interaction.followup.send(
            f"⚔️ **Op #{op.op_number}** posted in {thread.mention}.", ephemeral=True
        )

    async def _submit_allied(self, interaction: discord.Interaction, when: datetime) -> None:
        # The host's copy lives in its own bound channel for the room.
        host_channel = next(
            (cid for gid, cid in self.bot.store.ally_members(self.ally_room)
             if gid == interaction.guild_id),
            interaction.channel_id,
        )
        op = self.bot.store.create_operation(
            guild_id=interaction.guild_id or 0,
            channel_id=host_channel,
            name=self.name_input.value,
            scheduled_at=when,
            leader_user_id=interaction.user.id,
            description=self.description_input.value or "",
            location=self.location_input.value or "",
            war_number=getattr(self.bot, "war_number", None),
            squads=parse_squads(self.squads_input.value or ""),
            ally_room=self.ally_room,
        )
        # Record the creator's identity so the leader renders by name in every
        # mirror — without this they'd show as a raw <@id> that won't resolve in
        # the other servers' copies (they haven't RSVP'd, so have no meta yet).
        op = self.bot.store.set_participant_meta(
            op.id, interaction.user.id,
            name=interaction.user.display_name,
            faction=self.bot.store.get_guild_faction(interaction.guild_id),
            guild_id=interaction.guild_id,
            server=interaction.guild.name if interaction.guild else None,
        )
        # Respond first (fan-out is slow network I/O past Discord's ~3s window).
        await interaction.response.defer(ephemeral=True)
        count = await self.bot.post_allied_op(op)
        await interaction.followup.send(
            f"🤝 Allied op **#{op.op_number} — {op.name}** posted to **{count}** "
            f"server(s) in room `{self.ally_room}`.",
            ephemeral=True,
        )


class EditOperationModal(discord.ui.Modal, title="Edit Operation"):
    def __init__(self, bot: "StockpileBot", op_id: str):
        super().__init__()
        self.bot = bot
        self.op_id = op_id
        op = bot.store.get_operation(op_id)
        self.title = f"Edit Op #{op.op_number}"[:45] if op else "Edit Operation"

        prefill = op.scheduled_datetime.strftime("%Y-%m-%d %H:%M") if op else ""
        self.name_input = discord.ui.TextInput(
            label="Operation Name", max_length=100, default=op.name if op else "",
        )
        self.schedule_input = discord.ui.TextInput(
            label="Date & Time — UTC", max_length=20, default=prefill,
        )
        self.location_input = discord.ui.TextInput(
            label="Location (optional)", required=False, max_length=100,
            default=op.location if op else "",
        )
        self.description_input = discord.ui.TextInput(
            label="Briefing (optional)", required=False, style=discord.TextStyle.paragraph,
            max_length=1000, default=op.description if op else "",
        )
        self.squads_input = discord.ui.TextInput(
            label="Squads (optional) — one per line",
            required=False, style=discord.TextStyle.paragraph, max_length=500,
            default=squads_to_text(op.squads) if op else "",
        )
        for item in (self.name_input, self.schedule_input, self.location_input,
                     self.description_input, self.squads_input):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        # Reached only via the card's leader-gated Edit button; allied ops are
        # editable by the host from any member server, so don't filter by guild.
        op = self.bot.store.get_operation(self.op_id)
        if op is None:
            await interaction.response.send_message("That operation no longer exists.", ephemeral=True)
            return
        when = parse_schedule(self.schedule_input.value)
        if when is None:
            await interaction.response.send_message(
                "Couldn't read that date/time. Use **UTC** like `2026-06-28 23:30`.",
                ephemeral=True,
            )
            return

        from foxhole_buddy.core.store import dt_to_str
        op.name = self.name_input.value.strip()
        op.scheduled_at = dt_to_str(when)
        op.location = (self.location_input.value or "").strip()
        op.description = (self.description_input.value or "").strip()
        self.bot.store.update_operation(op)
        # Replace squads while preserving sign-ups for surviving squad names.
        new_defs = [(s["name"], s["capacity"]) for s in parse_squads(self.squads_input.value or "")]
        op = self.bot.store.set_squads(self.op_id, squad_defs=new_defs)

        await self.bot.update_operation_message(op)
        await interaction.response.send_message("✅ Operation updated.", ephemeral=True)


class NotifyOperationModal(discord.ui.Modal, title="Notify Attendees"):
    message_input = discord.ui.TextInput(
        label="Message (optional)", required=False, style=discord.TextStyle.paragraph,
        max_length=500, placeholder="e.g. Form up at the staging base now!",
    )

    def __init__(self, bot: "StockpileBot", op_id: str):
        super().__init__()
        self.bot = bot
        self.op_id = op_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        op = self.bot.store.get_operation(self.op_id)
        if op is None:
            await interaction.response.send_message("That operation no longer exists.", ephemeral=True)
            return
        recipients = op.participant_ids() + op.tentative
        if not recipients:
            await interaction.response.send_message("Nobody has signed up to notify yet.", ephemeral=True)
            return
        note = self.message_input.value.strip() or "Heads up — check the operation details."
        if op.ally_room:
            # Fan the notice to every allied server, pinging each one's own people.
            await interaction.response.send_message(
                "📣 Notified every allied server.", ephemeral=True
            )
            await self.bot.announce_allied_op(
                op, f"📣 **Op #{op.op_number} — {op.name}**\n{note}", recipients=recipients
            )
            return
        mentions = " ".join(f"<@{uid}>" for uid in recipients)
        await interaction.response.send_message(
            f"📣 **Op #{op.op_number} — {op.name}**\n{mentions}\n{note}"
        )
