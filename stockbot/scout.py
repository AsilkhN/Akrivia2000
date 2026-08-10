"""Scouting: what happened across the Uzbek exchange, and what deserves a look.

The daily report answers "how are my companies doing". This answers a different
question — "is anything happening on this exchange that I should know about" —
and it scans all listed securities, not just a watchlist. That asymmetry is
deliberate: UZSE shares are the ones actually buyable from Uzbekistan, so
discovery is worth something there and academic elsewhere.

The central problem this module exists to solve is that **percentage change is
almost meaningless on a market this thin.** KASU trades at 0.01 sums; a single
trade moves it 100%. Ranking by percentage would fill every report with
garbage. So:

  * moves are ranked by the money that actually changed hands, not by percent
  * anything below a turnover floor is quarantined in a "noise" bucket that the
    report shows *as* noise, rather than hiding
  * a company's size (price × shares outstanding) separates a real business
    from a shell that happened to tick
  * how many sessions a share traded in the window is reported, because "up 9%"
    on one trade and "up 9%" on five days of trading are different facts

All of it runs off data already paid for: the daily whole-market snapshot, the
history it accumulates, and two cheap reference endpoints.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta

from .services.uzse import UzseListing, UzseProvider, turnover_by_ticker
from .storage import Storage

logger = logging.getLogger(__name__)

# A share priced in fractions of a sum moves 50% on rounding alone.
MIN_SENSIBLE_PRICE = 1.0
# Below this much money changing hands, a percentage move is not a market
# opinion — it is one person selling a handful of shares.
NOISE_TURNOVER_UZS = 5_000_000.0
GOOD_TURNOVER_UZS = 100_000_000.0
# Sessions of silence before trading again counts as "woke up".
QUIET_SESSIONS = 5

DAILY_WINDOW_DAYS = 4  # covers a weekend, so Monday still has Friday to compare
WEEKLY_WINDOW_DAYS = 8

MAX_MOVERS = 6
MAX_TURNOVER_ROWS = 5
MAX_AWAKENED = 4
MAX_NOISE = 3


@dataclass
class ScoutRow:
    ticker: str
    name: str | None = None
    price: float | None = None
    change_pct: float | None = None
    sessions_traded: int = 0
    turnover: float | None = None
    market_cap: float | None = None
    category: str | None = None
    tags: list[str] = field(default_factory=list)

    @property
    def liquidity(self) -> str:
        if self.turnover is None:
            return "unknown"
        if self.turnover >= GOOD_TURNOVER_UZS:
            return "good"
        if self.turnover >= NOISE_TURNOVER_UZS:
            return "moderate"
        return "thin"

    @property
    def is_noise(self) -> bool:
        """A move we should actively tell the reader to ignore."""
        if self.price is not None and self.price < MIN_SENSIBLE_PRICE:
            return True
        if self.turnover is not None and self.turnover < NOISE_TURNOVER_UZS:
            return abs(self.change_pct or 0) > 5
        # With no turnover data, a single-session move is the weak signal.
        return self.sessions_traded <= 1 and abs(self.change_pct or 0) > 20


@dataclass
class ScoutReport:
    period: str  # 'daily' | 'weekly'
    start: str
    end: str
    movers: list[ScoutRow] = field(default_factory=list)
    turnover_leaders: list[ScoutRow] = field(default_factory=list)
    awakened: list[ScoutRow] = field(default_factory=list)
    noise: list[ScoutRow] = field(default_factory=list)
    news: dict[str, list[str]] = field(default_factory=dict)
    coverage_note: str | None = None
    comment: str | None = None

    @property
    def is_empty(self) -> bool:
        return not (self.movers or self.turnover_leaders or self.awakened)


class Scout:
    def __init__(self, storage: Storage, uzse: UzseProvider) -> None:
        self._storage = storage
        self._uzse = uzse

    async def build(self, period: str, *, scheduled: bool = False) -> ScoutReport:
        window = WEEKLY_WINDOW_DAYS if period == "weekly" else DAILY_WINDOW_DAYS
        today = date.today()
        start = (today - timedelta(days=window)).isoformat()

        await self._uzse.ensure_quotes(scheduled=scheduled)
        await self._uzse.ensure_listings(scheduled=scheduled)

        trades = await self._uzse.fetch_trades(start, today.isoformat(), scheduled=scheduled)
        turnover = turnover_by_ticker(trades)
        listings = self._uzse.listings()
        history = self._storage.history_since(start)

        # When the database itself is young every ticker looks brand new, which
        # says nothing about the market. Only flag a listing as new if the
        # records around it go back further.
        records_begin = min(
            (series[0][0] for series in history.values() if series), default=None
        )
        rows = [
            row
            for ticker, series in history.items()
            if (
                row := self._build_row(
                    ticker, series, turnover, listings, records_begin
                )
            )
            is not None
        ]

        report = ScoutReport(
            period=period,
            start=start,
            end=today.isoformat(),
            coverage_note=_coverage_note(trades, turnover),
        )
        if not rows:
            return report

        signal = [r for r in rows if not r.is_noise and r.change_pct]
        report.turnover_leaders = sorted(
            (r for r in rows if r.turnover and not r.is_noise),
            key=lambda r: -(r.turnover or 0),
        )[:MAX_TURNOVER_ROWS]
        # Each company appears once: the money section already made its case.
        shown = {r.ticker for r in report.turnover_leaders}
        report.movers = [
            r for r in sorted(signal, key=_mover_rank) if r.ticker not in shown
        ][:MAX_MOVERS]
        shown |= {r.ticker for r in report.movers}
        report.awakened = [
            r for r in rows if "woke up" in r.tags and r.ticker not in shown
        ][:MAX_AWAKENED]
        report.noise = sorted(
            (r for r in rows if r.is_noise and r.change_pct),
            key=lambda r: -abs(r.change_pct or 0),
        )[:MAX_NOISE]
        return report

    def _build_row(
        self,
        ticker: str,
        series: list[tuple[str, float]],
        turnover: dict[str, float],
        listings: dict[str, UzseListing],
        records_begin: str | None = None,
    ) -> ScoutRow | None:
        if not series:
            return None
        listing = listings.get(ticker)
        if listing is not None and not listing.is_share:
            return None  # bonds are not what we are scouting for

        first_price, last_price = series[0][1], series[-1][1]
        change = ((last_price - first_price) / first_price * 100) if first_price else None

        row = ScoutRow(
            ticker=ticker,
            name=self._uzse.company_name(ticker),
            price=last_price,
            change_pct=change,
            sessions_traded=len(series),
            turnover=turnover.get(ticker),
            market_cap=(
                last_price * listing.shares_count
                if listing and listing.shares_count
                else None
            ),
            category=listing.category if listing else None,
        )
        row.tags = self._tags(ticker, series, records_begin)
        return row

    def _tags(
        self,
        ticker: str,
        series: list[tuple[str, float]],
        records_begin: str | None = None,
    ) -> list[str]:
        tags: list[str] = []

        span = self._storage.history_span(ticker)
        full = self._storage.get_history(ticker, limit=60)

        # "Woke up": the session before this window was long ago.
        earlier = [s for s in full if s[0] < series[0][0]]
        if earlier:
            gap = _days_between(earlier[0][0], series[0][0])
            if gap is not None and gap >= QUIET_SESSIONS:
                tags.append("woke up")
        elif (
            span
            and span[0] == series[0][0]
            and len(full) == len(series)
            and records_begin is not None
            and span[0] > records_begin
        ):
            tags.append("newly listed")

        prices = [price for _, price in full]
        if len(prices) >= 8:
            latest = series[-1][1]
            if latest >= max(prices):
                tags.append(f"highest in {len(prices)} sessions")
            elif latest <= min(prices):
                tags.append(f"lowest in {len(prices)} sessions")

        rising = _rising_streak(series)
        if rising >= 3:
            tags.append(f"up {rising} sessions running")
        return tags


def _mover_rank(row: ScoutRow) -> float:
    """Rank by size of move, weighted by how real the trading behind it was."""
    weight = {"good": 1.0, "moderate": 0.6, "unknown": 0.4, "thin": 0.15}[row.liquidity]
    # More sessions traded means the move was not one lucky print.
    weight *= min(1.0, 0.4 + 0.2 * row.sessions_traded)
    return -abs(row.change_pct or 0) * weight


def _rising_streak(series: list[tuple[str, float]]) -> int:
    streak = 0
    for (_, previous), (_, current) in zip(series, series[1:]):
        streak = streak + 1 if current > previous else 0
    return streak + 1 if streak else 0


def _days_between(earlier: str, later: str) -> int | None:
    try:
        return (date.fromisoformat(later) - date.fromisoformat(earlier)).days
    except ValueError:
        return None


def _coverage_note(trades: list, turnover: dict[str, float]) -> str | None:
    """The trade endpoint caps its response, so never imply full coverage."""
    if not trades:
        return "No trade tape available, so moves are ranked by price action alone."
    return f"Turnover from {len(trades)} trades covering {len(turnover)} companies."
