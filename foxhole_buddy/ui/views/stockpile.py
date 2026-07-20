"""Stockpile management views."""

from typing import TYPE_CHECKING

import discord

from foxhole_buddy.theme import Color
from foxhole_buddy.ui.embeds import (
    _inventory_table,
    main_menu_embed,
    stockpile_actions_embed,
    stockpile_embed,
)
from foxhole_buddy.ui.modals import (
    AddStockpileItemModal,
    AddStockpileModal,
    BulkAddStockpileModal,
    BulkRemoveStockpileModal,
    DeleteStockpileModal,
    RefreshStockpileModal,
    RemoveStockpileItemModal,
)
from foxhole_buddy.utils.formatting import unix_ts

if TYPE_CHECKING:
    from foxhole_buddy.core.bot import StockpileBot


class StockpileActionsView(discord.ui.View):
    """Stockpile sub-menu: Add / List / Refresh / Delete / Back."""

    def __init__(self, bot: "StockpileBot"):
        super().__init__(timeout=120)
        self.bot = bot

    @discord.ui.button(label="Add", style=discord.ButtonStyle.success, emoji="➕", row=0)
    async def add_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await self.bot._check_channel(interaction):
            return
        await interaction.response.edit_message(
            embed=discord.Embed(
                title="📦 Add Stockpile — Choose Type",
                description="What kind of stockpile is this?",
                color=Color.BRAND,
            ),
            view=StockpileTypeView(self.bot),
        )

    @discord.ui.button(label="List", style=discord.ButtonStyle.primary, emoji="📋", row=0)
    async def list_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await self.bot._check_channel(interaction):
            return
        stockpiles = self.bot.store.all(guild_id=interaction.guild_id)
        if not stockpiles:
            await interaction.response.send_message(
                embed=discord.Embed(
                    title="No Stockpiles",
                    description="Nothing here yet. Use **Add** to create a stockpile or inventory.",
                    color=Color.GRAY,
                ),
                ephemeral=True,
            )
            return
        # Summary with jump links to each card (never re-post — forum stockpiles
        # already have a permanent post, and re-posting would duplicate cards).
        lines = []
        for sp in sorted(stockpiles, key=lambda s: (not s.track_expiry, s.name.lower())):
            jump = (
                f"https://discord.com/channels/{sp.guild_id}/{sp.channel_id}/{sp.message_id}"
                if sp.channel_id and sp.message_id else None
            )
            tag = "⏱️" if sp.track_expiry else "📦"
            title = f"[{sp.name}]({jump})" if jump else sp.name
            item_count = len(self.bot.store.get_stockpile_inventory(sp.id))
            extra = (
                f"expires <t:{unix_ts(sp.expires_datetime)}:R> · " if sp.track_expiry else ""
            )
            lines.append(f"{tag} {title}\n{extra}{item_count} item type(s)")
        embed = discord.Embed(
            title="📦 Stockpiles & Inventories",
            description="\n\n".join(lines)[:4000],
            color=Color.BRAND,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="Refresh", style=discord.ButtonStyle.secondary, emoji="🔄", row=0)
    async def refresh_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await self.bot._check_channel(interaction):
            return
        await interaction.response.send_modal(RefreshStockpileModal(self.bot))

    @discord.ui.button(label="Delete", style=discord.ButtonStyle.danger, emoji="🗑️", row=0)
    async def delete_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await self.bot._check_channel(interaction):
            return
        await interaction.response.send_modal(DeleteStockpileModal(self.bot))

    @discord.ui.select(
        cls=discord.ui.RoleSelect, placeholder="🔔 Urgent role for the 30m ping (optional)",
        min_values=0, max_values=1, row=1,
    )
    async def urgent_role(self, interaction: discord.Interaction, select: discord.ui.RoleSelect) -> None:
        if not await self.bot._check_channel(interaction):
            return
        role_id = select.values[0].id if select.values else None
        self.bot.store.update_guild_config(interaction.guild_id, urgent_role_id=role_id)
        await interaction.response.edit_message(
            embed=stockpile_actions_embed(role_id),
            view=StockpileActionsView(self.bot),
        )

    @discord.ui.button(label="Back", style=discord.ButtonStyle.secondary, emoji="◀️", row=2)
    async def back_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        from foxhole_buddy.ui.views.menu import MainMenuView

        await interaction.response.edit_message(
            embed=main_menu_embed(),
            view=MainMenuView(self.bot),
        )


