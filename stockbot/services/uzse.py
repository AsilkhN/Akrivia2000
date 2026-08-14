"""Uzbek Stock Exchange (UZSE) data via parse.bot scrapers.

All five scraper endpoints are used, each at a cadence matched to what it costs
against a metered plan:

  get_stock_quotes   whole market in one response — the daily workhorse, 1/day
  get_securities     ticker → official company name — reference data, 1/month
  get_stock_detail   one company: 20 sessions of history, day range, volume —
                     1 per /ai, on demand only
  get_listings       share counts and listing category, which give company size
                     and separate shares from bonds — 1/month
  get_trade_results  the trade tape: money actually changing hands, which is the
                     only honest way to rank a move on a market this thin — 1
                     per scouting window

Spending rules, enforced in code rather than by discipline:

  * the whole-market quotes response is cached per trading day, so the daily
    report, /now, /list and /add validation all share one request
  * every snapshot's closing prices are appended to a local history table, so
    day and week changes cost nothing
  * a per-company detail response is cached per day too, and its 20 sessions of
    history are folded into that same table
  * a monthly counter refuses to spend past the limit, and keeps a reserve so
    interactive commands can never starve the scheduled report
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime
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

CACHE_QUOTES = "quotes"
CACHE_SECURITIES = "securities"
CACHE_LISTINGS = "listings"

# How far the exchange's own implied previous close may sit from the price we
# stored before the two are considered to describe different things. Rounding in
# a published `change_value` is worth tolerating; a percent of the price is not.
CATCH_UP_TOLERANCE = 0.02
# Fallback when the feed publishes no change at all: a jump this large without
# corroboration is not treated as a move.
CATCH_UP_MOVE = 0.25

# This exchange formats numbers US-style: "16,100" is sixteen thousand, not
# sixteen. Getting this wrong understates a price by a factor of a thousand,
# so the grouping pattern is matched explicitly rather than guessed at.
_US_GROUPED = re.compile(r"^-?\d{1,3}(?:,\d{3})+(?:\.\d+)?$")
_EU_GROUPED = re.compile(r"^-?\d{1,3}(?:\.\d{3})+(?:,\d+)?$")


@dataclass
class UzseQuote:
    ticker: str
    company: str | None
    security_code: str | None
    closing_price: float | None
    last_trade_price: float | None
    last_trade_date: str | None  # ISO
    change_value: float | None = None
    change_direction: str | None = None  # 'up' | 'down' | ''

    @property
    def stated_change(self) -> float | None:
        """How much the feed says this close moved, signed. This is the
        exchange's own arithmetic and outranks anything we compute ourselves."""
        if self.change_value is None:
            return None
        direction = (self.change_direction or "").strip().lower()
        if direction == "down":
            return -abs(self.change_value)
        if direction == "up":
            return abs(self.change_value)
        return 0.0 if self.change_value == 0 else None


@dataclass
class UzseTrade:
    ticker: str | None
    security_code: str | None
    issuer: str | None
    price: float | None
    quantity: float | None
    volume: float | None  # money changing hands, in UZS


@dataclass
class UzseListing:
    ticker: str
    security_code: str | None
    issuer: str | None
    category: str | None  # Premium / Standard / Bond / Privatizatsiya
    nominal_value: float | None
    shares_count: float | None
    listing_date: str | None

    @property
    def is_share(self) -> bool:
        """Bonds are listed alongside shares and are not what we scout for."""
        return (self.category or "").strip().lower() != "bond"


@dataclass
class UzseDetail:
    ticker: str
    security_code: str | None
    start_price: float | None = None
    max_price: float | None = None
    min_price: float | None = None
    today_quantity: float | None = None
    today_volume: float | None = None
    issue_value: float | None = None
    history: dict[str, float] = field(default_factory=dict)  # ISO date → close


class BudgetExhausted(RuntimeError):
    """Raised instead of spending a request the plan cannot afford."""


