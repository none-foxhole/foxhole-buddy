import discord
from discord import app_commands
from foxhole_buddy.ui.embeds import main_menu_embed, setup_embed, war_room_embed, wiki_entry_embed
from foxhole_buddy.ui.views import MainMenuView, SetupView, WarRoomView
from foxhole_buddy.wikidex import wiki_image_url, wiki_page_url


def register_commands(bot) -> None:
    stockpile_group = app_commands.Group(name="foxhole_buddy", description="Foxhole regiment assistant.")

    @stockpile_group.command(name="setup", description="Open the interactive server setup panel (admin).")
    @app_commands.default_permissions(manage_guild=True)
    async def stockpile_setup(interaction: discord.Interaction) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return
        # Pre-fill the main channel with the current one as a sensible default if unset.
        config = bot.store.get_guild_config(interaction.guild_id)
        if config.get("channel_id") is None:
            bot.store.update_guild_config(interaction.guild_id, channel_id=interaction.channel_id)
            config = bot.store.get_guild_config(interaction.guild_id)
        await interaction.response.send_message(
            embed=setup_embed(config),
            view=SetupView(bot, interaction.guild_id),
            ephemeral=True,
        )

    @stockpile_group.command(name="help", description="Show information about the Foxhole Buddy bot.")
    async def stockpile_help(interaction: discord.Interaction) -> None:
        if not await bot._check_channel(interaction):
            return
        embed = discord.Embed(
            title="Foxhole Buddy Help",
            description="Your regiment's logistics assistant for Foxhole.",
            color=0x2D7D46,
        )
        embed.add_field(
            name="Getting Started",
            value=(
                "1. An admin runs `/foxhole_buddy setup` to open the setup panel — "
                "pick the main channel, faction, urgent role, and alert channels.\n"
                "2. Use `/foxhole_buddy manage` for logistics, or `/foxhole_buddy war_room` for ops & war data.\n"
                "3. From **manage**: Stockpile, Logistics, Inventory, Factories. "
                "From **war**: Operations, War Status, War Report."
            ),
            inline=False,
        )
        embed.add_field(
            name="Commands",
            value=(
                "`/foxhole_buddy setup` — (admin) configure the server\n"
                "`/foxhole_buddy manage` — logistics & base management menu\n"
                "`/foxhole_buddy war_room` — operations & live war data menu\n"
                "`/foxhole_buddy help` — show this info panel"
            ),
            inline=False,
        )
        embed.add_field(
            name="Features",
            value=(
                "📦 **Stockpile/Inv** — Reserve timers (48h expiry, alerts at 12h/6h/1h/30m) or "
                "timer-less inventories; each holds an item list, and reminders show the contents\n"
                "🚚 **Logistics** — Multi-item supply requests (search or browse); claim the whole list or per item\n"
                "🏭 **Factories** — Set 1-ping or 3-ping queue alarms (5m intervals)\n"
                "⚔️ **Operations** — Schedule ops, RSVP, optional squads with leads\n"
                "🤝 **Allied Ops** — One op shared live across an ally room; every server RSVPs together\n"
                "📡 **Regi Net** — `/g` broadcasts to every linked regiment (opt-in per server)\n"
                "🛡️ **Ally Chat** — `/a` private rooms with allied servers (join by invite code)\n"
                "🔎 **Wiki Search** — `/s <name>` looks up any item, vehicle, or structure"
            ),
            inline=False,
        )
        embed.add_field(
            name="Feedback & Support",
            value=(
                "Got feedback or found a bug? Post it in the **issues** channel of our "
                "Discord server: https://discord.gg/5u7bmdzT2"
            ),
            inline=False,
        )
        embed.set_footer(text="Foxhole Buddy | Keep the depot private")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @stockpile_group.command(name="manage", description="Open the regiment management menu.")
    async def regiment_manage(interaction: discord.Interaction) -> None:
        if not await bot._check_channel(interaction):
            return
        await interaction.response.send_message(
            embed=main_menu_embed(),
            view=MainMenuView(bot),
            ephemeral=True,
        )

    @stockpile_group.command(name="war_room", description="Open the War Room — operations & live war data.")
    async def war_room(interaction: discord.Interaction) -> None:
        if not await bot._check_channel(interaction, allow_ops=True):
            return
        await interaction.response.send_message(
            embed=war_room_embed(),
            view=WarRoomView(bot),
            ephemeral=True,
        )

    bot.tree.add_command(stockpile_group)

    # Wiki lookup. Top-level + short on purpose — it's a chat utility, so it
    # works in any channel and posts publicly for everyone to see. All data
    # comes from the locally cached weekly wiki sync; no live wiki calls.
    @bot.tree.command(name="s", description="Look up a Foxhole item, vehicle, or structure (wiki data).")
    @app_commands.describe(query="Name or nickname — e.g. 7.62mm, Outlaw, bmats")
    async def wiki_search(interaction: discord.Interaction, query: str) -> None:
        dex = bot.wikidex
        entry = dex.get(query)
        if entry is None:
            results = dex.search(query, limit=1)
            entry = results[0] if results else None
        if entry is None:
            suggestions = dex.suggest(query)
            hint = (
                "Did you mean: " + ", ".join(f"`{s['name']}`" for s in suggestions)
                if suggestions else "Try the autocomplete suggestions while typing."
            )
            await interaction.response.send_message(
                f"🔎 No wiki entry found for **{query}**.\n{hint}", ephemeral=True
            )
            return
        embed = wiki_entry_embed(
            entry,
            page_url=wiki_page_url(entry["name"]),
            image_url=wiki_image_url(entry["image"]) if entry.get("image") else None,
            fetched_at=dex.fetched_at,
        )
        await interaction.response.send_message(embed=embed)

    @wiki_search.autocomplete("query")
    async def wiki_search_autocomplete(
        interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        results = bot.wikidex.search(current, limit=25) if current.strip() else []
        return [
            app_commands.Choice(name=entry["name"][:100], value=entry["name"][:100])
            for entry in results
        ]

    # Regi Net broadcast. Top-level + short on purpose — it's a chat command.
    # Text arrives via the interaction, so no privileged Message Content intent.
    _REGI_ATTACHMENT_CAP = 8 * 1024 * 1024

    @bot.tree.command(name="g", description="Broadcast a message to every linked regiment (Regi Net).")
    @app_commands.describe(message="What to broadcast", image="Optional image to send along")
    async def regi_global(
        interaction: discord.Interaction,
        message: str,
        image: discord.Attachment | None = None,
    ) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message("Regi Net only works in a server.", ephemeral=True)
            return
        relay = bot.store.get_relay_channel(interaction.guild_id)
        if relay is None:
            await interaction.response.send_message(
                "📡 This server hasn't joined Regi Net. An admin can join via "
                "`/foxhole_buddy setup → 🌐 Regi Chat`.",
                ephemeral=True,
            )
            return
        if interaction.channel_id != relay:
            await interaction.response.send_message(
                f"Use `/g` in your Regi Net channel <#{relay}>.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)
        content = message
        attachments: list[tuple[str, bytes]] = []
        if image is not None:
            if image.size and image.size > _REGI_ATTACHMENT_CAP:
                content = f"{content}\n{image.url}".strip()
            else:
                try:
                    attachments.append((image.filename, await image.read()))
                except Exception:
                    content = f"{content}\n{image.url}".strip()

        count = await bot.broadcast_regi(
            author_name=interaction.user.display_name,
            regiment=interaction.guild.name,
            faction=bot.store.get_guild_faction(interaction.guild_id),
            avatar_url=interaction.user.display_avatar.url,
            content=content,
            attachments=attachments,
        )
        await interaction.followup.send(
            f"📡 Transmitted to **{count}** regiment(s) on the net.", ephemeral=True
        )

    @bot.tree.command(name="a", description="Broadcast a message to your allied servers in this channel's room.")
    @app_commands.describe(message="What to send", image="Optional image to send along")
    async def ally_cmd(
        interaction: discord.Interaction,
        message: str,
        image: discord.Attachment | None = None,
    ) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message("Ally chat only works in a server.", ephemeral=True)
            return
        room = bot.store.ally_room_by_channel(interaction.guild_id, interaction.channel_id)
        if room is None:
            await interaction.response.send_message(
                "🛡️ This channel isn't an ally chat. An admin can set one up via "
                "`/foxhole_buddy setup → 💬 Setup Chats → 🛡️ Ally Chats`.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        content = message
        attachments: list[tuple[str, bytes]] = []
        if image is not None:
            if image.size and image.size > _REGI_ATTACHMENT_CAP:
                content = f"{content}\n{image.url}".strip()
            else:
                try:
                    attachments.append((image.filename, await image.read()))
                except Exception:
                    content = f"{content}\n{image.url}".strip()

        count = await bot.broadcast_ally(
            room_code=room,
            author_name=interaction.user.display_name,
            regiment=interaction.guild.name,
            faction=bot.store.get_guild_faction(interaction.guild_id),
            avatar_url=interaction.user.display_avatar.url,
            content=content,
            attachments=attachments,
        )
        await interaction.followup.send(
            f"🛡️ Sent to **{count}** allied server(s).", ephemeral=True
        )