class StockpileTypeView(discord.ui.View):
    """Shown after clicking Add — picks the kind: a timered Seaport/Storage Depot,
    or an inventory-only container with no refresh timer."""

    def __init__(self, bot: "StockpileBot"):
        super().__init__(timeout=60)
        self.bot = bot

    @discord.ui.button(label="Seaport", style=discord.ButtonStyle.primary, emoji="⚓")
    async def seaport_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(AddStockpileModal(self.bot, "seaport"))

    @discord.ui.button(label="Storage Depot", style=discord.ButtonStyle.primary, emoji="🏭")
    async def depot_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(AddStockpileModal(self.bot, "storage_depot"))

    @discord.ui.button(label="Inventory Only (no timer)", style=discord.ButtonStyle.secondary, emoji="📦")
    async def inventory_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(
            AddStockpileModal(self.bot, "storage_depot", track_expiry=False)
        )

    @discord.ui.button(label="Back", style=discord.ButtonStyle.secondary, emoji="◀️")
    async def back_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        role_id = self.bot.store.get_guild_urgent_role(interaction.guild_id)
        await interaction.response.edit_message(
            embed=stockpile_actions_embed(role_id),
            view=StockpileActionsView(self.bot),
        )


class StockpileView(discord.ui.View):
    def __init__(self, bot: "StockpileBot", stockpile_id: str):
        super().__init__(timeout=None)
        self.bot = bot
        self.stockpile_id = stockpile_id
        # Timer controls only make sense for a timered stockpile; an
        # inventory-only one shows just the item buttons.
        stockpile = bot.store.get(stockpile_id)
        if stockpile is None or stockpile.track_expiry:
            self.add_item(RefreshStockpileButton(stockpile_id))
            self.add_item(DutyRosterButton(stockpile_id))
        # Inventory buttons (row 1) — available on every stockpile.
        self.add_item(StockpileItemButton(stockpile_id, "add", "Add", "➕",
                                          discord.ButtonStyle.success))
        self.add_item(StockpileItemButton(stockpile_id, "remove", "Remove", "➖",
                                          discord.ButtonStyle.danger))
        self.add_item(StockpileItemButton(stockpile_id, "add_list", "Add List", "📝",
                                          discord.ButtonStyle.success))
        self.add_item(StockpileItemButton(stockpile_id, "remove_list", "Remove List", "📝",
                                          discord.ButtonStyle.danger))
        self.add_item(StockpileItemButton(stockpile_id, "list", "List", "📋",
                                          discord.ButtonStyle.secondary))


class StockpileItemButton(discord.ui.Button):
    """Persistent inventory button on the stockpile card. ``action`` selects the
    modal (or the ephemeral inventory list) to open."""

    _MODALS = {
        "add": AddStockpileItemModal,
        "remove": RemoveStockpileItemModal,
        "add_list": BulkAddStockpileModal,
        "remove_list": BulkRemoveStockpileModal,
    }

    def __init__(self, stockpile_id: str, action: str, label: str, emoji: str, style):
        super().__init__(label=label, emoji=emoji, style=style, row=1,
                         custom_id=f"stockpile_item_{action}:{stockpile_id}")
        self.stockpile_id = stockpile_id
        self.action = action

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        bot = view.bot if isinstance(view, StockpileView) else None
        if bot is None:
            await interaction.response.send_message("This stockpile button is not active.", ephemeral=True)
            return
        stockpile = bot.store.get(self.stockpile_id, guild_id=interaction.guild_id)
        if stockpile is None:
            await interaction.response.send_message("That stockpile no longer exists.", ephemeral=True)
            return
        if self.action == "list":
            inventory = bot.store.get_stockpile_inventory(self.stockpile_id)
            embed = discord.Embed(
                title=f"📋 {stockpile.name} — Inventory",
                description=_inventory_table(inventory),
                color=Color.SLATE,
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return
        await interaction.response.send_modal(self._MODALS[self.action](bot, self.stockpile_id))


class RefreshStockpileButton(discord.ui.Button):
    def __init__(self, stockpile_id: str):
        super().__init__(
            label="Mark Refreshed",
            style=discord.ButtonStyle.success,
            custom_id=f"stockpile_refresh:{stockpile_id}",
        )
        self.stockpile_id = stockpile_id

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, StockpileView):
            await interaction.response.send_message("This stockpile button is not active.", ephemeral=True)
            return

        try:
            stockpile = view.bot.store.refresh(
                self.stockpile_id,
                user_id=interaction.user.id,
                guild_id=interaction.guild_id,
            )
        except KeyError:
            await interaction.response.send_message("That stockpile no longer exists.", ephemeral=True)
            return

        inventory = view.bot.store.get_stockpile_inventory(stockpile.id)
        await interaction.response.edit_message(
            embed=stockpile_embed(stockpile, inventory), view=StockpileView(view.bot, stockpile.id)
        )
        await interaction.followup.send(
            f"Updated `{stockpile.name}`. Next public-risk check: <t:{unix_ts(stockpile.expires_datetime)}:R>.",
            ephemeral=True,
        )


