"""Rendering of Telegram messages.

Messages use HTML parse mode (far less escaping pain than MarkdownV2) and are
kept deliberately short: two lines per company, numbers first.
"""

from __future__ import annotations

from datetime import date, datetime
from html import escape as _escape

from .services.prices import Quote

MAX_NAME_LENGTH = 24
TELEGRAM_MESSAGE_LIMIT = 4096


def escape(text: str) -> str:
    """Escape for Telegram HTML text nodes.

    Apostrophes are left alone: quote escaping only matters inside attributes,
    and Uzbek company names are full of them (O'zbektelekom).
    """
    return _escape(str(text), quote=False)


def money(value: float | None, currency: str = "USD") -> str:
    if value is None:
        return "—"
    # Uzbek sums run to five and six figures and are never quoted in decimals.
    if currency == "UZS":
        return f"{value:,.0f}".replace(",", " ") + " UZS"
    symbol = {"USD": "$", "EUR": "€", "GBP": "£"}.get(currency, "")
    return f"{symbol}{value:,.2f}" if symbol else f"{value:,.2f} {currency}"


def percent(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:+.1f}%"


def trend_emoji(value: float | None) -> str:
    if value is None:
        return "▪️"
    if value > 0.05:
        return "📈"
    if value < -0.05:
        return "📉"
    return "▪️"


def _short_name(name: str | None, ticker: str) -> str:
    if not name or name.upper() == ticker.upper():
        return ""
    if len(name) > MAX_NAME_LENGTH:
        name = name[: MAX_NAME_LENGTH - 1].rstrip() + "…"
    return name


def _pretty_date(iso_date: str | None) -> str:
    if not iso_date:
        return "—"
    try:
        parsed = date.fromisoformat(iso_date)
    except ValueError:
        return iso_date
    return parsed.strftime("%a, %d %b")


def quote_lines(quote: Quote) -> str:
    """Two lines for one company, or one line when data is missing."""
    if not quote.ok:
        reason = escape(quote.error or "data temporarily unavailable")
        return f"⚠️ <b>{escape(quote.ticker)}</b> — {reason}"

    name = _short_name(quote.name, quote.ticker)
    header = f"{trend_emoji(quote.day_change_pct)} <b>{escape(quote.ticker)}</b>"
    if name:
        header += f" — {escape(name)}"

    numbers = (
        f"{money(quote.price, quote.currency)}  "
        f"{percent(quote.day_change_pct)} day  "
        f"{percent(quote.week_change_pct)} week"
    )
    rendered = f"{header}\n<code>{numbers}</code>"
    if quote.note:
        rendered += f"\n   <i>⏳ {escape(quote.note)}</i>"
    return rendered


MARKET_HEADINGS = {
    "US": "🇺🇸 <b>US market</b>",
    "UZSE": "🇺🇿 <b>Uzbek exchange (UZSE)</b>",
}


def render_report(
    quotes: list[Quote],
    benchmark: Quote | None,
    ai_comment: str | None,
    session_date: str | None,
    is_live: bool,
    uzse_session_date: str | None = None,
) -> str:
    state = "live prices" if is_live else "market close"
    lines = [f"📊 <b>Daily report</b> · {_pretty_date(session_date)} · {state}"]

    # The two exchanges trade in different currencies, on different calendars,
    # at different hours — mixing them into one list would be misleading.
    us = [q for q in quotes if q.market != "UZSE"]
    uz = [q for q in quotes if q.market == "UZSE"]

    for market, group in (("US", us), ("UZSE", uz)):
        if not group:
            continue
        heading = MARKET_HEADINGS[market]
        if market == "UZSE" and uzse_session_date and uzse_session_date != session_date:
            heading += f" · {_pretty_date(uzse_session_date)}"
        lines.append(f"\n{heading}" if len(us) and len(uz) else "")
        lines.extend(quote_lines(q) for q in group)

    usable = [q.day_change_pct for q in us if q.ok and q.day_change_pct is not None]
    if usable:
        average = sum(usable) / len(usable)
        summary = f"\nYour US stocks on average: <b>{percent(average)}</b> today"
        if benchmark and benchmark.ok:
            summary += (
                f"\nWhole US market ({escape(benchmark.ticker)}): "
                f"{percent(benchmark.day_change_pct)}"
            )
        lines.append(summary)

    if ai_comment:
        lines.append(f"\n🤖 <b>What this means</b>\n{escape(ai_comment)}")

    sources = "Yahoo Finance" + (" and parse.bot (UZSE)" if uz else "")
    footer = f"Prices from {sources}."
    if ai_comment:
        footer += " Commentary is AI-generated and can be wrong."
    lines.append(f"\n<i>{footer} Not investment advice.</i>")
    return "\n".join(lines)


