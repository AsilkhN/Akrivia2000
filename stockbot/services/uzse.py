"""Uzbek Stock Exchange (UZSE) prices via a parse.bot scraper.

This provider is metered: the parse.bot plan allows a fixed number of requests
per month, so the rules here are strict.

  * One paid request per trading day, and only from the scheduled daily job.
  * That request fetches the **whole** exchange, and the raw response is cached
    in SQLite. Every /now, /add, /list and /ai for the rest of the day is served
    from that cache and costs nothing.
  * Each snapshot's prices are appended to a local history table, so day and
    week changes are computed from our own records rather than paid for.
  * A hard monthly counter refuses to spend past the limit, keeping a small
    reserve so the daily report never loses to an interactive command.

Worst case that is roughly 31 requests a month against a 200 request plan.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

from ..storage import Storage
from .prices import Quote

logger = logging.getLogger(__name__)

PROVIDER = "parsebot"
MARKET = "UZSE"
TASHKENT = ZoneInfo("Asia/Tashkent")
CURRENCY = "UZS"
TIMEOUT_SECONDS = 60

# Field names the parser will accept, in priority order. parse.bot names columns
# after the scraped page, so both English and Russian headings are covered.
TICKER_KEYS = ("ticker", "symbol", "code", "isin", "тикер", "код", "символ")
NAME_KEYS = ("name", "company", "issuer", "title", "наименование", "эмитент", "компания")
PRICE_KEYS = ("price", "last", "close", "last_price", "cena", "цена", "курс", "закрытие")
VOLUME_KEYS = ("volume", "vol", "turnover", "объем", "объём", "оборот")
DATE_KEYS = ("date", "session_date", "trade_date", "дата")
CONTAINER_KEYS = ("data", "results", "rows", "items", "records", "output")


@dataclass
class UzseRow:
    ticker: str
    name: str | None
    price: float | None
    volume: float | None = None
    session_date: str | None = None
    extra: dict = field(default_factory=dict)


class BudgetExhausted(RuntimeError):
    """Raised instead of spending a request the plan cannot afford."""


class UzseProvider:
    def __init__(
        self,
        storage: Storage,
        api_url: str,
        api_key: str,
        monthly_limit: int,
        reserve: int,
        auth_header: str = "Authorization",
        auth_scheme: str = "Bearer",
        method: str = "GET",
    ) -> None:
        self._storage = storage
        self._api_url = api_url
        self._api_key = api_key
        self._monthly_limit = monthly_limit
        self._reserve = reserve
        self._auth_header = auth_header
        self._auth_scheme = auth_scheme
        self._method = method.upper()

    @property
    def enabled(self) -> bool:
        return bool(self._api_url and self._api_key)

    # -- budget -------------------------------------------------------------

    @staticmethod
    def current_month() -> str:
        return datetime.now(TASHKENT).strftime("%Y-%m")

    def credits_used(self) -> int:
        return self._storage.budget_used(PROVIDER, self.current_month())

    def credits_remaining(self) -> int:
        return max(0, self._monthly_limit - self.credits_used())

    def seed_credits_used(self, already_used: int) -> None:
        self._storage.budget_seed(PROVIDER, self.current_month(), already_used)

    def _can_spend(self, *, scheduled: bool) -> bool:
        """Interactive commands must leave the reserve for the daily reports."""
        floor = 0 if scheduled else self._reserve
        return self.credits_remaining() > floor

    # -- snapshot -----------------------------------------------------------

    def snapshot_is_fresh(self) -> bool:
        """True when today's snapshot is already cached, so nothing must be paid."""
        cached = self._storage.load_snapshot()
        if cached is None:
            return False
        _, fetched_at, _ = cached
        try:
            fetched = datetime.fromisoformat(fetched_at).replace(tzinfo=ZoneInfo("UTC"))
        except ValueError:
            return False
        return fetched.astimezone(TASHKENT).date() == datetime.now(TASHKENT).date()

    async def ensure_snapshot(self, *, scheduled: bool = False) -> bool:
        """Make sure a snapshot is available. Returns True if a request was paid.

        Never spends a credit when today's data is already cached, and never
        lets an interactive command eat into the reserve.
        """
        if not self.enabled or self.snapshot_is_fresh():
            return False
        if not self._can_spend(scheduled=scheduled):
            logger.warning(
                "parse.bot budget guard: %s of %s credits used, refusing to fetch",
                self.credits_used(),
                self._monthly_limit,
            )
            if scheduled:
                raise BudgetExhausted(
                    f"monthly limit of {self._monthly_limit} parse.bot requests reached"
                )
            return False

        payload = await self._fetch_blocking_safe()
        if payload is None:
            return False

        rows = parse_rows(payload)
        session_date = _session_date(rows)
        self._storage.save_snapshot(payload, session_date)
        prices = {r.ticker: r.price for r in rows if r.price is not None}
        if prices and session_date:
            self._storage.save_history(prices, session_date)
        logger.info(
            "parse.bot snapshot: %s tickers for session %s, %s credits used this month",
            len(rows),
            session_date,
            self.credits_used(),
        )
        return True

    async def _fetch_blocking_safe(self) -> str | None:
        headers = {self._auth_header: f"{self._auth_scheme} {self._api_key}".strip()}
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
                response = await client.request(self._method, self._api_url, headers=headers)
                response.raise_for_status()
        except Exception as exc:  # noqa: BLE001 - never break a report over UZSE
            logger.warning("parse.bot request failed: %s", exc)
            return None

        # The credit is spent the moment the request succeeds, whatever we then
        # make of the body — so count it before parsing.
        self._storage.budget_spend(PROVIDER, self.current_month())
        return response.text

    def _cached_rows(self) -> dict[str, UzseRow]:
        cached = self._storage.load_snapshot()
        if cached is None:
            return {}
        payload, _, _ = cached
        return {row.ticker: row for row in parse_rows(payload)}

    # -- quotes -------------------------------------------------------------

    def known_tickers(self) -> set[str]:
        """Every UZSE symbol in the cached snapshot. Free — no request."""
        return set(self._cached_rows())

    def get_quote(self, ticker: str) -> Quote:
        """Build a Quote from cache plus locally accumulated history. Free."""
        ticker = ticker.upper()
        row = self._cached_rows().get(ticker)
        if row is None:
            return Quote(
                ticker=ticker,
                error="not listed on UZSE, or no snapshot yet",
                currency=CURRENCY,
                market=MARKET,
            )
        if row.price is None:
            return Quote(
                ticker=ticker,
                name=row.name,
                error="no price in the latest UZSE data",
                currency=CURRENCY,
                market=MARKET,
            )

        history = self._storage.get_history(ticker, limit=8)
        previous = history[1][1] if len(history) >= 2 else None
        week_ago = history[5][1] if len(history) >= 6 else None

        return Quote(
            ticker=ticker,
            name=row.name,
            price=row.price,
            day_change=(row.price - previous) if previous else None,
            day_change_pct=((row.price - previous) / previous * 100) if previous else None,
            week_change_pct=((row.price - week_ago) / week_ago * 100) if week_ago else None,
            currency=CURRENCY,
            session_date=row.session_date or (history[0][0] if history else None),
            is_live=False,
            market=MARKET,
        )

    def history_depth(self, ticker: str) -> int:
        """How many sessions we have stored — day/week changes need 2 and 6."""
        return len(self._storage.get_history(ticker, limit=10))


