"""Prices from Twelve Data — the keyed alternative to Yahoo.

Yahoo needs no API key, which is why it is the default, but it identifies
callers by IP and rate-limits datacenter ranges hard: a VPS commonly gets
`429 Edge: Too Many Requests` on its very first request, and no amount of
retrying helps. Twelve Data authenticates with a key instead, so where the
server is hosted stops mattering.

Free tier at the time of writing: 800 requests a day, 8 per minute. This
provider asks for the whole watchlist in one batched request, so a report costs
one request rather than one per company.

Same interface as `PriceProvider`, so nothing outside this file changes.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from datetime import datetime

import httpx

from ..markets import detect_market
from .prices import Quote

logger = logging.getLogger(__name__)

BASE_URL = "https://api.twelvedata.com/time_series"
TIMEOUT_SECONDS = 30
CACHE_TTL_SECONDS = 120
# Seven sessions covers the previous close and the five-session week figure.
SESSIONS = 7
# The free plan allows eight symbols per batched request.
BATCH_SIZE = 8
# ...and eight credits a minute, where every symbol in a batch costs one. A
# report asks for the watchlist plus a benchmark index, which quietly exceeds
# that: the benchmark request comes back rate-limited and the "whole market"
# comparison silently disappears from the report. Requests are therefore
# metered rather than fired off as fast as they are wanted.
CREDITS_PER_MINUTE = 8

# A close that lands exactly on the low of an unusually wide session, with no
# corresponding move the next day, is the signature of one erroneous print
# being recorded as both the low and the close. Twelve Data served exactly this
# for QBTS on 2026-08-12: open 20.79, high 21.00, low and close both 16.21,
# against a real close of 20.74.
SUSPECT_RANGE = 0.20
SUSPECT_MOVE = 0.15


class CreditThrottle:
    """Keeps requests inside the provider's per-minute credit allowance."""

    def __init__(self, per_minute: int = CREDITS_PER_MINUTE) -> None:
        self._per_minute = per_minute
        self._spent: deque[float] = deque()

    async def take(self, credits: int) -> None:
        while True:
            now = time.monotonic()
            while self._spent and now - self._spent[0] >= 60:
                self._spent.popleft()
            if len(self._spent) + credits <= self._per_minute:
                self._spent.extend([now] * credits)
                return
            wait = 60 - (now - self._spent[0]) + 0.05
            logger.info("Twelve Data credit limit reached, waiting %.1fs", wait)
            await asyncio.sleep(wait)


class TwelveDataProvider:
    def __init__(self, api_key: str) -> None:
        self._api_key = api_key
        self._cache: dict[str, tuple[float, Quote]] = {}
        self._throttle = CreditThrottle()

    @property
    def enabled(self) -> bool:
        return bool(self._api_key)

    async def get_quotes(self, tickers: list[str]) -> dict[str, Quote]:
        """Fetch in batches. One failing symbol never fails the batch."""
        if not tickers:
            return {}

        wanted = [t.upper() for t in tickers]
        quotes: dict[str, Quote] = {}
        pending: list[str] = []

        for ticker in wanted:
            cached = self._cache.get(ticker)
            if cached and time.monotonic() - cached[0] < CACHE_TTL_SECONDS:
                quotes[ticker] = cached[1]
            else:
                pending.append(ticker)

        batches = [pending[i : i + BATCH_SIZE] for i in range(0, len(pending), BATCH_SIZE)]
        # Sequential, because the throttle has to see one batch's cost before
        # deciding whether the next one fits inside the minute.
        results = []
        for batch in batches:
            try:
                results.append(await self._fetch_batch(batch))
            except Exception as exc:  # noqa: BLE001 - one batch must not sink the rest
                results.append(exc)
        for batch, result in zip(batches, results):
            if isinstance(result, BaseException):
                logger.warning("Twelve Data batch failed: %s", result)
                for ticker in batch:
                    quotes[ticker] = Quote(
                        ticker=ticker,
                        error="data temporarily unavailable",
                        market=detect_market(ticker).code,
                    )
            else:
                quotes.update(result)

        for ticker, quote in quotes.items():
            if quote.ok:
                self._cache[ticker] = (time.monotonic(), quote)
        return {ticker: quotes[ticker] for ticker in wanted if ticker in quotes}

    async def get_quote(self, ticker: str) -> Quote:
        quotes = await self.get_quotes([ticker])
        return quotes.get(
            ticker.upper(),
            Quote(ticker=ticker.upper(), error="data temporarily unavailable"),
        )

    async def resolve_ticker(self, ticker: str) -> Quote:
        return await self.get_quote(ticker)

    async def get_headlines(self, ticker: str, limit: int = 3) -> list[str]:
        """Twelve Data has no news on the free plan.

        Reports simply carry no headlines; the AI is given the numbers alone and
        told to say when a move has no visible explanation.
        """
        return []

    # -- internals ----------------------------------------------------------

    async def _fetch_batch(self, tickers: list[str]) -> dict[str, Quote]:
        await self._throttle.take(len(tickers))
        params = {
            "symbol": ",".join(tickers),
            "interval": "1day",
            "outputsize": str(SESSIONS),
            "apikey": self._api_key,
        }
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
            response = await client.get(BASE_URL, params=params)
            response.raise_for_status()
            payload = response.json()

        # A single symbol comes back unwrapped; several come back keyed by symbol.
        if len(tickers) == 1:
            payload = {tickers[0]: payload}

        quotes: dict[str, Quote] = {}
        for ticker in tickers:
            entry = payload.get(ticker) if isinstance(payload, dict) else None
            quotes[ticker] = _to_quote(ticker, entry)

        # A batch that yields nothing usable is worth retrying one symbol at a
        # time: a whole blank section in the report is far worse than a few
        # extra requests.
        if tickers and not any(q.ok for q in quotes.values()) and len(tickers) > 1:
            logger.warning("Twelve Data batch of %s returned nothing; retrying singly",
                           len(tickers))
            for ticker in tickers:
                quotes[ticker] = (await self._fetch_batch([ticker]))[ticker]
        return quotes


