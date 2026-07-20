"""Operations views: actions menu, the persistent op card, and its helper pickers."""

from typing import TYPE_CHECKING

import discord

from foxhole_buddy.core.store import OP_CANCELLED, OP_COMPLETED, OP_IN_PROGRESS
from foxhole_buddy.theme import Color
from foxhole_buddy.ui.embeds import operation_card_embed, war_room_embed
from foxhole_buddy.ui.modals import CreateOperationModal, EditOperationModal, NotifyOperationModal
from foxhole_buddy.utils.formatting import unix_ts

if TYPE_CHECKING:
    from foxhole_buddy.core.bot import StockpileBot


class OperationsActionsView(discord.ui.View):
    """Operations sub-menu: Schedule Op / View Ops / Back."""

    def __init__(self, bot: "StockpileBot"):
        super().__init__(timeout=120)
        self.bot = bot

    @discord.ui.button(label="Schedule Op", style=discord.ButtonStyle.danger, emoji="➕", row=0)
    async def schedule_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await self.bot._check_channel(interaction, allow_ops=True):
            return
        await interaction.response.send_modal(CreateOperationModal(self.bot))

    @discord.ui.button(label="Allied Op", style=discord.ButtonStyle.primary, emoji="🤝", row=0)
    async def allied_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await self.bot._check_channel(interaction, allow_ops=True):
            return
        rooms = self.bot.store.ally_rooms_for_guild(interaction.guild_id)
        if not rooms:
            await interaction.response.send_message(
                "🤝 You're not in any ally rooms yet. An admin can set one up under "
                "`/foxhole_buddy setup → 💬 Setup Chats → 🛡️ Ally Chats`, then schedule "
                "an allied op that fans out to every member server.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            "Pick an ally room to schedule this op into:",
            view=AlliedOpRoomView(self.bot, rooms),
            ephemeral=True,
        )

    @discord.ui.button(label="View Ops", style=discord.ButtonStyle.secondary, emoji="📋", row=1)
    async def view_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await self.bot._check_channel(interaction, allow_ops=True):
            return
        ops = self.bot.store.get_operations(guild_id=interaction.guild_id, open_only=True)
        # Allied ops this server is a member of (hosted elsewhere) show too.
        ops += self.bot.store.operations_for_member_guild(interaction.guild_id, open_only=True)
        ops.sort(key=lambda o: o.scheduled_at)
        if not ops:
            await interaction.response.send_message(
                embed=discord.Embed(
                    title="No Upcoming Operations",
                    description="Use **Schedule Op** to plan one.",
                    color=Color.GRAY,
                ),
                ephemeral=True,
            )
            return
        lines = []
        for op in ops:
            ts = unix_ts(op.scheduled_datetime)
            jump = (
                f"https://discord.com/channels/{op.guild_id}/{op.channel_id}/{op.message_id}"
                if op.message_id else None
            )
            name = f"{'🤝 ' if op.ally_room else ''}Op #{op.op_number} — {op.name}"
            title = f"[{name}]({jump})" if jump else name
            lines.append(f"{title}\n<t:{ts}:F> · ✅ {op.going_count()} going")
        embed = discord.Embed(
            title="⚔️ Upcoming Operations",
            description="\n\n".join(lines)[:4000],
            color=Color.PURPLE,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="Back", style=discord.ButtonStyle.secondary, emoji="◀️", row=1)
    async def back_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        from foxhole_buddy.ui.views.war import WarRoomView

        await interaction.response.edit_message(embed=war_room_embed(), view=WarRoomView(self.bot))


