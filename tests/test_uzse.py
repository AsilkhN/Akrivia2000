"""Tests run against trimmed copies of the real parse.bot responses."""

import json
from pathlib import Path

import pytest

from stockbot.services.uzse import (
    BudgetExhausted,
    UzseProvider,
    parse_detail,
    parse_quotes,
    parse_securities,
    to_float,
)
from stockbot.storage import Storage

FIXTURES = Path(__file__).parent / "fixtures"
QUOTES = (FIXTURES / "uzse_quotes.json").read_text()
SECURITIES = (FIXTURES / "uzse_securities.json").read_text()
DETAIL = (FIXTURES / "uzse_detail.json").read_text()


@pytest.fixture()
def storage(tmp_path):
    store = Storage(str(tmp_path / "uzse.db"))
    yield store
    store.close()


def make_provider(storage, limit=200, reserve=40) -> UzseProvider:
    return UzseProvider(
        storage,
        quotes_url="https://api.parse.bot/quotes",
        securities_url="https://api.parse.bot/securities",
        detail_url="https://api.parse.bot/ticker/{ticker}",
        api_key="key",
        monthly_limit=limit,
        reserve=reserve,
    )


# -- number parsing ---------------------------------------------------------


def test_comma_is_a_thousands_separator_not_a_decimal_point():
    """The exchange writes 16,100 for sixteen thousand one hundred.

    Reading it as 16.1 understates the price a thousandfold, which would make
    every percentage in the report wrong.
    """
    assert to_float("16,100") == 16100.0
    assert to_float("2,385") == 2385.0
    assert to_float("370,000") == 370000.0
    assert to_float("50,999.99") == 50999.99
    assert to_float("3,148,983,500") == 3148983500.0


def test_plain_and_decimal_numbers():
    assert to_float("0.19") == 0.19
    assert to_float("5.7") == 5.7
    assert to_float("82") == 82.0
    assert to_float(45000) == 45000.0
    assert to_float("UZS570,000") == 570000.0


def test_missing_numbers_are_none_not_zero():
    assert to_float("") is None
    assert to_float(None) is None
    assert to_float("Перейти") is None


# -- payload parsing --------------------------------------------------------


def test_quotes_are_keyed_by_ticker_with_prices_and_dates():
    quotes = parse_quotes(QUOTES)
    assert quotes["AGMKP"].closing_price == 16100.0
    assert quotes["AGMKP"].last_trade_price == 17000.0
    assert quotes["UTYK"].closing_price == 370000.0
    assert quotes["KVTS"].last_trade_date == "2026-08-10"  # 10.08.2026 → ISO
    assert quotes["SANE"].last_trade_date == "2026-08-05"  # stale by five days


def test_duplicate_rows_in_the_feed_are_collapsed():
    """The live feed repeats rows — 146 quotes for far fewer companies."""
    payload = json.loads(QUOTES)
    assert len(payload["data"]["quotes"]) == 8
    assert len(parse_quotes(QUOTES)) == 7


def test_securities_map_tickers_to_codes_and_names():
    securities = parse_securities(SECURITIES)
    assert securities["KVTS"][0] == "UZ7025770007"
    assert "Kvarts" in securities["KVTS"][1]


def test_detail_gives_range_volume_and_a_history_series():
    detail = parse_detail(DETAIL)
    assert detail.ticker == "KVTS"
    assert detail.min_price == 2280.0 and detail.max_price == 2400.0
    assert detail.issue_value == 3148983500.0
    assert detail.history["2026-08-10"] == 2385.0
    assert len(detail.history) == 7


def test_parsers_survive_junk_instead_of_crashing():
    assert parse_quotes("not json") == {}
    assert parse_quotes(json.dumps({"data": {"unexpected": 1}})) == {}
    assert parse_securities("not json") == {}
    assert parse_detail("not json").history == {}


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


async def test_cached_snapshot_is_served_without_spending(storage):
    provider = make_provider(storage)
    storage.save_cache("quotes", QUOTES, "2026-08-10")

    assert provider.snapshot_is_fresh() is True
    assert await provider.ensure_quotes(scheduled=True) is False
    assert provider.credits_remaining() == 200


async def test_interactive_commands_cannot_touch_the_reserve(storage):
    provider = make_provider(storage, limit=200, reserve=40)
    provider.seed_credits_used(160)  # exactly the reserve left

    assert await provider.ensure_quotes(scheduled=False) is False
    assert provider.credits_remaining() == 40  # nothing spent


async def test_scheduled_report_may_use_the_reserve(storage, monkeypatch):
    provider = make_provider(storage, limit=200, reserve=40)
    provider.seed_credits_used(160)

    async def fake_request(url):
        storage.budget_spend("parsebot", provider.current_month())
        return QUOTES

    monkeypatch.setattr(provider, "_request", fake_request)
    assert await provider.ensure_quotes(scheduled=True) is True
    assert provider.credits_remaining() == 39