class UzseProvider:
    def __init__(
        self,
        storage: Storage,
        quotes_url: str,
        api_key: str,
        monthly_limit: int,
        reserve: int,
        securities_url: str = "",
        detail_url: str = "",
        trades_url: str = "",
        listings_url: str = "",
        auth_header: str = "Authorization",
        auth_scheme: str = "Bearer",
        method: str = "GET",
    ) -> None:
        self._storage = storage
        self._quotes_url = quotes_url
        self._securities_url = securities_url
        self._detail_url = detail_url
        self._trades_url = trades_url
        self._listings_url = listings_url
        self._api_key = api_key
        self._monthly_limit = monthly_limit
        self._reserve = reserve
        self._auth_header = auth_header
        self._auth_scheme = auth_scheme
        self._method = method.upper()

    @property
    def enabled(self) -> bool:
        return bool(self._quotes_url and self._api_key)

    @property
    def detail_enabled(self) -> bool:
        return bool(self._detail_url and self._api_key)

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
        return self.credits_remaining() > (0 if scheduled else self._reserve)

    # -- cached fetching ----------------------------------------------------

    def _cache_is_fresh(self, key: str) -> bool:
        cached = self._storage.load_cache(key)
        if cached is None:
            return False
        try:
            fetched = datetime.fromisoformat(cached[1]).replace(tzinfo=ZoneInfo("UTC"))
        except ValueError:
            return False
        return fetched.astimezone(TASHKENT).date() == datetime.now(TASHKENT).date()

    def snapshot_is_fresh(self) -> bool:
        return self._cache_is_fresh(CACHE_QUOTES)

    async def ensure_quotes(self, *, scheduled: bool = False) -> bool:
        """Refresh the whole-market snapshot if today's is missing. Costs ≤1."""
        if not self.enabled or self._cache_is_fresh(CACHE_QUOTES):
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

        payload = await self._request(self._quotes_url)
        if payload is None:
            return False

        quotes = parse_quotes(payload)
        session_date = _latest_session(quotes)
        self._storage.save_cache(CACHE_QUOTES, payload, session_date)

        # Store each company's close under the date it actually last traded,
        # not under today's date — this market has stocks that go days without
        # a trade, and back-filling them would invent price movements.
        for quote in quotes.values():
            if quote.closing_price is not None and quote.last_trade_date:
                self._storage.save_history(
                    {quote.ticker: quote.closing_price},
                    quote.last_trade_date,
                    breaks=[quote.ticker] if self._is_catch_up(quote) else [],
                )

        logger.info(
            "parse.bot quotes: %s tickers, session %s, %s credits used this month",
            len(quotes),
            session_date,
            self.credits_used(),
        )
        return True

    def _is_catch_up(self, quote: UzseQuote) -> bool:
        """Is this close a bookkeeping correction rather than a market move?

        UZSE's `closing_price` is an official reference that can trail the
        trades it describes by days. When it finally catches up, subtracting the
        stale figure we stored earlier produces a jump nobody experienced — that
        is where the scout's fictional +110% moves came from.

        The feed answers this itself: `change_value` is the move the exchange
        claims for this close, so `close − change` is the exchange's own
        previous close. If that disagrees with what we have on file, our stored
        price belongs to a different regime and must not be compared across.
        """
        close = quote.closing_price
        if close is None or close <= 0:
            return False
        stored = self._storage.latest_history_point(quote.ticker)
        if stored is None or not quote.last_trade_date:
            return False
        stored_date, stored_price = stored
        if stored_date >= quote.last_trade_date or stored_price <= 0:
            return False  # same session re-read, or older data arriving late

        stated = quote.stated_change
        if stated is None:
            # No corroboration available. Only an implausible jump is treated as
            # a break, so ordinary moves on quiet tickers still accumulate.
            return abs(close - stored_price) / stored_price > CATCH_UP_MOVE
        implied_previous = close - stated
        return abs(implied_previous - stored_price) > max(1.0, close * CATCH_UP_TOLERANCE)

    async def fetch_detail(self, ticker: str, *, scheduled: bool = False) -> UzseDetail | None:
        """Per-company detail with 20 sessions of history. Costs ≤1 per day."""
        ticker = ticker.upper()
        key = f"detail:{ticker}"
        if not self._cache_is_fresh(key):
            if not self.detail_enabled or not self._can_spend(scheduled=scheduled):
                cached = self._storage.load_cache(key)
                return parse_detail(cached[0]) if cached else None

            url = self._detail_url.format(
                ticker=ticker, security_code=self.security_code(ticker) or ticker
            )
            payload = await self._request(url)
            if payload is not None:
                detail = parse_detail(payload)
                self._storage.save_cache(key, payload, _max_date(detail.history))
                if detail.history:
                    # 20 sessions in one response — this is what makes the week
                    # figure available immediately instead of after six days.
                    self._storage.save_history_series(ticker, detail.history)
                return detail

        cached = self._storage.load_cache(key)
        return parse_detail(cached[0]) if cached else None

    async def ensure_securities(self, *, scheduled: bool = False) -> bool:
        """Official company names. Reference data — refreshed at most monthly."""
        if not self._securities_url or not self._api_key:
            return False
        cached = self._storage.load_cache(CACHE_SECURITIES)
        if cached is not None:
            try:
                fetched = datetime.fromisoformat(cached[1])
            except ValueError:
                fetched = None
            if fetched and (datetime.utcnow() - fetched).days < 30:
                return False
        if not self._can_spend(scheduled=scheduled):
            return False

        payload = await self._request(self._securities_url)
        if payload is None:
            return False
        self._storage.save_cache(CACHE_SECURITIES, payload, None)
        return True

    async def fetch_trades(
        self, date_from: str, date_to: str, *, scheduled: bool = False
    ) -> list[UzseTrade]:
        """The trade tape for a date range — who actually moved money.

        On an exchange this thin, turnover is the only honest way to rank a
        move: a 40% jump on two hundred sums is noise, and percentage alone
        cannot tell the difference.

        The endpoint caps its response, so this may cover only the largest or
        most recent trades in the window. `trades_coverage` reports what came
        back so the report can say so rather than implying completeness.
        """
        key = f"trades:{date_from}:{date_to}"
        if not self._cache_is_fresh(key):
            if not self._trades_url or not self._can_spend(scheduled=scheduled):
                cached = self._storage.load_cache(key)
                return parse_trades(cached[0]) if cached else []
            url = self._trades_url.format(date_from=date_from, date_to=date_to)
            payload = await self._request(url)
            if payload is not None:
                self._storage.save_cache(key, payload, date_to)
                return parse_trades(payload)
        cached = self._storage.load_cache(key)
        return parse_trades(cached[0]) if cached else []

    async def ensure_listings(self, *, scheduled: bool = False) -> bool:
        """Share counts and listing categories. Refreshed at most monthly.

        Multiplying `shares_count` by the price gives a company's market value,
        which is what separates a real business from a shell that happens to
        have moved 20%.
        """
        if not self._listings_url or not self._api_key:
            return False
        cached = self._storage.load_cache(CACHE_LISTINGS)
        if cached is not None:
            try:
                if (datetime.utcnow() - datetime.fromisoformat(cached[1])).days < 30:
                    return False
            except ValueError:
                pass
        if not self._can_spend(scheduled=scheduled):
            return False

        payload = await self._request(self._listings_url)
        if payload is None:
            return False
        self._storage.save_cache(CACHE_LISTINGS, payload, None)
        return True

    def listings(self) -> dict[str, UzseListing]:
        cached = self._storage.load_cache(CACHE_LISTINGS)
        return parse_listings(cached[0]) if cached else {}

    async def _request(self, url: str) -> str | None:
        headers = {self._auth_header: f"{self._auth_scheme} {self._api_key}".strip()}
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
                response = await client.request(self._method, url, headers=headers)
                response.raise_for_status()
        except Exception as exc:  # noqa: BLE001 - never break a report over UZSE
            logger.warning("parse.bot request to %s failed: %s", url, exc)
            return None

        # The credit is spent the moment the request succeeds, whatever we then
        # make of the body — so count it before parsing.
        self._storage.budget_spend(PROVIDER, self.current_month())
        return response.text

    # -- reading the cache --------------------------------------------------

    def _quotes(self) -> dict[str, UzseQuote]:
        cached = self._storage.load_cache(CACHE_QUOTES)
        return parse_quotes(cached[0]) if cached else {}

    def _securities(self) -> dict[str, tuple[str, str]]:
        cached = self._storage.load_cache(CACHE_SECURITIES)
        return parse_securities(cached[0]) if cached else {}

    def known_tickers(self) -> set[str]:
        """Every UZSE symbol we have seen. Free — reads cache only."""
        return set(self._quotes()) | set(self._securities())

    def security_code(self, ticker: str) -> str | None:
        ticker = ticker.upper()
        quote = self._quotes().get(ticker)
        if quote and quote.security_code:
            return quote.security_code
        listed = self._securities().get(ticker)
        return listed[0] if listed else None

    def company_name(self, ticker: str) -> str | None:
        """The short trading name, e.g. "Kvarts AJ".

        The quotes feed abbreviates ("<Kvarts> AJ") while the securities
        register spells the legal name out in full ("<Kvarts> aksiyadorlik
        jamiyati"); the short one survives a two-line message intact.
        """
        ticker = ticker.upper()
        quote = self._quotes().get(ticker)
        if quote and quote.company:
            return _clean_company(quote.company)
        listed = self._securities().get(ticker)
        return _clean_company(listed[1]) if listed else None

    def history_depth(self, ticker: str) -> int:
        return len(self._storage.get_history(ticker, limit=30))

    def get_quote(self, ticker: str) -> Quote:
        """Build a Quote from the cached snapshot and local history. Free."""
        ticker = ticker.upper()
        quotes = self._quotes()
        if not quotes:
            # No snapshot at all is a configuration or connectivity problem, not
            # an unknown ticker — saying "not listed" would send the reader off
            # to check a symbol that was never the issue.
            return Quote(
                ticker=ticker,
                error="no UZSE data yet — check PARSEBOT_QUOTES_URL and the server logs",
                currency=CURRENCY,
                market=MARKET,
            )
        row = quotes.get(ticker)
        if row is None:
            return Quote(
                ticker=ticker,
                error="not listed on UZSE",
                currency=CURRENCY,
                market=MARKET,
            )
        if row.closing_price is None:
            return Quote(
                ticker=ticker,
                name=self.company_name(ticker),
                error="no closing price in the latest UZSE data",
                currency=CURRENCY,
                market=MARKET,
            )

        history = self._storage.get_history(ticker, limit=12)
        week_ago = history[5][1] if len(history) >= 6 else None

        # The exchange publishes the day's move itself; prefer it over
        # subtracting our own stored close, which may be a stale official price
        # from before a catch-up. Fall back to the series only when the feed
        # says nothing.
        day_change = row.stated_change
        if day_change is None:
            stored_previous = history[1][1] if len(history) >= 2 else None
            day_change = (
                (row.closing_price - stored_previous) if stored_previous else None
            )
        previous = (row.closing_price - day_change) if day_change is not None else None

        return Quote(
            ticker=ticker,
            name=self.company_name(ticker),
            price=row.closing_price,
            day_change=day_change,
            day_change_pct=(
                day_change / previous * 100 if day_change is not None and previous else None
            ),
            week_change_pct=(
                (row.closing_price - week_ago) / week_ago * 100 if week_ago else None
            ),
            currency=CURRENCY,
            session_date=row.last_trade_date,
            is_live=False,
            market=MARKET,
            note=self._staleness_note(row),
        )

    def _staleness_note(self, row: UzseQuote) -> str | None:
        """Many UZSE shares go days without a trade — say so instead of implying
        the price is current."""
        latest = _latest_session(self._quotes())
        if not latest or not row.last_trade_date or row.last_trade_date >= latest:
            return None
        try:
            gap = (date.fromisoformat(latest) - date.fromisoformat(row.last_trade_date)).days
        except ValueError:
            return None
        day_word = "day" if gap == 1 else "days"
        return f"no trades for {gap} {day_word} — price is from {row.last_trade_date}"