def _duty_roster_embed(stockpile) -> discord.Embed:
    """Ephemeral panel showing who gets pinged when this stockpile runs low."""
    if stockpile.duty_user_ids:
        who = ", ".join(f"<@{uid}>" for uid in stockpile.duty_user_ids)
        desc = (
            f"These members get pinged when **{stockpile.name}** runs low "
            f"(instead of the whole channel):\n\n{who}"
        )
    else:
        desc = (
            f"No one is assigned to **{stockpile.name}** yet.\n\n"
            "Use the picker to assign members, or press **Join** to add yourself. "
            "Assigned members are pinged when the stockpile runs low."
        )
    embed = discord.Embed(title="👥 Refresh Duty", description=desc, color=Color.BLUE)
    embed.set_footer(text="Foxhole Buddy | Only assigned members are pinged")
    return embed


class DutyRosterButton(discord.ui.Button):
    """Persistent card button opening the ephemeral duty-roster panel."""

    def __init__(self, stockpile_id: str):
        super().__init__(
            label="Duty Roster",
            style=discord.ButtonStyle.secondary,
            emoji="👥",
            custom_id=f"stockpile_duty:{stockpile_id}",
        )
        self.stockpile_id = stockpile_id

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        bot = view.bot if isinstance(view, StockpileView) else None
        if bot is None:
            await interaction.response.send_message("This stockpile button is not active.", ephemeral=True)
            return
        stockpile = bot.store.get(self.stockpile_id, guild_id=interaction.guild_id)
        if stockpile is None:
            await interaction.response.send_message("That stockpile no longer exists.", ephemeral=True)
            return
        await interaction.response.send_message(
            embed=_duty_roster_embed(stockpile),
            view=DutyRosterView(bot, self.stockpile_id),
            ephemeral=True,
        )


class DutyRosterView(discord.ui.View):
    """Ephemeral panel to set the refresh-duty roster for one stockpile.

    The member picker replaces the whole roster; Join/Leave are quick self
    toggles for members who just want to add or remove themselves.
    """

    def __init__(self, bot: "StockpileBot", stockpile_id: str):
        super().__init__(timeout=180)
        self.bot = bot
        self.stockpile_id = stockpile_id
        self.add_item(_DutyMemberSelect(stockpile_id))

    async def _save_and_render(self, interaction: discord.Interaction, user_ids) -> None:
        try:
            stockpile = self.bot.store.set_stockpile_duty(
                self.stockpile_id, user_ids, guild_id=interaction.guild_id
            )
        except KeyError:
            await interaction.response.edit_message(
                content="That stockpile no longer exists.", embed=None, view=None
            )
            return
        await interaction.response.edit_message(
            embed=_duty_roster_embed(stockpile), view=DutyRosterView(self.bot, self.stockpile_id)
        )
        # Keep the public card's "On Duty" field in sync.
        try:
            await self.bot.update_stockpile_message(stockpile)
        except Exception:  # noqa: BLE001 — card refresh is best-effort
            pass

    @discord.ui.button(label="Join", style=discord.ButtonStyle.success, emoji="✅", row=1)
    async def join_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        stockpile = self.bot.store.get(self.stockpile_id, guild_id=interaction.guild_id)
        current = list(stockpile.duty_user_ids) if stockpile else []
        if interaction.user.id not in current:
            current.append(interaction.user.id)
        await self._save_and_render(interaction, current)

    @discord.ui.button(label="Leave", style=discord.ButtonStyle.secondary, emoji="🚪", row=1)
    async def leave_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        stockpile = self.bot.store.get(self.stockpile_id, guild_id=interaction.guild_id)
        current = [uid for uid in (stockpile.duty_user_ids if stockpile else []) if uid != interaction.user.id]
        await self._save_and_render(interaction, current)

    @discord.ui.button(label="Clear all", style=discord.ButtonStyle.danger, emoji="🗑️", row=1)
    async def clear_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._save_and_render(interaction, [])


class _DutyMemberSelect(discord.ui.UserSelect):
    def __init__(self, stockpile_id: str):
        super().__init__(placeholder="📋 Set assigned members…", min_values=0, max_values=25, row=0)
        self.stockpile_id = stockpile_id

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, DutyRosterView):
            await interaction.response.send_message("This picker is not active.", ephemeral=True)
            return
        await view._save_and_render(interaction, [u.id for u in self.values])
