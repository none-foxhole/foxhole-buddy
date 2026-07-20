"""Factory alarm views."""

from typing import TYPE_CHECKING

import discord

from foxhole_buddy.ui.embeds import factory_alarm_embed, main_menu_embed
from foxhole_buddy.ui.modals import AddFactoryAlarmModal

if TYPE_CHECKING:
    from foxhole_buddy.core.bot import StockpileBot


class FactoryMenuView(discord.ui.View):
    """Menu for managing factory alarms."""

    def __init__(self, bot: "StockpileBot"):
        super().__init__(timeout=120)
        self.bot = bot

    @discord.ui.button(label="Add 3-Ping Alarm", style=discord.ButtonStyle.success, emoji="🔔", row=0)
    async def add_3ping_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await self.bot._check_channel(interaction):
            return
        await interaction.response.send_modal(AddFactoryAlarmModal(self.bot, single_ping=False))

    @discord.ui.button(label="Add 1-Ping Alarm", style=discord.ButtonStyle.primary, emoji="⏱️", row=0)
    async def add_1ping_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await self.bot._check_channel(interaction):
            return
        await interaction.response.send_modal(AddFactoryAlarmModal(self.bot, single_ping=True))

    @discord.ui.button(label="List Active", style=discord.ButtonStyle.secondary, emoji="📋", row=1)
    async def list_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await self.bot._check_channel(interaction):
            return

        alarms = self.bot.store.get_factory_alarms(guild_id=interaction.guild_id)
        if not alarms:
            await interaction.response.send_message("No active factory alarms.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        for alarm in alarms:
            await interaction.followup.send(
                embed=factory_alarm_embed(alarm),
                view=FactoryAlarmCardView(self.bot, alarm.id),
                ephemeral=True
            )

    @discord.ui.button(label="Back", style=discord.ButtonStyle.secondary, emoji="◀️", row=1)
    async def back_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        from foxhole_buddy.ui.views.menu import MainMenuView

        await interaction.response.edit_message(
            embed=main_menu_embed(),
            view=MainMenuView(self.bot),
        )


class FactoryAlarmCardView(discord.ui.View):
    def __init__(self, bot: "StockpileBot", alarm_id: str):
        super().__init__(timeout=None)
        self.bot = bot
        self.alarm_id = alarm_id

        button = discord.ui.Button(
            label="Turn Off Queue",
            style=discord.ButtonStyle.danger,
            emoji="⏹️",
            custom_id=f"factory_alarm_off:{alarm_id}"
        )
        button.callback = self.turn_off_callback
        self.add_item(button)

    async def turn_off_callback(self, interaction: discord.Interaction) -> None:
        deleted = self.bot.store.delete_factory_alarm(self.alarm_id, interaction.guild_id)
        if deleted:
            try:
                if interaction.message and interaction.message.flags.ephemeral:
                    await interaction.response.edit_message(content="*Alarm turned off.*", embed=None, view=None)
                else:
                    await interaction.response.defer()
                    if interaction.message:
                        await interaction.message.delete()
            except (discord.NotFound, discord.HTTPException):
                # The interaction response might already be done or message deleted
                pass
        else:
            await interaction.response.send_message("This alarm is already completed or deleted.", ephemeral=True)
