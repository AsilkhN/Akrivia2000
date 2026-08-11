"""Assembling a report: prices first, AI commentary on top of the numbers."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from .formatting import money, render_report, render_scout, render_ticker_report
from .markets import market_by_code
from .services.ai import AIClient
from .services.prices import PriceProvider, Quote
from .services.uzse import MARKET as UZSE_MARKET
from .services.news import NewsProvider, match_headlines
from .services.uzse import BudgetExhausted, UzseDetail, UzseProvider
from .scout import ScoutReport
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
        scout=None,
        news: NewsProvider | None = None,
    ) -> None:
        self._storage = storage
        self._prices = prices
        self._ai = ai
        self._benchmark_ticker = benchmark_ticker
        self._uzse = uzse
        self._scout = scout
        self._news = news

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

        yahoo_tickers = [e.ticker for e in entries if e.market != UZSE_MARKET]
        uz_tickers = [e.ticker for e in entries if e.market == UZSE_MARKET]
        names = {e.ticker: e.name for e in entries}

        quotes_map = await self._prices.get_quotes(yahoo_tickers)
        quotes = sorted((quotes_map[t] for t in yahoo_tickers), key=_sort_key)

        uz_quotes = sorted(
            await self._uzse_quotes(uz_tickers, scheduled=scheduled), key=_sort_key
        )
        everything = quotes + uz_quotes

        for quote in everything:
            # Prefer the name captured at /add time; `.info` is slow and flaky.
            if names.get(quote.ticker):
                quote.name = names[quote.ticker]

        benchmarks = await self._benchmarks_for(quotes)
        session_date, is_live = _session_state(everything)

        headlines = await self._headlines_for_movers(quotes)
        comment = await self._ai.portfolio_comment(
            facts=_facts_block(everything, benchmarks),
            headlines=_headlines_block(headlines),
        )

        return BuiltReport(
            text=render_report(everything, benchmarks, comment, session_date, is_live),
            session_date=session_date,
            is_live=is_live,
        )

    async def _benchmarks_for(self, quotes: list[Quote]) -> dict[str, Quote]:
        """One local index per market the user actually holds.

        London gets the FTSE, Tokyo the Nikkei, and so on — comparing a Japanese
        stock against the S&P would say nothing. Yahoo charges nothing for these,
        and UZSE has no index to fetch.
        """
        wanted: dict[str, str] = {}
        for quote in quotes:
            market = market_by_code(quote.market)
            symbol = market.benchmark
            if symbol and market.code not in wanted:
                # The configured benchmark overrides the default for the US, so
                # BENCHMARK_TICKER=QQQ still works.
                wanted[market.code] = (
                    self._benchmark_ticker if market.code == "US" else symbol
                )
        if not wanted:
            return {}

        codes = list(wanted)
        results = await self._prices.get_quotes([wanted[c] for c in codes])
        return {code: results[wanted[code]] for code in codes}

    async def _uzse_quotes(self, tickers: list[str], *, scheduled: bool) -> list[Quote]:
        """UZSE quotes come from the cached snapshot; only `scheduled` may pay."""
        if not tickers or self._uzse is None or not self._uzse.enabled:
            return []
        try:
            await self._uzse.ensure_quotes(scheduled=scheduled)
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

        headlines = await self._headlines_for(quote, limit=3)
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

        await self._uzse.ensure_quotes(scheduled=False)
        # The per-company endpoint returns 20 sessions of history in one paid
        # request, and folds them into local history — so this is the command
        # that makes the week figure real, rather than waiting six days.
        detail = await self._uzse.fetch_detail(ticker)
        quote = self._uzse.get_quote(ticker)
        if not quote.ok:
            return render_ticker_report(quote, None, [])

        depth = self._uzse.history_depth(quote.ticker)
        facts = _facts_block([quote], None) + _uzse_detail_facts(detail)
        facts += (
            "\nExchange: Uzbek Stock Exchange (UZSE), currency UZS. This is a "
            "small, thinly traded market: many shares go days without a single "
            "trade, so a price can be stale and a percentage move can come from "
            "one small transaction."
            f"\nTrading sessions on record for this company: {depth}."
            "\nNo news feed exists for this exchange, so if the numbers do not "
            "explain a move, say the reason is not visible in the data."
        )
        comment = await self._ai.ticker_comment(
            ticker=quote.ticker, facts=facts, headlines=""
        )
        return render_ticker_report(
            quote, comment, [], history_depth=depth, detail=_detail_rows(detail)
        )

    async def build_scout_report(
        self, period: str, *, scheduled: bool = False
    ) -> tuple[str, bool]:
        """The scouting brief. Returns the message and whether it is worth sending."""
        if self._scout is None or self._uzse is None or not self._uzse.enabled:
            return "UZSE data is not configured, so there is nothing to scout.", False

        try:
            report = await self._scout.build(period, scheduled=scheduled)
        except BudgetExhausted as exc:
            logger.warning("scout skipped: %s", exc)
            return "The parse.bot budget for this month is used up.", False

        news = await self._scout_news(report)

        # Only a short ranked table reaches the model — the free Groq tier is
        # capped by tokens per day, and 120 rows of a thin market would burn it
        # for no gain.
        report.comment = await self._ai.scout_comment(
            period=period,
            facts=_scout_facts(report),
            headlines=_headlines_block(news),
        )
        return render_scout(report, news), not report.is_empty

    async def _scout_news(self, report) -> dict[str, list[str]]:
        if self._news is None or not self._news.enabled:
            return {}
        interesting = {
            row.ticker: row.name
            for row in report.turnover_leaders + report.movers + report.awakened
        }
        if not interesting:
            return {}
        headlines = await self._news.fetch()
        return match_headlines(headlines, interesting)

    async def _headlines_for_movers(self, quotes: list[Quote]) -> dict[str, list[str]]:
        if not self._ai.enabled:
            return {}
        movers = [q for q in quotes if q.ok][:NEWS_FOR_TOP_MOVERS]
        results = await asyncio.gather(
            *(self._headlines_for(q, limit=2) for q in movers)
        )
        return {q.ticker: titles for q, titles in zip(movers, results) if titles}

    async def _headlines_for(self, quote: Quote, limit: int = 3) -> list[str]:
        """Headlines for one company, from whichever source has them.

        The price provider is asked first because when it carries news the data
        is already paid for. Twelve Data has none on the free plan, so without
        the RSS fallback a report would explain a 20% drop with silence.
        """
        titles = await self._prices.get_headlines(quote.ticker, limit=limit)
        if titles or self._news is None or not self._news.enabled:
            return titles
        return await self._news.fetch_for_ticker(quote.ticker, quote.name, limit=limit)


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


def _facts_block(quotes: list[Quote], benchmarks) -> str:
    """Compact table for the model. Kept small on purpose: the free Groq tier
    is limited by tokens per day, not just requests."""
    lines = []
    for quote in quotes:
        if not quote.ok:
            continue
        name = quote.name or quote.ticker
        where = market_by_code(quote.market).label
        line = f"{quote.ticker} ({name}, {where}): price {quote.price:.2f} {quote.currency}"
        if quote.day_change_pct is not None:
            line += f", day {quote.day_change_pct:+.2f}%"
        if quote.week_change_pct is not None:
            line += f", week {quote.week_change_pct:+.2f}%"
        lines.append(line)
    if isinstance(benchmarks, Quote):
        benchmarks = {benchmarks.market: benchmarks}
    for code, index in (benchmarks or {}).items():
        if index and index.ok and index.day_change_pct is not None:
            lines.append(
                f"{index.ticker} ({market_by_code(code).label} index): "
                f"day {index.day_change_pct:+.2f}%"
            )
    return "\n".join(lines) or "(no price data available)"


def _uzse_detail_facts(detail: UzseDetail | None) -> str:
    """Extra numbers the per-company endpoint gives that the quotes feed does not."""
    if detail is None:
        return ""
    lines = []
    if detail.min_price and detail.max_price:
        lines.append(f"Today's range: {detail.min_price:,.2f} to {detail.max_price:,.2f} UZS")
    if detail.today_quantity is not None:
        lines.append(f"Shares traded today: {detail.today_quantity:,.0f}")
    if detail.today_volume is not None:
        lines.append(f"Money traded today: {detail.today_volume:,.0f} UZS")
    if len(detail.history) >= 2:
        closes = [detail.history[d] for d in sorted(detail.history)]
        lines.append(
            f"Last {len(closes)} closes, oldest first: "
            + ", ".join(f"{c:,.2f}" for c in closes)
        )
    return ("\n" + "\n".join(lines)) if lines else ""


def _detail_rows(detail: UzseDetail | None) -> list[tuple[str, str]]:
    """Label/value pairs shown above the AI commentary in a UZSE briefing."""
    if detail is None:
        return []
    rows: list[tuple[str, str]] = []
    if detail.min_price and detail.max_price:
        rows.append(
            ("Day range", f"{money(detail.min_price, 'UZS')} – {money(detail.max_price, 'UZS')}")
        )
    if detail.today_quantity:
        rows.append(("Shares traded", f"{detail.today_quantity:,.0f}".replace(",", " ")))
    if detail.today_volume:
        rows.append(("Money traded", money(detail.today_volume, "UZS")))
    if detail.history:
        closes = [detail.history[d] for d in sorted(detail.history)]
        rows.append(
            (
                f"{len(closes)}-session range",
                f"{money(min(closes), 'UZS')} – {money(max(closes), 'UZS')}",
            )
        )
    return rows


def _scout_facts(report: ScoutReport) -> str:
    """A compact ranked table for the model — never the whole exchange."""
    lines = [f"Period: {report.period}, {report.start} to {report.end}"]

    def block(title: str, rows) -> None:
        if not rows:
            return
        lines.append(title)
        for row in rows:
            parts = [f"{row.ticker} ({row.name or row.ticker})"]
            if row.change_pct is not None:
                parts.append(f"{row.change_pct:+.1f}%")
            if row.turnover:
                parts.append(f"{row.turnover:,.0f} UZS traded")
            parts.append(f"{row.sessions_traded} sessions, liquidity {row.liquidity}")
            if row.market_cap:
                parts.append(f"company value {row.market_cap:,.0f} UZS")
            if row.tags:
                parts.append("; ".join(row.tags))
            lines.append("  " + ", ".join(parts))

    block("Most money traded:", report.turnover_leaders)
    block("Biggest moves that were not noise:", report.movers)
    block("Started trading again after being quiet:", report.awakened)
    block("Moves on almost no money, treat as noise:", report.noise)
    if report.coverage_note:
        lines.append(report.coverage_note)
    return "\n".join(lines)


def _headlines_block(headlines: dict[str, list[str]]) -> str:
    lines = []
    for ticker, titles in headlines.items():
        for title in titles:
            lines.append(f"{ticker}: {title}")
    return "\n".join(lines)
