from datetime import datetime
from zoneinfo import ZoneInfo

from stockbot.handlers.scheduler import _is_due
from stockbot.storage import User

UTC = ZoneInfo("UTC")


def make_user(**overrides) -> User:
    defaults = dict(
        chat_id=1,
        timezone="UTC",
        digest_time="09:00",
        enabled=True,
        last_digest_date=None,
        last_session_sent=None,
    )
    defaults.update(overrides)
    return User(**defaults)


def test_not_due_before_the_configured_time():
    assert _is_due(make_user(), datetime(2026, 8, 7, 8, 59, tzinfo=UTC)) is False


def test_due_at_the_configured_minute():
    assert _is_due(make_user(), datetime(2026, 8, 7, 9, 0, tzinfo=UTC)) is True


def test_still_due_later_the_same_day_after_a_restart():
    """A bot that was down at 09:00 must catch up rather than skip the day."""
    assert _is_due(make_user(), datetime(2026, 8, 7, 14, 30, tzinfo=UTC)) is True


def test_not_due_twice_on_the_same_local_day():
    user = make_user(last_digest_date="2026-08-07")
    assert _is_due(user, datetime(2026, 8, 7, 23, 59, tzinfo=UTC)) is False


def test_due_again_the_next_local_day():
    user = make_user(last_digest_date="2026-08-07")
    assert _is_due(user, datetime(2026, 8, 8, 9, 0, tzinfo=UTC)) is True


def test_time_is_evaluated_in_the_users_timezone():
    user = make_user(timezone="Asia/Tashkent")  # UTC+5
    assert _is_due(user, datetime(2026, 8, 7, 3, 0, tzinfo=UTC)) is False  # 08:00 local
    assert _is_due(user, datetime(2026, 8, 7, 4, 0, tzinfo=UTC)) is True  # 09:00 local


def test_invalid_timezone_is_skipped_instead_of_crashing():
    assert _is_due(make_user(timezone="Mars/Olympus")) is False
