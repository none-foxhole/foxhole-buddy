"""Logistics views: the cart builder (search + browse) and the request card.

A request is a *shopping list*: the user fills an ephemeral cart (by typing an
item name or browsing the catalog), then submits it once. The posted card lets
drivers claim/validate the whole list or individual line items.
"""

from typing import TYPE_CHECKING

import discord

from foxhole_buddy.core.store import LOGI_CLAIMED, LOGI_DELIVERED, LOGI_OPEN
from foxhole_buddy.theme import Color
from foxhole_buddy.ui.embeds import (
    cart_embed,
    logistics_menu_embed,
    logistics_request_embed,
    main_menu_embed,
)
from foxhole_buddy.ui.modals import (
    LineQuantityModal,
    LogisticsNotesModal,
    SearchItemModal,
    parse_quantity_list,
)

if TYPE_CHECKING:
    from foxhole_buddy.core.bot import StockpileBot


class LogisticsDraft:
    """In-memory request being assembled in the cart (one user, one session).

    Threaded by reference through the cart's sub-views/modals; never persisted
    until the user hits Submit.
    """

    def __init__(self, faction: str | None = None):
        self.lines: list[dict] = []
        self.notes: str = ""
        self.faction = faction


class LogisticsActionsView(discord.ui.View):
    """Logistics sub-menu: New Request (cart builder) / Open Requests / Back."""

    def __init__(self, bot: "StockpileBot"):
        super().__init__(timeout=120)
        self.bot = bot

    @discord.ui.button(label="New Request", style=discord.ButtonStyle.danger, emoji="➕", row=0)
    async def new_request_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await self.bot._check_channel(interaction):
            return
        faction = self.bot.store.get_guild_faction(interaction.guild_id)
        if self.bot.catalog.is_empty() or not self.bot.catalog.categories(faction):
            await interaction.response.send_message(
                "The item catalog isn't available yet — try again in a moment.", ephemeral=True
            )
            return
        draft = LogisticsDraft(faction)
        await interaction.response.edit_message(embed=cart_embed(draft), view=LogisticsCartView(self.bot, draft))

    @discord.ui.button(label="Open Requests", style=discord.ButtonStyle.secondary, emoji="📊", row=0)
    async def board_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await self.bot._check_channel(interaction):
            return
        requests = self.bot.store.get_logistics_requests(
            guild_id=interaction.guild_id, include_delivered=False
        )
        if not requests:
            await interaction.response.send_message(
                embed=discord.Embed(
                    title="No Open Requests",
                    description="Nothing needs delivering right now.",
                    color=Color.GRAY,
                ),
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True)
        for request in requests:
            await interaction.followup.send(
                embed=logistics_request_embed(request),
                view=LogisticsRequestCardView(self.bot, request.id),
                ephemeral=True,
            )

    @discord.ui.button(label="Back", style=discord.ButtonStyle.secondary, emoji="◀️", row=1)
    async def back_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        from foxhole_buddy.ui.views.menu import MainMenuView

        await interaction.response.edit_message(embed=main_menu_embed(), view=MainMenuView(self.bot))


# ── Cart builder ──────────────────────────────────────────────────────────────────


