import io
import logging
import os
import discord
from discord import app_commands
from foxhole_buddy.core.store import StockpileStore, Stockpile, LogisticsRequest, Operation
from foxhole_buddy.catalog import Catalog
from foxhole_buddy.utils import foxhole_api
from foxhole_buddy.utils.env import optional_int_env
from foxhole_buddy.utils.formatting import relay_display_name
from foxhole_buddy.ui.embeds import stockpile_embed, logistics_request_embed, operation_card_embed
from foxhole_buddy.ui.views import StockpileView
from foxhole_buddy.commands import register_commands
from foxhole_buddy.tasks import reminder_loop, catalog_sync_loop, war_sync_loop, wikidex_sync_loop
from foxhole_buddy.wikidex import WikiDex

log = logging.getLogger("foxhole_buddy.bot")

# Name of the webhook the bot creates in each regi-net channel to post relays.
RELAY_WEBHOOK_NAME = "Foxhole Regi Net"

class StockpileBot(discord.Client):
    def __init__(self, store: StockpileStore):
        # Regi Net relays via the /g command (interaction text), so the bot
        # needs NO privileged Message Content intent — default intents suffice.
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.store = store
        # Per-channel webhook cache for the relay, keyed by channel id.
        self._relay_webhooks: dict[int, discord.Webhook] = {}
        self.guild_id = optional_int_env("DISCORD_GUILD_ID")
        # Wiki-synced item catalog (seed fallback until the first sync writes the cache).
        self.catalog_path = os.getenv("CATALOG_FILE", "data/catalog.json")
        self.catalog = Catalog.load(self.catalog_path)
        # Wiki encyclopedia for /s (seed fallback until the first weekly sync).
        self.wikidex_path = os.getenv("WIKIDEX_FILE", "data/wikidex.json")
        self.wikidex = WikiDex.load(self.wikidex_path)
        # Current Foxhole war number, refreshed ~daily; cached across restarts.
        self.war_path = os.getenv("WAR_CACHE_FILE", "data/war.json")
        self.war_number, _ = foxhole_api.load_cache(self.war_path)
        self.war_maps: list[str] = []  # populated by the daily war sync

    async def setup_hook(self) -> None:
        register_commands(self)

        # Re-attach persistent views for all stockpiles across all guilds
        for stockpile in self.store.all():
            self.add_view(StockpileView(self, stockpile.id), message_id=stockpile.message_id)

        # Re-attach persistent views for factory alarms
        from foxhole_buddy.ui.views import FactoryAlarmCardView
        for alarm in self.store.get_factory_alarms():
            if alarm.message_id:
                self.add_view(FactoryAlarmCardView(self, alarm.id), message_id=alarm.message_id)

        # Re-attach persistent views for live logistics request cards
        from foxhole_buddy.ui.views import LogisticsRequestCardView
        for request in self.store.get_logistics_requests(include_delivered=False):
            if request.message_id:
                self.add_view(
                    LogisticsRequestCardView(self, request.id), message_id=request.message_id
                )

        # Re-attach persistent views for open operation cards. Allied ops are
        # handled by the mirror sweep below (their copies, incl. the host's, all
        # live in operation_mirrors), so skip them here to avoid a double add.
        from foxhole_buddy.ui.views import OperationCardView
        for op in self.store.get_operations(open_only=True):
            if op.message_id and not op.ally_room:
                self.add_view(OperationCardView(self, op.id), message_id=op.message_id)
        for mirror in self.store.all_operation_mirrors(open_only=True):
            if mirror["message_id"]:
                self.add_view(
                    OperationCardView(self, mirror["op_id"]), message_id=mirror["message_id"]
                )

        # One Regi Net / Ally panel view serves every server (static custom_ids).
        from foxhole_buddy.ui.views import AllyNetPanelView, RegiNetPanelView
        self.add_view(RegiNetPanelView(self))
        self.add_view(AllyNetPanelView(self))

        if self.guild_id:
            guild = discord.Object(id=self.guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()

        reminder_loop.change_interval(seconds=int(os.getenv("REMINDER_INTERVAL_SECONDS", "60")))
        if not reminder_loop.is_running():
            reminder_loop.start(self)

        if not catalog_sync_loop.is_running():
            catalog_sync_loop.start(self)

        if not war_sync_loop.is_running():
            war_sync_loop.start(self)

        if not wikidex_sync_loop.is_running():
            wikidex_sync_loop.start(self)

    async def on_ready(self) -> None:
        # Startup sweep: drop data for any guild the bot is no longer a member of.
        self._purge_stale_guilds()

    async def on_guild_remove(self, guild: discord.Guild) -> None:
        # Kicked/left a server — wipe its data so it doesn't linger in the DB.
        removed = self.store.purge_guild(guild.id)
        log.info("Left guild %s; purged %d row(s).", guild.id, removed)

    # ------------------------------------------------------------------
    # Regi Net — global cross-server broadcast (the /g command)
    # ------------------------------------------------------------------

    async def broadcast_regi(
        self,
        *,
        author_name: str,
        regiment: str,
        faction: str | None,
        avatar_url: str | None,
        content: str,
        attachments: list[tuple[str, bytes]] | None = None,
    ) -> int:
        """Fan a message out to every joined regi-chat channel (one global net,
        all factions), including the sender's own so the channel reads like live
        chat. Each post is stamped with the sender's name, regiment, and faction.

        Returns the number of channels successfully delivered to. ``attachments``
        is a list of already-read ``(filename, bytes)`` pairs.
        """
        username = relay_display_name(author_name, regiment, faction)
        content = (content or "")[:2000]
        attachments = attachments or []
        delivered = 0
        for _guild_id, channel_id in self.store.relay_channels():
            webhook = await self._get_relay_webhook(channel_id)
            if webhook is None:
                continue
            # Fresh File objects per send — a buffer is consumed once.
            files = [discord.File(io.BytesIO(data), filename=name) for name, data in attachments]
            try:
                await webhook.send(
                    content=content or None,
                    username=username,
                    avatar_url=avatar_url,
                    files=files,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                delivered += 1
            except Exception as exc:
                log.warning("Regi Net broadcast to channel %s failed: %s", channel_id, exc)
        return delivered

    async def broadcast_ally(
        self,
        *,
        room_code: str,
        author_name: str,
        regiment: str,
        faction: str | None,
        avatar_url: str | None,
        content: str,
        attachments: list[tuple[str, bytes]] | None = None,
    ) -> int:
        """Fan a message out to every member channel of an ally ``room_code``
        (including the sender's own). Same webhook engine as the global net,
        scoped to the private room. Returns the number of channels delivered to.
        """
        username = relay_display_name(author_name, regiment, faction)
        content = (content or "")[:2000]
        attachments = attachments or []
        delivered = 0
        for _guild_id, channel_id in self.store.ally_members(room_code):
            webhook = await self._get_relay_webhook(channel_id)
            if webhook is None:
                continue
            files = [discord.File(io.BytesIO(data), filename=name) for name, data in attachments]
            try:
                await webhook.send(
                    content=content or None,
                    username=username,
                    avatar_url=avatar_url,
                    files=files,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                delivered += 1
            except Exception as exc:
                log.warning("Ally broadcast to channel %s failed: %s", channel_id, exc)
        return delivered

    async def _get_relay_webhook(self, channel_id: int) -> discord.Webhook | None:
        cached = self._relay_webhooks.get(channel_id)
        if cached is not None:
            return cached
        try:
            channel = self.get_channel(channel_id) or await self.fetch_channel(channel_id)
            hooks = await channel.webhooks()
            hook = discord.utils.get(hooks, name=RELAY_WEBHOOK_NAME)
            if hook is None:
                hook = await channel.create_webhook(name=RELAY_WEBHOOK_NAME)
        except discord.Forbidden:
            log.warning(
                "Missing Manage Webhooks for regi-chat channel %s; skipping relay.", channel_id
            )
            return None
        except Exception as exc:
            log.warning("Could not resolve regi-chat webhook for channel %s: %s", channel_id, exc)
            return None
        self._relay_webhooks[channel_id] = hook
        return hook

    def _purge_stale_guilds(self) -> None:
        active = {guild.id for guild in self.guilds}
        if not active:
            # Avoid wiping everything during a transient/empty connection state.
            return
        for guild_id in self.store.known_guild_ids() - active:
            removed = self.store.purge_guild(guild_id)
            log.info("No longer in guild %s; purged %d row(s).", guild_id, removed)

    async def _check_channel(self, interaction: discord.Interaction, *, allow_ops: bool = False) -> bool:
        """Verify the interaction is in this guild's configured channel.

        With ``allow_ops=True`` the dedicated operations channel is also accepted
        (operations are usable in both the main and ops channels). Sends an
        ephemeral error and returns False if the check fails.
        """
        if interaction.guild_id is None:
            await interaction.response.send_message(
                "Foxhole Buddy only works inside a server.", ephemeral=True
            )
            return False

        configured = self.store.get_guild_channel(interaction.guild_id)
        if configured is None:
            await interaction.response.send_message(
                "⚙️ **Setup required.** An admin needs to run `/foxhole_buddy setup` "
                "in the desired reminder channel first.",
                ephemeral=True,
            )
            return False

        allowed = {configured}
        if allow_ops:
            # get_alert_channel("ops") is the ops channel if set, else the main one.
            allowed.add(self.store.get_alert_channel(interaction.guild_id, "ops"))

        if interaction.channel_id not in allowed:
            where = " or ".join(f"<#{cid}>" for cid in sorted(allowed))
            await interaction.response.send_message(
                f"Please use Foxhole Buddy commands in {where}.", ephemeral=True
            )
            return False

        return True

    async def update_stockpile_message(self, stockpile: Stockpile) -> None:
        if stockpile.message_id is None:
            return
        inventory = self.store.get_stockpile_inventory(stockpile.id)
        try:
            channel = self.get_channel(stockpile.channel_id) or await self.fetch_channel(stockpile.channel_id)
            message = await channel.fetch_message(stockpile.message_id)
            await message.edit(
                embed=stockpile_embed(stockpile, inventory),
                view=StockpileView(self, stockpile.id),
            )
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
            log.warning("Could not update stockpile card %s: %s", stockpile.id, exc)

    # ------------------------------------------------------------------
    # Stockpile card posting — honours the configured stockpile channel; a
    # forum channel gets one post per stockpile (like ops), a text channel gets
    # the cards, and unset falls back to the invoking channel.
    # ------------------------------------------------------------------

    async def post_stockpile_card(self, stockpile: Stockpile, invoking_channel) -> None:
        inventory = self.store.get_stockpile_inventory(stockpile.id)
        embed = stockpile_embed(stockpile, inventory)
        view = StockpileView(self, stockpile.id)

        dest_id = self.store.get_alert_channel(stockpile.guild_id, "stockpile")
        dest = self.get_channel(dest_id) if dest_id else None
        if dest is None and dest_id:
            try:
                dest = await self.fetch_channel(dest_id)
            except Exception:  # noqa: BLE001 — unreachable configured channel
                dest = None

        if isinstance(dest, discord.ForumChannel):
            name = f"📦 {stockpile.name}"[:100]
            created = await self._create_forum_post(dest, name, embed, view)
            self.store.attach_stockpile_thread(
                stockpile.id, thread_id=created.thread.id, message_id=created.message.id
            )
            return created.thread

        channel = dest or invoking_channel
        message = await channel.send(embed=embed, view=view)
        self.store.set_message_id(stockpile.id, message.id)
        return None

    async def update_logistics_message(self, request: LogisticsRequest) -> None:
        if request.message_id is None:
            return
        from foxhole_buddy.ui.views import LogisticsRequestCardView
        try:
            channel = self.get_channel(request.channel_id) or await self.fetch_channel(request.channel_id)
            message = await channel.fetch_message(request.message_id)
            await message.edit(
                embed=logistics_request_embed(request),
                view=LogisticsRequestCardView(self, request.id),
            )
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
            log.warning("Could not update logistics card %s: %s", request.id, exc)

    async def update_operation_message(self, op: Operation) -> None:
        if op.ally_room:
            await self.update_allied_op_messages(op)
            return
        if op.message_id is None:
            return
        from foxhole_buddy.ui.views import OperationCardView
        linked = self.store.get_logistics_requests_for_op(op.id)
        try:
            channel = self.get_channel(op.channel_id) or await self.fetch_channel(op.channel_id)
            message = await channel.fetch_message(op.message_id)
            await message.edit(
                embed=operation_card_embed(op, linked_requests=linked),
                view=OperationCardView(self, op.id),
            )
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
            log.warning("Could not update operation card %s: %s", op.id, exc)

    # ------------------------------------------------------------------
    # Forum ops — one forum post (thread) per operation when the ops channel
    # is a forum. The post's starter message is the op card.
    # ------------------------------------------------------------------

    @staticmethod
    def _op_thread_name(op: Operation, prefix: str = "⚔️") -> str:
        return f"{prefix} Op #{op.op_number} — {op.name}"[:100]

    async def _create_forum_post(self, forum: "discord.ForumChannel", name: str, embed, view):
        """Create one forum post whose starter message carries ``embed``/``view``.
        Returns the ``ThreadWithMessage`` (``.thread`` / ``.message``); raises on
        missing perms or a required-tag forum so the caller can react."""
        return await forum.create_thread(
            name=name[:100],
            embed=embed,
            view=view,
            auto_archive_duration=10080,  # 7 days — outlast most scheduling windows
        )

    async def archive_thread(
        self, channel_id: int, message_id: int | None, name: str
    ) -> None:
        """Wind a forum post down: strip the starter message's buttons (leaving
        the embed as a record), rename it, then archive + lock. The starter
        message can't be deleted without deleting the whole post, so it stays."""
        try:
            thread = self.get_channel(channel_id) or await self.fetch_channel(channel_id)
            if message_id:
                try:
                    message = await thread.fetch_message(message_id)
                    await message.edit(view=None)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    pass
            await thread.edit(name=name[:100], archived=True, locked=True)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
            log.warning("Could not archive thread %s: %s", channel_id, exc)

    async def open_operation_thread(self, op: Operation, forum: "discord.ForumChannel"):
        """Create a forum post for ``op`` with the op card as its starter message.
        Returns (thread, message) to persist the binding."""
        from foxhole_buddy.ui.views import OperationCardView

        created = await self._create_forum_post(
            forum, self._op_thread_name(op), operation_card_embed(op), OperationCardView(self, op.id)
        )
        return created.thread, created.message

    async def archive_operation_thread(self, op: Operation, prefix: str) -> None:
        """Archive an op's forum post, re-titled with ``prefix`` (✅/🛑)."""
        await self.archive_thread(op.thread_id, op.message_id, self._op_thread_name(op, prefix))

    # ------------------------------------------------------------------
    # Allied operations — mirror one op into every ally-room member channel
    # ------------------------------------------------------------------

    async def post_allied_op(self, op: Operation) -> int:
        """Post the interactive op card into every member channel of the op's
        ally room, recording each copy as a mirror so later edits can fan out.
        The host's own copy doubles as the canonical message (jump links).
        Returns the number of channels posted to."""
        from foxhole_buddy.ui.views import OperationCardView
        linked = self.store.get_logistics_requests_for_op(op.id)
        posted = 0
        for guild_id, channel_id in self.store.ally_members(op.ally_room):
            try:
                channel = self.get_channel(channel_id) or await self.fetch_channel(channel_id)
                message = await channel.send(
                    embed=operation_card_embed(op, linked_requests=linked),
                    view=OperationCardView(self, op.id),
                )
            except Exception as exc:
                # Record the mirror only on a successful send — a server we
                # couldn't post to gets no row, so it isn't left permanently
                # stale (and isn't silently skipped by later fan-outs forever).
                log.warning("Allied op post to channel %s failed: %s", channel_id, exc)
                continue
            self.store.add_operation_mirror(op.id, guild_id, channel_id, message.id)
            self.add_view(OperationCardView(self, op.id), message_id=message.id)
            posted += 1
            if guild_id == op.guild_id:
                # Point the canonical record at the host's copy.
                self.store.set_operation_message_id(op.id, message.id)
                op.channel_id = channel_id
                op.message_id = message.id
        return posted

    async def announce_allied_op(
        self, op: Operation, heading: str, *, recipients: list[int] | None = None
    ) -> None:
        """Post ``heading`` into every mirror channel, @-pinging that server's own
        participants (a mention only resolves/pings in the user's home server) and
        naming the rest from ``participant_meta``. Defaults to committed + tentative.
        """
        meta = op.participant_meta or {}
        if recipients is None:
            recipients = op.participant_ids() + op.tentative
        recipients = list(dict.fromkeys(recipients))  # de-dupe, keep order
        for mirror in self.store.operation_mirrors(op.id):
            gid = mirror["guild_id"]
            local: list[str] = []
            away: list[str] = []
            for uid in recipients:
                info = meta.get(str(uid))
                if info is not None and info.get("guild_id") == gid:
                    local.append(f"<@{uid}>")  # resolves/pings only if truly local
                elif info is not None:
                    away.append(info.get("name") or f"User {uid}")
                else:
                    # Unknown home guild — name it plainly so no broken mention leaks.
                    away.append(f"User {uid}")
            line = heading
            if local:
                line += "\n" + " ".join(local)
            if away:
                line += "\n_Also from allied servers:_ " + ", ".join(away)
            try:
                channel = (
                    self.get_channel(mirror["channel_id"])
                    or await self.fetch_channel(mirror["channel_id"])
                )
                await channel.send(line)
            except Exception as exc:
                log.warning("Allied op announce to channel %s failed: %s", mirror["channel_id"], exc)

    async def update_allied_op_messages(self, op: Operation) -> None:
        """Edit every mirrored copy of an allied op with the refreshed card."""
        from foxhole_buddy.ui.views import OperationCardView
        linked = self.store.get_logistics_requests_for_op(op.id)
        embed = operation_card_embed(op, linked_requests=linked)
        for mirror in self.store.operation_mirrors(op.id):
            if not mirror["message_id"]:
                continue
            try:
                channel = (
                    self.get_channel(mirror["channel_id"])
                    or await self.fetch_channel(mirror["channel_id"])
                )
                message = await channel.fetch_message(mirror["message_id"])
                await message.edit(embed=embed, view=OperationCardView(self, op.id))
            except Exception as exc:
                log.warning(
                    "Allied op mirror update (channel %s) failed: %s", mirror["channel_id"], exc
                )

    # ------------------------------------------------------------------
    # Card cleanup — remove a bot message when its data is no longer in use
    # ------------------------------------------------------------------

    async def delete_card_message(self, channel_id: int | None, message_id: int | None) -> None:
        """Delete one of the bot's posted cards, tolerating an already-gone
        message / lost perms. The single primitive every 'done → gone' path uses."""
        if not channel_id or not message_id:
            return
        try:
            channel = self.get_channel(channel_id) or await self.fetch_channel(channel_id)
            message = await channel.fetch_message(message_id)
            await message.delete()
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
            log.warning("Could not delete card message %s in %s: %s", message_id, channel_id, exc)

    async def delete_allied_op_cards(self, op: Operation) -> None:
        """Delete every copy of an op card. Allied ops fan out across each mirror
        channel; local ops have just the single host card."""
        if op.ally_room:
            for mirror in self.store.operation_mirrors(op.id):
                await self.delete_card_message(mirror["channel_id"], mirror["message_id"])
        else:
            await self.delete_card_message(op.channel_id, op.message_id)
