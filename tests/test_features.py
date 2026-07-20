from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from foxhole_buddy.core.store import (
    StockpileStore, make_squad, make_line, warning_due, mark_warning_sent, utc_now,
    LOGI_OPEN, LOGI_CLAIMED, LOGI_DELIVERED,
)
from foxhole_buddy.core.models import LogisticsRequest, derive_logi_status
from foxhole_buddy.catalog import Catalog
from foxhole_buddy.utils.formatting import relay_display_name
from foxhole_buddy.ui.embeds import operation_card_embed
from foxhole_buddy.ui.modals import parse_quantity_list, plan_removals
from foxhole_buddy.core.models import canonicalize_material_name, make_line
from foxhole_buddy.ui.views.logistics import (
    LogisticsDraft, apply_cart_bulk_add, apply_cart_bulk_remove,
    CUSTOM_CATEGORY,
)


def _line(item: str, qty: int = 1, category: str = "Resource", subcategory: str = "Material") -> dict:
    return make_line(category, subcategory, item, qty)


def _store() -> StockpileStore:
    return StockpileStore(Path(tempfile.mkdtemp()) / "f.db")


class GraduatedWarningTest(unittest.TestCase):
    def test_all_intervals_fire_and_persist(self) -> None:
        store = _store()
        now = datetime(2026, 6, 5, tzinfo=timezone.utc)
        sp = store.create(guild_id=1, channel_id=9, name="A", location="L",
                          stockpile_type="seaport", user_id=1, now=now)
        fired = []
        # Walk time down; at each tick fire whatever is due (catch-up included).
        for hrs in (13, 11, 5, 0.4, 0.1, -1):
            t = sp.expires_datetime - timedelta(hours=hrs)
            while (w := warning_due(sp, t)) is not None:
                mark_warning_sent(sp, w)
                fired.append(w)
        self.assertEqual(set(fired), {"12h", "6h", "1h", "30m", "expired"})

    def test_refresh_clears_reminders(self) -> None:
        store = _store()
        now = datetime(2026, 6, 5, tzinfo=timezone.utc)
        sp = store.create(guild_id=1, channel_id=9, name="A", location="L",
                          stockpile_type="seaport", user_id=1, now=now)
        sp.reminders_sent = ["12h", "6h"]
        store.update(sp)
        sp = store.refresh(sp.id, user_id=1, now=now)
        self.assertEqual(sp.reminders_sent, [])


class LogisticsLifecycleTest(unittest.TestCase):
    def test_claim_all_validate_all_and_op_link(self) -> None:
        store = _store()
        r = store.create_logistics_request(
            guild_id=1, channel_id=2, user_id=5,
            items=[_line("Basic Materials", 100)],
        )
        self.assertEqual(r.status, LOGI_OPEN)
        r = store.claim_all_logistics(r.id, user_id=6)
        self.assertEqual((r.status, r.claimed_by_user_id), (LOGI_CLAIMED, 6))
        r = store.validate_all_logistics(r.id, user_id=6)
        self.assertEqual(r.status, LOGI_DELIVERED)
        # op linking still works on a (now multi-line) request
        op = store.create_operation(guild_id=1, channel_id=2, name="Op",
                                    scheduled_at=datetime(2026, 7, 1, tzinfo=timezone.utc), leader_user_id=5)
        store.set_logistics_op(r.id, op.id)
        self.assertEqual([x.id for x in store.get_logistics_requests_for_op(op.id)], [r.id])
        store.set_logistics_op(r.id, None)
        self.assertEqual(store.get_logistics_requests_for_op(op.id), [])

    def test_multi_item_per_line_claim_and_validate(self) -> None:
        store = _store()
        r = store.create_logistics_request(
            guild_id=1, channel_id=2, user_id=5,
            items=[_line("Basic Materials", 50), _line("7.62mm", 20), _line("Bandages", 10)],
        )
        self.assertEqual(r.item_count(), 3)
        self.assertEqual(r.status, LOGI_OPEN)
        lids = [ln["lid"] for ln in r.line_items()]
        # Two different drivers claim two different lines → partial (CLAIMED).
        r = store.claim_logistics_line(r.id, lids[0], user_id=6)
        r = store.claim_logistics_line(r.id, lids[1], user_id=7)
        self.assertEqual(r.status, LOGI_CLAIMED)
        self.assertIsNone(r.claimed_by_user_id)  # mixed drivers
        # Validate one line; request stays in progress (line 2 claimed, line 3 open).
        r = store.validate_logistics_line(r.id, lids[0])
        statuses = {ln["lid"]: ln["status"] for ln in r.line_items()}
        self.assertEqual(statuses[lids[0]], LOGI_DELIVERED)
        self.assertEqual(r.status, LOGI_CLAIMED)
        # Manager claims the last open line, then validates everything.
        r = store.claim_logistics_line(r.id, lids[2], user_id=8)
        r = store.validate_all_logistics(r.id, user_id=999, is_manager=True)
        self.assertEqual(r.status, LOGI_DELIVERED)

    def test_revoke_returns_lines_to_open(self) -> None:
        store = _store()
        r = store.create_logistics_request(
            guild_id=1, channel_id=2, user_id=5, items=[_line("Bandages", 10)],
        )
        r = store.claim_all_logistics(r.id, user_id=6)
        self.assertEqual(r.status, LOGI_CLAIMED)
        r = store.revoke_logistics(r.id, user_id=6)
        self.assertEqual(r.status, LOGI_OPEN)

    def test_legacy_single_item_row_synthesizes_line(self) -> None:
        # A request that predates the items list still exposes one line.
        legacy = LogisticsRequest(
            id="old", guild_id=1, channel_id=2, message_id=None,
            category="Resource", subcategory="Material", item="Basic Materials",
            quantity=100, requested_by_user_id=5, status=LOGI_OPEN,
            claimed_by_user_id=None, op_id=None, notes="", created_at="x", updated_at="x",
            items=[],
        )
        lines = legacy.line_items()
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["item"], "Basic Materials")
        self.assertEqual(derive_logi_status(lines), LOGI_OPEN)


class OperationSquadTest(unittest.TestCase):
    def test_capacity_waitlist_promotion_and_lead(self) -> None:
        store = _store()
        op = store.create_operation(
            guild_id=1, channel_id=2, name="Push",
            scheduled_at=datetime(2026, 7, 1, tzinfo=timezone.utc), leader_user_id=1,
            squads=[make_squad("Armor", 2)],
        )
        key = op.squads[0]["key"]
        store.signup_squad(op.id, user_id=11, squad_key=key)
        store.signup_squad(op.id, user_id=12, squad_key=key)
        op, outcome = store.signup_squad(op.id, user_id=13, squad_key=key)
        self.assertEqual(outcome, "waitlist")
        # 11 withdraws → 13 promoted from waitlist
        op = store.withdraw(op.id, user_id=11)
        squad = op.find_squad(key)
        self.assertIn(13, squad["members"])
        self.assertEqual(squad["waitlist"], [])
        # lead assignment + clear on withdraw
        op = store.set_squad_lead(op.id, squad_key=key, user_id=12)
        self.assertEqual(op.find_squad(key)["lead_user_id"], 12)
        op = store.withdraw(op.id, user_id=12)
        self.assertIsNone(op.find_squad(key)["lead_user_id"])

    def test_set_squads_preserves_surviving_members(self) -> None:
        store = _store()
        op = store.create_operation(guild_id=1, channel_id=2, name="P",
                                    scheduled_at=datetime(2026, 7, 1, tzinfo=timezone.utc), leader_user_id=1,
                                    squads=[make_squad("Armor", 0)])
        key = op.squads[0]["key"]
        store.signup_squad(op.id, user_id=11, squad_key=key)
        op = store.set_squads(op.id, squad_defs=[("Armor", 4), ("Air Wing", 2)])
        self.assertEqual(op.find_squad(key)["members"], [11])
        self.assertEqual(op.find_squad(key)["capacity"], 4)
        self.assertEqual(len(op.squads), 2)


