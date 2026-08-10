import json

import pytest

from stockbot.services.uzse import BudgetExhausted, UzseProvider, parse_rows
from stockbot.storage import Storage

SNAPSHOT = json.dumps(
    {
        "data": [
            {"Ticker": "KVTS", "Name": "Kvarts", "Price": "12 500,00", "Date": "07.08.2026"},
            {"Ticker": "UZMK", "Name": "Uzmetkombinat", "Price": 45000, "Date": "07.08.2026"},
            {"Ticker": "HMKB", "Name": "Hamkorbank", "Price": "1,250.5", "Date": "07.08.2026"},
        ]
    }
)


@pytest.fixture()
def storage(tmp_path):
    store = Storage(str(tmp_path / "uzse.db"))
    yield store
    store.close()


def make_provider(storage, limit=200, reserve=40) -> UzseProvider:
    return UzseProvider(
        storage,
        api_url="https://api.parse.bot/scrapers/test",
        api_key="key",
        monthly_limit=limit,
        reserve=reserve,
    )


# -- parsing ----------------------------------------------------------------


def test_parser_reads_tickers_names_and_prices():
    rows = {row.ticker: row for row in parse_rows(SNAPSHOT)}
    assert set(rows) == {"KVTS", "UZMK", "HMKB"}
    assert rows["KVTS"].name == "Kvarts"
    assert rows["KVTS"].price == 12500.0  # "12 500,00" — space grouping, comma decimal
    assert rows["UZMK"].price == 45000.0  # already numeric
    assert rows["HMKB"].price == 1250.5  # "1,250.5" — comma grouping, dot decimal
    assert rows["KVTS"].session_date == "2026-08-07"  # dd.mm.yyyy normalised


def test_parser_accepts_a_bare_list_and_alternative_column_names():
    payload = json.dumps([{"symbol": "ABCD", "company": "Test", "last": "100"}])
    rows = parse_rows(payload)
    assert rows[0].ticker == "ABCD"
    assert rows[0].name == "Test"
    assert rows[0].price == 100.0


def test_parser_survives_junk_instead_of_crashing():
    assert parse_rows("not json at all") == []
    assert parse_rows(json.dumps({"unexpected": "shape"})) == []


# -- budget -----------------------------------------------------------------


def test_credits_start_at_the_full_limit(storage):
    assert make_provider(storage).credits_remaining() == 200


def test_seeding_credits_already_spent_elsewhere(storage):
    provider = make_provider(storage)
    provider.seed_credits_used(9)
    assert provider.credits_remaining() == 191


def test_seeding_never_rewinds_the_counter(storage):
    """A restart must not hand back credits that were already spent."""
    provider = make_provider(storage)
    provider.seed_credits_used(50)
    provider.seed_credits_used(9)
    assert provider.credits_remaining() == 150


@pytest.mark.asyncio
async def test_cached_snapshot_is_served_without_spending(storage):
    provider = make_provider(storage)
    storage.save_snapshot(SNAPSHOT, "2026-08-07")

    assert provider.snapshot_is_fresh() is True
    assert await provider.ensure_snapshot(scheduled=True) is False
    assert provider.credits_remaining() == 200


@pytest.mark.asyncio
async def test_interactive_commands_cannot_touch_the_reserve(storage):
    provider = make_provider(storage, limit=200, reserve=40)
    provider.seed_credits_used(160)  # exactly the reserve left

    assert await provider.ensure_snapshot(scheduled=False) is False
    assert provider.credits_remaining() == 40  # nothing spent


@pytest.mark.asyncio
async def test_scheduled_report_may_use_the_reserve(storage, monkeypatch):
    provider = make_provider(storage, limit=200, reserve=40)
    provider.seed_credits_used(160)

    async def fake_fetch():
        storage.budget_spend("parsebot", provider.current_month())
        return SNAPSHOT

    monkeypatch.setattr(provider, "_fetch_blocking_safe", fake_fetch)
    assert await provider.ensure_snapshot(scheduled=True) is True
    assert provider.credits_remaining() == 39


@pytest.mark.asyncio
async def test_exhausted_budget_raises_for_the_scheduled_job(storage):
    provider = make_provider(storage, limit=200, reserve=40)
    provider.seed_credits_used(200)

    with pytest.raises(BudgetExhausted):
        await provider.ensure_snapshot(scheduled=True)


# -- quotes -----------------------------------------------------------------


def test_quote_is_built_from_cache_and_local_history(storage):
    provider = make_provider(storage)
    storage.save_snapshot(SNAPSHOT, "2026-08-07")
    # Six sessions recorded, so both the day and the week change are available.
    for index, date in enumerate(
        ["2026-07-31", "2026-08-03", "2026-08-04", "2026-08-05", "2026-08-06", "2026-08-07"]
    ):
        storage.save_history({"KVTS": 10000.0 + index * 500}, date)

    quote = provider.get_quote("KVTS")
    assert quote.ok
    assert quote.market == "UZSE"
    assert quote.currency == "UZS"
    assert quote.price == 12500.0
    assert quote.day_change_pct == pytest.approx(4.1666, abs=0.01)  # 12000 → 12500
    assert quote.week_change_pct == pytest.approx(25.0)  # 10000 → 12500


def test_quote_without_history_reports_price_only(storage):
    provider = make_provider(storage)
    storage.save_snapshot(SNAPSHOT, "2026-08-07")
    quote = provider.get_quote("KVTS")
    assert quote.ok and quote.price == 12500.0
    assert quote.day_change_pct is None and quote.week_change_pct is None


def test_unknown_uzse_ticker_is_reported_not_raised(storage):
    provider = make_provider(storage)
    storage.save_snapshot(SNAPSHOT, "2026-08-07")
    quote = provider.get_quote("NOSUCH")
    assert not quote.ok
    assert "UZSE" in (quote.error or "")


def test_known_tickers_come_from_the_cache_for_free(storage):
    provider = make_provider(storage)
    storage.save_snapshot(SNAPSHOT, "2026-08-07")
    assert provider.known_tickers() == {"KVTS", "UZMK", "HMKB"}
    assert provider.credits_remaining() == 200
