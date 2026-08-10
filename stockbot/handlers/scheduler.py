"""The daily report job.

Design note: instead of registering one scheduled job per user (which has to be
rebuilt whenever someone runs /settime and is lost on restart), a single job
ticks once a minute and asks "who is due right now?". That survives restarts,
handles per-user timezones, and catches up on a report that was missed because
the bot was down at the exact minute it was scheduled.
"""

from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from telegram.constants import ParseMode
from telegram.error import Forbidden, TelegramError
from telegram.ext import ContextTypes

from ..config import Config
from ..formatting import split_message
from ..report import ReportBuilder
from ..storage import Storage, User

logger = logging.getLogger(__name__)

TICK_SECONDS = 60


async def daily_tick(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Runs every minute; sends to whoever is due."""
    storage: Storage = context.bot_data["storage"]

    for user in storage.all_users():
        try:
            if _is_due(user):
                await _send_daily_report(context, user)
            await _maybe_send_scout(context, user)
        except Exception:  # noqa: BLE001 - one user must never break the loop
            logger.exception("daily report failed for chat %s", user.chat_id)


def _is_due(user: User, now: datetime | None = None) -> bool:
    """True when it is at or past the user's report time and today's is unsent.

    `now`, when given, must be timezone-aware.
    """
    try:
        zone = ZoneInfo(user.timezone)
        local_now = now.astimezone(zone) if now else datetime.now(zone)
    except (ZoneInfoNotFoundError, ValueError):
        logger.warning("chat %s has invalid timezone %s", user.chat_id, user.timezone)
        return False

    if user.last_digest_date == local_now.date().isoformat():
        return False

    hour, minute = (int(part) for part in user.digest_time.split(":"))
    return (local_now.hour, local_now.minute) >= (hour, minute)


async def _maybe_send_scout(context: ContextTypes.DEFAULT_TYPE, user: User) -> None:
    """The scout runs after the market has closed, and again on Monday for the
    week just finished."""
    config: Config = context.bot_data["config"]
    storage: Storage = context.bot_data["storage"]
    reports: ReportBuilder = context.bot_data["reports"]
    if not config.scout_enabled or not config.uzse_enabled:
        return

    zone = ZoneInfo(user.timezone)
    local_now = datetime.now(zone)
    local_date = local_now.date().isoformat()
    hour, minute = (int(p) for p in config.scout_time.split(":"))
    if (local_now.hour, local_now.minute) < (hour, minute):
        return

    period = "weekly" if local_now.weekday() == 0 else "daily"
    marker = f"scout:{period}:{user.chat_id}"
    if storage.load_cache(marker) and storage.load_cache(marker)[2] == local_date:
        return

    text, worth_sending = await reports.build_scout_report(period, scheduled=True)
    storage.save_cache(marker, "sent", local_date)
    if not worth_sending:
        logger.info("scout for chat %s had nothing to report", user.chat_id)
        return

    try:
        for chunk in split_message(text):
            await context.bot.send_message(
                chat_id=user.chat_id,
                text=chunk,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
    except Forbidden:
        storage.set_enabled(user.chat_id, False)
    except TelegramError:
        logger.exception("could not deliver scout to chat %s", user.chat_id)


async def _send_daily_report(context: ContextTypes.DEFAULT_TYPE, user: User) -> None:
    storage: Storage = context.bot_data["storage"]
    reports: ReportBuilder = context.bot_data["reports"]

    local_date = datetime.now(ZoneInfo(user.timezone)).date().isoformat()
    # Only the scheduled run may spend a parse.bot credit, at most once a day.
    report = await reports.build_portfolio_report(user.chat_id, scheduled=True)

    if report is None:
        logger.info("chat %s has an empty watchlist, nothing to send", user.chat_id)
        storage.mark_digest_run(user.chat_id, local_date, session_date=None)
        return

    # Weekends and holidays produce the same session as yesterday's report —
    # there is nothing new to say, so stay quiet.
    if report.session_date and report.session_date == user.last_session_sent:
        logger.info(
            "chat %s already has session %s, skipping", user.chat_id, report.session_date
        )
        storage.mark_digest_run(user.chat_id, local_date, session_date=None)
        return

    try:
        for chunk in split_message(report.text):
            await context.bot.send_message(
                chat_id=user.chat_id,
                text=chunk,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
    except Forbidden:
        # The user blocked the bot or deleted the chat — stop bothering them.
        logger.info("chat %s blocked the bot, pausing reports", user.chat_id)
        storage.set_enabled(user.chat_id, False)
        return
    except TelegramError:
        logger.exception("could not deliver report to chat %s", user.chat_id)
        return

    storage.mark_digest_run(user.chat_id, local_date, report.session_date)
    logger.info("sent report to chat %s for session %s", user.chat_id, report.session_date)

    config: Config = context.bot_data["config"]
    if config.heartbeat_url:
        await _ping_heartbeat(config.heartbeat_url)


async def _ping_heartbeat(url: str) -> None:
    """Tell an uptime monitor the scheduler is alive, so silent death is noticed."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.get(url)
    except Exception as exc:  # noqa: BLE001 - monitoring must never break the bot
        logger.debug("heartbeat ping failed: %s", exc)
