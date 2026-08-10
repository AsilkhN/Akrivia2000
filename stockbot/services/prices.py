"""Price data.

Source is Yahoo Finance through `yfinance`: no API key, no request quota to
juggle, and one call returns everything a report needs (last close, previous
close, a month of history for the weekly number).

Everything here is deliberately behind `PriceProvider`, so swapping Yahoo for
Finnhub or Twelve Data later means rewriting this one file and nothing else.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

import yfinance as yf

logger = logging.getLogger(__name__)

NY = ZoneInfo("America/New_York")

# Quotes are cached briefly so that ten users asking for the same ticker in the
# same minute cause one network call, not ten.
CACHE_TTL_SECONDS = 120


@dataclass
class Quote:
    ticker: str
    name: str | None = None
    price: float | None = None
    day_change: float | None = None
    day_change_pct: float | None = None
    week_change_pct: float | None = None
    currency: str = "USD"
    session_date: str | None = None  # YYYY-MM-DD of the bar being reported
    is_live: bool = False  # True when that bar is today and still moving
    market: str = "US"  # 'US' (Yahoo) or 'UZSE' (parse.bot)
    note: str | None = None  # short caveat shown under the numbers
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.price is not None


class PriceProvider:
    def __init__(self) -> None:
        self._cache: dict[str, tuple[float, Quote]] = {}

    async def get_quotes(self, tickers: list[str]) -> dict[str, Quote]:
        """Fetch quotes concurrently. A failing ticker never fails the batch."""
        results = await asyncio.gather(
            *(self.get_quote(t) for t in tickers), return_exceptions=True
        )
        quotes: dict[str, Quote] = {}
        for ticker, result in zip(tickers, results):
            if isinstance(result, BaseException):
                logger.warning("quote fetch crashed for %s: %s", ticker, result)
                quotes[ticker] = Quote(ticker=ticker, error="data temporarily unavailable")
            else:
                quotes[ticker] = result
        return quotes

    async def get_quote(self, ticker: str) -> Quote:
        ticker = ticker.upper()
        cached = self._cache.get(ticker)
        if cached and time.monotonic() - cached[0] < CACHE_TTL_SECONDS:
            return cached[1]

        quote = await asyncio.to_thread(self._fetch_quote_blocking, ticker)
        if quote.ok:
            self._cache[ticker] = (time.monotonic(), quote)
        return quote

    async def resolve_ticker(self, ticker: str) -> Quote:
        """Validate a symbol for /add and pick up the company name."""
        return await self.get_quote(ticker)

    async def get_headlines(self, ticker: str, limit: int = 3) -> list[str]:
        try:
            return await asyncio.to_thread(self._fetch_headlines_blocking, ticker, limit)
        except Exception as exc:  # noqa: BLE001 - news is a nice-to-have, never fatal
            logger.warning("news fetch failed for %s: %s", ticker, exc)
            return []

    # -- blocking internals -------------------------------------------------

    def _fetch_quote_blocking(self, ticker: str) -> Quote:
        try:
            handle = yf.Ticker(ticker)
            history = handle.history(period="1mo", interval="1d", auto_adjust=False)
        except Exception as exc:  # noqa: BLE001 - network/parse errors are expected
            logger.warning("history failed for %s: %s", ticker, exc)
            return Quote(ticker=ticker, error="data temporarily unavailable")

        if history is None or history.empty:
            return Quote(ticker=ticker, error="unknown ticker or no data")

        closes = history["Close"].dropna()
        if closes.empty:
            return Quote(ticker=ticker, error="no price data")

        last = float(closes.iloc[-1])
        prev = float(closes.iloc[-2]) if len(closes) >= 2 else None
        week_ago = float(closes.iloc[-6]) if len(closes) >= 6 else None

        last_index = closes.index[-1]
        session_date = last_index.date().isoformat()
        is_live = last_index.date() == datetime.now(NY).date()

        return Quote(
            ticker=ticker,
            name=self._short_name(handle, ticker),
            price=last,
            day_change=(last - prev) if prev else None,
            day_change_pct=((last - prev) / prev * 100) if prev else None,
            week_change_pct=((last - week_ago) / week_ago * 100) if week_ago else None,
            currency=self._currency(handle),
            session_date=session_date,
            is_live=is_live,
        )

    @staticmethod
    def _short_name(handle: "yf.Ticker", ticker: str) -> str:
        for attr in ("shortName", "longName", "displayName"):
            try:
                value = handle.info.get(attr)
            except Exception:  # noqa: BLE001 - `.info` is slow and flaky by nature
                return ticker
            if value:
                return str(value)
        return ticker

    @staticmethod
    def _currency(handle: "yf.Ticker") -> str:
        try:
            return str(handle.fast_info.get("currency") or "USD").upper()
        except Exception:  # noqa: BLE001
            return "USD"

    @staticmethod
    def _fetch_headlines_blocking(ticker: str, limit: int) -> list[str]:
        raw = yf.Ticker(ticker).news or []
        titles: list[str] = []
        for item in raw:
            # yfinance has moved this payload around between versions, so read
            # defensively rather than trusting one shape.
            content = item.get("content") if isinstance(item, dict) else None
            title = None
            if isinstance(content, dict):
                title = content.get("title")
            if not title and isinstance(item, dict):
                title = item.get("title")
            if title and title not in titles:
                titles.append(str(title))
            if len(titles) >= limit:
                break
        return titles
