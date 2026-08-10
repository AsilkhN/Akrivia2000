"""SQLite persistence: users, their settings and their watchlists.

Everything is keyed by Telegram `chat_id`, so several people can use the same
bot instance with completely separate watchlists.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    chat_id            INTEGER PRIMARY KEY,
    timezone           TEXT    NOT NULL,
    digest_time        TEXT    NOT NULL,
    enabled            INTEGER NOT NULL DEFAULT 1,
    -- last local date (YYYY-MM-DD) on which the daily job ran for this user;
    -- prevents double sends after a restart
    last_digest_date   TEXT,
    -- last market session (YYYY-MM-DD) actually reported; prevents resending
    -- the same numbers on weekends and holidays
    last_session_sent  TEXT,
    created_at         TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS watchlist (
    chat_id     INTEGER NOT NULL,
    ticker      TEXT    NOT NULL,
    name        TEXT,
    -- which exchange the ticker belongs to: 'US' (Yahoo) or 'UZSE' (parse.bot).
    -- Kept per row because the two are priced, formatted and budgeted differently.
    market      TEXT    NOT NULL DEFAULT 'US',
    added_at    TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (chat_id, ticker),
    FOREIGN KEY (chat_id) REFERENCES users(chat_id) ON DELETE CASCADE
);

-- One row per calendar month, counting metered parse.bot requests. The bot
-- refuses to spend past the configured limit rather than failing at the API.
CREATE TABLE IF NOT EXISTS api_budget (
    provider   TEXT NOT NULL,
    month      TEXT NOT NULL,          -- YYYY-MM, UTC
    used       INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (provider, month)
);

-- The full UZSE snapshot, kept so that one paid request serves the whole day:
-- the daily report, every /now, every /add validation and every /ai.
CREATE TABLE IF NOT EXISTS uzse_snapshot (
    id           INTEGER PRIMARY KEY CHECK (id = 1),
    payload      TEXT NOT NULL,        -- raw JSON exactly as the scraper returned it
    fetched_at   TEXT NOT NULL,        -- UTC timestamp of the paid request
    session_date TEXT                  -- trading date the snapshot describes
);

-- Price history for UZSE, accumulated from the daily snapshots. The scraper
-- only reports "today", so day-over-day and weekly changes are computed from
-- what we have stored ourselves — which costs no extra requests.
CREATE TABLE IF NOT EXISTS uzse_history (
    ticker       TEXT NOT NULL,
    session_date TEXT NOT NULL,
    price        REAL NOT NULL,
    PRIMARY KEY (ticker, session_date)
);
"""


@dataclass
class WatchlistEntry:
    ticker: str
    name: str | None
    market: str


@dataclass
class User:
    chat_id: int
    timezone: str
    digest_time: str
    enabled: bool
    last_digest_date: str | None
    last_session_sent: str | None