class LogisticsCartView(discord.ui.View):
    """The cart: add items (search or browse), tweak, and submit as one request."""

    def __init__(self, bot: "StockpileBot", draft: LogisticsDraft):
        super().__init__(timeout=300)
        self.bot = bot
        self.draft = draft
        has_items = bool(draft.lines)
        self.remove_button.disabled = not has_items
        self.submit_button.disabled = not has_items

    @discord.ui.button(label="Add by Name", style=discord.ButtonStyle.primary, emoji="🔎", row=0)
    async def search_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(SearchItemModal(self.bot, self.draft))

    @discord.ui.button(label="Browse", style=discord.ButtonStyle.secondary, emoji="📂", row=0)
    async def browse_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.edit_message(
            embed=_browse_embed("Choose a **category**."),
            view=CategorySelectView(self.bot, self.draft),
        )

    @discord.ui.button(label="Bulk", style=discord.ButtonStyle.secondary, emoji="📋", row=0)
    async def bulk_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.edit_message(
            embed=cart_embed(self.draft, hint="Paste a list to add or remove many items at once."),
            view=BulkCartView(self.bot, self.draft),
        )

    @discord.ui.button(label="Notes", style=discord.ButtonStyle.secondary, emoji="📝", row=0)
    async def notes_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(LogisticsNotesModal(self.bot, self.draft))

    @discord.ui.button(label="Remove", style=discord.ButtonStyle.secondary, emoji="➖", row=1)
    async def remove_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.edit_message(
            embed=cart_embed(self.draft, hint="Pick the item(s) to remove:"),
            view=RemoveLineView(self.bot, self.draft),
        )

    @discord.ui.button(label="Submit", style=discord.ButtonStyle.success, emoji="✅", row=1)
    async def submit_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not self.draft.lines:
            await interaction.response.send_message("Your cart is empty.", ephemeral=True)
            return
        request = self.bot.store.create_logistics_request(
            guild_id=interaction.guild_id or 0,
            channel_id=interaction.channel_id,
            items=self.draft.lines,
            user_id=interaction.user.id,
            notes=self.draft.notes,
        )
        await interaction.response.edit_message(
            embed=discord.Embed(
                title="✅ Request posted",
                description=f"Your {request.item_count()}-item request is now on the board below.",
                color=Color.BRAND,
            ),
            view=None,
        )
        channel = (
            interaction.channel
            or self.bot.get_channel(interaction.channel_id)
            or await self.bot.fetch_channel(interaction.channel_id)
        )
        msg = await channel.send(
            embed=logistics_request_embed(request),
            view=LogisticsRequestCardView(self.bot, request.id),
        )
        self.bot.store.set_logistics_request_message_id(request.id, msg.id)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger, emoji="✖️", row=1)
    async def cancel_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.edit_message(
            embed=logistics_menu_embed(), view=LogisticsActionsView(self.bot)
        )