# -- parsing ----------------------------------------------------------------
# Isolated on purpose: parse.bot names its columns after the scraped page, so
# this is the single place to adjust once the real response shape is known.


def parse_rows(payload: str) -> list[UzseRow]:
    try:
        data = json.loads(payload)
    except (TypeError, ValueError):
        logger.warning("parse.bot payload is not JSON")
        return []

    records = _find_records(data)
    rows: list[UzseRow] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        ticker = _pick_str(record, TICKER_KEYS)
        if not ticker:
            continue
        rows.append(
            UzseRow(
                ticker=ticker.upper(),
                name=_pick_str(record, NAME_KEYS),
                price=_pick_float(record, PRICE_KEYS),
                volume=_pick_float(record, VOLUME_KEYS),
                session_date=_normalise_date(_pick_str(record, DATE_KEYS)),
                extra=record,
            )
        )
    if not rows:
        logger.warning("parse.bot payload had no recognisable ticker rows")
    return rows


def _find_records(data: object) -> list:
    """Locate the list of rows, whatever wrapper the scraper puts around it."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in CONTAINER_KEYS:
            value = data.get(key)
            if isinstance(value, list):
                return value
        # Fall back to the first list of dicts found anywhere in the object.
        for value in data.values():
            if isinstance(value, list) and value and isinstance(value[0], dict):
                return value
            if isinstance(value, dict):
                nested = _find_records(value)
                if nested:
                    return nested
    return []


def _lookup(record: dict, keys: tuple[str, ...]) -> object | None:
    lowered = {str(k).strip().lower(): v for k, v in record.items()}
    for key in keys:
        if key in lowered and lowered[key] not in (None, ""):
            return lowered[key]
    # Tolerate decorated headings such as "Last price, UZS".
    for candidate, value in lowered.items():
        if value in (None, ""):
            continue
        if any(candidate.startswith(key) for key in keys):
            return value
    return None


def _pick_str(record: dict, keys: tuple[str, ...]) -> str | None:
    value = _lookup(record, keys)
    return str(value).strip() if value is not None else None


def _pick_float(record: dict, keys: tuple[str, ...]) -> float | None:
    value = _lookup(record, keys)
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    # Prices arrive as text like "12 500,00" or "1,234.50".
    text = str(value).strip().replace(" ", "").replace(" ", "")
    if "," in text and "." in text:
        text = text.replace(",", "")
    else:
        text = text.replace(",", ".")
    text = "".join(ch for ch in text if ch.isdigit() or ch in ".-")
    try:
        return float(text)
    except ValueError:
        return None


def _normalise_date(value: str | None) -> str | None:
    if not value:
        return None
    text = value.strip()[:10]
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _session_date(rows: list[UzseRow]) -> str | None:
    """The trading date the snapshot describes, falling back to today in Tashkent."""
    dates = sorted({row.session_date for row in rows if row.session_date})
    if dates:
        return dates[-1]
    return datetime.now(TASHKENT).date().isoformat()