class GuildConfigTest(unittest.TestCase):
    def test_faction_and_alert_routing(self) -> None:
        store = _store()
        store.update_guild_config(1, channel_id=100, faction="warden",
                                  stockpile_channel_id=200, ops_channel_id=300)
        self.assertEqual(store.get_guild_faction(1), "warden")
        self.assertEqual(store.get_alert_channel(1, "stockpile"), 200)
        self.assertEqual(store.get_alert_channel(1, "ops"), 300)
        self.assertEqual(store.get_alert_channel(1, "anything-else"), 100)
        store.update_guild_config(2, channel_id=500)
        self.assertEqual(store.get_alert_channel(2, "stockpile"), 500)  # falls back

    def test_relay_channel_round_trips_and_clears(self) -> None:
        store = _store()
        store.update_guild_config(1, channel_id=100, relay_channel_id=777)
        self.assertEqual(store.get_relay_channel(1), 777)
        self.assertEqual(store.get_guild_config(1)["relay_channel_id"], 777)
        # Leaving the lobby clears membership.
        store.update_guild_config(1, relay_channel_id=None)
        self.assertIsNone(store.get_relay_channel(1))


class RelayTest(unittest.TestCase):
    def test_relay_channels_lists_only_joined_guilds(self) -> None:
        store = _store()
        store.update_guild_config(1, channel_id=10, relay_channel_id=111)
        store.update_guild_config(2, channel_id=20, relay_channel_id=222)
        store.update_guild_config(3, channel_id=30)  # configured but not joined
        self.assertEqual(set(store.relay_channels()), {(1, 111), (2, 222)})


class AllyChatTest(unittest.TestCase):
    def test_create_and_lookup(self) -> None:
        store = _store()
        code = store.create_ally_room(guild_id=1, channel_id=100)
        self.assertTrue(code.startswith("ALLY-"))
        self.assertEqual(store.ally_room_by_channel(1, 100), code)
        self.assertEqual(store.ally_members(code), [(1, 100)])

    def test_second_guild_joins(self) -> None:
        store = _store()
        code = store.create_ally_room(guild_id=1, channel_id=100)
        self.assertEqual(store.join_ally_room(2, 200, code), "ok")
        self.assertEqual(set(store.ally_members(code)), {(1, 100), (2, 200)})
        # Case-insensitive code entry works.
        self.assertEqual(store.join_ally_room(3, 300, code.lower()), "ok")
        self.assertEqual(len(store.ally_members(code)), 3)

    def test_multiple_rooms_per_guild(self) -> None:
        store = _store()
        a = store.create_ally_room(guild_id=1, channel_id=100)
        b = store.create_ally_room(guild_id=1, channel_id=101)
        self.assertNotEqual(a, b)
        self.assertEqual({r["room_code"] for r in store.ally_rooms_for_guild(1)}, {a, b})
        self.assertEqual(store.ally_room_by_channel(1, 101), b)

    def test_join_rejections(self) -> None:
        store = _store()
        code = store.create_ally_room(guild_id=1, channel_id=100)
        self.assertEqual(store.join_ally_room(2, 200, "ALLY-NOPE12"), "not_found")
        self.assertEqual(store.join_ally_room(1, 100, code), "already_member")
        # A channel already bound to one room can't join another.
        other = store.create_ally_room(guild_id=2, channel_id=200)
        self.assertEqual(store.join_ally_room(2, 200, code), "channel_in_use")
        self.assertNotEqual(other, code)

    def test_leave_and_purge(self) -> None:
        store = _store()
        code = store.create_ally_room(guild_id=1, channel_id=100)
        store.join_ally_room(2, 200, code)
        store.leave_ally_room(1, code)
        self.assertEqual(store.ally_members(code), [(2, 200)])
        store.create_ally_room(guild_id=2, channel_id=201)
        store.purge_guild(2)
        self.assertEqual(store.ally_rooms_for_guild(2), [])


class AlliedOpTest(unittest.TestCase):
    """Allied ops: one canonical op shared across an ally room, mirrored per server."""

    def _allied_op(self, store):
        # Host (guild 1) + ally (guild 2) share a room; host schedules an allied op.
        code = store.create_ally_room(guild_id=1, channel_id=100)
        store.join_ally_room(2, 200, code)
        op = store.create_operation(
            guild_id=1, channel_id=100, name="Coalition Push",
            scheduled_at=datetime(2026, 7, 1, tzinfo=timezone.utc), leader_user_id=1,
            squads=[make_squad("Armor", 2)], ally_room=code,
        )
        return code, op

    def test_create_persists_room_and_mirrors(self) -> None:
        store = _store()
        code, op = self._allied_op(store)
        self.assertEqual(op.ally_room, code)
        self.assertTrue(op.is_allied)
        store.add_operation_mirror(op.id, 1, 100, 9001)
        store.add_operation_mirror(op.id, 2, 200, 9002)
        mirrors = store.operation_mirrors(op.id)
        self.assertEqual({m["guild_id"] for m in mirrors}, {1, 2})
        self.assertEqual({m["message_id"] for m in mirrors}, {9001, 9002})
        # The ally guild sees it via its mirror; the host does not list it as a member op.
        member_ops = store.operations_for_member_guild(2, open_only=True)
        self.assertEqual([o.id for o in member_ops], [op.id])
        self.assertEqual(store.operations_for_member_guild(1, open_only=True), [])

    def test_rsvp_from_non_host_guild_updates_shared_op(self) -> None:
        store = _store()
        code, op = self._allied_op(store)
        # A user from the ally guild RSVPs — no guild filter, so it lands on the op.
        store.set_rsvp(op.id, user_id=42, state="going")
        store.set_participant_meta(
            op.id, 42, name="Ally Pilot", faction="colonial", guild_id=2, server="Server B"
        )
        op = store.get_operation(op.id)
        self.assertIn(42, op.going)
        self.assertEqual(op.participant_meta["42"]["server"], "Server B")
        # Withdrawing prunes both the bucket and the identity meta.
        op = store.withdraw(op.id, user_id=42)
        self.assertNotIn(42, op.going)
        self.assertNotIn("42", op.participant_meta)

    def test_squad_signup_and_lead_across_guilds(self) -> None:
        store = _store()
        code, op = self._allied_op(store)
        key = op.squads[0]["key"]
        # Signup + lead with no guild filter (the cross-server path).
        store.signup_squad(op.id, user_id=51, squad_key=key)
        op = store.set_squad_lead(op.id, squad_key=key, user_id=51)
        squad = op.find_squad(key)
        self.assertIn(51, squad["members"])
        self.assertEqual(squad["lead_user_id"], 51)

    def test_purge_member_keeps_op_purge_host_removes_it(self) -> None:
        store = _store()
        code, op = self._allied_op(store)
        store.add_operation_mirror(op.id, 1, 100, 9001)
        store.add_operation_mirror(op.id, 2, 200, 9002)
        # Ally leaves: only its mirror row goes; the op (hosted by guild 1) survives.
        store.purge_guild(2)
        self.assertIsNotNone(store.get_operation(op.id))
        self.assertEqual({m["guild_id"] for m in store.operation_mirrors(op.id)}, {1})
        # Host leaves: the op is deleted and its orphaned mirrors are swept.
        store.purge_guild(1)
        self.assertIsNone(store.get_operation(op.id))
        self.assertEqual(store.operation_mirrors(op.id), [])


