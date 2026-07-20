"""Top-level regiment management menu."""

from typing import TYPE_CHECKING

import discord

from foxhole_buddy.ui.embeds import (
    factory_menu_embed,
    logistics_menu_embed,
    stockpile_actions_embed,
)
from foxhole_buddy.ui.views.factory import FactoryMenuView
from foxhole_buddy.ui.views.logistics import LogisticsActionsView
from foxhole_buddy.ui.views.stockpile import StockpileActionsView

if TYPE_CHECKING:
    from foxhole_buddy.core.bot import StockpileBot


class MainMenuView(discord.ui.View):
    """Top-level regiment management menu."""

    def __init__(self, bot: "StockpileBot"):
        super().__init__(timeout=120)
        self.bot = bot

    @discord.ui.button(label="Stockpile/Inv", style=discord.ButtonStyle.primary, emoji="📦")
    async def stockpile_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        role_id = self.bot.store.get_guild_urgent_role(interaction.guild_id)
        await interaction.response.edit_message(
            embed=stockpile_actions_embed(role_id),
            view=StockpileActionsView(self.bot),
        )

    @discord.ui.button(label="Logistics", style=discord.ButtonStyle.secondary, emoji="🚚")
    async def logistics_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.edit_message(
            embed=logistics_menu_embed(),
            view=LogisticsActionsView(self.bot),
        )

    @discord.ui.button(label="Factories", style=discord.ButtonStyle.danger, emoji="🏭")
    async def factories_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.edit_message(
            embed=factory_menu_embed(),
            view=FactoryMenuView(self.bot),
        )