class RemoveLineView(discord.ui.View):
    """Ephemeral multi-select to drop items from the cart, paginated 25 at a time
    (a bulk-added cart can hold more items than one dropdown can show)."""

    PER_PAGE = 25

    def __init__(self, bot: "StockpileBot", draft: LogisticsDraft, page: int = 0):
        super().__init__(timeout=300)
        self.bot = bot
        self.draft = draft
        self.pages = max(1, (len(draft.lines) + self.PER_PAGE - 1) // self.PER_PAGE)
        self.page = max(0, min(page, self.pages - 1))

        start = self.page * self.PER_PAGE
        page_lines = draft.lines[start : start + self.PER_PAGE]
        options = [
            discord.SelectOption(label=f"{line['item']} ×{line['quantity']:,}"[:100], value=line["lid"])
            for line in page_lines
        ]
        placeholder = "Select item(s) to remove…"
        if self.pages > 1:
            placeholder = f"Select item(s) to remove… (page {self.page + 1}/{self.pages})"
        if options:
            select = discord.ui.Select(
                placeholder=placeholder, min_values=0, max_values=len(options), options=options, row=0,
            )
            select.callback = self.on_select
            self.select = select
            self.add_item(select)

        if self.pages > 1:
            prev = discord.ui.Button(label="Prev", emoji="◀️", style=discord.ButtonStyle.secondary,
                                     row=1, disabled=self.page == 0)
            prev.callback = self.prev_page
            nxt = discord.ui.Button(label="Next", emoji="▶️", style=discord.ButtonStyle.secondary,
                                    row=1, disabled=self.page >= self.pages - 1)
            nxt.callback = self.next_page
            self.add_item(prev)
            self.add_item(nxt)

    async def on_select(self, interaction: discord.Interaction) -> None:
        dropped = set(self.select.values)
        self.draft.lines = [line for line in self.draft.lines if line["lid"] not in dropped]
        await interaction.response.edit_message(
            embed=cart_embed(self.draft), view=LogisticsCartView(self.bot, self.draft)
        )

    async def prev_page(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(
            embed=cart_embed(self.draft, hint="Pick the item(s) to remove:"),
            view=RemoveLineView(self.bot, self.draft, self.page - 1),
        )

    async def next_page(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(
            embed=cart_embed(self.draft, hint="Pick the item(s) to remove:"),
            view=RemoveLineView(self.bot, self.draft, self.page + 1),
        )

    @discord.ui.button(label="Back", style=discord.ButtonStyle.secondary, emoji="🛒", row=2)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.edit_message(
            embed=cart_embed(self.draft), view=LogisticsCartView(self.bot, self.draft)
        )


# ── Search disambiguation ──────────────────────────────────────────────────────────


class SearchResultView(discord.ui.View):
    """Shown when a name search returns several items — pick one to add. Paginated
    25 at a time so a broad query's matches beyond the dropdown cap stay reachable."""

    PER_PAGE = 25

    def __init__(self, bot: "StockpileBot", draft: LogisticsDraft, matches: list[dict],
                 quantity: int, page: int = 0):
        super().__init__(timeout=300)
        self.bot = bot
        self.draft = draft
        self.quantity = quantity
        self.all_matches = matches
        self.matches = {m["name"]: m for m in matches}
        self.pages = max(1, (len(matches) + self.PER_PAGE - 1) // self.PER_PAGE)
        self.page = max(0, min(page, self.pages - 1))

        start = self.page * self.PER_PAGE
        page_matches = matches[start : start + self.PER_PAGE]
        options = [
            discord.SelectOption(
                label=m["name"][:100],
                value=m["name"][:100],
                description=f"{m['category_label']} › {m['subcategory_label']}"[:100],
            )
            for m in page_matches
        ]
        placeholder = "Pick the item you meant…"
        if self.pages > 1:
            placeholder = f"Pick the item you meant… (page {self.page + 1}/{self.pages})"
        select = discord.ui.Select(placeholder=placeholder, options=options, row=0)
        select.callback = self.on_select
        self.select = select
        self.add_item(select)

        if self.pages > 1:
            prev = discord.ui.Button(label="Prev", emoji="◀️", style=discord.ButtonStyle.secondary,
                                     row=1, disabled=self.page == 0)
            prev.callback = self.prev_page
            nxt = discord.ui.Button(label="Next", emoji="▶️", style=discord.ButtonStyle.secondary,
                                    row=1, disabled=self.page >= self.pages - 1)
            nxt.callback = self.next_page
            self.add_item(prev)
            self.add_item(nxt)

    async def on_select(self, interaction: discord.Interaction) -> None:
        from foxhole_buddy.core.models import make_line

        m = self.matches.get(self.select.values[0])
        if m:
            self.draft.lines.append(
                make_line(m["category_label"], m["subcategory_label"], m["name"], self.quantity)
            )
        await interaction.response.edit_message(
            embed=cart_embed(self.draft), view=LogisticsCartView(self.bot, self.draft)
        )

    async def prev_page(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(
            view=SearchResultView(self.bot, self.draft, self.all_matches, self.quantity, self.page - 1),
        )

    async def next_page(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(
            view=SearchResultView(self.bot, self.draft, self.all_matches, self.quantity, self.page + 1),
        )

    @discord.ui.button(label="Back", style=discord.ButtonStyle.secondary, emoji="🛒", row=2)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.edit_message(
            embed=cart_embed(self.draft), view=LogisticsCartView(self.bot, self.draft)
        )


# Category/subcategory shown for an off-catalog (player-typed) item.
CUSTOM_CATEGORY = "Custom"
CUSTOM_SUBCATEGORY = "Off-catalog"


class NoMatchView(discord.ui.View):
    """Shown when a name search finds nothing: offer fuzzy suggestions and the
    option to add the typed text as a custom item."""

    def __init__(self, bot: "StockpileBot", draft: LogisticsDraft, query: str,
                 quantity: int, suggestions: list[dict]):
        super().__init__(timeout=300)
        self.bot = bot
        self.draft = draft
        self.query = query
        self.quantity = quantity
        self.suggestions = {s["name"]: s for s in suggestions}
        if suggestions:
            options = [
                discord.SelectOption(
                    label=s["name"][:100], value=s["name"][:100],
                    description=f"{s['category_label']} › {s['subcategory_label']}"[:100],
                )
                for s in suggestions[:25]
            ]
            select = discord.ui.Select(placeholder="Did you mean…?", options=options, row=0)
            select.callback = self.on_suggestion
            self.select = select
            self.add_item(select)
        # "Add custom" button label carries the typed text (capped to fit).
        self.add_custom_btn.label = f'Add "{query}" as custom'[:80]

    async def on_suggestion(self, interaction: discord.Interaction) -> None:
        from foxhole_buddy.core.models import make_line

        s = self.suggestions.get(self.select.values[0])
        if s:
            self.draft.lines.append(
                make_line(s["category_label"], s["subcategory_label"], s["name"], self.quantity)
            )
        await interaction.response.edit_message(
            embed=cart_embed(self.draft), view=LogisticsCartView(self.bot, self.draft)
        )

    @discord.ui.button(label="Add as custom", style=discord.ButtonStyle.primary, emoji="➕", row=1)
    async def add_custom_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        from foxhole_buddy.core.models import make_line

        self.draft.lines.append(
            make_line(CUSTOM_CATEGORY, CUSTOM_SUBCATEGORY, self.query, self.quantity)
        )
        await interaction.response.edit_message(
            embed=cart_embed(self.draft), view=LogisticsCartView(self.bot, self.draft)
        )

    @discord.ui.button(label="Back to cart", style=discord.ButtonStyle.secondary, emoji="🛒", row=1)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.edit_message(
            embed=cart_embed(self.draft), view=LogisticsCartView(self.bot, self.draft)
        )


# ── Bulk paste into / out of the cart ───────────────────────────────────────────────


def apply_cart_bulk_add(draft: "LogisticsDraft", items, catalog) -> list[tuple[str, int, bool]]:
    """Add parsed ``(name, qty)`` items to the cart, snapping to catalog lines
    when confident and falling back to custom lines otherwise. Returns
    ``[(name, qty, is_custom)]`` for the summary."""
    from foxhole_buddy.core.models import make_line

    added: list[tuple[str, int, bool]] = []
    for name, qty in items:
        canon = catalog.best_match(name, faction=draft.faction) if catalog else None
        entry = catalog.find(canon, faction=draft.faction) if canon else None
        if entry:
            draft.lines.append(
                make_line(entry["category_label"], entry["subcategory_label"], entry["name"], int(qty))
            )
            added.append((entry["name"], int(qty), False))
        else:
            draft.lines.append(make_line(CUSTOM_CATEGORY, CUSTOM_SUBCATEGORY, name, int(qty)))
            added.append((name, int(qty), True))
    return added


def apply_cart_bulk_remove(draft: "LogisticsDraft", items, catalog) -> list[str]:
    """Reduce cart lines matching parsed ``(name, qty)`` items, dropping any that
    hit zero. Returns the names with no matching line."""
    not_found: list[str] = []
    for name, qty in items:
        canon = (catalog.best_match(name, faction=draft.faction) if catalog else None) or name
        wanted = {canon.strip().lower(), name.strip().lower()}
        matched = [ln for ln in draft.lines if ln["item"].strip().lower() in wanted]
        if not matched:
            not_found.append(name)
            continue
        remaining = int(qty)
        for line in matched:
            if remaining <= 0:
                break
            take = min(line["quantity"], remaining)
            line["quantity"] -= take
            remaining -= take
    draft.lines = [ln for ln in draft.lines if ln["quantity"] > 0]
    return not_found


class BulkAddCartModal(discord.ui.Modal, title="Add List to Cart"):
    list_input = discord.ui.TextInput(
        label="Items — one per line: <qty> <name>",
        style=discord.TextStyle.paragraph,
        placeholder="20 Bmats\n5 Rmats\n2 Emats\n10 Bandages",
        max_length=2000,
    )

    def __init__(self, bot: "StockpileBot", draft: "LogisticsDraft"):
        super().__init__()
        self.bot = bot
        self.draft = draft

    async def on_submit(self, interaction: discord.Interaction) -> None:
        items, skipped = parse_quantity_list(self.list_input.value)
        if not items:
            await interaction.response.send_message(
                "❌ Couldn't read any `<quantity> <item>` lines. Example: `20 Bmats`",
                ephemeral=True,
            )
            return
        added = apply_cart_bulk_add(self.draft, items, self.bot.catalog)
        custom = sum(1 for _, _, is_custom in added if is_custom)
        hint = f"Added {len(added)} item(s)"
        if custom:
            hint += f" ({custom} custom)"
        if skipped:
            hint += f" · skipped {len(skipped)}"
        await interaction.response.edit_message(
            embed=cart_embed(self.draft, hint=hint), view=LogisticsCartView(self.bot, self.draft)
        )


class BulkRemoveCartModal(discord.ui.Modal, title="Remove List from Cart"):
    list_input = discord.ui.TextInput(
        label="Items — one per line: <qty> <name>",
        style=discord.TextStyle.paragraph,
        placeholder="10 Bmats\n2 Emats",
        max_length=2000,
    )

    def __init__(self, bot: "StockpileBot", draft: "LogisticsDraft"):
        super().__init__()
        self.bot = bot
        self.draft = draft

    async def on_submit(self, interaction: discord.Interaction) -> None:
        items, skipped = parse_quantity_list(self.list_input.value)
        if not items:
            await interaction.response.send_message(
                "❌ Couldn't read any `<quantity> <item>` lines. Example: `10 Bmats`",
                ephemeral=True,
            )
            return
        not_found = apply_cart_bulk_remove(self.draft, items, self.bot.catalog)
        hint = "Updated the cart"
        misses = not_found + skipped
        if misses:
            hint += f" · not in cart: {', '.join(misses[:5])}"
        await interaction.response.edit_message(
            embed=cart_embed(self.draft, hint=hint), view=LogisticsCartView(self.bot, self.draft)
        )


class BulkCartView(discord.ui.View):
    """Small sub-menu for pasting a list to add to / remove from the cart."""

    def __init__(self, bot: "StockpileBot", draft: "LogisticsDraft"):
        super().__init__(timeout=300)
        self.bot = bot
        self.draft = draft
        self.remove_btn.disabled = not draft.lines

    @discord.ui.button(label="Paste to Add", style=discord.ButtonStyle.success, emoji="➕", row=0)
    async def add_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(BulkAddCartModal(self.bot, self.draft))

    @discord.ui.button(label="Paste to Remove", style=discord.ButtonStyle.secondary, emoji="➖", row=0)
    async def remove_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(BulkRemoveCartModal(self.bot, self.draft))

    @discord.ui.button(label="Cart", style=discord.ButtonStyle.secondary, emoji="🛒", row=1)
    async def to_cart(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.edit_message(
            embed=cart_embed(self.draft), view=LogisticsCartView(self.bot, self.draft)
        )


# ── Browse the catalog (Category → Subcategory → Item) into the cart ────────────────


def _browse_embed(instruction: str, breadcrumb: str | None = None) -> discord.Embed:
    embed = discord.Embed(title="📂 Browse Catalog", description=instruction, color=Color.AMBER)
    if breadcrumb:
        embed.add_field(name="Selection", value=breadcrumb, inline=False)
    embed.set_footer(text="Foxhole Buddy | Pick an item to add it to your cart")
    return embed


class CategorySelect(discord.ui.Select):
    def __init__(self, bot: "StockpileBot", draft: LogisticsDraft):
        self.bot = bot
        self.draft = draft
        options = [
            discord.SelectOption(label=label[:100], value=key)
            for key, label in bot.catalog.categories(draft.faction)[:25]
        ]
        super().__init__(placeholder="Select a category…", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction) -> None:
        cat_key = self.values[0]
        cat_label = self.bot.catalog.category_label(cat_key)
        await interaction.response.edit_message(
            embed=_browse_embed("Choose a **subcategory**.", breadcrumb=cat_label),
            view=SubcategorySelectView(self.bot, self.draft, cat_key),
        )


class CategorySelectView(discord.ui.View):
    def __init__(self, bot: "StockpileBot", draft: LogisticsDraft):
        super().__init__(timeout=300)
        self.bot = bot
        self.draft = draft
        self.add_item(CategorySelect(bot, draft))

    @discord.ui.button(label="Cart", style=discord.ButtonStyle.secondary, emoji="🛒", row=1)
    async def to_cart(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.edit_message(
            embed=cart_embed(self.draft), view=LogisticsCartView(self.bot, self.draft)
        )


class SubcategorySelect(discord.ui.Select):
    def __init__(self, bot: "StockpileBot", draft: LogisticsDraft, category_key: str):
        self.bot = bot
        self.draft = draft
        self.category_key = category_key
        options = [
            discord.SelectOption(label=label[:100], value=key)
            for key, label in bot.catalog.subcategories(category_key, draft.faction)[:25]
        ]
        super().__init__(placeholder="Select a subcategory…", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction) -> None:
        sub_key = self.values[0]
        cat_label = self.bot.catalog.category_label(self.category_key)
        sub_label = self.bot.catalog.subcategory_label(self.category_key, sub_key)
        await interaction.response.edit_message(
            embed=_browse_embed("Choose the **item** to add.", breadcrumb=f"{cat_label} › {sub_label}"),
            view=ItemSelectView(self.bot, self.draft, self.category_key, sub_key),
        )


class SubcategorySelectView(discord.ui.View):
    def __init__(self, bot: "StockpileBot", draft: LogisticsDraft, category_key: str):
        super().__init__(timeout=300)
        self.bot = bot
        self.draft = draft
        self.add_item(SubcategorySelect(bot, draft, category_key))

    @discord.ui.button(label="Back", style=discord.ButtonStyle.secondary, emoji="◀️", row=1)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.edit_message(
            embed=_browse_embed("Choose a **category**."),
            view=CategorySelectView(self.bot, self.draft),
        )


class ItemSelect(discord.ui.Select):
    def __init__(self, bot: "StockpileBot", draft: LogisticsDraft, category_key: str, subcategory_key: str):
        self.bot = bot
        self.draft = draft
        self.category_key = category_key
        self.subcategory_key = subcategory_key
        options = []
        for item in bot.catalog.items(category_key, subcategory_key, draft.faction)[:25]:
            desc = f"{item.crate_amount} per crate" if item.crate_amount else None
            options.append(discord.SelectOption(label=item.name[:100], value=item.name[:100], description=desc))
        super().__init__(placeholder="Select an item…", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction) -> None:
        item_name = self.values[0]
        cat_label = self.bot.catalog.category_label(self.category_key)
        sub_label = self.bot.catalog.subcategory_label(self.category_key, self.subcategory_key)
        await interaction.response.send_modal(
            LineQuantityModal(self.bot, self.draft, cat_label, sub_label, item_name)
        )


class ItemSelectView(discord.ui.View):
    def __init__(self, bot: "StockpileBot", draft: LogisticsDraft, category_key: str, subcategory_key: str):
        super().__init__(timeout=300)
        self.bot = bot
        self.draft = draft
        self.category_key = category_key
        self.add_item(ItemSelect(bot, draft, category_key, subcategory_key))

    @discord.ui.button(label="Back", style=discord.ButtonStyle.secondary, emoji="◀️", row=1)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        cat_label = self.bot.catalog.category_label(self.category_key)
        await interaction.response.edit_message(
            embed=_browse_embed("Choose a **subcategory**.", breadcrumb=cat_label),
            view=SubcategorySelectView(self.bot, self.draft, self.category_key),
        )


# ── Persistent logistics request card ─────────────────────────────────────────────


def _line_label(line: dict) -> str:
    return f"{line['item']} ×{line['quantity']:,}"[:100]


class LogisticsRequestCardView(discord.ui.View):
    """Persistent card: claim/validate the whole list or individual line items."""

    def __init__(self, bot: "StockpileBot", request_id: str):
        super().__init__(timeout=None)
        self.bot = bot
        self.request_id = request_id

        request = bot.store.get_logistics_request(request_id)
        lines = request.line_items() if request else []
        open_lines = [ln for ln in lines if ln["status"] == LOGI_OPEN]
        claimed_lines = [ln for ln in lines if ln["status"] == LOGI_CLAIMED]

        claim_all = discord.ui.Button(
            label="Claim All", style=discord.ButtonStyle.success, emoji="🙋",
            custom_id=f"logi_claimall:{request_id}", disabled=not open_lines, row=0,
        )
        claim_all.callback = self.claim_all_cb
        validate_all = discord.ui.Button(
            label="Validate All", style=discord.ButtonStyle.primary, emoji="✅",
            custom_id=f"logi_validateall:{request_id}", disabled=not claimed_lines, row=0,
        )
        validate_all.callback = self.validate_all_cb
        revoke = discord.ui.Button(
            label="Revoke", style=discord.ButtonStyle.secondary, emoji="↩️",
            custom_id=f"logi_revoke:{request_id}", disabled=not claimed_lines, row=0,
        )
        revoke.callback = self.revoke_cb
        for btn in (claim_all, validate_all, revoke):
            self.add_item(btn)

        # Per-line pickers open an ephemeral, paginated list — a request can hold
        # more than the 25 items a Discord dropdown can show, so we can't inline
        # every item as a select option on the card itself.
        claim_pick = discord.ui.Button(
            label="Claim item", style=discord.ButtonStyle.secondary, emoji="🙋",
            custom_id=f"logi_claimpick:{request_id}", disabled=not open_lines, row=1,
        )
        claim_pick.callback = self.claim_pick_cb
        validate_pick = discord.ui.Button(
            label="Validate item", style=discord.ButtonStyle.secondary, emoji="✅",
            custom_id=f"logi_validatepick:{request_id}", disabled=not claimed_lines, row=1,
        )
        validate_pick.callback = self.validate_pick_cb
        for btn in (claim_pick, validate_pick):
            self.add_item(btn)

    # -- helpers --
    def _is_manager(self, interaction: discord.Interaction) -> bool:
        perms = getattr(interaction.user, "guild_permissions", None)
        return bool(perms and perms.manage_guild)

    async def _load(self, interaction: discord.Interaction):
        request = self.bot.store.get_logistics_request(self.request_id, interaction.guild_id)
        if request is None:
            await interaction.response.send_message("That request no longer exists.", ephemeral=True)
        return request

    async def _render(self, interaction: discord.Interaction, request) -> None:
        await interaction.response.edit_message(
            embed=logistics_request_embed(request),
            view=LogisticsRequestCardView(self.bot, request.id),
        )
        # Keep a linked op's card in sync.
        if request.op_id:
            op = self.bot.store.get_operation(request.op_id)
            if op:
                await self.bot.update_operation_message(op)

    async def _render_or_clear(self, interaction: discord.Interaction, request) -> None:
        """A fully-delivered request is done: delete its card + row (and refresh
        any linked op). Otherwise re-render the card in place."""
        if request.status != LOGI_DELIVERED:
            await self._render(interaction, request)
            return
        op_id = request.op_id
        # Respond first (the clicked card is the message we're about to delete).
        await interaction.response.send_message("✅ Delivered — request cleared.", ephemeral=True)
        await self.bot.delete_card_message(request.channel_id, request.message_id)
        self.bot.store.delete_logistics_request(request.id)
        if op_id:
            op = self.bot.store.get_operation(op_id)
            if op:
                await self.bot.update_operation_message(op)

    # -- whole-list actions --
    async def claim_all_cb(self, interaction: discord.Interaction) -> None:
        request = await self._load(interaction)
        if request is None:
            return
        request = self.bot.store.claim_all_logistics(self.request_id, user_id=interaction.user.id)
        await self._render(interaction, request)

    async def validate_all_cb(self, interaction: discord.Interaction) -> None:
        request = await self._load(interaction)
        if request is None:
            return
        request = self.bot.store.validate_all_logistics(
            self.request_id, user_id=interaction.user.id, is_manager=self._is_manager(interaction)
        )
        await self._render_or_clear(interaction, request)

    async def revoke_cb(self, interaction: discord.Interaction) -> None:
        request = await self._load(interaction)
        if request is None:
            return
        request = self.bot.store.revoke_logistics(
            self.request_id, user_id=interaction.user.id, is_manager=self._is_manager(interaction)
        )
        await self._render(interaction, request)

    # -- per-line actions (open an ephemeral paginated picker) --
    async def claim_pick_cb(self, interaction: discord.Interaction) -> None:
        request = await self._load(interaction)
        if request is None:
            return
        view = LinePickerView(self.bot, self.request_id, "claim")
        if view.total == 0:
            await interaction.response.send_message("Nothing left to claim here.", ephemeral=True)
            return
        await interaction.response.send_message(embed=view.embed(), view=view, ephemeral=True)

    async def validate_pick_cb(self, interaction: discord.Interaction) -> None:
        request = await self._load(interaction)
        if request is None:
            return
        view = LinePickerView(self.bot, self.request_id, "validate")
        if view.total == 0:
            await interaction.response.send_message("Nothing is claimed to validate here.", ephemeral=True)
            return
        await interaction.response.send_message(embed=view.embed(), view=view, ephemeral=True)


async def refresh_logistics_card(bot: "StockpileBot", request) -> None:
    """Edit the request's posted card in place (used from the ephemeral picker,
    which isn't attached to that message). Tolerates a gone message / lost perms
    and keeps a linked op card in sync."""
    if request is None or not request.message_id:
        return
    try:
        channel = bot.get_channel(request.channel_id) or await bot.fetch_channel(request.channel_id)
        message = await channel.fetch_message(request.message_id)
        await message.edit(
            embed=logistics_request_embed(request),
            view=LogisticsRequestCardView(bot, request.id),
        )
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        pass
    if request.op_id:
        op = bot.store.get_operation(request.op_id)
        if op:
            await bot.update_operation_message(op)


class LinePickerView(discord.ui.View):
    """Ephemeral, paginated picker to claim/validate one line at a time — the way
    around Discord's 25-option dropdown cap for requests with many items."""

    PER_PAGE = 25

    def __init__(self, bot: "StockpileBot", request_id: str, mode: str, page: int = 0):
        super().__init__(timeout=180)
        self.bot = bot
        self.request_id = request_id
        self.mode = mode  # "claim" | "validate"

        lines = self._eligible()
        self.total = len(lines)
        self.pages = max(1, (self.total + self.PER_PAGE - 1) // self.PER_PAGE)
        self.page = max(0, min(page, self.pages - 1))

        start = self.page * self.PER_PAGE
        page_lines = lines[start : start + self.PER_PAGE]
        if page_lines:
            placeholder = "Pick an item to claim…" if mode == "claim" else "Pick an item to validate…"
            select = discord.ui.Select(
                placeholder=placeholder, row=0,
                options=[discord.SelectOption(label=_line_label(ln), value=ln["lid"]) for ln in page_lines],
            )
            select.callback = self.on_pick
            self.add_item(select)

        if self.pages > 1:
            prev = discord.ui.Button(label="Prev", emoji="◀️", style=discord.ButtonStyle.secondary,
                                     row=1, disabled=self.page == 0)
            prev.callback = self.prev_page
            nxt = discord.ui.Button(label="Next", emoji="▶️", style=discord.ButtonStyle.secondary,
                                    row=1, disabled=self.page >= self.pages - 1)
            nxt.callback = self.next_page
            self.add_item(prev)
            self.add_item(nxt)

    def _eligible(self) -> list[dict]:
        request = self.bot.store.get_logistics_request(self.request_id)
        lines = request.line_items() if request else []
        want = LOGI_OPEN if self.mode == "claim" else LOGI_CLAIMED
        return [ln for ln in lines if ln["status"] == want]

    def embed(self) -> discord.Embed:
        title = "🙋 Claim an item" if self.mode == "claim" else "✅ Validate an item"
        return discord.Embed(
            title=title,
            description=f"{self.total} item(s) available · page **{self.page + 1}/{self.pages}**",
            color=Color.BRAND,
        )

    def _is_manager(self, interaction: discord.Interaction) -> bool:
        perms = getattr(interaction.user, "guild_permissions", None)
        return bool(perms and perms.manage_guild)

    async def _reopen(self, interaction: discord.Interaction, page: int) -> None:
        """Re-render this ephemeral picker at ``page`` (clamped), or close it when
        nothing is left to act on."""
        view = LinePickerView(self.bot, self.request_id, self.mode, page)
        if view.total == 0:
            done = "Nothing left to claim." if self.mode == "claim" else "All caught up — nothing left to validate."
            await interaction.response.edit_message(
                embed=discord.Embed(title="✔️ Done", description=done, color=Color.GRAY), view=None
            )
            return
        await interaction.response.edit_message(embed=view.embed(), view=view)

    async def prev_page(self, interaction: discord.Interaction) -> None:
        await self._reopen(interaction, self.page - 1)

    async def next_page(self, interaction: discord.Interaction) -> None:
        await self._reopen(interaction, self.page + 1)

    async def on_pick(self, interaction: discord.Interaction) -> None:
        lid = interaction.data["values"][0]
        request = self.bot.store.get_logistics_request(self.request_id, interaction.guild_id)
        if request is None:
            await interaction.response.edit_message(
                embed=discord.Embed(title="Gone", description="That request no longer exists.", color=Color.GRAY),
                view=None,
            )
            return

        if self.mode == "claim":
            request = self.bot.store.claim_logistics_line(self.request_id, lid, user_id=interaction.user.id)
        else:
            line = request.find_line(lid)
            if line is None or line["status"] != LOGI_CLAIMED:
                await interaction.response.send_message("That item can't be validated.", ephemeral=True)
                return
            if not (self._is_manager(interaction) or line["claimed_by_user_id"] == interaction.user.id):
                await interaction.response.send_message(
                    "Only the driver who claimed this item or a server manager can validate it.",
                    ephemeral=True,
                )
                return
            request = self.bot.store.validate_logistics_line(self.request_id, lid)

        # A fully-delivered request is done: clear its card + row.
        if request.status == LOGI_DELIVERED:
            op_id = request.op_id
            await interaction.response.edit_message(
                embed=discord.Embed(title="✅ Delivered", description="Request complete — card cleared.", color=Color.BRAND),
                view=None,
            )
            await self.bot.delete_card_message(request.channel_id, request.message_id)
            self.bot.store.delete_logistics_request(request.id)
            if op_id:
                op = self.bot.store.get_operation(op_id)
                if op:
                    await self.bot.update_operation_message(op)
            return

        # Update the public card, then refresh this picker with what's left.
        await refresh_logistics_card(self.bot, request)
        await self._reopen(interaction, self.page)
