"""Rendering of Telegram messages.

Messages use HTML parse mode (far less escaping pain than MarkdownV2) and are
kept deliberately short: two lines per company, numbers first.
"""

from __future__ import annotations

from datetime import date, datetime
from html import escape

from .services.prices import Quote

MAX_NAME_LENGTH = 24
TELEGRAM_MESSAGE_LIMIT = 4096


def money(value: float | None, currency: str = "USD") -> str:
    if value is None:
        return "—"
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
    return f"{header}\n<code>{numbers}</code>"


def render_report(
    quotes: list[Quote],
    benchmark: Quote | None,
    ai_comment: str | None,
    session_date: str | None,
    is_live: bool,
) -> str:
    state = "live prices" if is_live else "market close"
    lines = [f"📊 <b>Daily report</b> · {_pretty_date(session_date)} · {state}", ""]

    lines.extend(quote_lines(q) for q in quotes)

    usable = [q.day_change_pct for q in quotes if q.ok and q.day_change_pct is not None]
    if usable:
        average = sum(usable) / len(usable)
        summary = f"\nYour list on average: <b>{percent(average)}</b> today"
        if benchmark and benchmark.ok:
            summary += (
                f"\nWhole US market ({escape(benchmark.ticker)}): "
                f"{percent(benchmark.day_change_pct)}"
            )
        lines.append(summary)

    if ai_comment:
        lines.append(f"\n🤖 <b>What this means</b>\n{escape(ai_comment)}")
        lines.append(
            "\n<i>Prices from Yahoo Finance. Commentary is AI-generated and can be "
            "wrong. Not investment advice.</i>"
        )
    else:
        lines.append("\n<i>Prices from Yahoo Finance. Not investment advice.</i>")
    return "\n".join(lines)


def render_ticker_report(quote: Quote, ai_comment: str | None, headlines: list[str]) -> str:
    if not quote.ok:
        return (
            f"⚠️ <b>{escape(quote.ticker)}</b> — "
            f"{escape(quote.error or 'data temporarily unavailable')}"
        )

    parts = [quote_lines(quote)]

    if headlines:
        news = "\n".join(f"• {escape(h)}" for h in headlines[:3])
        parts.append(f"\n📰 <b>Headlines</b>\n{news}")

    if ai_comment:
        parts.append(f"\n🤖 <b>In plain English</b>\n{escape(ai_comment)}")

    parts.append("\n<i>AI-generated, can be wrong. Not investment advice.</i>")
    return "\n".join(parts)


def render_watchlist(entries: list[tuple[str, str | None]], time_hint: str) -> str:
    if not entries:
        return (
            "Your watchlist is empty.\n"
            "Add a company with <code>/add ONTO</code>."
        )
    lines = [f"<b>Watching {len(entries)} companies</b>", ""]
    for ticker, name in entries:
        label = _short_name(name, ticker)
        lines.append(f"• <b>{escape(ticker)}</b>" + (f" — {escape(label)}" if label else ""))
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
