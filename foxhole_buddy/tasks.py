import os
import logging
import discord
from discord.ext import tasks
from collections import defaultdict
from typing import TYPE_CHECKING
from foxhole_buddy.core.store import (
    utc_now, warning_due, mark_warning_sent,
    OP_SCHEDULED, URGENT_WARNING,
)
from foxhole_buddy.catalog import Catalog, sync as catalog_sync
from foxhole_buddy.wikidex import WikiDex
from foxhole_buddy.wikidex import sync as wikidex_sync
from foxhole_buddy.utils import foxhole_api
from foxhole_buddy.utils.formatting import stockpile_status, unix_ts, stockpile_type_label
from foxhole_buddy.ui.embeds import _inventory_table

if TYPE_CHECKING:
    from foxhole_buddy.core.bot import StockpileBot
    from foxhole_buddy.core.store import Stockpile

log = logging.getLogger("foxhole_buddy.tasks")

CATALOG_SYNC_HOURS = int(os.getenv("CATALOG_SYNC_HOURS", "48"))
WAR_SYNC_HOURS = int(os.getenv("WAR_SYNC_HOURS", "24"))
WIKIDEX_SYNC_HOURS = int(os.getenv("WIKIDEX_SYNC_HOURS", "168"))  # weekly


@tasks.loop(hours=WAR_SYNC_HOURS)
async def war_sync_loop(bot: "StockpileBot") -> None:
    """Refresh the current Foxhole war number at most once a day.

    The war number rarely changes, so this is polite to the public API; any
    failure keeps the last cached value (operations just omit it if unknown).
    """
    _, fetched = foxhole_api.load_cache(bot.war_path)
    if not foxhole_api.cache_is_stale(fetched):
        return
    number = await foxhole_api.fetch_war_number()
    if number is not None:
        bot.war_number = number
        foxhole_api.save_cache(bot.war_path, number, utc_now())
        log.info("Current war number: %s", number)
    else:
        log.warning("Could not fetch war number; keeping cached value.")

    maps = await foxhole_api.fetch_maps()
    if maps:
        bot.war_maps = maps


@tasks.loop(hours=CATALOG_SYNC_HOURS)
async def catalog_sync_loop(bot: "StockpileBot") -> None:
    """Refresh the wiki-synced item catalog on a slow cadence.

    Fires immediately on startup, but only actually hits the wiki when the
    cached catalog is older than the sync window — so restarts don't hammer
    the wiki. On any failure the previous catalog is kept.
    """
    age = bot.catalog.age_seconds()
    if age is not None and age < CATALOG_SYNC_HOURS * 3600:
        return
    try:
        await catalog_sync.refresh_catalog(bot.catalog_path, fetched_at=utc_now())
        bot.catalog = Catalog.load(bot.catalog_path)
        log.info("Synced %d catalog items from the Foxhole wiki.", bot.catalog.item_count)
    except Exception as exc:  # noqa: BLE001 — never let a sync failure crash the loop
        log.warning("Catalog sync failed (%r); keeping existing catalog.", exc)


@tasks.loop(hours=WIKIDEX_SYNC_HOURS)
async def wikidex_sync_loop(bot: "StockpileBot") -> None:
    """Refresh the /s wiki encyclopedia weekly.

    Fires immediately on startup, but only actually hits the wiki when the
    cached dex is older than the sync window — so restarts don't hammer the
    wiki. On any failure the previous cache (or seed) is kept.
    """
    age = bot.wikidex.age_seconds()
    if age is not None and age < WIKIDEX_SYNC_HOURS * 3600:
        return
    try:
        await wikidex_sync.refresh_wikidex(bot.wikidex_path, fetched_at=utc_now())
        bot.wikidex = WikiDex.load(bot.wikidex_path)
        log.info("Synced %d wikidex entries from the Foxhole wiki.", bot.wikidex.entry_count)
    except Exception as exc:  # noqa: BLE001 — never let a sync failure crash the loop
        log.warning("Wikidex sync failed (%r); keeping existing data.", exc)

async def _resolve_channel(bot: "StockpileBot", cache: dict, guild_id: int, kind: str):
    """Resolve (and cache) a guild's alert channel for a given alert kind."""
    key = (guild_id, kind)
    if key in cache:
        return cache[key]
    channel_id = bot.store.get_alert_channel(guild_id, kind)
    channel = None
    if channel_id:
        try:
            channel = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
        except Exception:
            channel = None
    cache[key] = channel
    return channel


