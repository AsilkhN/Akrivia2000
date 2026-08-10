"""Assembling a report: prices first, AI commentary on top of the numbers."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from .formatting import render_report, render_ticker_report
from .services.ai import AIClient
from .services.prices import PriceProvider, Quote
from .storage import Storage

logger = logging.getLogger(__name__)

# Headlines are only pulled for the few biggest movers — that is where an
# explanation is actually worth having, and it keeps the report fast.
NEWS_FOR_TOP_MOVERS = 3


@dataclass
class BuiltReport:
    text: str
    session_date: str | None
    is_live: bool


class ReportBuilder:
    def __init__(
        self,
        storage: Storage,
        prices: PriceProvider,
        ai: AIClient,
        benchmark_ticker: str,
    ) -> None:
        self._storage = storage
        self._prices = prices
        self._ai = ai
        self._benchmark_ticker = benchmark_ticker

    async def build_portfolio_report(self, chat_id: int) -> BuiltReport | None:
        """Return the daily report, or None when the watchlist is empty."""
        entries = self._storage.get_watchlist(chat_id)
        if not entries:
            return None

        tickers = [ticker for ticker, _ in entries]
        names = dict(entries)

        quotes_map, benchmark = await asyncio.gather(
            self._prices.get_quotes(tickers),
            self._prices.get_quote(self._benchmark_ticker),
        )

        quotes = []
        for ticker in tickers:
            quote = quotes_map[ticker]
            # Prefer the name captured at /add time; `.info` is slow and flaky.
            if names.get(ticker):
                quote.name = names[ticker]
            quotes.append(quote)

        quotes.sort(key=_sort_key)

        session_date, is_live = _session_state(quotes)
        headlines = await self._headlines_for_movers(quotes)
        comment = await self._ai.portfolio_comment(
            facts=_facts_block(quotes, benchmark),
            headlines=_headlines_block(headlines),
        )

        return BuiltReport(
            text=render_report(quotes, benchmark, comment, session_date, is_live),
            session_date=session_date,
            is_live=is_live,
        )

    async def build_ticker_report(self, ticker: str) -> str:
        quote = await self._prices.get_quote(ticker)
        if not quote.ok:
            return render_ticker_report(quote, None, [])

        headlines = await self._prices.get_headlines(quote.ticker, limit=3)
        comment = await self._ai.ticker_comment(
            ticker=quote.ticker,
            facts=_facts_block([quote], None),
            headlines=_headlines_block({quote.ticker: headlines}),
        )
        return render_ticker_report(quote, comment, headlines)

    async def _headlines_for_movers(self, quotes: list[Quote]) -> dict[str, list[str]]:
        if not self._ai.enabled:
            return {}
        movers = [q for q in quotes if q.ok][:NEWS_FOR_TOP_MOVERS]
        results = await asyncio.gather(
            *(self._prices.get_headlines(q.ticker, limit=2) for q in movers)
        )
        return {q.ticker: titles for q, titles in zip(movers, results) if titles}


def _sort_key(quote: Quote) -> tuple[int, float]:
    """Biggest movers first (in either direction), broken tickers last."""
    if not quote.ok or quote.day_change_pct is None:
        return (1, 0.0)
    return (0, -abs(quote.day_change_pct))


def _session_state(quotes: list[Quote]) -> tuple[str | None, bool]:
    """The session the report describes: the newest bar any ticker returned."""
    dated = [q for q in quotes if q.ok and q.session_date]
    if not dated:
        return None, False
    newest = max(dated, key=lambda q: q.session_date or "")
    return newest.session_date, newest.is_live


def _facts_block(quotes: list[Quote], benchmark: Quote | None) -> str:
    lines = []
    for quote in quotes:
        if not quote.ok:
            continue
        name = quote.name or quote.ticker
        line = f"{quote.ticker} ({name}): price {quote.price:.2f} {quote.currency}"
        if quote.day_change_pct is not None:
            line += f", day {quote.day_change_pct:+.2f}%"
        if quote.week_change_pct is not None:
            line += f", week {quote.week_change_pct:+.2f}%"
        lines.append(line)
    if benchmark and benchmark.ok and benchmark.day_change_pct is not None:
        lines.append(
            f"{benchmark.ticker} (whole US market benchmark): "
            f"day {benchmark.day_change_pct:+.2f}%"
        )
    return "\n".join(lines) or "(no price data available)"


def _headlines_block(headlines: dict[str, list[str]]) -> str:
    lines = []
    for ticker, titles in headlines.items():
        for title in titles:
            lines.append(f"{ticker}: {title}")
    return "\n".join(lines)
