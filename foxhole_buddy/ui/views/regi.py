"""Regi Net — the persistent 'Net Control' panel and quick-compose modal.

The panel lives in a server's regi-chat channel. Its buttons carry static
custom_ids and derive everything from the click's guild, so a single persistent
view instance (registered in ``setup_hook``) serves every server's panel and
survives restarts.
"""

from typing import TYPE_CHECKING

import discord

from foxhole_buddy.ui.embeds import ally_net_panel_embed, regi_net_panel_embed

if TYPE_CHECKING:
    from foxhole_buddy.core.bot import StockpileBot


async def _guard(bot: "StockpileBot", interaction: discord.Interaction) -> int | None:
    """Return the guild's relay channel id if it's joined, else explain & None."""
    if interaction.guild_id is None:
        await interaction.response.send_message("Regi Net only works in a server.", ephemeral=True)
        return None
    relay = bot.store.get_relay_channel(interaction.guild_id)
    if relay is None:
        await interaction.response.send_message(
            "📡 This server hasn't joined Regi Net. An admin can join via "
            "`/foxhole_buddy setup → 💬 Setup Chats`.",
            ephemeral=True,
        )
        return None
    return relay


async def _ally_guard(bot: "StockpileBot", interaction: discord.Interaction) -> str | None:
    """Return the ally room bound to this channel, else explain & None."""
    if interaction.guild_id is None:
        await interaction.response.send_message("Ally chat only works in a server.", ephemeral=True)
        return None
    room = bot.store.ally_room_by_channel(interaction.guild_id, interaction.channel_id)
    if room is None:
        await interaction.response.send_message(
            "🛡️ This channel isn't an ally chat. An admin can set one up via "
            "`/foxhole_buddy setup → 💬 Setup Chats → 🛡️ Ally Chats`.",
            ephemeral=True,
        )
        return None
    return room


class TransmitModal(discord.ui.Modal, title="📡 Regi Net — Transmit"):
    message = discord.ui.TextInput(
        label="Message",
        style=discord.TextStyle.paragraph,
        placeholder="Broadcast to every linked regiment…",
        max_length=2000,
        required=True,
    )

    def __init__(self, bot: "StockpileBot"):
        super().__init__()
        self.bot = bot

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        faction = self.bot.store.get_guild_faction(interaction.guild_id)
        count = await self.bot.broadcast_regi(
            author_name=interaction.user.display_name,
            regiment=interaction.guild.name,
            faction=faction,
            avatar_url=interaction.user.display_avatar.url,
            content=str(self.message),
        )
        await interaction.followup.send(
            f"📡 Transmitted to **{count}** regiment(s) on the net.", ephemeral=True
        )


class RegiNetPanelView(discord.ui.View):
    """Persistent panel: ✍️ Transmit (modal) + 🔄 Refresh (recount)."""

    def __init__(self, bot: "StockpileBot"):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(
        label="Transmit", emoji="✍️", style=discord.ButtonStyle.primary,
        custom_id="reginet:transmit",
    )
    async def transmit(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if await _guard(self.bot, interaction) is None:
            return
        await interaction.response.send_modal(TransmitModal(self.bot))

    @discord.ui.button(label="Refresh", emoji="🔄", custom_id="reginet:refresh")
    async def refresh(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        faction = self.bot.store.get_guild_faction(interaction.guild_id) if interaction.guild_id else None
        linked = len(self.bot.store.relay_channels())
        await interaction.response.edit_message(
            embed=regi_net_panel_embed(faction, linked), view=self
        )


class AllyTransmitModal(discord.ui.Modal, title="🛡️ Ally Net — Transmit"):
    message = discord.ui.TextInput(
        label="Message",
        style=discord.TextStyle.paragraph,
        placeholder="Send to your allied servers in this room…",
        max_length=2000,
        required=True,
    )

    def __init__(self, bot: "StockpileBot", room_code: str):
        super().__init__()
        self.bot = bot
        self.room_code = room_code

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        count = await self.bot.broadcast_ally(
            room_code=self.room_code,
            author_name=interaction.user.display_name,
            regiment=interaction.guild.name,
            faction=self.bot.store.get_guild_faction(interaction.guild_id),
            avatar_url=interaction.user.display_avatar.url,
            content=str(self.message),
        )
        await interaction.followup.send(
            f"🛡️ Sent to **{count}** allied server(s).", ephemeral=True
        )


class AllyNetPanelView(discord.ui.View):
    """Persistent ally-room panel: ✍️ Transmit (modal) + 🔄 Refresh (recount).

    The room is derived from the channel the panel lives in, so one view serves
    every ally channel across all servers.
    """

    def __init__(self, bot: "StockpileBot"):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(
        label="Transmit", emoji="✍️", style=discord.ButtonStyle.primary,
        custom_id="allynet:transmit",
    )
    async def transmit(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        room = await _ally_guard(self.bot, interaction)
        if room is None:
            return
        await interaction.response.send_modal(AllyTransmitModal(self.bot, room))

    @discord.ui.button(label="Refresh", emoji="🔄", custom_id="allynet:refresh")
    async def refresh(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        room = self.bot.store.ally_room_by_channel(interaction.guild_id, interaction.channel_id) \
            if interaction.guild_id else None
        if room is None:
            await interaction.response.send_message(
                "🛡️ This channel isn't an ally chat anymore.", ephemeral=True
            )
            return
        members = len(self.bot.store.ally_members(room))
        await interaction.response.edit_message(
            embed=ally_net_panel_embed(room, members), view=self
        )