def _embed_text(embed) -> str:
    parts = [embed.title or "", embed.description or ""]
    for f in embed.fields:
        parts.append(f.name or "")
        parts.append(str(f.value) if f.value is not None else "")
    return "\n".join(parts)


class AlliedOpRenderTest(unittest.TestCase):
    """Cross-server cards must render participants by name, never as a raw <@id>
    that would show as a broken mention in the away servers' copies."""

    def _allied_op(self, store):
        code = store.create_ally_room(guild_id=1, channel_id=100)
        store.join_ally_room(2, 200, code)
        op = store.create_operation(
            guild_id=1, channel_id=100, name="Coalition Push",
            scheduled_at=datetime(2026, 7, 1, tzinfo=timezone.utc), leader_user_id=1,
            ally_room=code,
        )
        return code, op

    def test_creator_renders_by_name_not_mention(self) -> None:
        store = _store()
        _, op = self._allied_op(store)
        # _submit_allied records the creator's identity; mirror that here.
        op = store.set_participant_meta(
            op.id, 1, name="Host Lead", faction="warden", guild_id=1, server="Server A"
        )
        text = _embed_text(operation_card_embed(op))
        self.assertIn("Host Lead", text)
        self.assertNotIn("<@1>", text)

    def test_meta_less_id_falls_back_to_plain_label(self) -> None:
        store = _store()
        _, op = self._allied_op(store)
        # A participant with no recorded meta must not leak a broken mention.
        op = store.set_rsvp(op.id, user_id=77, state="going")
        text = _embed_text(operation_card_embed(op))
        self.assertNotIn("<@77>", text)
        self.assertIn("User 77", text)


class _FakeMessage:
    def __init__(self, mid: int) -> None:
        self.id = mid


class _FakeChannel:
    def __init__(self, *, fail: bool = False, mid: int = 1000) -> None:
        self.fail = fail
        self.mid = mid
        self.sends: list = []

    async def send(self, *args, **kwargs):
        if self.fail:
            raise RuntimeError("missing Send Messages")
        self.sends.append((args, kwargs))
        return _FakeMessage(self.mid)


class _FakeBot:
    """Minimal stand-in exposing only what the loop / fan-out helpers touch."""

    def __init__(self, store: StockpileStore, channels: dict) -> None:
        self.store = store
        self._channels = channels

    def get_channel(self, cid):
        return self._channels.get(cid)

    async def fetch_channel(self, cid):
        ch = self._channels.get(cid)
        if ch is None:
            raise RuntimeError("unknown channel")
        return ch

    def add_view(self, *args, **kwargs) -> None:
        pass

    async def update_stockpile_message(self, stockpile) -> None:
        pass

    async def delete_card_message(self, channel_id, message_id) -> None:
        self.deleted = getattr(self, "deleted", [])
        self.deleted.append((channel_id, message_id))


class ReminderLoopResilienceTest(unittest.IsolatedAsyncioTestCase):
    async def test_one_failing_channel_does_not_abort_the_tick(self) -> None:
        from foxhole_buddy.tasks import reminder_loop

        store = _store()
        store.update_guild_config(1, channel_id=10)  # this guild's send will fail
        store.update_guild_config(2, channel_id=20)  # this one succeeds
        # ~20min of headroom → the "30m" warning (not expiry, so it persists a
        # reminder rather than deleting the row — that's what this test checks).
        past = utc_now() - timedelta(hours=47, minutes=40)
        store.create(guild_id=1, channel_id=10, name="A", location="L",
                     stockpile_type="seaport", user_id=1, now=past)
        sp2 = store.create(guild_id=2, channel_id=20, name="B", location="L",
                           stockpile_type="seaport", user_id=1, now=past)
        bad, good = _FakeChannel(fail=True), _FakeChannel()
        bot = _FakeBot(store, {10: bad, 20: good})

        # Must not raise even though guild 1's channel.send blows up.
        await reminder_loop.coro(bot)

        # Guild 2 was still processed: it got a send and its warning persisted.
        self.assertTrue(good.sends)
        self.assertTrue(store.get(sp2.id).reminders_sent)


class PostAlliedOpResilienceTest(unittest.IsolatedAsyncioTestCase):
    async def test_failed_send_records_no_mirror(self) -> None:
        from foxhole_buddy.core.bot import StockpileBot

        store = _store()
        code = store.create_ally_room(guild_id=1, channel_id=100)
        store.join_ally_room(2, 200, code)
        op = store.create_operation(
            guild_id=1, channel_id=100, name="Push",
            scheduled_at=datetime(2026, 7, 1, tzinfo=timezone.utc), leader_user_id=1,
            ally_room=code,
        )
        host = _FakeChannel(mid=9001)          # host post succeeds
        ally = _FakeChannel(fail=True)         # ally post fails
        bot = _FakeBot(store, {100: host, 200: ally})

        posted = await StockpileBot.post_allied_op(bot, op)

        self.assertEqual(posted, 1)
        mirrors = store.operation_mirrors(op.id)
        # Only the server we actually posted to has a mirror row; the failed one
        # is absent rather than left permanently stale with a NULL message_id.
        self.assertEqual([(m["guild_id"], m["message_id"]) for m in mirrors], [(1, 9001)])
        self.assertEqual(store.get_operation(op.id).message_id, 9001)


class DataAutoDeleteTest(unittest.TestCase):
    """'Done → gone': closed ops / delivered requests are fully removable, and
    deleting an op takes its mirrors with it and unlinks its supply requests."""

    def test_delete_operation_clears_mirrors_and_unlinks_logistics(self) -> None:
        store = _store()
        code = store.create_ally_room(guild_id=1, channel_id=100)
        store.join_ally_room(2, 200, code)
        op = store.create_operation(
            guild_id=1, channel_id=100, name="X",
            scheduled_at=datetime(2026, 7, 1, tzinfo=timezone.utc), leader_user_id=1,
            ally_room=code,
        )
        store.add_operation_mirror(op.id, 1, 100, 9001)
        store.add_operation_mirror(op.id, 2, 200, 9002)
        req = store.create_logistics_request(
            guild_id=1, channel_id=2, user_id=5, items=[_line("Bandages", 10)],
        )
        store.set_logistics_op(req.id, op.id)

        self.assertTrue(store.delete_operation(op.id))
        self.assertIsNone(store.get_operation(op.id))
        self.assertEqual(store.operation_mirrors(op.id), [])  # mirrors went too
        self.assertIsNone(store.get_logistics_request(req.id).op_id)  # request unlinked, not deleted

    def test_delivered_request_is_deletable(self) -> None:
        store = _store()
        r = store.create_logistics_request(
            guild_id=1, channel_id=2, user_id=5,
            items=[_line("Basic Materials", 50), _line("Bandages", 10)],
        )
        for lid in [ln["lid"] for ln in r.line_items()]:
            store.claim_logistics_line(r.id, lid, user_id=6)
        r = store.validate_all_logistics(r.id, user_id=6)
        self.assertEqual(r.status, LOGI_DELIVERED)  # the trigger condition
        self.assertTrue(store.delete_logistics_request(r.id))
        self.assertIsNone(store.get_logistics_request(r.id))


