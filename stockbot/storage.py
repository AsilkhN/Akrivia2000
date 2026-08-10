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
    added_at    TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (chat_id, ticker),
    FOREIGN KEY (chat_id) REFERENCES users(chat_id) ON DELETE CASCADE
);
"""


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
        self._conn.commit()

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

    def add_ticker(self, chat_id: int, ticker: str, name: str | None) -> bool:
        """Return False when the ticker was already on the list."""
        try:
            self._conn.execute(
                "INSERT INTO watchlist (chat_id, ticker, name) VALUES (?, ?, ?)",
                (chat_id, ticker.upper(), name),
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

    def get_watchlist(self, chat_id: int) -> list[tuple[str, str | None]]:
        rows = self._conn.execute(
            "SELECT ticker, name FROM watchlist WHERE chat_id = ? ORDER BY ticker",
            (chat_id,),
        ).fetchall()
        return [(r["ticker"], r["name"]) for r in rows]

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