async def test_exhausted_budget_raises_for_the_scheduled_job(storage):
    provider = make_provider(storage, limit=200, reserve=40)
    provider.seed_credits_used(200)

    with pytest.raises(BudgetExhausted):
        await provider.ensure_quotes(scheduled=True)


async def test_one_fetch_records_history_for_every_ticker(storage, monkeypatch):
    provider = make_provider(storage)

    async def fake_request(url):
        storage.budget_spend("parsebot", provider.current_month())
        return QUOTES

    monkeypatch.setattr(provider, "_request", fake_request)
    await provider.ensure_quotes(scheduled=True)

    assert provider.credits_remaining() == 199  # one request for the whole market
    assert storage.get_history("KVTS") == [("2026-08-10", 2385.0)]
    # A stale ticker is filed under the day it actually traded, not today.
    assert storage.get_history("SANE") == [("2026-08-05", 13000.0)]


async def test_detail_fetch_backfills_twenty_sessions_at_once(storage, monkeypatch):
    provider = make_provider(storage)
    storage.save_cache("quotes", QUOTES, "2026-08-10")

    async def fake_request(url):
        assert "KVTS" in url
        storage.budget_spend("parsebot", provider.current_month())
        return DETAIL

    monkeypatch.setattr(provider, "_request", fake_request)
    detail = await provider.fetch_detail("KVTS")

    assert detail is not None and detail.max_price == 2400.0
    assert provider.credits_remaining() == 199
    # One request turned into a full week of history, so the week figure works
    # immediately instead of after six days of snapshots.
    assert provider.history_depth("KVTS") == 7


async def test_detail_is_cached_for_the_day(storage, monkeypatch):
    provider = make_provider(storage)
    calls = []

    async def fake_request(url):
        calls.append(url)
        storage.budget_spend("parsebot", provider.current_month())
        return DETAIL

    monkeypatch.setattr(provider, "_request", fake_request)
    await provider.fetch_detail("KVTS")
    await provider.fetch_detail("KVTS")
    assert len(calls) == 1


# -- quotes -----------------------------------------------------------------


def test_quote_combines_snapshot_price_with_local_history(storage):
    provider = make_provider(storage)
    storage.save_cache("quotes", QUOTES, "2026-08-10")
    storage.save_cache("securities", SECURITIES, None)
    storage.save_history_series(
        "KVTS",
        {
            "2026-07-31": 2235.0,
            "2026-08-03": 2225.0,
            "2026-08-04": 2250.0,
            "2026-08-05": 2250.0,
            "2026-08-06": 2300.0,
            "2026-08-07": 2284.0,
            "2026-08-10": 2385.0,
        },
    )

    quote = provider.get_quote("KVTS")
    assert quote.ok and quote.market == "UZSE" and quote.currency == "UZS"
    assert quote.price == 2385.0
    assert quote.day_change_pct == pytest.approx(4.42, abs=0.01)  # 2284 → 2385
    assert quote.week_change_pct == pytest.approx(7.19, abs=0.01)  # 2225 → 2385
    assert "Kvarts" in (quote.name or "")  # angle brackets stripped


def test_a_stock_that_has_not_traded_recently_says_so(storage):
    """Thin trading is the defining risk of this market — never hide it."""
    provider = make_provider(storage)
    storage.save_cache("quotes", QUOTES, "2026-08-10")

    quote = provider.get_quote("SANE")
    assert quote.ok
    assert quote.note is not None
    assert "5 days" in quote.note and "2026-08-05" in quote.note


def test_actively_traded_stock_has_no_staleness_note(storage):
    provider = make_provider(storage)
    storage.save_cache("quotes", QUOTES, "2026-08-10")
    assert provider.get_quote("KVTS").note is None


def test_unknown_uzse_ticker_is_reported_not_raised(storage):
    provider = make_provider(storage)
    storage.save_cache("quotes", QUOTES, "2026-08-10")
    quote = provider.get_quote("NOSUCH")
    assert not quote.ok and quote.error == "not listed on UZSE"


def test_an_empty_cache_blames_the_configuration_not_the_ticker(storage):
    """A failed fetch must not send the reader off to check a valid symbol."""
    provider = make_provider(storage)
    quote = provider.get_quote("KVTS")
    assert not quote.ok
    assert "PARSEBOT_QUOTES_URL" in (quote.error or "")


def test_known_tickers_come_from_the_cache_for_free(storage):
    provider = make_provider(storage)
    storage.save_cache("quotes", QUOTES, "2026-08-10")
    storage.save_cache("securities", SECURITIES, None)
    assert {"KVTS", "UZMK", "UZMT", "MXUS"} <= provider.known_tickers()
    assert provider.credits_remaining() == 200