@tasks.loop(seconds=60)
async def reminder_loop(bot: "StockpileBot") -> None:
    now = utc_now()
    channels: dict = {}  # (guild_id, kind) -> resolved channel

    for stockpile in bot.store.all():
        # Guard each stockpile independently: a single failing channel (lost
        # perms, deleted channel/message) must never abort the whole tick and
        # silently kill every other reminder.
        try:
            # Inventory-only stockpiles have no timer, so they never alert.
            if not stockpile.track_expiry:
                continue
            warning = warning_due(stockpile, now)
            if warning is None:
                continue

            # Stockpile alerts route to the guild's stockpile-alert channel (falls
            # back to the main channel).
            channel = await _resolve_channel(bot, channels, stockpile.guild_id, "stockpile")
            if channel is None:
                continue

            # Ping only the members assigned to this stockpile's refresh duty (if
            # any), plus the per-guild urgent role on the most urgent interval.
            mentions = [f"<@{uid}>" for uid in stockpile.duty_user_ids]
            urgent_role_id = bot.store.get_guild_urgent_role(stockpile.guild_id)
            if warning == URGENT_WARNING and urgent_role_id:
                mentions.insert(0, f"<@&{urgent_role_id}>")
            prefix = (" ".join(mentions) + " ") if mentions else ""

            status, color = stockpile_status(stockpile)
            if warning == "expired":
                title = "Public Risk"
                description = (
                    f"**{stockpile.name}** may now be public.\n"
                    f"Last tracked expiry was <t:{unix_ts(stockpile.expires_datetime)}:R>."
                )
            else:
                title = f"{warning.upper()} Stockpile Alert"
                description = (
                    f"**{stockpile.name}** at **{stockpile.location}** is entering the **{status}** window.\n"
                    f"Time left: <t:{unix_ts(stockpile.expires_datetime)}:R>"
                )

            embed = discord.Embed(title=title, description=description, color=color)
            embed.add_field(name="Stockpile ID", value=f"`{stockpile.id}`", inline=True)
            embed.add_field(name="Type", value=stockpile_type_label(stockpile), inline=True)
            embed.add_field(name="Expires", value=f"<t:{unix_ts(stockpile.expires_datetime)}:R>", inline=True)
            # Show what's inside so people know what they're about to lose.
            inventory = bot.store.get_stockpile_inventory(stockpile.id)
            if inventory:
                embed.add_field(name="📋 Inventory", value=_inventory_table(inventory), inline=False)
            embed.set_footer(text="Foxhole Buddy | Refresh in-game, then press Mark Refreshed")

            await channel.send(content=prefix or None, embed=embed)
            if warning == "expired":
                # Expired = done; the Public-Risk alert was the final notice. Remove
                # the tracked stockpile and its card (forum posts are archived so
                # the record survives) so nothing lingers.
                if stockpile.thread_id:
                    await bot.archive_thread(
                        stockpile.channel_id, stockpile.message_id, f"⚠️ {stockpile.name}"
                    )
                else:
                    await bot.delete_card_message(stockpile.channel_id, stockpile.message_id)
                bot.store.delete(stockpile.id)
                continue
            mark_warning_sent(stockpile, warning)
            bot.store.update(stockpile)
            await bot.update_stockpile_message(stockpile)
        except Exception as exc:  # noqa: BLE001 — one bad stockpile must not stop the loop
            log.warning(
                "Stockpile reminder for %s (guild %s) failed: %r",
                stockpile.id, stockpile.guild_id, exc,
            )

    # Process Factory Alarms
    from datetime import timedelta
    for alarm in bot.store.get_factory_alarms():
        # Guard each alarm independently so one bad channel can't stop the rest.
        try:
            channel = bot.get_channel(alarm.channel_id) or await bot.fetch_channel(alarm.channel_id)

            remaining = alarm.end_datetime - now

            if not alarm.single_ping:
                # 3-Ping: T-10m, T=0, T+10m — use sequential `if` so missed pings
                # still fire if the bot was offline (e.g. bot restarts).
                if remaining <= timedelta(minutes=10) and remaining > timedelta(minutes=0) and not alarm.warned_before:
                    await channel.send(f"⏰ <@{alarm.created_by_user_id}>, your queue at **{alarm.facility_name}** finishes in 10 minutes!")
                    bot.store.mark_factory_alarm_warned(alarm.id, "before")

                if remaining <= timedelta(minutes=0) and not alarm.warned_exact:
                    await channel.send(f"⏰ <@{alarm.created_by_user_id}>, your queue at **{alarm.facility_name}** is finished! Please clear it.")
                    bot.store.mark_factory_alarm_warned(alarm.id, "exact")

                if remaining <= timedelta(minutes=-10) and not alarm.warned_after:
                    await channel.send(f"🚨 <@{alarm.created_by_user_id}>, your queue at **{alarm.facility_name}** has been finished for 10 minutes! Clear it now so others can use it!")
                    # Send first, THEN delete — so a failed send doesn't lose the alarm
                    bot.store.delete_factory_alarm(alarm.id)
                    await bot.delete_card_message(alarm.channel_id, alarm.message_id)
            else:
                # 1-Ping: exactly at T=0
                if remaining <= timedelta(minutes=0) and not alarm.warned_exact:
                    await channel.send(f"⏰ <@{alarm.created_by_user_id}>, your queue at **{alarm.facility_name}** is finished! Please clear it.")
                    bot.store.delete_factory_alarm(alarm.id)
                    await bot.delete_card_message(alarm.channel_id, alarm.message_id)
        except Exception as exc:  # noqa: BLE001 — one bad alarm must not stop the loop
            log.warning("Factory alarm %s reminder failed: %r", alarm.id, exc)

    # Process Operation reminders (30 minutes before, and at start time)
    for op in bot.store.get_operations(open_only=True):
        # Guard each op independently so one failing channel can't stop the rest.
        try:
            if op.status != OP_SCHEDULED:
                continue

            delta = op.scheduled_datetime - now
            fire_30m = timedelta(minutes=0) < delta <= timedelta(minutes=30) and not op.warned_30m
            fire_start = delta <= timedelta(minutes=0) and not op.warned_start
            if not (fire_30m or fire_start):
                continue

            recipients = op.participant_ids() + op.tentative

            # Allied ops fan out: ping each server's own attendees in its own channel.
            if op.ally_room:
                if fire_30m:
                    await bot.announce_allied_op(
                        op,
                        f"⏰ **Op #{op.op_number} — {op.name}** starts "
                        f"<t:{unix_ts(op.scheduled_datetime)}:R>!",
                        recipients=recipients,
                    )
                    bot.store.mark_operation_warned(op.id, "30m")
                if fire_start:
                    await bot.announce_allied_op(
                        op,
                        f"🚨 **Op #{op.op_number} — {op.name}** is starting now!",
                        recipients=recipients,
                    )
                    bot.store.mark_operation_warned(op.id, "start")
                continue

            # Thread-mode ops get pinged inside their own thread (op.channel_id);
            # everything else uses the guild's ops alert channel.
            if op.thread_id:
                try:
                    channel = bot.get_channel(op.channel_id) or await bot.fetch_channel(op.channel_id)
                except Exception:
                    channel = None
            else:
                channel = await _resolve_channel(bot, channels, op.guild_id, "ops")
            if channel is None:
                continue
            mentions = " ".join(f"<@{uid}>" for uid in recipients)

            if fire_30m:
                await channel.send(
                    f"⏰ **Op #{op.op_number} — {op.name}** starts <t:{unix_ts(op.scheduled_datetime)}:R>! "
                    f"{mentions}".strip()
                )
                bot.store.mark_operation_warned(op.id, "30m")

            if fire_start:
                await channel.send(
                    f"🚨 **Op #{op.op_number} — {op.name}** is starting now! {mentions}".strip()
                )
                bot.store.mark_operation_warned(op.id, "start")
        except Exception as exc:  # noqa: BLE001 — one bad op must not stop the loop
            log.warning("Operation reminder for %s (guild %s) failed: %r", op.id, op.guild_id, exc)


