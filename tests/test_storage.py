import pytest

from stockbot.storage import Storage


@pytest.fixture()
def storage(tmp_path):
    store = Storage(str(tmp_path / "test.db"))
    yield store
    store.close()


def test_ensure_user_is_idempotent(storage):
    user, created = storage.ensure_user(1, "UTC", "09:00")
    assert created is True
    again, created_again = storage.ensure_user(1, "Europe/Berlin", "20:00")
    assert created_again is False
    assert again.timezone == user.timezone == "UTC"


def test_watchlist_is_per_user(storage):
    storage.ensure_user(1, "UTC", "09:00")
    storage.ensure_user(2, "UTC", "09:00")
    storage.add_ticker(1, "onto", "Onto Innovation")
    storage.add_ticker(2, "NOW", "ServiceNow")

    assert storage.get_watchlist(1) == [("ONTO", "Onto Innovation")]
    assert storage.get_watchlist(2) == [("NOW", "ServiceNow")]


def test_duplicate_ticker_is_rejected(storage):
    storage.ensure_user(1, "UTC", "09:00")
    assert storage.add_ticker(1, "ONTO", None) is True
    assert storage.add_ticker(1, "ONTO", None) is False
    assert storage.count_tickers(1) == 1


def test_remove_reports_whether_anything_was_removed(storage):
    storage.ensure_user(1, "UTC", "09:00")
    storage.add_ticker(1, "ONTO", None)
    assert storage.remove_ticker(1, "onto") is True
    assert storage.remove_ticker(1, "ONTO") is False


def test_settings_survive_reopening_the_database(tmp_path):
    path = str(tmp_path / "persist.db")
    store = Storage(path)
    store.ensure_user(7, "UTC", "09:00")
    store.add_ticker(7, "CRDO", "Credo Technology")
    store.set_digest_time(7, "18:30")
    store.set_timezone(7, "Asia/Tashkent")
    store.close()

    reopened = Storage(path)
    user = reopened.get_user(7)
    assert user is not None
    assert user.digest_time == "18:30"
    assert user.timezone == "Asia/Tashkent"
    assert reopened.get_watchlist(7) == [("CRDO", "Credo Technology")]
    reopened.close()


def test_paused_users_are_excluded_from_the_daily_loop(storage):
    storage.ensure_user(1, "UTC", "09:00")
    storage.ensure_user(2, "UTC", "09:00")
    storage.set_enabled(2, False)
    assert [u.chat_id for u in storage.all_users()] == [1]


def test_mark_digest_run_keeps_last_session_when_nothing_was_sent(storage):
    storage.ensure_user(1, "UTC", "09:00")
    storage.mark_digest_run(1, "2026-08-07", "2026-08-06")
    storage.mark_digest_run(1, "2026-08-08", None)

    user = storage.get_user(1)
    assert user.last_digest_date == "2026-08-08"
    assert user.last_session_sent == "2026-08-06"