def test_configuration_errors_do_not_tell_you_to_check_the_symbol():
    """Sending someone to verify a valid ticker hides the real problem."""
    from stockbot.handlers.commands import _add_hint
    from stockbot.services.prices import Quote

    broken_config = Quote(
        ticker="KVTS",
        error="no UZSE data yet — check PARSEBOT_QUOTES_URL and the server logs",
        market="UZSE",
    )
    assert _add_hint(broken_config, "UZSE") == ""

    unknown_symbol = Quote(ticker="NOPE", error="not listed on UZSE", market="UZSE")
    assert "UZSE listing" in _add_hint(unknown_symbol, "UZSE")


# -- the official close catching up ------------------------------------------


def _quote_row(ticker, close, change=None, direction="", date="10.08.2026"):
    return {
        "ticker": ticker,
        "closing_price": close,
        "last_trade_price": close,
        "change_value": change,
        "change_direction": direction,
        "last_trade_date": date,
    }


def _payload(rows):
    return json.dumps({"status": "success", "data": {"quotes": rows}})


def test_the_feeds_own_change_is_read_with_its_direction():
    quotes = parse_quotes(
        _payload(
            [
                _quote_row("KVTS", "2,385", "101.0", "up"),
                _quote_row("UTYK", "370,000", "29000.0", "down"),
                _quote_row("KASU", "0.01", "", ""),
            ]
        )
    )
    assert quotes["KVTS"].stated_change == 101.0
    assert quotes["UTYK"].stated_change == -29000.0
    assert quotes["KASU"].stated_change is None


@pytest.mark.asyncio
async def test_a_stale_official_close_catching_up_is_not_a_move(storage, monkeypatch):
    """The real failure: BNGP was published at 7 100 while trades had already
    happened near 14 976. When the official close caught up, the scout reported
    it as +110.9% in a single session. The feed's own change_value says the day
    moved by 12, so the two prices cannot belong to the same series.
    """
    provider = make_provider(storage)
    storage.save_history({"BNGP": 7100.0}, "2026-08-10")

    async def fake_request(url):
        return _payload(
            [_quote_row("BNGP", "14,975.97", "12.0", "up", "13.08.2026")]
        )

    monkeypatch.setattr(provider, "_request", lambda url: fake_request(url))
    await provider.ensure_quotes(scheduled=True)

    series = storage.history_since("2026-08-01")["BNGP"]
    assert series == [("2026-08-13", 14975.97)]  # the stale 7 100 is not compared


@pytest.mark.asyncio
async def test_an_ordinary_move_the_feed_confirms_keeps_accumulating(storage, monkeypatch):
    """KVTS really did go 2 284 → 2 385, and the feed says +101. Nothing to cut."""
    provider = make_provider(storage)
    storage.save_history({"KVTS": 2284.0}, "2026-08-07")

    async def fake_request(url):
        return _payload([_quote_row("KVTS", "2,385", "101.0", "up", "10.08.2026")])

    monkeypatch.setattr(provider, "_request", lambda url: fake_request(url))
    await provider.ensure_quotes(scheduled=True)

    series = storage.history_since("2026-08-01")["KVTS"]
    assert series == [("2026-08-07", 2284.0), ("2026-08-10", 2385.0)]


@pytest.mark.asyncio
async def test_the_day_change_comes_from_the_exchange_not_our_stored_close(
    storage, monkeypatch
):
    provider = make_provider(storage)
    storage.save_history({"BNGP": 7100.0}, "2026-08-10")

    async def fake_request(url):
        return _payload(
            [_quote_row("BNGP", "14,975.97", "12.0", "up", "13.08.2026")]
        )

    monkeypatch.setattr(provider, "_request", lambda url: fake_request(url))
    await provider.ensure_quotes(scheduled=True)

    quote = provider.get_quote("BNGP")
    assert quote.day_change == 12.0
    assert quote.day_change_pct == pytest.approx(12.0 / 14963.97 * 100)


def test_a_break_stops_the_series_but_keeps_the_price(storage):
    storage.save_history({"UZTL": 6900.0}, "2026-08-10")
    storage.save_history({"UZTL": 13500.0}, "2026-08-13", breaks=["UZTL"])
    storage.save_history({"UZTL": 13600.0}, "2026-08-14")

    assert storage.get_history("UZTL", limit=10) == [
        ("2026-08-14", 13600.0),
        ("2026-08-13", 13500.0),
    ]
    assert storage.history_since("2026-08-01")["UZTL"] == [
        ("2026-08-13", 13500.0),
        ("2026-08-14", 13600.0),
    ]


def test_a_detail_backfill_heals_a_break_it_covers(storage):
    """20 consistent sessions from one response are internally comparable, so
    the gap the break was guarding against is gone."""
    storage.save_history({"KVTS": 2385.0}, "2026-08-10", breaks=["KVTS"])
    storage.save_history_series(
        "KVTS", {"2026-08-07": 2284.0, "2026-08-10": 2385.0}
    )
    assert storage.history_since("2026-08-01")["KVTS"] == [
        ("2026-08-07", 2284.0),
        ("2026-08-10", 2385.0),
    ]
