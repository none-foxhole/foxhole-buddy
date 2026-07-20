"""Interactive server-config panel."""

from typing import TYPE_CHECKING

import discord

from foxhole_buddy.ui.embeds import (
    ally_net_panel_embed,
    ally_setup_embed,
    chats_setup_embed,
    regi_net_panel_embed,
    setup_embed,
)

if TYPE_CHECKING:
    from foxhole_buddy.core.bot import StockpileBot


class SetupView(discord.ui.View):
    """Interactive, ephemeral server-config panel with native pickers.

    Every change saves immediately and the panel re-renders to reflect it.
    """

    def __init__(self, bot: "StockpileBot", guild_id: int):
        super().__init__(timeout=300)
        self.bot = bot
        self.guild_id = guild_id
        config = bot.store.get_guild_config(guild_id)
        faction = config.get("faction")
        # Highlight the active faction.
        self.warden_btn.style = (
            discord.ButtonStyle.primary if faction == "warden" else discord.ButtonStyle.secondary
        )
        self.colonial_btn.style = (
            discord.ButtonStyle.success if faction == "colonial" else discord.ButtonStyle.secondary
        )
        # Main channel + faction are required before setup can be finished.
        self.done_btn.disabled = not (config.get("channel_id") and faction)

    async def _refresh(self, interaction: discord.Interaction) -> None:
        config = self.bot.store.get_guild_config(self.guild_id)
        await interaction.response.edit_message(
            embed=setup_embed(config), view=SetupView(self.bot, self.guild_id)
        )

    @discord.ui.select(
        cls=discord.ui.ChannelSelect, channel_types=[discord.ChannelType.text],
        placeholder="📍 Main channel (commands & default alerts)", row=0,
    )
    async def main_channel(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect) -> None:
        self.bot.store.update_guild_config(self.guild_id, channel_id=select.values[0].id)
        await self._refresh(interaction)

    @discord.ui.select(
        cls=discord.ui.ChannelSelect,
        channel_types=[discord.ChannelType.text, discord.ChannelType.forum],
        placeholder="⚔️ Operations channel (text, or forum for 1 thread/op)",
        min_values=0, max_values=1, row=1,
    )
    async def ops_channel(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect) -> None:
        cid = select.values[0].id if select.values else None
        self.bot.store.update_guild_config(self.guild_id, ops_channel_id=cid)
        await self._refresh(interaction)

    @discord.ui.select(
        cls=discord.ui.ChannelSelect,
        channel_types=[discord.ChannelType.text, discord.ChannelType.forum],
        placeholder="📦 Stockpile channel (text, or forum for 1 post/stockpile)",
        min_values=0, max_values=1, row=2,
    )
    async def stockpile_channel(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect) -> None:
        cid = select.values[0].id if select.values else None
        self.bot.store.update_guild_config(self.guild_id, stockpile_channel_id=cid)
        await self._refresh(interaction)

    @discord.ui.button(label="Setup Chats", emoji="💬", row=3)
    async def chats_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        relay_id = self.bot.store.get_relay_channel(self.guild_id)
        rooms = self.bot.store.ally_rooms_for_guild(self.guild_id)
        await interaction.response.edit_message(
            embed=chats_setup_embed(relay_id, len(rooms)),
            view=ChatsSetupView(self.bot, self.guild_id),
        )

    @discord.ui.button(label="Warden", emoji="🔵", row=3)
    async def warden_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.bot.store.update_guild_config(self.guild_id, faction="warden")
        await self._refresh(interaction)

    @discord.ui.button(label="Colonial", emoji="🟢", row=3)
    async def colonial_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.bot.store.update_guild_config(self.guild_id, faction="colonial")
        await self._refresh(interaction)

    @discord.ui.button(label="Done", emoji="✅", style=discord.ButtonStyle.success, row=3)
    async def done_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        config = self.bot.store.get_guild_config(self.guild_id)
        embed = setup_embed(config)
        embed.title = "✅ Foxhole Buddy — Setup Saved"
        embed.set_footer(text="Foxhole Buddy | Run /foxhole_buddy setup again anytime to change this")
        await interaction.response.edit_message(embed=embed, view=None)