# -- parsing ----------------------------------------------------------------


def to_float(value: object) -> float | None:
    """Parse the exchange's number formats without losing a factor of 1000."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(" ", "").replace(" ", "")
    text = text.replace("UZS", "")
    if not text:
        return None
    if _US_GROUPED.match(text):  # 16,100 / 50,999.99
        return _safe_float(text.replace(",", ""))
    if _EU_GROUPED.match(text):  # 16.100 / 50.999,99
        return _safe_float(text.replace(".", "").replace(",", "."))
    return _safe_float(text.replace(",", "."))  # 0.19 / 5,7


def _safe_float(text: str) -> float | None:
    try:
        return float(text)
    except ValueError:
        return None


def _payload(raw: str) -> dict:
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        logger.warning("parse.bot payload is not JSON")
        return {}
    if not isinstance(data, dict):
        return {}
    inner = data.get("data")
    return inner if isinstance(inner, dict) else data


def parse_quotes(raw: str) -> dict[str, UzseQuote]:
    """Whole-market quotes, keyed by ticker.

    The feed repeats rows, so later duplicates are dropped unless they carry a
    newer trade date.
    """
    rows = _payload(raw).get("quotes")
    if not isinstance(rows, list):
        logger.warning("parse.bot quotes payload has no 'quotes' list")
        return {}

    quotes: dict[str, UzseQuote] = {}
    for row in rows:
        if not isinstance(row, dict) or not row.get("ticker"):
            continue
        ticker = str(row["ticker"]).strip().upper()
        candidate = UzseQuote(
            ticker=ticker,
            company=row.get("company") or None,
            security_code=row.get("security_code") or None,
            closing_price=to_float(row.get("closing_price")),
            last_trade_price=to_float(row.get("last_trade_price")),
            last_trade_date=_iso_date(row.get("last_trade_date")),
            change_value=to_float(row.get("change_value")),
            change_direction=(str(row.get("change_direction") or "").strip() or None),
        )
        existing = quotes.get(ticker)
        if existing is None or _newer(candidate, existing):
            quotes[ticker] = candidate
    return quotes


def _newer(candidate: UzseQuote, existing: UzseQuote) -> bool:
    return (candidate.last_trade_date or "") > (existing.last_trade_date or "")


def parse_securities(raw: str) -> dict[str, tuple[str, str]]:
    """ticker → (security_code, company_name)."""
    rows = _payload(raw).get("securities")
    if not isinstance(rows, list):
        return {}
    return {
        str(row["ticker"]).strip().upper(): (
            str(row.get("security_code") or ""),
            str(row.get("company_name") or ""),
        )
        for row in rows
        if isinstance(row, dict) and row.get("ticker")
    }


def parse_detail(raw: str) -> UzseDetail:
    """One company: intraday range, volume, and its recent closing prices."""
    data = _payload(raw)
    history: dict[str, float] = {}
    for row in data.get("historical_data") or []:
        if not isinstance(row, dict):
            continue
        session = _iso_date(row.get("date"))
        close = to_float(row.get("closing_price"))
        if session and close is not None:
            history[session] = close

    return UzseDetail(
        ticker=str(data.get("ticker") or "").upper(),
        security_code=data.get("security_code") or None,
        start_price=to_float(data.get("start_price")),
        max_price=to_float(data.get("max_price")),
        min_price=to_float(data.get("min_price")),
        today_quantity=to_float(data.get("today_quantity")),
        today_volume=to_float(data.get("today_volume")),
        issue_value=to_float(data.get("issue_value")),
        history=history,
    )


def parse_trades(raw: str) -> list[UzseTrade]:
    """The trade tape. `security_code` arrives with the ticker glued on the end
    (UZ7058980010UZNF), so the ticker is recovered from that suffix."""
    rows = _payload(raw).get("trades") or _payload(raw).get("recent_trades")
    if not isinstance(rows, list):
        return []
    trades: list[UzseTrade] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        code = str(row.get("security_code") or "")
        trades.append(
            UzseTrade(
                ticker=_ticker_from_code(code),
                security_code=code[:12] or None,
                issuer=_clean_company(row.get("issuer")),
                price=to_float(row.get("trade_price")),
                quantity=to_float(row.get("quantity")),
                volume=to_float(row.get("volume")),
            )
        )
    return trades


def turnover_by_ticker(trades: list[UzseTrade]) -> dict[str, float]:
    """Money traded per company, summed across the tape."""
    totals: dict[str, float] = {}
    for trade in trades:
        if trade.ticker and trade.volume:
            totals[trade.ticker] = totals.get(trade.ticker, 0.0) + trade.volume
    return totals


def _ticker_from_code(code: str) -> str | None:
    """UZ7058980010UZNF → UZNF. ISIN-style codes are 12 characters."""
    text = code.strip().upper()
    if len(text) > 12:
        return text[12:] or None
    return None


def parse_listings(raw: str) -> dict[str, UzseListing]:
    rows = _payload(raw).get("listings")
    if not isinstance(rows, list):
        return {}
    listings: dict[str, UzseListing] = {}
    for row in rows:
        if not isinstance(row, dict) or not row.get("ticker"):
            continue
        ticker = str(row["ticker"]).strip().upper()
        listings[ticker] = UzseListing(
            ticker=ticker,
            security_code=row.get("security_code") or None,
            issuer=_clean_company(row.get("issuer")),
            category=row.get("category") or None,
            nominal_value=to_float(row.get("nominal_value")),
            shares_count=to_float(row.get("shares_count")),
            listing_date=_iso_date(row.get("listing_date")),
        )
    return listings


def _clean_company(name: str | None) -> str | None:
    """Strip the angle brackets the exchange wraps trading names in."""
    if not name:
        return None
    return name.replace("<", "").replace(">", "").strip() or None


def _iso_date(value: object) -> str | None:
    if not value:
        return None
    text = str(value).strip()[:10]
    for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _latest_session(quotes: dict[str, UzseQuote]) -> str | None:
    dates = [q.last_trade_date for q in quotes.values() if q.last_trade_date]
    return max(dates) if dates else None


def _max_date(history: dict[str, float]) -> str | None:
    return max(history) if history else None
