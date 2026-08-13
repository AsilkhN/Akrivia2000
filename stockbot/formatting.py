"""Rendering of Telegram messages.

Messages use HTML parse mode (far less escaping pain than MarkdownV2) and are
kept deliberately short: two lines per company, numbers first.
"""

from __future__ import annotations

from datetime import date, datetime
from html import escape as _escape

from .markets import UZSE, market_by_code
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
        icon = "⚠️" if quote.suspect else "⏳"
        rendered += f"\n   <i>{icon} {escape(quote.note)}</i>"
    return rendered


def market_heading(code: str) -> str:
    return f"<b>{escape(market_by_code(code).heading)}</b>"


def _market_section(
    code: str,
    group: list[Quote],
    benchmarks: dict[str, Quote],
    show_heading: bool,
) -> list[str]:
    """One exchange: its own heading, its own session date, its own average."""
    lines: list[str] = []
    if show_heading:
        heading = market_heading(code)
        session = _group_session(group)
        if session:
            heading += f" · {_pretty_date(session)}"
        lines.append(f"\n{heading}")

    lines.extend(quote_lines(q) for q in group)

    changes = [q.day_change_pct for q in group if q.ok and q.day_change_pct is not None]
    if changes:
        average = percent(sum(changes) / len(changes))
        summary = f"<i>Your {len(changes)} on average: {average}</i>"
        index = benchmarks.get(code)
        if index and index.ok and index.day_change_pct is not None:
            summary = summary[:-4] + (
                f" · whole market {percent(index.day_change_pct)}</i>"
            )
        lines.append(summary)
    return lines


def render_report(
    quotes: list[Quote],
    benchmarks: dict[str, Quote] | None,
    ai_comment: str | None,
    session_date: str | None,
    is_live: bool,
) -> str:
    """The daily report, grouped by exchange.

    Markets are never blended: different currencies, calendars and trading
    hours mean one average across them would describe nothing real.
    """
    state = "live prices" if is_live else "market close"
    lines = [f"📊 <b>Daily report</b> · {_pretty_date(session_date)} · {state}"]

    groups = _group_by_market(quotes)
    benchmarks = benchmarks or {}
    show_headings = len(groups) > 1
    if not show_headings:
        lines.append("")

    for code, group in groups:
        lines.extend(_market_section(code, group, benchmarks, show_headings))

    if ai_comment:
        lines.append(f"\n🤖 <b>What this means</b>\n{escape(ai_comment)}")

    has_uzse = any(code == "UZSE" for code, _ in groups)
    sources = "Yahoo Finance" + (" and parse.bot (UZSE)" if has_uzse else "")
    footer = f"Prices from {sources}."
    if ai_comment:
        footer += " Commentary is AI-generated and can be wrong."
    lines.append(f"\n<i>{footer} Not investment advice.</i>")
    return "\n".join(lines)


def _group_by_market(quotes: list[Quote]) -> list[tuple[str, list[Quote]]]:
    """Biggest holding group first; UZSE last, since it is the odd one out."""
    grouped: dict[str, list[Quote]] = {}
    for quote in quotes:
        grouped.setdefault(quote.market, []).append(quote)
    return sorted(
        grouped.items(), key=lambda item: (item[0] == "UZSE", -len(item[1]), item[0])
    )


def _group_session(group: list[Quote]) -> str | None:
    dates = [q.session_date for q in group if q.ok and q.session_date]
    return max(dates) if dates else None


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

    parts = [market_heading(quote.market), quote_lines(quote)]

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


def big_money(value: float | None) -> str:
    """Uzbek turnover runs to billions; 6 840 000 UZS is unreadable in a report."""
    if value is None:
        return "—"
    for limit, suffix in ((1_000_000_000, "bn"), (1_000_000, "mn"), (1_000, "k")):
        if abs(value) >= limit:
            return f"{value / limit:,.1f} {suffix} UZS"
    return f"{value:,.0f} UZS"


LIQUIDITY_LABEL = {
    "good": "",
    "moderate": " · moderate volume",
    "thin": " · thin",
    "unknown": "",
}


def _scout_row(row, show_turnover: bool = True) -> str:
    name = _short_name(row.name, row.ticker)
    head = f"{trend_emoji(row.change_pct)} <b>{escape(row.ticker)}</b>"
    if name:
        head += f" — {escape(name)}"

    facts = [percent(row.change_pct)]
    if show_turnover and row.turnover:
        facts.append(f"{big_money(row.turnover)} traded")
    facts.append(f"{row.sessions_traded} session{'s' if row.sessions_traded != 1 else ''}")
    line = f"<code>{'  '.join(facts)}{LIQUIDITY_LABEL[row.liquidity]}</code>"

    if row.tags:
        line += f"\n   <i>{escape(', '.join(row.tags))}</i>"
    return f"{head}\n{line}"


def render_scout(report, news: dict | None = None) -> str:
    """The scouting brief: what moved, on what money, and what to ignore."""
    title = "Weekly scout" if report.period == "weekly" else "Daily scout"
    span = (
        f"{_pretty_date(report.start)} – {_pretty_date(report.end)}"
        if report.period == "weekly"
        else _pretty_date(report.end)
    )
    lines = [f"🔭 <b>{title}</b> · 🇺🇿 UZSE · {span}"]

    if report.is_empty:
        lines.append("\nNothing traded on the Uzbek exchange in this window.")
        return "\n".join(lines)

    if report.turnover_leaders:
        lines.append("\n💰 <b>Where the money went</b>")
        lines.extend(_scout_row(r) for r in report.turnover_leaders)

    if report.movers:
        lines.append("\n📊 <b>Biggest real moves</b>")
        lines.extend(_scout_row(r) for r in report.movers)

    if report.awakened:
        lines.append("\n⚡ <b>Woke up</b>")
        lines.extend(_scout_row(r, show_turnover=False) for r in report.awakened)

    if news:
        lines.append("\n📰 <b>In the news</b>")
        for ticker, titles in list(news.items())[:4]:
            for title_text in titles[:1]:
                lines.append(f"• <b>{escape(ticker)}</b> — {escape(title_text)}")

    if report.noise:
        ignored = ", ".join(
            f"{escape(r.ticker)} {percent(r.change_pct)}" for r in report.noise
        )
        lines.append(
            f"\n⚠️ <b>Ignore these</b>\n<i>{ignored} — moves on almost no money "
            "changing hands, not market opinion.</i>"
        )

    if report.comment:
        lines.append(f"\n🤖 <b>What this means</b>\n{escape(report.comment)}")

    if report.coverage_note:
        lines.append(f"\n<i>{escape(report.coverage_note)} Not investment advice.</i>")
    return "\n".join(lines)


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
    by_market: dict[str, list] = {}
    for entry in entries:
        by_market.setdefault(entry.market, []).append(entry)
    for market in sorted(by_market, key=lambda m: (m == UZSE.code, -len(by_market[m]), m)):
        group = by_market[market]
        lines.append(f"\n{market_heading(market)}")
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