class ChatsSetupView(discord.ui.View):
    """Hub for cross-server chat config: global (Regi Net) + ally rooms."""

    def __init__(self, bot: "StockpileBot", guild_id: int):
        super().__init__(timeout=300)
        self.bot = bot
        self.guild_id = guild_id

    async def _refresh(self, interaction: discord.Interaction) -> None:
        relay_id = self.bot.store.get_relay_channel(self.guild_id)
        rooms = self.bot.store.ally_rooms_for_guild(self.guild_id)
        await interaction.response.edit_message(
            embed=chats_setup_embed(relay_id, len(rooms)),
            view=ChatsSetupView(self.bot, self.guild_id),
        )

    @discord.ui.select(
        cls=discord.ui.ChannelSelect, channel_types=[discord.ChannelType.text],
        placeholder="🌐 Global (Regi Net) channel (optional)", min_values=0, max_values=1, row=0,
    )
    async def relay_channel(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect) -> None:
        old = self.bot.store.get_relay_channel(self.guild_id)
        cid = select.values[0].id if select.values else None
        self.bot.store.update_guild_config(self.guild_id, relay_channel_id=cid)
        await self._refresh(interaction)
        if cid is not None and cid != old:
            faction = self.bot.store.get_guild_faction(self.guild_id)
            linked = len(self.bot.store.relay_channels())
            from foxhole_buddy.ui.views.regi import RegiNetPanelView
            try:
                channel = self.bot.get_channel(cid) or await self.bot.fetch_channel(cid)
                await channel.send(
                    embed=regi_net_panel_embed(faction, linked), view=RegiNetPanelView(self.bot)
                )
            except Exception:
                await interaction.followup.send(
                    f"Joined Regi Net, but I couldn't post the Net Control panel in <#{cid}> "
                    "— check my **View Channel / Send Messages** there.",
                    ephemeral=True,
                )

    @discord.ui.button(label="Ally Chats", emoji="🛡️", row=1)
    async def ally_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        rooms = self.bot.store.ally_rooms_for_guild(self.guild_id)
        await interaction.response.edit_message(
            embed=ally_setup_embed(rooms), view=AllyChatsView(self.bot, self.guild_id)
        )

    @discord.ui.button(label="Back", emoji="◀️", style=discord.ButtonStyle.secondary, row=1)
    async def back_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        config = self.bot.store.get_guild_config(self.guild_id)
        await interaction.response.edit_message(
            embed=setup_embed(config), view=SetupView(self.bot, self.guild_id)
        )


class AllyChatsView(discord.ui.View):
    """Manage this guild's ally rooms: pick a channel, then create or join.

    ``self.channel_id`` holds the channel picked via the select, kept across the
    follow-up button click (the select callback defers without re-rendering).
    """

    def __init__(self, bot: "StockpileBot", guild_id: int):
        super().__init__(timeout=300)
        self.bot = bot
        self.guild_id = guild_id
        self.channel_id: int | None = None
        # A Leave select is only valid when there is ≥1 room to list.
        rooms = bot.store.ally_rooms_for_guild(guild_id)
        if rooms:
            self.add_item(_LeaveRoomSelect(rooms))

    async def _refresh(self, interaction: discord.Interaction) -> None:
        rooms = self.bot.store.ally_rooms_for_guild(self.guild_id)
        await interaction.response.edit_message(
            embed=ally_setup_embed(rooms), view=AllyChatsView(self.bot, self.guild_id)
        )

    async def _post_ally_panel(self, channel_id: int, room_code: str) -> None:
        members = len(self.bot.store.ally_members(room_code))
        try:
            channel = self.bot.get_channel(channel_id) or await self.bot.fetch_channel(channel_id)
            await channel.send(
                embed=ally_net_panel_embed(room_code, members), view=AllyNetPanelView(self.bot)
            )
        except Exception:
            pass

    @discord.ui.select(
        cls=discord.ui.ChannelSelect, channel_types=[discord.ChannelType.text],
        placeholder="🛡️ Ally channel (pick first, then Create/Join)", min_values=1, max_values=1,
        row=0,
    )
    async def ally_channel(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect) -> None:
        self.channel_id = select.values[0].id
        # Keep the picked channel without re-rendering (so Create/Join can use it).
        await interaction.response.defer()

    @discord.ui.button(label="Create room", emoji="➕", style=discord.ButtonStyle.success, row=1)
    async def create_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if self.channel_id is None:
            await interaction.response.send_message(
                "Pick an ally channel above first.", ephemeral=True
            )
            return
        if self.bot.store.ally_room_by_channel(self.guild_id, self.channel_id):
            await interaction.response.send_message(
                f"<#{self.channel_id}> is already an ally channel. Pick a different one.",
                ephemeral=True,
            )
            return
        channel_id = self.channel_id
        code = self.bot.store.create_ally_room(self.guild_id, channel_id)
        # Respond to the interaction FIRST (the edit is the initial response and must
        # land within Discord's ~3s window); the panel post is slower network I/O.
        await self._refresh(interaction)
        await self._post_ally_panel(channel_id, code)
        await interaction.followup.send(
            f"🛡️ Ally room created: **`{code}`**\nShare this code with allied admins so they "
            f"can **Join with code** from their own server. Talk in <#{channel_id}> with "
            "`/a` or the panel.",
            ephemeral=True,
        )

    @discord.ui.button(label="Join with code", emoji="🔗", style=discord.ButtonStyle.primary, row=1)
    async def join_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if self.channel_id is None:
            await interaction.response.send_message(
                "Pick an ally channel above first.", ephemeral=True
            )
            return
        await interaction.response.send_modal(
            AllyJoinModal(self.bot, self.guild_id, self.channel_id)
        )