class Storage:
    def __init__(self, path: str) -> None:
        db_path = Path(path)
        if db_path.parent and str(db_path.parent) not in ("", "."):
            db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(SCHEMA)
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        """Bring a database created by an older version up to the current schema."""
        columns = {
            row["name"]
            for row in self._conn.execute("PRAGMA table_info(watchlist)").fetchall()
        }
        if "market" not in columns:
            self._conn.execute(
                "ALTER TABLE watchlist ADD COLUMN market TEXT NOT NULL DEFAULT 'US'"
            )

    def close(self) -> None:
        self._conn.close()

    # -- users --------------------------------------------------------------

    def ensure_user(self, chat_id: int, timezone: str, digest_time: str) -> tuple[User, bool]:
        """Return the user, creating them with defaults if they are new.

        The boolean is True when the user row was just created.
        """
        existing = self.get_user(chat_id)
        if existing is not None:
            return existing, False
        self._conn.execute(
            "INSERT INTO users (chat_id, timezone, digest_time) VALUES (?, ?, ?)",
            (chat_id, timezone, digest_time),
        )
        self._conn.commit()
        user = self.get_user(chat_id)
        assert user is not None
        return user, True

    def get_user(self, chat_id: int) -> User | None:
        row = self._conn.execute(
            "SELECT * FROM users WHERE chat_id = ?", (chat_id,)
        ).fetchone()
        return _row_to_user(row) if row else None

    def all_users(self) -> list[User]:
        rows = self._conn.execute("SELECT * FROM users WHERE enabled = 1").fetchall()
        return [_row_to_user(r) for r in rows]

    def set_digest_time(self, chat_id: int, digest_time: str) -> None:
        self._conn.execute(
            "UPDATE users SET digest_time = ? WHERE chat_id = ?", (digest_time, chat_id)
        )
        self._conn.commit()

    def set_timezone(self, chat_id: int, timezone: str) -> None:
        self._conn.execute(
            "UPDATE users SET timezone = ? WHERE chat_id = ?", (timezone, chat_id)
        )
        self._conn.commit()

    def set_enabled(self, chat_id: int, enabled: bool) -> None:
        self._conn.execute(
            "UPDATE users SET enabled = ? WHERE chat_id = ?", (int(enabled), chat_id)
        )
        self._conn.commit()

    def mark_digest_run(
        self, chat_id: int, local_date: str, session_date: str | None
    ) -> None:
        """Record that the daily job ran today, and which session was reported.

        `session_date` stays unchanged when nothing new was reported (weekend,
        holiday, or a failed run), so the report is retried on the next day.
        """
        if session_date is None:
            self._conn.execute(
                "UPDATE users SET last_digest_date = ? WHERE chat_id = ?",
                (local_date, chat_id),
            )
        else:
            self._conn.execute(
                "UPDATE users SET last_digest_date = ?, last_session_sent = ? "
                "WHERE chat_id = ?",
                (local_date, session_date, chat_id),
            )
        self._conn.commit()

    # -- watchlist ----------------------------------------------------------

    def add_ticker(
        self, chat_id: int, ticker: str, name: str | None, market: str = "US"
    ) -> bool:
        """Return False when the ticker was already on the list."""
        try:
            self._conn.execute(
                "INSERT INTO watchlist (chat_id, ticker, name, market) VALUES (?, ?, ?, ?)",
                (chat_id, ticker.upper(), name, market),
            )
        except sqlite3.IntegrityError:
            return False
        self._conn.commit()
        return True

    def remove_ticker(self, chat_id: int, ticker: str) -> bool:
        cursor = self._conn.execute(
            "DELETE FROM watchlist WHERE chat_id = ? AND ticker = ?",
            (chat_id, ticker.upper()),
        )
        self._conn.commit()
        return cursor.rowcount > 0

    def get_watchlist(self, chat_id: int) -> list[WatchlistEntry]:
        rows = self._conn.execute(
            "SELECT ticker, name, market FROM watchlist WHERE chat_id = ? "
            "ORDER BY market, ticker",
            (chat_id,),
        ).fetchall()
        return [
            WatchlistEntry(ticker=r["ticker"], name=r["name"], market=r["market"])
            for r in rows
        ]

    # -- metered API budget --------------------------------------------------

    def budget_used(self, provider: str, month: str) -> int:
        row = self._conn.execute(
            "SELECT used FROM api_budget WHERE provider = ? AND month = ?",
            (provider, month),
        ).fetchone()
        return int(row["used"]) if row else 0

    def budget_spend(self, provider: str, month: str, amount: int = 1) -> int:
        """Record spent requests and return the new total for the month."""
        self._conn.execute(
            "INSERT INTO api_budget (provider, month, used) VALUES (?, ?, ?) "
            "ON CONFLICT(provider, month) DO UPDATE SET used = used + excluded.used",
            (provider, month, amount),
        )
        self._conn.commit()
        return self.budget_used(provider, month)

    def budget_seed(self, provider: str, month: str, already_used: int) -> None:
        """Set the starting count for a month, for credits spent outside the bot.

        Only ever raises the number, so restarting the bot cannot rewind the
        counter and hand out credits that were already spent.
        """
        current = self.budget_used(provider, month)
        if already_used > current:
            self._conn.execute(
                "INSERT INTO api_budget (provider, month, used) VALUES (?, ?, ?) "
                "ON CONFLICT(provider, month) DO UPDATE SET used = excluded.used",
                (provider, month, already_used),
            )
            self._conn.commit()

    # -- UZSE snapshot cache -------------------------------------------------

    def save_snapshot(self, payload: str, session_date: str | None) -> None:
        self._conn.execute(
            "INSERT INTO uzse_snapshot (id, payload, fetched_at, session_date) "
            "VALUES (1, ?, datetime('now'), ?) "
            "ON CONFLICT(id) DO UPDATE SET payload = excluded.payload, "
            "fetched_at = excluded.fetched_at, session_date = excluded.session_date",
            (payload, session_date),
        )
        self._conn.commit()

    def load_snapshot(self) -> tuple[str, str, str | None] | None:
        """Return (payload, fetched_at, session_date) of the cached snapshot."""
        row = self._conn.execute(
            "SELECT payload, fetched_at, session_date FROM uzse_snapshot WHERE id = 1"
        ).fetchone()
        if row is None:
            return None
        return row["payload"], row["fetched_at"], row["session_date"]

    def save_history(self, prices: dict[str, float], session_date: str) -> None:
        """Store one session's closing prices, ignoring re-runs of the same day."""
        self._conn.executemany(
            "INSERT INTO uzse_history (ticker, session_date, price) VALUES (?, ?, ?) "
            "ON CONFLICT(ticker, session_date) DO UPDATE SET price = excluded.price",
            [(ticker, session_date, price) for ticker, price in prices.items()],
        )
        self._conn.commit()

    def get_history(self, ticker: str, limit: int = 10) -> list[tuple[str, float]]:
        """Most recent sessions first: [(session_date, price), …]."""
        rows = self._conn.execute(
            "SELECT session_date, price FROM uzse_history WHERE ticker = ? "
            "ORDER BY session_date DESC LIMIT ?",
            (ticker.upper(), limit),
        ).fetchall()
        return [(r["session_date"], float(r["price"])) for r in rows]

    def count_tickers(self, chat_id: int) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM watchlist WHERE chat_id = ?", (chat_id,)
        ).fetchone()
        return int(row["n"])


def _row_to_user(row: sqlite3.Row) -> User:
    return User(
        chat_id=row["chat_id"],
        timezone=row["timezone"],
        digest_time=row["digest_time"],
        enabled=bool(row["enabled"]),
        last_digest_date=row["last_digest_date"],
        last_session_sent=row["last_session_sent"],
    )