# --------------------------------------------------------------------------
# Loop error handlers — a backstop so an unexpected escape (anything the
# per-item guards above didn't catch) doesn't permanently kill the task.
# discord.ext.tasks stops a loop on an unhandled exception; these log it and
# re-arm. The handler receives the loop's start args followed by the exception,
# so for `loop.start(bot)` that's (bot, exc) — read positionally and defensively.
# --------------------------------------------------------------------------

def _restart_loop(loop, bot, name: str, exc: Exception) -> None:
    log.exception("%s crashed; restarting.", name, exc_info=exc)
    if bot is None:
        return
    try:
        loop.restart(bot)
    except Exception:  # noqa: BLE001 — fall back to a plain start if restart can't schedule
        if not loop.is_running():
            loop.start(bot)


@reminder_loop.error
async def _reminder_loop_error(*args) -> None:
    bot = args[0] if len(args) > 1 else None
    _restart_loop(reminder_loop, bot, "reminder_loop", args[-1])


@war_sync_loop.error
async def _war_sync_loop_error(*args) -> None:
    bot = args[0] if len(args) > 1 else None
    _restart_loop(war_sync_loop, bot, "war_sync_loop", args[-1])


@catalog_sync_loop.error
async def _catalog_sync_loop_error(*args) -> None:
    bot = args[0] if len(args) > 1 else None
    _restart_loop(catalog_sync_loop, bot, "catalog_sync_loop", args[-1])


@wikidex_sync_loop.error
async def _wikidex_sync_loop_error(*args) -> None:
    bot = args[0] if len(args) > 1 else None
    _restart_loop(wikidex_sync_loop, bot, "wikidex_sync_loop", args[-1])