class StockpileExpiryDeleteTest(unittest.IsolatedAsyncioTestCase):
    async def test_expired_stockpile_is_alerted_then_deleted(self) -> None:
        from foxhole_buddy.tasks import reminder_loop

        store = _store()
        store.update_guild_config(1, channel_id=10)
        past = utc_now() - timedelta(hours=49)  # 48h window → already expired
        sp = store.create(guild_id=1, channel_id=10, name="A", location="L",
                          stockpile_type="seaport", user_id=1, now=past)
        ch = _FakeChannel()
        bot = _FakeBot(store, {10: ch})

        await reminder_loop.coro(bot)

        self.assertTrue(ch.sends)            # the Public-Risk alert still went out
        self.assertIsNone(store.get(sp.id))  # ...then the stockpile was cleared


class RelayDisplayNameTest(unittest.TestCase):
    def test_basic_format_with_faction(self) -> None:
        self.assertEqual(relay_display_name("Bob", "Wardens", "warden"), "Bob • Wardens · 🔵 Warden")
        self.assertEqual(relay_display_name("Sue", "Legion", "colonial"), "Sue • Legion · 🟢 Colonial")

    def test_unknown_faction_omits_badge(self) -> None:
        self.assertEqual(relay_display_name("Bob", "Wardens"), "Bob • Wardens")

    def test_truncates_to_webhook_limit_keeping_badge(self) -> None:
        name = relay_display_name("Bob", "X" * 200, "warden")
        self.assertLessEqual(len(name), 80)
        self.assertTrue(name.startswith("Bob • "))
        self.assertTrue(name.endswith("🔵 Warden"))

    def test_long_author_still_within_limit(self) -> None:
        name = relay_display_name("A" * 200, "Wardens", "warden")
        self.assertLessEqual(len(name), 80)


class CatalogFactionFilterTest(unittest.TestCase):
    def test_faction_filter_narrows_items(self) -> None:
        cat = Catalog.load("foxhole_buddy/catalog/seed_catalog.json")
        if cat.is_empty():
            self.skipTest("seed catalog missing")
        total = sum(len(cat.items(c, s)) for c, _ in cat.categories() for s, _ in cat.subcategories(c))
        warden = sum(len(cat.items(c, s, "warden")) for c, _ in cat.categories("warden")
                     for s, _ in cat.subcategories(c, "warden"))
        self.assertLess(warden, total)
        self.assertGreater(warden, 0)


class CatalogSearchTest(unittest.TestCase):
    def setUp(self) -> None:
        self.cat = Catalog.load("foxhole_buddy/catalog/seed_catalog.json")
        if self.cat.is_empty():
            self.skipTest("seed catalog missing")

    def test_search_ranks_and_shapes(self) -> None:
        results = self.cat.search("grenade")
        self.assertTrue(results)
        # Each result carries enough to build a request line.
        for key in ("name", "category_label", "subcategory_label", "crate_amount"):
            self.assertIn(key, results[0])

    def test_prefix_beats_substring(self) -> None:
        # "bandages" starts with "band" → must rank above any mid-word match.
        results = self.cat.search("band")
        self.assertTrue(results)
        self.assertTrue(results[0]["name"].lower().startswith("band"))

    def test_faction_filter_applies(self) -> None:
        everyone = len(self.cat.search("", limit=10000) or [])
        # Empty query returns nothing by contract.
        self.assertEqual(everyone, 0)
        warden = self.cat.search("materials", "warden")
        allf = self.cat.search("materials", None)
        self.assertLessEqual(len(warden), len(allf))

    def test_slang_alias_resolves(self) -> None:
        names = [m["name"] for m in self.cat.search("bmats")]
        self.assertIn("Basic Materials", names)

    def test_suggest_handles_typos(self) -> None:
        self.assertIn("Bandages", [m["name"] for m in self.cat.suggest("bandags")])
        self.assertEqual(self.cat.suggest("zzqqxx"), [])

    def test_best_match_snaps_only_when_confident(self) -> None:
        # Exact, case-insensitive, and slang-alias hits resolve to the canonical name.
        self.assertEqual(self.cat.best_match("Bandages"), "Bandages")
        self.assertEqual(self.cat.best_match("BASIC MATERIALS"), "Basic Materials")
        self.assertEqual(self.cat.best_match("bmats"), "Basic Materials")
        # A close typo still snaps.
        self.assertEqual(self.cat.best_match("component"), "Components")
        # Nonsense stays unmatched so the caller keeps it as a custom entry.
        self.assertIsNone(self.cat.best_match("xyzzynope"))
        self.assertIsNone(self.cat.best_match(""))

    def test_find_returns_entry_for_exact_name(self) -> None:
        entry = self.cat.find("basic materials")  # case-insensitive
        self.assertIsNotNone(entry)
        self.assertEqual(entry["name"], "Basic Materials")
        self.assertIn("category_label", entry)
        self.assertIn("subcategory_label", entry)
        self.assertIsNone(self.cat.find("Basic Material"))  # not exact
        self.assertIsNone(self.cat.find(""))


class CanonicalizeMaterialTest(unittest.TestCase):
    def test_preserves_calibers_and_leading_dot(self) -> None:
        self.assertEqual(canonicalize_material_name("9mm"), "9mm")
        self.assertEqual(canonicalize_material_name("12.7MM Ammunition"), "12.7mm Ammunition")
        self.assertEqual(canonicalize_material_name(".44 mag"), ".44 Mag")

    def test_titlecases_words_and_dedups_case(self) -> None:
        self.assertEqual(canonicalize_material_name("bayonet crates"), "Bayonet Crates")
        self.assertEqual(canonicalize_material_name("BMATS"), canonicalize_material_name("bmats"))

    def test_drops_note_and_trailing_punctuation(self) -> None:
        self.assertEqual(canonicalize_material_name("Hoplites (armored javelin"), "Hoplites")
        self.assertEqual(canonicalize_material_name("Smelters."), "Smelters")


class QuantityListParseTest(unittest.TestCase):
    def test_parses_the_users_example(self) -> None:
        raw = (
            "10 Smelters.\n"
            "2 Siginatari\n"
            "4 hoplites (armored variant of javelin\n"
            "15 Haulers \n"
            "6 Pelfasts)"
        )
        items, skipped = parse_quantity_list(raw)
        self.assertEqual(
            items,
            [("Smelters", 10.0), ("Siginatari", 2.0), ("Hoplites", 4.0),
             ("Haulers", 15.0), ("Pelfasts", 6.0)],
        )
        self.assertEqual(skipped, [])

    def test_number_may_trail_and_carry_commas_decimals(self) -> None:
        items, _ = parse_quantity_list("Bmats 1,000\n10.5 Diesel\n10x Gmats")
        self.assertEqual(items, [("Bmats", 1000.0), ("Diesel", 10.5), ("Gmats", 10.0)])

    def test_duplicates_are_summed_in_first_seen_order(self) -> None:
        items, _ = parse_quantity_list("5 Bmats\n3 Rmats\n5 bmats")
        self.assertEqual(items, [("Bmats", 10.0), ("Rmats", 3.0)])

    def test_bad_lines_are_skipped_not_fatal(self) -> None:
        items, skipped = parse_quantity_list("10 Bmats\njust text\n0 Nope\n\n42")
        self.assertEqual(items, [("Bmats", 10.0)])
        self.assertEqual(skipped, ["just text", "0 Nope", "42"])

    def test_the_real_failing_paste(self) -> None:
        # Calibers embedded in names, a leading-dot name, a lowercase-mm name,
        # and five comma-separated items on one line — the paste that broke.
        raw = (
            "65 12.7mm Ammunition\n"
            "3 .44\n"
            "65 7.62mm Ammunition\n"
            "3 7.92\n"
            "8 9mm\n"
            "5 Bayonet Crates\n"
            "10 Cartena, 20 Fuscina, 10 Argenti, 10 Volta, 10 Omen"
        )
        items, skipped = parse_quantity_list(raw)
        self.assertEqual(
            items,
            [("12.7mm Ammunition", 65.0), (".44", 3.0), ("7.62mm Ammunition", 65.0),
             ("7.92", 3.0), ("9mm", 8.0), ("Bayonet Crates", 5.0),
             ("Cartena", 10.0), ("Fuscina", 20.0), ("Argenti", 10.0),
             ("Volta", 10.0), ("Omen", 10.0)],
        )
        self.assertEqual(skipped, [])

    def test_thousands_comma_is_not_a_separator(self) -> None:
        items, _ = parse_quantity_list("1,000 Bmats, 20 Rmats")
        self.assertEqual(items, [("Bmats", 1000.0), ("Rmats", 20.0)])

    def test_caliber_in_name_not_read_as_quantity(self) -> None:
        # Quantity trails here; the leading caliber token must stay in the name.
        items, _ = parse_quantity_list("12.7mm Ammunition 65\n9mm 8")
        self.assertEqual(items, [("12.7mm Ammunition", 65.0), ("9mm", 8.0)])