def render_ticker_report(
    quote: Quote,
    ai_comment: str | None,
    headlines: list[str],
    history_depth: int | None = None,
    detail: list[tuple[str, str]] | None = None,
) -> str:
    if not quote.ok:
        return (
            f"⚠️ <b>{escape(quote.ticker)}</b> — "
            f"{escape(quote.error or 'data temporarily unavailable')}"
        )

    parts = [MARKET_HEADINGS[quote.market], quote_lines(quote)]

    # UZSE history is built up one snapshot a day, so early on the comparison
    # numbers do not exist yet. Say so rather than showing a silent dash.
    if history_depth is not None and history_depth < 6:
        missing = "day and week" if history_depth < 2 else "week"
        parts.append(
            f"\n<i>Only {history_depth} trading day(s) recorded so far, so the "
            f"{missing} change is not available yet. It fills in automatically.</i>"
        )

    if detail:
        rows = "\n".join(f"{escape(label)}: <b>{escape(value)}</b>" for label, value in detail)
        parts.append(f"\n📐 <b>Trading detail</b>\n{rows}")

    if headlines:
        news = "\n".join(f"• {escape(h)}" for h in headlines[:3])
        parts.append(f"\n📰 <b>Headlines</b>\n{news}")

    if ai_comment:
        parts.append(f"\n🤖 <b>In plain English</b>\n{escape(ai_comment)}")

    parts.append("\n<i>AI-generated, can be wrong. Not investment advice.</i>")
    return "\n".join(parts)


def render_status(
    *,
    followed: int,
    digest_time: str,
    timezone: str,
    local_now: str,
    enabled: bool,
    ai_enabled: bool,
    last_session_sent: str | None,
    uzse: dict | None = None,
) -> str:
    lines = [
        "<b>Your settings</b>",
        f"Companies followed: <b>{followed}</b>",
        f"Daily report: <b>{escape(digest_time)}</b> "
        f"({escape(timezone)}, now {escape(local_now)})",
        f"Reports: <b>{'on' if enabled else 'paused'}</b>",
        f"AI commentary: <b>{'on' if ai_enabled else 'off'}</b>",
        f"Last report sent for session: <b>{escape(last_session_sent or 'none yet')}</b>",
    ]
    if uzse:
        lines.append(
            "\n🇺🇿 <b>UZSE data (parse.bot)</b>\n"
            f"Credits left this month: <b>{uzse['remaining']}</b> of {uzse['limit']}\n"
            f"Today's snapshot cached: <b>{'yes' if uzse['cached'] else 'not yet today'}</b>"
        )
    return "\n".join(lines)


def render_watchlist(entries, time_hint: str) -> str:
    if not entries:
        return (
            "Your watchlist is empty.\n"
            "Add a US company with <code>/add ONTO</code>, "
            "or an Uzbek one with <code>/add UZ:KVTS</code>."
        )
    lines = [f"<b>Watching {len(entries)} companies</b>"]
    for market in ("US", "UZSE"):
        group = [e for e in entries if e.market == market]
        if not group:
            continue
        lines.append(f"\n{MARKET_HEADINGS[market]}")
        for entry in group:
            label = _short_name(entry.name, entry.ticker)
            lines.append(
                f"• <b>{escape(entry.ticker)}</b>" + (f" — {escape(label)}" if label else "")
            )
    lines.append(f"\nDaily report at {escape(time_hint)}.")
    return "\n".join(lines)


def split_message(text: str, limit: int = TELEGRAM_MESSAGE_LIMIT) -> list[str]:
    """Split on line boundaries so a long watchlist never hits Telegram's limit."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        if len(current) + len(line) + 1 > limit:
            chunks.append(current.rstrip("\n"))
            current = ""
        current += line + "\n"
    if current.strip():
        chunks.append(current.rstrip("\n"))
    return chunks


def now_label(moment: datetime) -> str:
    return moment.strftime("%H:%M")