class _LeaveRoomSelect(discord.ui.Select):
    def __init__(self, rooms: list[dict]):
        options = [
            discord.SelectOption(label=r["room_code"], description=f"in #{r['channel_id']}", value=r["room_code"])
            for r in rooms[:25]
        ]
        super().__init__(placeholder="🚪 Leave a room…", min_values=0, max_values=1,
                         options=options, row=2)

    async def callback(self, interaction: discord.Interaction) -> None:
        view: "AllyChatsView" = self.view  # type: ignore[assignment]
        if not self.values:
            await interaction.response.defer()
            return
        view.bot.store.leave_ally_room(view.guild_id, self.values[0])
        await view._refresh(interaction)


class AllyJoinModal(discord.ui.Modal, title="🛡️ Join an Ally Room"):
    code = discord.ui.TextInput(
        label="Room code",
        placeholder="e.g. ALLY-7F3K2P",
        max_length=20,
        required=True,
    )

    def __init__(self, bot: "StockpileBot", guild_id: int, channel_id: int):
        super().__init__()
        self.bot = bot
        self.guild_id = guild_id
        self.channel_id = channel_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        status = self.bot.store.join_ally_room(self.guild_id, self.channel_id, str(self.code))
        if status == "ok":
            room = str(self.code).strip().upper()
            # Respond to the interaction FIRST (initial response within ~3s), then do
            # the slower panel post as a follow-up.
            rooms = self.bot.store.ally_rooms_for_guild(self.guild_id)
            await interaction.response.edit_message(
                embed=ally_setup_embed(rooms), view=AllyChatsView(self.bot, self.guild_id)
            )
            members = len(self.bot.store.ally_members(room))
            try:
                channel = self.bot.get_channel(self.channel_id) or await self.bot.fetch_channel(self.channel_id)
                await channel.send(
                    embed=ally_net_panel_embed(room, members), view=AllyNetPanelView(self.bot)
                )
            except Exception:
                pass
            await interaction.followup.send(
                f"🛡️ Joined **`{room}`** in <#{self.channel_id}>.", ephemeral=True
            )
            return
        messages = {
            "not_found": "No ally room with that code. Double-check it with your ally.",
            "channel_in_use": f"<#{self.channel_id}> is already linked to another ally room.",
            "already_member": "This server has already joined that room.",
        }
        await interaction.response.send_message(
            messages.get(status, "Couldn't join that room."), ephemeral=True
        )


# Imported at the bottom to avoid a circular import at module load
# (regi.py imports embeds, not setup).
from foxhole_buddy.ui.views.regi import AllyNetPanelView  # noqa: E402
