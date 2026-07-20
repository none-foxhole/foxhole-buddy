"""War room views: operations entry point plus live war status and reports."""

from typing import TYPE_CHECKING

import discord

from foxhole_buddy.ui.embeds import operations_menu_embed, war_report_embed, war_status_embed
from foxhole_buddy.ui.views.operations import OperationsActionsView
from foxhole_buddy.utils import foxhole_api

if TYPE_CHECKING:
    from foxhole_buddy.core.bot import StockpileBot


class WarRoomView(discord.ui.View):
    """Menu grouping Operations and live war data (status + reports)."""

    def __init__(self, bot: "StockpileBot"):
        super().__init__(timeout=180)
        self.bot = bot

    @discord.ui.button(label="Operations", style=discord.ButtonStyle.primary, emoji="⚔️", row=0)
    async def operations_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.edit_message(
            embed=operations_menu_embed(), view=OperationsActionsView(self.bot)
        )

    @discord.ui.button(label="War Status", style=discord.ButtonStyle.secondary, emoji="🌐", row=0)
    async def status_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.defer()
        data = await foxhole_api.fetch_war_status()
        if data is None:
            await interaction.followup.send(
                "Foxhole's war API is unavailable right now. Try again later.", ephemeral=True
            )
            return
        await interaction.followup.send(embed=war_status_embed(data), ephemeral=True)

    @discord.ui.button(label="War Report", style=discord.ButtonStyle.secondary, emoji="💀", row=0)
    async def report_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        maps = self.bot.war_maps or await foxhole_api.fetch_maps()
        if maps:
            self.bot.war_maps = maps
        else:
            await interaction.response.send_message(
                "Map list unavailable right now. Try again shortly.", ephemeral=True
            )
            return
        await interaction.response.send_message(
            "Pick a map/hex:", view=WarReportSelectView(self.bot), ephemeral=True
        )


class WarReportSelect(discord.ui.Select):
    def __init__(self, maps: list[str]):
        options = [
            discord.SelectOption(label=foxhole_api.prettify_map(m)[:100], value=m[:100])
            for m in maps
        ]
        super().__init__(placeholder="Select a map/hex…", options=options)

    async def callback(self, interaction: discord.Interaction) -> None:
        hex_name = self.values[0]
        await interaction.response.defer()
        data = await foxhole_api.fetch_war_report(hex_name)
        if data is None:
            await interaction.edit_original_response(content="Couldn't fetch that hex.", view=None)
            return
        await interaction.edit_original_response(
            content=None, embed=war_report_embed(foxhole_api.prettify_map(hex_name), data), view=None
        )


class WarReportSelectView(discord.ui.View):
    """Paginated map picker (Discord caps a select at 25 options)."""

    PER_PAGE = 25

    def __init__(self, bot: "StockpileBot", page: int = 0):
        super().__init__(timeout=120)
        self.bot = bot
        self.page = page
        maps = bot.war_maps
        chunk = maps[page * self.PER_PAGE : (page + 1) * self.PER_PAGE]
        self.add_item(WarReportSelect(chunk))
        if page > 0:
            self.add_item(self._nav("◀️ Prev", page - 1))
        if (page + 1) * self.PER_PAGE < len(maps):
            self.add_item(self._nav("Next ▶️", page + 1))

    def _nav(self, label: str, target: int) -> discord.ui.Button:
        btn = discord.ui.Button(label=label, style=discord.ButtonStyle.secondary, row=1)

        async def go(interaction: discord.Interaction) -> None:
            await interaction.response.edit_message(view=WarReportSelectView(self.bot, target))

        btn.callback = go
        return btn