class PlanRemovalsTest(unittest.TestCase):
    def test_partitions_clean_notfound_and_over(self) -> None:
        inventory = {"Basic Materials": 100.0, "Diesel": 30.0}
        # resolve() maps aliases/case to stored keys, like the modal does.
        resolve = {"bmats": "Basic Materials", "diesel": "Diesel"}.get
        items = [("bmats", 40.0), ("diesel", 50.0), ("nope", 5.0)]
        to_remove, not_found, over = plan_removals(inventory, items, lambda n: resolve(n) or n)
        self.assertEqual(to_remove, [("Basic Materials", 40.0)])
        self.assertEqual(not_found, ["nope"])
        self.assertEqual(over, [("Diesel", 50.0, 30.0)])


class BulkRemoveStoreTest(unittest.TestCase):
    def test_apply_removals_and_zero_out(self) -> None:
        store = _store()
        store.add_to_base_inventory(1, "Basic Materials", 100)
        store.add_to_base_inventory(1, "Diesel", 30)

        inv = store.get_base_inventory(1)
        items = [("bmats", 40.0), ("Diesel", 50.0)]
        resolve = {"bmats": "Basic Materials"}.get
        to_remove, _nf, over = plan_removals(inv, items, lambda n: resolve(n) or canonicalize_material_name(n))
        for key, qty in to_remove:
            store.remove_from_base_inventory(1, key, qty)
        self.assertEqual(store.get_base_inventory(1)["Basic Materials"], 60.0)

        # Zero-out path for the over item: remove all that's available -> row gone.
        for key, _req, avail in over:
            store.remove_from_base_inventory(1, key, avail)
        self.assertNotIn("Diesel", store.get_base_inventory(1))


class CartBulkTest(unittest.TestCase):
    def setUp(self) -> None:
        self.cat = Catalog.load("foxhole_buddy/catalog/seed_catalog.json")
        if self.cat.is_empty():
            self.skipTest("seed catalog missing")

    def test_bulk_add_resolves_catalog_and_custom(self) -> None:
        draft = LogisticsDraft()
        added = apply_cart_bulk_add(draft, [("bmats", 20.0), ("zzznope", 3.0)], self.cat)
        self.assertEqual(added[0], ("Basic Materials", 20, False))
        self.assertEqual(added[1], ("zzznope", 3, True))
        self.assertEqual(len(draft.lines), 2)
        self.assertEqual(draft.lines[0]["item"], "Basic Materials")
        self.assertEqual(draft.lines[1]["category"], CUSTOM_CATEGORY)

    def test_bulk_remove_reduces_and_reports_misses(self) -> None:
        draft = LogisticsDraft()
        apply_cart_bulk_add(draft, [("bmats", 20.0)], self.cat)
        not_found = apply_cart_bulk_remove(draft, [("bmats", 5.0), ("ghost", 1.0)], self.cat)
        self.assertEqual(draft.lines[0]["quantity"], 15)
        self.assertEqual(not_found, ["ghost"])
        # Removing the rest drops the line entirely.
        apply_cart_bulk_remove(draft, [("bmats", 15.0)], self.cat)
        self.assertEqual(draft.lines, [])


class LinePickerPaginationTest(unittest.TestCase):
    def test_paginates_beyond_the_25_option_cap(self) -> None:
        import types
        from foxhole_buddy.ui.views.logistics import LinePickerView

        store = _store()
        items = [make_line("Resource", "Material", f"Item {i:02d}", 1) for i in range(30)]
        req = store.create_logistics_request(guild_id=1, channel_id=2, user_id=1, items=items)
        bot = types.SimpleNamespace(store=store)

        view = LinePickerView(bot, req.id, "claim")
        self.assertEqual(view.total, 30)     # all 30 are reachable, not capped at 25
        self.assertEqual(view.pages, 2)      # 25 + 5
        self.assertEqual(view.page, 0)
        # Page index is clamped to the last page when asked for one past the end.
        last = LinePickerView(bot, req.id, "claim", page=99)
        self.assertEqual(last.page, 1)
        # Nothing is claimed yet, so the validate picker is empty.
        self.assertEqual(LinePickerView(bot, req.id, "validate").total, 0)


class CartPickerPaginationTest(unittest.TestCase):
    def test_remove_and_search_pickers_paginate(self) -> None:
        import types
        from foxhole_buddy.ui.views.logistics import (
            LogisticsDraft, RemoveLineView, SearchResultView,
        )

        bot = types.SimpleNamespace()
        draft = LogisticsDraft()
        draft.lines = [make_line("Resource", "Material", f"Item {i:02d}", 1) for i in range(30)]

        rv = RemoveLineView(bot, draft)
        self.assertEqual(rv.pages, 2)          # 30 lines → 2 pages, none hidden
        self.assertEqual(len(rv.select.options), 25)
        self.assertEqual(RemoveLineView(bot, draft, page=99).page, 1)  # clamped
        self.assertEqual(len(RemoveLineView(bot, draft, page=1).select.options), 5)

        matches = [
            {"name": f"Match {i:02d}", "category_label": "C", "subcategory_label": "S"}
            for i in range(40)
        ]
        sv = SearchResultView(bot, draft, matches, quantity=1)
        self.assertEqual(sv.pages, 2)
        self.assertEqual(len(sv.select.options), 25)
        # The lookup map covers every match across pages, so page-2 picks resolve.
        self.assertEqual(len(sv.matches), 40)
        self.assertEqual(len(SearchResultView(bot, draft, matches, 1, page=1).select.options), 15)


class PurgeTest(unittest.TestCase):
    def test_purge_removes_all_guild_data(self) -> None:
        store = _store()
        store.update_guild_config(1, channel_id=1)
        store.create(guild_id=1, channel_id=2, name="A", location="L", stockpile_type="seaport", user_id=1)
        store.create_logistics_request(guild_id=1, channel_id=2, user_id=1, items=[_line("i", 1, "c", "s")])
        store.create_operation(guild_id=1, channel_id=2, name="op",
                               scheduled_at=datetime(2026, 7, 1, tzinfo=timezone.utc), leader_user_id=1)
        store.create(guild_id=2, channel_id=2, name="B", location="L", stockpile_type="seaport", user_id=1)
        store.purge_guild(1)
        self.assertEqual(store.known_guild_ids(), {2})