class AlliedOpRoomView(discord.ui.View):
    """Ephemeral picker: choose which ally room to schedule an allied op into."""

    def __init__(self, bot: "StockpileBot", rooms: list[dict]):
        super().__init__(timeout=120)
        self.bot = bot
        options = [
            discord.SelectOption(
                label=room["room_code"][:100],
                value=room["room_code"],
                description=f"{room['members']} server(s) linked"[:100],
            )
            for room in rooms[:25]
        ]
        select = discord.ui.Select(placeholder="Choose an ally room…", options=options)
        select.callback = self.on_select
        self.select = select
        self.add_item(select)

    async def on_select(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(
            CreateOperationModal(self.bot, ally_room=self.select.values[0])
        )


class OperationCardView(discord.ui.View):
    """Persistent operation card: RSVP + squad sign-up + leader controls."""

    def __init__(self, bot: "StockpileBot", op_id: str):
        super().__init__(timeout=None)
        self.bot = bot
        self.op_id = op_id

        op = bot.store.get_operation(op_id)
        has_squads = bool(op and op.squads)
        closed = op is not None and op.status in (OP_COMPLETED, OP_CANCELLED)
        started = op is not None and op.status == OP_IN_PROGRESS

        def add(label, emoji, cid, handler, row, style=discord.ButtonStyle.secondary, disabled=False):
            btn = discord.ui.Button(label=label, emoji=emoji, style=style,
                                    custom_id=f"{cid}:{op_id}", row=row, disabled=disabled)
            btn.callback = handler
            self.add_item(btn)

        add("Going", "✅", "op_going", self.going_cb, 0, discord.ButtonStyle.success, disabled=closed)
        if has_squads:
            add("Pick Squad", "🪖", "op_squad", self.squad_cb, 0, discord.ButtonStyle.primary, disabled=closed)
        add("Tentative", "❓", "op_tentative", self.tentative_cb, 0, disabled=closed)
        add("Can't", "🚫", "op_cant", self.cant_cb, 0, disabled=closed)
        add("Withdraw", "↩️", "op_withdraw", self.withdraw_cb, 0, disabled=closed)

        if has_squads:
            add("Leads", "👑", "op_leads", self.leads_cb, 1, disabled=closed)
        add("Start", "▶️", "op_start", self.start_cb, 1, discord.ButtonStyle.success,
            disabled=closed or started)
        add("Notify", "📣", "op_notify", self.notify_cb, 1, disabled=closed)
        add("Edit", "✏️", "op_edit", self.edit_cb, 1, disabled=closed)
        add("Cancel", "🛑", "op_cancel", self.cancel_cb, 1, discord.ButtonStyle.danger, disabled=closed)
        # Row 1 is full (5 buttons), so Finish + Logistics sit on row 2.
        add("Finish", "🏁", "op_finish", self.finish_cb, 2, discord.ButtonStyle.success, disabled=closed)
        add("Logistics", "📦", "op_logi", self.logistics_cb, 2, discord.ButtonStyle.primary, disabled=closed)

    # -- helpers --
    def _is_leader(self, interaction: discord.Interaction, op) -> bool:
        if interaction.user.id == op.leader_user_id:
            return True
        perms = getattr(interaction.user, "guild_permissions", None)
        if op.ally_room:
            # On a shared op, only the *host* server's managers run leader actions.
            return bool(perms and perms.manage_guild and interaction.guild_id == op.guild_id)
        return bool(perms and perms.manage_guild)

    async def _get_open_op(self, interaction: discord.Interaction):
        # Allied ops are reachable from any member server; local ops stay isolated.
        op = self.bot.store.get_operation(self.op_id)
        if op is None or (not op.ally_room and op.guild_id != interaction.guild_id):
            await interaction.response.send_message("That operation no longer exists.", ephemeral=True)
            return None
        return op

    def _record_meta(self, interaction: discord.Interaction, op):
        """For allied ops, remember who this participant is (name + faction +
        home server) so cross-server cards can render them. Returns the op."""
        if not op.ally_room:
            return op
        return self.bot.store.set_participant_meta(
            op.id, interaction.user.id,
            name=interaction.user.display_name,
            faction=self.bot.store.get_guild_faction(interaction.guild_id),
            guild_id=interaction.guild_id,
            server=interaction.guild.name if interaction.guild else None,
        )

    async def _refresh_in_place(self, interaction: discord.Interaction, op) -> None:
        linked = self.bot.store.get_logistics_requests_for_op(op.id)
        # Edit the clicked card first (the initial response, within Discord's ~3s
        # window); for allied ops fan the same update out to every mirror after.
        await interaction.response.edit_message(
            embed=operation_card_embed(op, linked_requests=linked),
            view=OperationCardView(self.bot, op.id),
        )
        if op.ally_room:
            await self.bot.update_allied_op_messages(op)

    # -- RSVP (act in place on the card) --
    async def going_cb(self, interaction: discord.Interaction) -> None:
        op = await self._get_open_op(interaction)
        if op is None:
            return
        op = self.bot.store.set_rsvp(self.op_id, user_id=interaction.user.id, state="going")
        op = self._record_meta(interaction, op)
        await self._refresh_in_place(interaction, op)

    async def tentative_cb(self, interaction: discord.Interaction) -> None:
        op = await self._get_open_op(interaction)
        if op is None:
            return
        op = self.bot.store.set_rsvp(self.op_id, user_id=interaction.user.id, state="tentative")
        op = self._record_meta(interaction, op)
        await self._refresh_in_place(interaction, op)

    async def cant_cb(self, interaction: discord.Interaction) -> None:
        op = await self._get_open_op(interaction)
        if op is None:
            return
        op = self.bot.store.set_rsvp(self.op_id, user_id=interaction.user.id, state="not_available")
        op = self._record_meta(interaction, op)
        await self._refresh_in_place(interaction, op)

    async def withdraw_cb(self, interaction: discord.Interaction) -> None:
        op = await self._get_open_op(interaction)
        if op is None:
            return
        op = self.bot.store.withdraw(self.op_id, user_id=interaction.user.id)
        await self._refresh_in_place(interaction, op)

    # -- Squad sign-up (secondary ephemeral picker) --
    async def squad_cb(self, interaction: discord.Interaction) -> None:
        op = await self._get_open_op(interaction)
        if op is None:
            return
        await interaction.response.send_message(
            "Pick a squad to join:", view=SquadSignupView(self.bot, self.op_id), ephemeral=True
        )

    async def leads_cb(self, interaction: discord.Interaction) -> None:
        op = await self._get_open_op(interaction)
        if op is None:
            return
        if not self._is_leader(interaction, op):
            await interaction.response.send_message(
                "Only the op leader or a server manager can manage leads.", ephemeral=True
            )
            return
        await interaction.response.send_message(
            "Pick a squad to set its lead:", view=ManageLeadsView(self.bot, self.op_id), ephemeral=True
        )

    # -- Leader controls --
    async def start_cb(self, interaction: discord.Interaction) -> None:
        op = await self._get_open_op(interaction)
        if op is None:
            return
        if not self._is_leader(interaction, op):
            await interaction.response.send_message("Only the op leader can start it.", ephemeral=True)
            return
        op = self.bot.store.set_operation_status(self.op_id, OP_IN_PROGRESS)
        await self._refresh_in_place(interaction, op)
        if op.ally_room:
            await self.bot.announce_allied_op(
                op, f"▶️ **Op #{op.op_number} — {op.name}** is starting now!",
                recipients=op.participant_ids(),
            )
            return
        recipients = op.participant_ids()
        if recipients:
            mentions = " ".join(f"<@{uid}>" for uid in recipients)
            await interaction.followup.send(f"▶️ **Op #{op.op_number}** is starting now! {mentions}")

    async def notify_cb(self, interaction: discord.Interaction) -> None:
        op = await self._get_open_op(interaction)
        if op is None:
            return
        if not self._is_leader(interaction, op):
            await interaction.response.send_message("Only the op leader can notify attendees.", ephemeral=True)
            return
        await interaction.response.send_modal(NotifyOperationModal(self.bot, self.op_id))

    async def edit_cb(self, interaction: discord.Interaction) -> None:
        op = await self._get_open_op(interaction)
        if op is None:
            return
        if not self._is_leader(interaction, op):
            await interaction.response.send_message("Only the op leader can edit it.", ephemeral=True)
            return
        await interaction.response.send_modal(EditOperationModal(self.bot, self.op_id))

    async def _finalize_and_delete(
        self, interaction: discord.Interaction, op, note: str, thread_prefix: str = "✅"
    ) -> None:
        """An op is done (cancelled or finished): remove every card copy and the
        row. Respond first — the clicked card is one of the messages we delete —
        then fan the deletes out (same ~3s-rule ordering used elsewhere). A
        thread-mode op keeps its thread instead: card dropped, thread archived +
        locked with ``thread_prefix`` so the coordination chat survives."""
        await interaction.response.send_message(note, ephemeral=True)
        if op.ally_room:
            await self.bot.delete_allied_op_cards(op)  # reads mirrors before the row goes
        elif op.thread_id:
            await self.bot.archive_operation_thread(op, thread_prefix)
        else:
            await self.bot.delete_card_message(op.channel_id, op.message_id)
        self.bot.store.delete_operation(op.id)

    async def finish_cb(self, interaction: discord.Interaction) -> None:
        op = await self._get_open_op(interaction)
        if op is None:
            return
        if not self._is_leader(interaction, op):
            await interaction.response.send_message("Only the op leader can finish it.", ephemeral=True)
            return
        note = (
            "🏁 Op finished — thread archived." if op.thread_id and not op.ally_room
            else "🏁 Op finished and cleared."
        )
        await self._finalize_and_delete(interaction, op, note, thread_prefix="✅")

    async def cancel_cb(self, interaction: discord.Interaction) -> None:
        op = await self._get_open_op(interaction)
        if op is None:
            return
        if not self._is_leader(interaction, op):
            await interaction.response.send_message("Only the op leader can cancel it.", ephemeral=True)
            return
        note = (
            "🛑 Op cancelled — thread archived." if op.thread_id and not op.ally_room
            else "🛑 Op cancelled and cleared."
        )
        await self._finalize_and_delete(interaction, op, note, thread_prefix="🛑")

    async def logistics_cb(self, interaction: discord.Interaction) -> None:
        op = await self._get_open_op(interaction)
        if op is None:
            return
        if op.ally_room and interaction.guild_id != op.guild_id:
            await interaction.response.send_message(
                "📦 Logistics for an allied op are managed in the host server.", ephemeral=True
            )
            return
        if not self._is_leader(interaction, op):
            await interaction.response.send_message(
                "Only the op leader or a server manager can link logistics.", ephemeral=True
            )
            return
        requests = self.bot.store.get_logistics_requests(
            guild_id=interaction.guild_id, include_delivered=False
        )
        # Include requests already linked to this op even if delivered, so they
        # can be unlinked.
        linked = self.bot.store.get_logistics_requests_for_op(op.id, interaction.guild_id)
        seen = {r.id for r in requests}
        requests += [r for r in linked if r.id not in seen]
        if not requests:
            await interaction.response.send_message(
                "No logistics requests to link. Create some from the **Logistics** menu first.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            "Select which logistics requests belong to this op:",
            view=LinkLogisticsView(self.bot, self.op_id, requests),
            ephemeral=True,
        )


class LinkLogisticsView(discord.ui.View):
    """Ephemeral multi-select to link/unlink logistics requests to an op."""

    def __init__(self, bot: "StockpileBot", op_id: str, requests: list):
        super().__init__(timeout=120)
        self.bot = bot
        self.op_id = op_id
        self.shown_ids = [r.id for r in requests][:25]
        options = []
        for req in requests[:25]:
            options.append(discord.SelectOption(
                label=f"{req.item} ×{req.quantity}"[:100],
                value=req.id,
                description=f"{req.category} › {req.subcategory}"[:100],
                default=(req.op_id == op_id),
            ))
        select = discord.ui.Select(
            placeholder="Pick the requests for this op…",
            min_values=0, max_values=len(options), options=options,
        )
        select.callback = self.on_select
        self.select = select
        self.add_item(select)

    async def on_select(self, interaction: discord.Interaction) -> None:
        selected = set(self.select.values)
        changed = 0
        for rid in self.shown_ids:
            req = self.bot.store.get_logistics_request(rid, interaction.guild_id)
            if req is None:
                continue
            if rid in selected and req.op_id != self.op_id:
                self.bot.store.set_logistics_op(rid, self.op_id)
                changed += 1
            elif rid not in selected and req.op_id == self.op_id:
                self.bot.store.set_logistics_op(rid, None)
                changed += 1
        op = self.bot.store.get_operation(self.op_id, interaction.guild_id)
        # Respond first; the op-card fan-out (slow for allied ops) follows.
        await interaction.response.edit_message(
            content=f"🔗 Updated logistics links ({len(selected)} attached).", view=None
        )
        if op:
            await self.bot.update_operation_message(op)


class SquadSignupView(discord.ui.View):
    """Ephemeral squad picker shown when a player clicks Pick Squad."""

    def __init__(self, bot: "StockpileBot", op_id: str):
        super().__init__(timeout=120)
        self.bot = bot
        self.op_id = op_id
        op = bot.store.get_operation(op_id)
        options = []
        for squad in (op.squads if op else [])[:25]:
            cap = squad["capacity"]
            used = len(squad["members"])
            count = f"{used}/{cap}" if cap else f"{used}"
            full = " — FULL (waitlist)" if cap and used >= cap else ""
            options.append(discord.SelectOption(
                label=f"{squad['name']} ({count})"[:100], value=squad["key"],
                description=(f"Sign up{full}")[:100],
            ))
        select = discord.ui.Select(placeholder="Choose a squad…", options=options or [
            discord.SelectOption(label="No squads", value="_none")
        ])
        select.callback = self.on_select
        self.select = select
        self.add_item(select)

    async def on_select(self, interaction: discord.Interaction) -> None:
        key = self.select.values[0]
        if key == "_none":
            await interaction.response.edit_message(content="This op has no squads.", view=None)
            return
        op0 = self.bot.store.get_operation(self.op_id)
        # Allied ops are shared across servers, so don't filter the lookup by guild.
        gid = None if (op0 and op0.ally_room) else interaction.guild_id
        try:
            op, outcome = self.bot.store.signup_squad(
                self.op_id, user_id=interaction.user.id, squad_key=key, guild_id=gid
            )
        except KeyError:
            await interaction.response.edit_message(content="That squad no longer exists.", view=None)
            return
        if op.ally_room:
            op = self.bot.store.set_participant_meta(
                op.id, interaction.user.id,
                name=interaction.user.display_name,
                faction=self.bot.store.get_guild_faction(interaction.guild_id),
                guild_id=interaction.guild_id,
                server=interaction.guild.name if interaction.guild else None,
            )
        squad = op.find_squad(key)
        # Respond first (within Discord's ~3s window); the op-card fan-out is slow
        # network I/O for allied ops and must not block the interaction response.
        verb = "waitlisted for" if outcome == "waitlist" else "signed up to"
        await interaction.response.edit_message(content=f"✅ You're {verb} **{squad['name']}**.", view=None)
        await self.bot.update_operation_message(op)


class ManageLeadsView(discord.ui.View):
    """Ephemeral two-step lead manager (squad → member)."""

    def __init__(self, bot: "StockpileBot", op_id: str):
        super().__init__(timeout=120)
        self.bot = bot
        self.op_id = op_id
        op = bot.store.get_operation(op_id)
        options = [
            discord.SelectOption(label=f"{s['name']}"[:100], value=s["key"])
            for s in (op.squads if op else [])[:25]
        ]
        select = discord.ui.Select(placeholder="Choose a squad…", options=options or [
            discord.SelectOption(label="No squads", value="_none")
        ])
        select.callback = self.on_squad
        self.squad_select = select
        self.add_item(select)

    async def on_squad(self, interaction: discord.Interaction) -> None:
        key = self.squad_select.values[0]
        op = self.bot.store.get_operation(self.op_id)
        if op is None or key == "_none":
            await interaction.response.edit_message(content="That operation no longer exists.", view=None)
            return
        squad = op.find_squad(key)
        if not squad or not squad["members"]:
            await interaction.response.edit_message(
                content=f"**{squad['name'] if squad else 'Squad'}** has no members to lead yet.", view=None
            )
            return
        await interaction.response.edit_message(
            content=f"Set the lead for **{squad['name']}**:",
            view=_LeadMemberView(self.bot, self.op_id, key, interaction.guild),
        )


class _LeadMemberView(discord.ui.View):
    def __init__(self, bot: "StockpileBot", op_id: str, squad_key: str, guild) -> None:
        super().__init__(timeout=120)
        self.bot = bot
        self.op_id = op_id
        self.squad_key = squad_key
        op = bot.store.get_operation(op_id)
        squad = op.find_squad(squad_key) if op else None

        meta = (op.participant_meta if op else None) or {}

        def name_for(uid: int) -> str:
            member = guild.get_member(uid) if guild else None
            if member:
                return member.display_name
            info = meta.get(str(uid))
            return info["name"] if info else f"User {uid}"

        options = [
            discord.SelectOption(label=name_for(uid)[:100], value=str(uid))
            for uid in (squad["members"] if squad else [])[:24]
        ]
        options.append(discord.SelectOption(label="Clear lead", value="_clear", emoji="✖️"))
        select = discord.ui.Select(placeholder="Choose the lead…", options=options)
        select.callback = self.on_member
        self.member_select = select
        self.add_item(select)

    async def on_member(self, interaction: discord.Interaction) -> None:
        value = self.member_select.values[0]
        user_id = None if value == "_clear" else int(value)
        op0 = self.bot.store.get_operation(self.op_id)
        gid = None if (op0 and op0.ally_room) else interaction.guild_id
        try:
            op = self.bot.store.set_squad_lead(
                self.op_id, squad_key=self.squad_key, user_id=user_id, guild_id=gid
            )
        except KeyError:
            await interaction.response.edit_message(content="That squad no longer exists.", view=None)
            return
        squad = op.find_squad(self.squad_key)
        msg = "Lead cleared." if user_id is None else f"Lead set to <@{user_id}>."
        # Respond first; fan the op-card update out to mirrors afterward.
        await interaction.response.edit_message(content=f"👑 **{squad['name']}** — {msg}", view=None)
        await self.bot.update_operation_message(op)
