"""Telegram command handlers."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from telegram import Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import ContextTypes

from ..config import Config, is_valid_hhmm
from ..formatting import render_watchlist, split_message
from ..report import ReportBuilder
from ..services.prices import PriceProvider
from ..storage import Storage

logger = logging.getLogger(__name__)

HELP_TEXT = """<b>What this bot does</b>
Every day it sends one short report on the companies you follow: the price, how
much it moved that day and over the week, and a plain-English note explaining
what happened.

<b>Commands</b>
/add <code>TICKER</code> — follow a company (e.g. <code>/add NVDA</code>)
/remove <code>TICKER</code> — stop following it
/list — everything you follow right now
/now — send the report immediately
/ai <code>TICKER</code> — a longer plain-English briefing on one company
/settime <code>HH:MM</code> — when the daily report arrives
/settz <code>Area/City</code> — your timezone (e.g. <code>Europe/Berlin</code>)
/pause and /resume — stop or restart the daily report
/status — your current settings
/help — this message

<b>Reading the numbers</b>
A <b>ticker</b> is a company's short code on the stock exchange — <code>NVDA</code>
is Nvidia. <b>day</b> is the change since the previous trading day's close;
<b>week</b> is the change over the last five trading days. Percentages matter more
than dollars: +2% means the same thing on a $10 stock and a $500 one.
The report also shows the whole US market, so you can tell a company-specific
move apart from a day when everything went up or down together."""


def _config(context: ContextTypes.DEFAULT_TYPE) -> Config:
    return context.bot_data["config"]


def _storage(context: ContextTypes.DEFAULT_TYPE) -> Storage:
    return context.bot_data["storage"]


def _prices(context: ContextTypes.DEFAULT_TYPE) -> PriceProvider:
    return context.bot_data["prices"]


def _reports(context: ContextTypes.DEFAULT_TYPE) -> ReportBuilder:
    return context.bot_data["reports"]


async def _reply(update: Update, text: str) -> None:
    for chunk in split_message(text):
        await update.effective_message.reply_text(
            chunk, parse_mode=ParseMode.HTML, disable_web_page_preview=True
        )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config = _config(context)
    storage = _storage(context)
    chat_id = update.effective_chat.id

    _, created = storage.ensure_user(
        chat_id, config.default_timezone, config.default_digest_time
    )

    if not created:
        await _reply(update, "You are already set up.\n\n" + HELP_TEXT)
        return

    await _reply(
        update,
        "👋 Welcome. Setting up your starting watchlist, one moment…",
    )

    added, failed = await _seed_default_tickers(context, chat_id)
    summary = [f"✅ Following <b>{len(added)}</b> companies: {', '.join(added)}."]
    if failed:
        summary.append(
            f"⚠️ Could not find data for: {', '.join(failed)} — remove or replace them."
        )
    summary.append(
        f"\nDaily report at <b>{config.default_digest_time}</b> "
        f"({config.default_timezone}). Change it with /settime and /settz."
    )
    summary.append("\nTry /now to see a report right away.")
    await _reply(update, "\n".join(summary))


async def _seed_default_tickers(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int
) -> tuple[list[str], list[str]]:
    config = _config(context)
    storage = _storage(context)
    prices = _prices(context)

    quotes = await asyncio.gather(
        *(prices.resolve_ticker(t) for t in config.default_tickers)
    )
    added, failed = [], []
    for quote in quotes:
        if quote.ok:
            storage.add_ticker(chat_id, quote.ticker, quote.name)
            added.append(quote.ticker)
        else:
            failed.append(quote.ticker)
    return added, failed


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _reply(update, HELP_TEXT)


async def add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config = _config(context)
    storage = _storage(context)
    chat_id = update.effective_chat.id
    _ensure_user(context, chat_id)

    if not context.args:
        await _reply(update, "Usage: <code>/add NVDA</code>")
        return

    if storage.count_tickers(chat_id) >= config.max_tickers_per_user:
        await _reply(
            update,
            f"You already follow {config.max_tickers_per_user} companies — "
            "remove one first with /remove.",
        )
        return

    ticker = context.args[0].strip().upper()
    await update.effective_chat.send_action(ChatAction.TYPING)
    quote = await _prices(context).resolve_ticker(ticker)

    if not quote.ok:
        await _reply(
            update,
            f"❌ <b>{ticker}</b> — {quote.error}.\n"
            "Check the ticker on finance.yahoo.com and try again.",
        )
        return

    if storage.add_ticker(chat_id, quote.ticker, quote.name):
        await _reply(update, f"✅ Now following <b>{quote.ticker}</b> — {quote.name}.")
    else:
        await _reply(update, f"<b>{quote.ticker}</b> is already on your list.")


async def remove(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    _ensure_user(context, chat_id)

    if not context.args:
        await _reply(update, "Usage: <code>/remove NVDA</code>")
        return

    ticker = context.args[0].strip().upper()
    if _storage(context).remove_ticker(chat_id, ticker):
        await _reply(update, f"🗑 Removed <b>{ticker}</b>.")
    else:
        await _reply(update, f"<b>{ticker}</b> was not on your list.")


async def list_tickers(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    user = _ensure_user(context, chat_id)
    entries = _storage(context).get_watchlist(chat_id)
    await _reply(update, render_watchlist(entries, f"{user.digest_time} {user.timezone}"))


async def now(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    _ensure_user(context, chat_id)
    await update.effective_chat.send_action(ChatAction.TYPING)

    report = await _reports(context).build_portfolio_report(chat_id)
    if report is None:
        await _reply(
            update, "Your watchlist is empty. Add a company with <code>/add NVDA</code>."
        )
        return
    await _reply(update, report.text)


async def ai_briefing(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    _ensure_user(context, chat_id)

    if not context.args:
        await _reply(update, "Usage: <code>/ai NVDA</code>")
        return

    if not _config(context).ai_enabled:
        await _reply(
            update,
            "AI commentary is off — <code>GROQ_API_KEY</code> is not configured "
            "on the server.",
        )
        return

    await update.effective_chat.send_action(ChatAction.TYPING)
    text = await _reports(context).build_ticker_report(context.args[0].strip().upper())
    await _reply(update, text)


async def settime(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    user = _ensure_user(context, chat_id)

    if not context.args or not is_valid_hhmm(context.args[0]):
        await _reply(update, "Usage: <code>/settime 09:00</code> (24-hour clock)")
        return

    value = context.args[0]
    _storage(context).set_digest_time(chat_id, value)
    await _reply(
        update, f"⏰ Daily report set to <b>{value}</b> ({user.timezone})."
    )


async def settz(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    _ensure_user(context, chat_id)

    if not context.args:
        await _reply(
            update,
            "Usage: <code>/settz Europe/Berlin</code>\n"
            "Full list: en.wikipedia.org/wiki/List_of_tz_database_time_zones",
        )
        return

    name = context.args[0].strip()
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        await _reply(update, f"❌ <code>{name}</code> is not a known timezone.")
        return

    _storage(context).set_timezone(chat_id, name)
    local_now = datetime.now(ZoneInfo(name)).strftime("%H:%M")
    await _reply(update, f"🌍 Timezone set to <b>{name}</b> — it is {local_now} for you.")


async def pause(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    _ensure_user(context, chat_id)
    _storage(context).set_enabled(chat_id, False)
    await _reply(update, "⏸ Daily reports paused. /resume turns them back on.")


async def resume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    _ensure_user(context, chat_id)
    _storage(context).set_enabled(chat_id, True)
    await _reply(update, "▶️ Daily reports are on again.")


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    user = _ensure_user(context, chat_id)
    storage = _storage(context)
    config = _config(context)

    local_now = datetime.now(ZoneInfo(user.timezone)).strftime("%H:%M")
    lines = [
        "<b>Your settings</b>",
        f"Companies followed: <b>{storage.count_tickers(chat_id)}</b>",
        f"Daily report: <b>{user.digest_time}</b> ({user.timezone}, now {local_now})",
        f"Reports: <b>{'on' if user.enabled else 'paused'}</b>",
        f"AI commentary: <b>{'on' if config.ai_enabled else 'off'}</b>",
        f"Last report sent for session: <b>{user.last_session_sent or 'none yet'}</b>",
    ]
    await _reply(update, "\n".join(lines))


async def unknown(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _reply(update, "Unknown command. /help lists everything I can do.")


def _ensure_user(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    config = _config(context)
    user, _ = _storage(context).ensure_user(
        chat_id, config.default_timezone, config.default_digest_time
    )
    return user