class OffsiteInventoryTest(unittest.TestCase):
    def test_location_crud_and_isolation(self) -> None:
        store = _store()
        depot = store.create_offsite_location(
            guild_id=1, name="West Depot", location="Callahan", type="storage_depot", user_id=1
        )
        port = store.create_offsite_location(
            guild_id=1, name="North Port", location="Origin", type="seaport", user_id=1
        )
        # Guild-scoped listing, alphabetical by name.
        names = [loc.name for loc in store.list_offsite_locations(1)]
        self.assertEqual(names, ["North Port", "West Depot"])
        self.assertEqual(store.list_offsite_locations(2), [])
        # Cross-guild get returns None.
        self.assertIsNone(store.get_offsite_location(depot.id, guild_id=2))

        # Items are keyed per-location: same material stays independent.
        # (The store canonicalizes the name but does not catalog-snap; that's the
        # modal layer's job, so "Basic Materials" is what lands here.)
        store.add_to_offsite_inventory(1, depot.id, "Basic Materials", 100)
        store.add_to_offsite_inventory(1, port.id, "Basic Materials", 40)
        self.assertEqual(store.get_offsite_inventory(depot.id)["Basic Materials"], 100)
        self.assertEqual(store.get_offsite_inventory(port.id)["Basic Materials"], 40)

        # Accumulate, decrement, and delete-at-zero.
        store.add_to_offsite_inventory(1, depot.id, "Basic Materials", 50)
        self.assertEqual(store.get_offsite_inventory(depot.id)["Basic Materials"], 150)
        store.remove_from_offsite_inventory(depot.id, "Basic Materials", 150)
        self.assertNotIn("Basic Materials", store.get_offsite_inventory(depot.id))

        # Error contract matches base inventory.
        with self.assertRaises(KeyError):
            store.remove_from_offsite_inventory(depot.id, "Diesel", 1)
        store.add_to_offsite_inventory(1, depot.id, "Diesel", 10)
        with self.assertRaises(ValueError):
            store.remove_from_offsite_inventory(depot.id, "Diesel", 999)

    def test_rename_clear_and_delete(self) -> None:
        store = _store()
        loc = store.create_offsite_location(
            guild_id=1, name="Old", location="X", type="base", user_id=1
        )
        store.add_to_offsite_inventory(1, loc.id, "Diesel", 5)

        loc.name = "New"
        loc.location = "Y"
        store.update_offsite_location(loc)
        self.assertEqual(store.get_offsite_location(loc.id).name, "New")

        store.clear_offsite_inventory(loc.id)
        self.assertEqual(store.get_offsite_inventory(loc.id), {})

        store.add_to_offsite_inventory(1, loc.id, "Diesel", 5)
        self.assertTrue(store.delete_offsite_location(loc.id, guild_id=1))
        self.assertIsNone(store.get_offsite_location(loc.id))
        # Child rows go with the location — no orphans.
        self.assertEqual(store.get_offsite_inventory(loc.id), {})
        # Deleting a non-existent / wrong-guild location is a no-op.
        self.assertFalse(store.delete_offsite_location(loc.id, guild_id=1))

    def test_plan_removals_against_offsite_inventory(self) -> None:
        store = _store()
        loc = store.create_offsite_location(
            guild_id=1, name="D", location="X", type="seaport", user_id=1
        )
        store.add_to_offsite_inventory(1, loc.id, "Basic Materials", 100)
        store.add_to_offsite_inventory(1, loc.id, "Diesel", 30)

        inv = store.get_offsite_inventory(loc.id)
        items = [("bmats", 40.0), ("Diesel", 50.0), ("ghost", 1.0)]
        resolve = {"bmats": "Basic Materials"}.get
        to_remove, not_found, over = plan_removals(
            inv, items, lambda n: resolve(n) or canonicalize_material_name(n)
        )
        self.assertEqual(to_remove, [("Basic Materials", 40.0)])
        self.assertEqual(not_found, ["ghost"])
        self.assertEqual(over, [("Diesel", 50.0, 30)])

    def test_purge_removes_offsite_tables(self) -> None:
        store = _store()
        loc = store.create_offsite_location(
            guild_id=1, name="D", location="X", type="base", user_id=1
        )
        store.add_to_offsite_inventory(1, loc.id, "Diesel", 5)
        store.purge_guild(1)
        self.assertEqual(store.list_offsite_locations(1), [])
        self.assertEqual(store.get_offsite_inventory(loc.id), {})


class DutyRosterTest(unittest.TestCase):
    def _stockpile(self, store):
        return store.create(
            guild_id=1, channel_id=2, name="West Depot", location="Callahan",
            stockpile_type="storage_depot", user_id=1,
        )

    def test_set_dedup_and_persist(self) -> None:
        store = _store()
        sp = self._stockpile(store)
        self.assertEqual(sp.duty_user_ids, [])  # empty on create

        updated = store.set_stockpile_duty(sp.id, [10, 20, 10, 30], guild_id=1)
        self.assertEqual(updated.duty_user_ids, [10, 20, 30])  # deduped, order kept
        # Persisted + decoded back as a list of ints.
        self.assertEqual(store.get(sp.id).duty_user_ids, [10, 20, 30])

        # Clearing works.
        store.set_stockpile_duty(sp.id, [], guild_id=1)
        self.assertEqual(store.get(sp.id).duty_user_ids, [])

    def test_roster_survives_refresh(self) -> None:
        store = _store()
        sp = self._stockpile(store)
        store.set_stockpile_duty(sp.id, [42], guild_id=1)
        # A refresh resets the timer/reminders but must NOT drop the roster.
        store.refresh(sp.id, user_id=1, guild_id=1)
        self.assertEqual(store.get(sp.id).duty_user_ids, [42])

    def test_set_unknown_raises_and_guild_scoped(self) -> None:
        store = _store()
        sp = self._stockpile(store)
        with self.assertRaises(KeyError):
            store.set_stockpile_duty("nope", [1], guild_id=1)
        # Wrong guild is treated as not found.
        with self.assertRaises(KeyError):
            store.set_stockpile_duty(sp.id, [1], guild_id=999)


if __name__ == "__main__":
    unittest.main()


