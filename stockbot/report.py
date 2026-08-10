"""Assembling a report: prices first, AI commentary on top of the numbers."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from .formatting import render_report, render_ticker_report
from .services.ai import AIClient
from .services.prices import PriceProvider, Quote
from .services.uzse import MARKET as UZSE_MARKET
from .services.uzse import BudgetExhausted, UzseProvider
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
        uzse: UzseProvider | None = None,
    ) -> None:
        self._storage = storage
        self._prices = prices
        self._ai = ai
        self._benchmark_ticker = benchmark_ticker
        self._uzse = uzse

    async def build_portfolio_report(
        self, chat_id: int, *, scheduled: bool = False
    ) -> BuiltReport | None:
        """Return the daily report, or None when the watchlist is empty.

        `scheduled` is passed through to the metered UZSE provider: only the
        daily job is allowed to spend a parse.bot credit, and only once a day.
        """
        entries = self._storage.get_watchlist(chat_id)
        if not entries:
            return None

        us_tickers = [e.ticker for e in entries if e.market != UZSE_MARKET]
        uz_tickers = [e.ticker for e in entries if e.market == UZSE_MARKET]
        names = {e.ticker: e.name for e in entries}

        quotes_map, benchmark = await asyncio.gather(
            self._prices.get_quotes(us_tickers),
            self._prices.get_quote(self._benchmark_ticker),
        )
        quotes = [quotes_map[t] for t in us_tickers]
        quotes.sort(key=_sort_key)

        uz_quotes = await self._uzse_quotes(uz_tickers, scheduled=scheduled)
        uz_quotes.sort(key=_sort_key)

        for quote in quotes + uz_quotes:
            # Prefer the name captured at /add time; `.info` is slow and flaky.
            if names.get(quote.ticker):
                quote.name = names[quote.ticker]

        session_date, is_live = _session_state(quotes or uz_quotes)
        uzse_session_date, _ = _session_state(uz_quotes)

        headlines = await self._headlines_for_movers(quotes)
        comment = await self._ai.portfolio_comment(
            facts=_facts_block(quotes + uz_quotes, benchmark),
            headlines=_headlines_block(headlines),
        )

        return BuiltReport(
            text=render_report(
                quotes + uz_quotes,
                benchmark,
                comment,
                session_date,
                is_live,
                uzse_session_date=uzse_session_date,
            ),
            session_date=session_date,
            is_live=is_live,
        )

    async def _uzse_quotes(self, tickers: list[str], *, scheduled: bool) -> list[Quote]:
        """UZSE quotes come from the cached snapshot; only `scheduled` may pay."""
        if not tickers or self._uzse is None or not self._uzse.enabled:
            return []
        try:
            await self._uzse.ensure_snapshot(scheduled=scheduled)
        except BudgetExhausted as exc:
            logger.warning("UZSE data skipped: %s", exc)
        return [self._uzse.get_quote(ticker) for ticker in tickers]

    async def build_ticker_report(self, ticker: str, market: str | None = None) -> str:
        """Deep briefing on one company, routed to the right exchange."""
        if market == UZSE_MARKET:
            return await self._build_uzse_ticker_report(ticker)

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

    async def _build_uzse_ticker_report(self, ticker: str) -> str:
        """UZSE briefing. Reads the cached snapshot only — never spends a credit.

        There is no news feed for UZSE, so the AI works from the exchange data
        alone and is told to say so rather than speculate about causes.
        """
        if self._uzse is None or not self._uzse.enabled:
            return render_ticker_report(
                Quote(ticker=ticker, market=UZSE_MARKET, error="UZSE data is not configured"),
                None,
                [],
            )

        await self._uzse.ensure_snapshot(scheduled=False)
        quote = self._uzse.get_quote(ticker)
        if not quote.ok:
            return render_ticker_report(quote, None, [])

        depth = self._uzse.history_depth(quote.ticker)
        facts = _facts_block([quote], None)
        facts += (
            f"\nExchange: Uzbek Stock Exchange (UZSE), currency UZS."
            f"\nLocally recorded sessions for this ticker: {depth}."
            "\nNo news feed is available for this exchange."
        )
        comment = await self._ai.ticker_comment(
            ticker=quote.ticker, facts=facts, headlines=""
        )
        return render_ticker_report(quote, comment, [], history_depth=depth)

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
        where = "UZSE, Uzbekistan" if quote.market == UZSE_MARKET else "US market"
        line = f"{quote.ticker} ({name}, {where}): price {quote.price:.2f} {quote.currency}"
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