def _to_quote(ticker: str, entry: object) -> Quote:
    market = detect_market(ticker).code
    if not isinstance(entry, dict):
        return Quote(ticker=ticker, error="unknown ticker or no data", market=market)

    if entry.get("status") == "error" or "values" not in entry:
        message = str(entry.get("message") or "unknown ticker or no data")
        # The plan's limits are worth surfacing verbatim rather than as a
        # generic failure, because the fix is different.
        if "limit" in message.lower():
            logger.warning("Twelve Data limit reached: %s", message)
            return Quote(ticker=ticker, error="data provider limit reached", market=market)
        return Quote(ticker=ticker, error=_short(message), market=market)

    values = entry.get("values") or []
    closes: list[tuple[str, float]] = []
    rows: dict[str, dict] = {}
    for row in values:
        if not isinstance(row, dict):
            continue
        close = _to_float(row.get("close"))
        session = str(row.get("datetime") or "")[:10]
        if close is not None and session:
            closes.append((session, close))
            rows[session] = row
    if not closes:
        return Quote(ticker=ticker, error="no price data", market=market)

    # Twelve Data returns newest first.
    closes.sort(key=lambda item: item[0], reverse=True)
    last_session, last = closes[0]
    previous = closes[1][1] if len(closes) >= 2 else None
    week_ago = closes[5][1] if len(closes) >= 6 else None

    suspect = looks_like_bad_tick(rows.get(last_session))
    meta = entry.get("meta") if isinstance(entry.get("meta"), dict) else {}
    return Quote(
        ticker=ticker,
        name=str(meta.get("symbol") or ticker),
        price=last,
        day_change=(last - previous) if previous else None,
        day_change_pct=((last - previous) / previous * 100) if previous else None,
        week_change_pct=((last - week_ago) / week_ago * 100) if week_ago else None,
        currency=str(meta.get("currency") or "USD").upper(),
        session_date=last_session,
        is_live=last_session == datetime.utcnow().date().isoformat(),
        market=market,
        suspect=suspect,
        note=(
            "this close looks like a data error, not a real move — check before "
            "believing it"
            if suspect
            else None
        ),
    )


def looks_like_bad_tick(row: dict | None) -> bool:
    """Does this session's bar look like one erroneous print rather than a move?

    The tell is a close sitting exactly on the extreme of an unusually wide
    session. A genuine crash closes somewhere inside its range far more often
    than precisely on the low.
    """
    if not isinstance(row, dict):
        return False
    opened = _to_float(row.get("open"))
    high = _to_float(row.get("high"))
    low = _to_float(row.get("low"))
    close = _to_float(row.get("close"))
    if not all(v and v > 0 for v in (opened, high, low, close)):
        return False

    span = (high - low) / high
    move = abs(close - opened) / opened
    return span > SUSPECT_RANGE and move > SUSPECT_MOVE and close in (low, high)


def _to_float(value: object) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _short(message: str, limit: int = 80) -> str:
    message = " ".join(message.split())
    return message if len(message) <= limit else message[: limit - 1] + "…"