class WikiDexTest(unittest.TestCase):
    """The /s wiki encyclopedia: sync normalization, search, embed rendering."""

    @staticmethod
    def _document():
        from foxhole_buddy.wikidex.sync import build_wikidex
        table_rows = {
            "item": [
                {"name": "7.62mm", "category": "Small Arms", "type": "Magazine",
                 "faction": "Both", "damage": "45", "damage_type": "Light Kinetic",
                 "encumbrance": "10", "crate_amount": "40", "fire_rate": "",
                 "image": "RifleAmmoItemIcon.png"},
                {"name": "Basic Materials", "category": "Resources", "faction": "",
                 "crate_amount": "100", "uses": "[[Garrison House|Garrisons]]"},
                # Cargo returns some keys with spaces instead of underscores.
                {"name": "Spaced Keys", "category": "Test", "crate amount": "5"},
                {"name": ""},  # nameless rows are dropped
            ],
            "vehicle": [
                {"name": "Gallagher Outlaw Mk. II", "type": "Light Tank",
                 "faction": "War", "vehicle_hp": "2350", "crew": "3",
                 "aliases": "Outlaw,GO2"},
            ],
            "structure": [],
        }
        production_rows = [
            {"Output": "7.62mm", "Source": "Factory", "InputItem1": "Basic Materials",
             "InputItem1Amount": "80", "OutputAmount": "1", "ProductionTime": "50",
             "IsMPFable": "1"},
            {"Output": "Nobody", "Source": "Factory"},  # no inputs -> dropped
        ]
        armament_rows = [
            {"parent_name": "Gallagher Outlaw Mk. II", "ArmamentName": "40mm Cannon",
             "AmmoName1": "40mm", "RangeMax": "45", "MagazineSize": "1"},
            {"parent_name": "Nobody", "ArmamentName": ""},  # nameless -> dropped
        ]
        now = datetime(2026, 7, 1, tzinfo=timezone.utc)
        return build_wikidex(
            table_rows, production_rows, fetched_at=now, armament_rows=armament_rows
        )

    def _dex(self):
        from foxhole_buddy.wikidex import WikiDex
        return WikiDex(self._document())

    def test_build_normalizes_and_merges_production(self) -> None:
        doc = self._document()
        self.assertEqual(doc["entry_count"], 4)
        seven = next(e for e in doc["entries"] if e["name"] == "7.62mm")
        # Empty fields dropped, non-empty kept as-is.
        self.assertNotIn("fire_rate", seven["stats"])
        self.assertEqual(seven["stats"]["damage"], "45")
        # Production recipe merged onto the right entry.
        self.assertEqual(seven["production"][0]["inputs"], [["Basic Materials", 80]])
        self.assertTrue(seven["production"][0]["mpf"])
        # Wikitext links stripped; space-keys normalized; wiki aliases split.
        bmats = next(e for e in doc["entries"] if e["name"] == "Basic Materials")
        self.assertEqual(bmats["stats"]["uses"], "Garrisons")
        self.assertNotIn("production", bmats)
        spaced = next(e for e in doc["entries"] if e["name"] == "Spaced Keys")
        self.assertEqual(spaced["stats"]["crate_amount"], "5")
        outlaw = next(e for e in doc["entries"] if e["kind"] == "vehicle")
        self.assertEqual(outlaw["aliases"], ["Outlaw", "GO2"])
        self.assertEqual(outlaw["faction"], ["warden"])
        # Armament rows joined by parent_name; nameless weapons dropped.
        self.assertEqual(outlaw["armament"][0]["name"], "40mm Cannon")
        self.assertEqual(outlaw["armament"][0]["AmmoName1"], "40mm")

    def test_search_ranking_and_aliases(self) -> None:
        dex = self._dex()
        # Exact beats substring; prefix works mid-word.
        self.assertEqual(dex.search("7.62mm")[0]["name"], "7.62mm")
        self.assertEqual(dex.search("7.6")[0]["name"], "7.62mm")
        # Wiki alias ("Outlaw") and community slang ("bmats") both resolve.
        self.assertEqual(dex.search("outlaw")[0]["name"], "Gallagher Outlaw Mk. II")
        self.assertEqual(dex.search("bmats")[0]["name"], "Basic Materials")
        # Fuzzy fallback catches typos.
        self.assertEqual(dex.suggest("basic materails")[0]["name"], "Basic Materials")
        self.assertEqual(dex.search("zzzz"), [])
        # Exact get round-trips autocomplete values case-insensitively.
        self.assertEqual(dex.get("7.62MM")["name"], "7.62mm")
        self.assertIsNone(dex.get("nope"))

    def test_wiki_urls(self) -> None:
        from foxhole_buddy.wikidex import wiki_image_url, wiki_page_url
        # Known-good hash path (verified against the live wiki).
        self.assertEqual(
            wiki_image_url("RifleAmmoItemIcon.png"),
            "https://foxhole.wiki.gg/images/0/0b/RifleAmmoItemIcon.png",
        )
        self.assertEqual(
            wiki_page_url("Gallagher Outlaw Mk. II"),
            "https://foxhole.wiki.gg/wiki/Gallagher_Outlaw_Mk._II",
        )

    def test_embed_shows_only_present_stats(self) -> None:
        from foxhole_buddy.ui.embeds import wiki_entry_embed
        dex = self._dex()
        seven = dex.get("7.62mm")
        embed = wiki_entry_embed(seven, page_url="https://x", image_url=None,
                                 fetched_at=dex.fetched_at)
        text = " ".join(f"{f.name} {f.value}" for f in embed.fields)
        self.assertIn("Damage:** 45", text)
        self.assertIn("Crate Amount:** 40", text)
        self.assertNotIn("Fire Rate", text)  # empty stat never renders
        self.assertIn("Factory:** 80 × Basic Materials → 1 (50s) · MPF", text)
        self.assertIn("synced 2026-07-01", embed.footer.text)
        # Vehicle card gets armament, no combat/logistics leakage from items.
        outlaw = dex.get("Gallagher Outlaw Mk. II")
        embed = wiki_entry_embed(outlaw, page_url="https://x", image_url=None)
        text = " ".join(f"{f.name} {f.value}" for f in embed.fields)
        self.assertIn("40mm Cannon", text)
        self.assertIn("Vehicle Hp:** 2350", text)

    def test_seed_fallback_and_load(self) -> None:
        from foxhole_buddy.wikidex import WikiDex
        # Missing cache path -> committed seed snapshot keeps /s alive.
        dex = WikiDex.load(Path(tempfile.mkdtemp()) / "missing.json")
        self.assertFalse(dex.is_empty())
        self.assertIsNotNone(dex.get("7.62mm"))


class OpsForumThreadTest(unittest.TestCase):
    """Forum-post ops: binding a thread to an op + the schema migration."""

    def test_attach_operation_thread_binds_card_to_thread(self) -> None:
        s = _store()
        op = s.create_operation(
            guild_id=1, channel_id=100, name="Assault",
            scheduled_at=datetime(2026, 7, 1, tzinfo=timezone.utc), leader_user_id=7,
        )
        self.assertIsNone(op.thread_id)  # not a thread op until bound
        bound = s.attach_operation_thread(op.id, thread_id=999, message_id=777)
        # channel_id now points at the forum thread so edits/reminders target it.
        self.assertEqual(bound.thread_id, 999)
        self.assertEqual(bound.channel_id, 999)
        self.assertEqual(bound.message_id, 777)
        # Survives a reload (decode round-trip).
        self.assertEqual(s.get_operation(op.id).thread_id, 999)

    def test_migration_adds_thread_id_to_old_operations_table(self) -> None:
        import sqlite3
        from foxhole_buddy.core.store import StockpileStore
        path = Path(tempfile.mkdtemp()) / "old.db"
        conn = sqlite3.connect(path)
        # An operations table predating this feature (no thread_id column).
        conn.executescript(
            "CREATE TABLE operations (id TEXT PRIMARY KEY, op_number INTEGER,"
            " guild_id INTEGER NOT NULL, channel_id INTEGER, message_id INTEGER, name TEXT,"
            " description TEXT, location TEXT, war_number INTEGER, scheduled_at TEXT,"
            " leader_user_id INTEGER, status TEXT, squads TEXT, going TEXT, tentative TEXT,"
            " not_available TEXT, warned_30m INTEGER, warned_start INTEGER, created_at TEXT,"
            " ally_room TEXT, participant_meta TEXT);"
        )
        conn.commit()
        conn.close()
        # Reopening runs the lightweight migration; thread_id must appear.
        store = StockpileStore(path)
        op = store.create_operation(
            guild_id=1, channel_id=10, name="X",
            scheduled_at=datetime(2026, 7, 1, tzinfo=timezone.utc), leader_user_id=5,
        )
        op = store.attach_operation_thread(op.id, thread_id=88, message_id=99)
        self.assertEqual(store.get_operation(op.id).thread_id, 88)


class StockpileInvTest(unittest.TestCase):
    """Unified Stockpile/Inv: per-stockpile inventory, timer-less entries,
    forum-thread binding, the embed, and the legacy migration."""

    def test_stockpile_inventory_crud_and_dedup(self) -> None:
        s = _store()
        sp = s.create(guild_id=1, channel_id=9, name="Depot", location="L",
                      stockpile_type="storage_depot", user_id=1)
        s.add_to_stockpile_inventory(1, sp.id, "Basic Materials", 100)
        s.add_to_stockpile_inventory(1, sp.id, "basic materials", 50)  # canonicalized merge
        inv = s.get_stockpile_inventory(sp.id)
        self.assertEqual(inv["Basic Materials"], 150.0)
        s.remove_from_stockpile_inventory(sp.id, "Basic Materials", 150)
        self.assertNotIn("Basic Materials", s.get_stockpile_inventory(sp.id))
        with self.assertRaises(KeyError):
            s.remove_from_stockpile_inventory(sp.id, "Diesel", 1)
        # Deleting the stockpile clears its inventory too.
        s.add_to_stockpile_inventory(1, sp.id, "Diesel", 5)
        s.delete(sp.id)
        self.assertEqual(s.get_stockpile_inventory(sp.id), {})

    def test_timerless_flag_persists_and_gates_reminders(self) -> None:
        from foxhole_buddy.core.models import warning_due
        s = _store()
        past = datetime(2000, 1, 1, tzinfo=timezone.utc)
        inv = s.create(guild_id=1, channel_id=9, name="Base", location="",
                       stockpile_type="storage_depot", user_id=1, track_expiry=False, now=past)
        timed = s.create(guild_id=1, channel_id=9, name="SP", location="L",
                         stockpile_type="seaport", user_id=1, now=past)
        self.assertFalse(s.get(inv.id).track_expiry)
        self.assertTrue(s.get(timed.id).track_expiry)
        # The loop skips on `track_expiry` before ever calling warning_due; assert
        # that flag is the gate (both are long expired by warning_due itself).
        self.assertEqual(warning_due(s.get(timed.id)), "expired")
        self.assertFalse(s.get(inv.id).track_expiry)

    def test_attach_stockpile_thread_binds_card(self) -> None:
        s = _store()
        sp = s.create(guild_id=1, channel_id=9, name="SP", location="L",
                      stockpile_type="seaport", user_id=1)
        bound = s.attach_stockpile_thread(sp.id, thread_id=555, message_id=777)
        self.assertEqual(bound.thread_id, 555)
        self.assertEqual(bound.channel_id, 555)
        self.assertEqual(bound.message_id, 777)
        self.assertEqual(s.get(sp.id).thread_id, 555)

    def test_embed_inventory_and_timerless_variant(self) -> None:
        from foxhole_buddy.ui.embeds import stockpile_embed
        s = _store()
        inv_sp = s.create(guild_id=1, channel_id=9, name="Base", location="",
                          stockpile_type="storage_depot", user_id=1, track_expiry=False)
        emb = stockpile_embed(inv_sp, {"Bmats": 40})
        names = [f.name for f in emb.fields]
        self.assertIn("📋 Inventory", names)
        self.assertNotIn("Timer", names)  # timer-less hides the countdown
        self.assertIn("Bmats", " ".join(f.value for f in emb.fields))
        # Timered card keeps the timer AND shows inventory.
        timed = s.create(guild_id=1, channel_id=9, name="SP", location="L",
                         stockpile_type="seaport", user_id=1)
        emb2 = stockpile_embed(timed, {"Rmats": 10})
        names2 = [f.name for f in emb2.fields]
        self.assertIn("Timer", names2)
        self.assertIn("📋 Inventory", names2)

    def test_migration_offsite_and_base_to_stockpiles(self) -> None:
        import sqlite3
        from foxhole_buddy.core.store import StockpileStore
        path = Path(tempfile.mkdtemp()) / "legacy.db"
        conn = sqlite3.connect(path)
        conn.executescript(
            "CREATE TABLE stockpiles (id TEXT PRIMARY KEY, guild_id INTEGER NOT NULL,"
            " channel_id INTEGER, message_id INTEGER, name TEXT, location TEXT, type TEXT,"
            " created_by_user_id INTEGER, last_refreshed_at TEXT, expires_at TEXT,"
            " last_refreshed_by_user_id INTEGER, reminders_sent TEXT, created_at TEXT,"
            " updated_at TEXT, duty_user_ids TEXT);"
            "CREATE TABLE offsite_locations (id TEXT PRIMARY KEY, guild_id INTEGER NOT NULL,"
            " name TEXT, location TEXT, type TEXT, created_by_user_id INTEGER, created_at TEXT,"
            " updated_at TEXT);"
            "CREATE TABLE offsite_inventory (location_id TEXT NOT NULL, guild_id INTEGER NOT NULL,"
            " material TEXT NOT NULL, amount REAL NOT NULL, PRIMARY KEY (location_id, material));"
            "CREATE TABLE base_inventory (guild_id INTEGER NOT NULL, material TEXT NOT NULL,"
            " amount REAL NOT NULL, PRIMARY KEY (guild_id, material));"
            "INSERT INTO stockpiles VALUES ('sp1',1,10,NULL,'Timer','L','seaport',5,"
            " '2026-07-01T00:00:00+00:00','2026-07-03T00:00:00+00:00',5,'[]',"
            " '2026-07-01T00:00:00+00:00','2026-07-01T00:00:00+00:00','[]');"
            "INSERT INTO offsite_locations VALUES ('loc1',1,'Front','Kuoppa','storage_depot',7,"
            " '2026-07-01T00:00:00+00:00','2026-07-01T00:00:00+00:00');"
            "INSERT INTO offsite_inventory VALUES ('loc1',1,'Basic Materials',120);"
            "INSERT INTO base_inventory VALUES (1,'Bmats',50);"
        )
        conn.commit()
        conn.close()
        store = StockpileStore(path)
        by_id = {sp.id: sp for sp in store.all(guild_id=1)}
        # Legacy timered stockpile keeps its timer (backfilled, not NULL->False).
        self.assertTrue(by_id["sp1"].track_expiry)
        # Off-site location became a timer-less stockpile carrying its items.
        self.assertFalse(by_id["loc1"].track_expiry)
        self.assertEqual(store.get_stockpile_inventory("loc1")["Basic Materials"], 120.0)
        # Base inventory folded into one deterministic timer-less stockpile.
        self.assertFalse(by_id["base-1"].track_expiry)
        self.assertEqual(store.get_stockpile_inventory("base-1")["Bmats"], 50.0)
        # Old rows drained; re-opening is a no-op (still exactly 3 stockpiles).
        self.assertEqual(len(StockpileStore(path).all(guild_id=1)), 3)


class StockpileOrphanRecoveryTest(unittest.TestCase):
    """A stockpile_inventory row whose parent stockpile is missing (e.g. an
    over-eager expiry deleted it before the timer-less skip existed) is
    recovered into a fresh timer-less parent on the next open."""

    def test_orphaned_inventory_gets_a_parent(self) -> None:
        import sqlite3
        from foxhole_buddy.core.store import StockpileStore
        path = Path(tempfile.mkdtemp()) / "orphan.db"
        StockpileStore(path)  # create full current schema
        conn = sqlite3.connect(path)
        conn.executescript(
            "INSERT INTO stockpile_inventory VALUES ('base-42', 42, 'Bmats', 55);"
            "INSERT INTO stockpile_inventory VALUES ('lost99', 42, 'Diesel', 10);"
        )
        conn.commit()
        conn.close()
        store = StockpileStore(path)  # re-open → recovery runs
        ids = {sp.id: sp for sp in store.all(guild_id=42)}
        self.assertIn("base-42", ids)
        self.assertIn("lost99", ids)
        self.assertFalse(ids["base-42"].track_expiry)  # recovered as inventory-only
        self.assertEqual(ids["base-42"].name, "Base Inventory")
        self.assertEqual(ids["lost99"].name, "Recovered Inventory")
        self.assertEqual(store.get_stockpile_inventory("base-42")["Bmats"], 55.0)
