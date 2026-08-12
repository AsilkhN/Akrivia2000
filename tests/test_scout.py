from pathlib import Path

import pytest

from stockbot.scout import (
    GOOD_TURNOVER_UZS,
    NOISE_TURNOVER_UZS,
    Scout,
    ScoutRow,
    _rising_streak,
)
from stockbot.services.news import Headline, keywords_for, match_headlines, parse_feed
from stockbot.services.uzse import UzseProvider, parse_listings, parse_trades, turnover_by_ticker
from stockbot.storage import Storage

FIXTURES = Path(__file__).parent / "fixtures"
QUOTES = (FIXTURES / "uzse_quotes.json").read_text()
SECURITIES = (FIXTURES / "uzse_securities.json").read_text()
LISTINGS = (FIXTURES / "uzse_listings.json").read_text()


@pytest.fixture()
def storage(tmp_path):
    store = Storage(str(tmp_path / "scout.db"))
    yield store
    store.close()


def make_provider(storage) -> UzseProvider:
    return UzseProvider(
        storage,
        quotes_url="https://api/quotes",
        securities_url="https://api/securities",
        listings_url="https://api/listings",
        trades_url="https://api/trades?from={date_from}&to={date_to}",
        api_key="key",
        monthly_limit=200,
        reserve=40,
    )


# -- the noise rule ---------------------------------------------------------
# This is the load-bearing judgement of the whole scout. On UZSE most large
# percentage moves are one person selling a handful of shares.


def test_a_big_move_on_no_money_is_noise():
    row = ScoutRow(ticker="KASU", price=0.19, change_pct=50.0, turnover=200_000.0,
                   sessions_traded=1)
    assert row.is_noise
    assert row.liquidity == "thin"


def test_a_big_move_on_real_money_is_signal():
    row = ScoutRow(ticker="UZTL", price=6900.0, change_pct=9.4,
                   turnover=410_000_000.0, sessions_traded=4)
    assert not row.is_noise
    assert row.liquidity == "good"


def test_sub_sum_prices_are_always_noise():
    """A share at 0.01 sums moves 100% on rounding alone."""
    row = ScoutRow(ticker="KASU", price=0.01, change_pct=100.0,
                   turnover=GOOD_TURNOVER_UZS, sessions_traded=5)
    assert row.is_noise


def test_without_turnover_data_a_one_session_spike_is_treated_as_noise():
    row = ScoutRow(ticker="XXXX", price=5000.0, change_pct=35.0, sessions_traded=1)
    assert row.liquidity == "unknown"
    assert row.is_noise


def test_without_turnover_data_a_sustained_move_is_kept():
    row = ScoutRow(ticker="XXXX", price=5000.0, change_pct=35.0, sessions_traded=5)
    assert not row.is_noise


def test_moderate_turnover_sits_between_the_two():
    row = ScoutRow(ticker="MID", price=1000.0, change_pct=6.0,
                   turnover=NOISE_TURNOVER_UZS + 1, sessions_traded=3)
    assert row.liquidity == "moderate"
    assert not row.is_noise


def test_streaks_are_counted():
    assert _rising_streak([("d1", 1.0), ("d2", 2.0), ("d3", 3.0)]) == 3
    assert _rising_streak([("d1", 3.0), ("d2", 2.0)]) == 0


# -- turnover ---------------------------------------------------------------


def test_turnover_recovers_the_ticker_from_the_glued_security_code():
    """security_code arrives as UZ7058980010UZNF — code plus ticker."""
    trades = parse_trades(QUOTES)
    assert trades[0].ticker == "UZNF"
    assert turnover_by_ticker(trades) == {"UZNF": 570000.0}


def test_listings_give_share_counts_for_company_size():
    listings = parse_listings(LISTINGS)
    assert listings["KVTS"].shares_count == 96449218.0
    assert listings["KVTS"].is_share


def test_bonds_are_recognised_so_the_scout_can_skip_them():
    assert parse_listings(LISTINGS)["IMKF3"].is_share is False


# -- the scan ---------------------------------------------------------------


async def test_scout_ranks_by_money_not_by_percentage(storage, monkeypatch):
    provider = make_provider(storage)
    storage.save_cache("quotes", QUOTES, "2026-08-10")
    storage.save_cache("securities", SECURITIES, None)
    storage.save_cache("listings", LISTINGS, None)

    # A quiet stock that doubled on nothing, and a real one that rose modestly.
    storage.save_history_series("KASU", {"2026-08-07": 0.10, "2026-08-10": 0.19})
    storage.save_history_series(
        "UZMT",
        {"2026-08-06": 49000.0, "2026-08-07": 50000.0, "2026-08-10": 51500.0},
    )

    async def fake_request(url):
        return None  # no trade tape available

    monkeypatch.setattr(provider, "_request", fake_request)
    scout = Scout(storage, provider)
    report = await scout.build("weekly")

    movers = [r.ticker for r in report.movers]
    noise = [r.ticker for r in report.noise]
    assert "UZMT" in movers
    assert "KASU" in noise and "KASU" not in movers


async def test_scout_flags_a_share_that_started_trading_again(storage, monkeypatch):
    provider = make_provider(storage)
    storage.save_cache("quotes", QUOTES, "2026-08-10")
    storage.save_history_series(
        "SANE", {"2026-06-01": 12000.0, "2026-08-07": 12500.0, "2026-08-10": 13000.0}
    )

    async def fake_request(url):
        return None

    monkeypatch.setattr(provider, "_request", fake_request)
    report = await Scout(storage, provider).build("weekly")

    # It may surface in either section — what matters is that it appears once,
    # carrying the tag that says why it is interesting.
    everywhere = report.movers + report.awakened + report.turnover_leaders
    sane = [r for r in everywhere if r.ticker == "SANE"]
    assert len(sane) == 1
    assert "woke up" in sane[0].tags


async def test_scout_says_so_when_there_is_no_trade_tape(storage, monkeypatch):
    provider = make_provider(storage)
    storage.save_cache("quotes", QUOTES, "2026-08-10")

    async def fake_request(url):
        return None

    monkeypatch.setattr(provider, "_request", fake_request)
    report = await Scout(storage, provider).build("daily")
    assert "ranked by price action alone" in (report.coverage_note or "")


async def test_scout_on_an_empty_history_reports_nothing_rather_than_crashing(storage):
    provider = make_provider(storage)
    report = await Scout(storage, provider).build("daily")
    assert report.is_empty


# -- news matching ----------------------------------------------------------

RSS = """<?xml version="1.0"?>
<rss version="2.0"><channel>
  <item><title>Kvarts oynа zavodi ishlab chiqarishni kengaytirmoqda</title>
        <link>https://gazeta.uz/1</link></item>
  <item><title>O'zbektelekom yangi tarif rejalarini e'lon qildi</title>
        <link>https://gazeta.uz/2</link></item>
  <item><title>Ob-havo: haftaning ikkinchi yarmida yomg'ir kutilmoqda</title>
        <link>https://gazeta.uz/3</link></item>
</channel></rss>"""


def test_rss_titles_are_read_without_a_third_party_library():
    items = parse_feed(RSS, "gazeta.uz")
    assert len(items) == 3
    assert items[0].source == "gazeta.uz"
    assert items[0].link == "https://gazeta.uz/1"


def test_atom_feeds_work_too():
    atom = """<?xml version="1.0"?>
    <feed xmlns="http://www.w3.org/2005/Atom">
      <entry><title>Kvarts news</title><link href="https://spot.uz/1"/></entry>
    </feed>"""
    items = parse_feed(atom, "spot.uz")
    assert items[0].title == "Kvarts news"
    assert items[0].link == "https://spot.uz/1"


def test_broken_xml_yields_nothing_instead_of_raising():
    assert parse_feed("<not xml", "x") == []


def test_legal_form_words_are_not_used_as_search_keywords():
    """Otherwise 'aksiyadorlik jamiyati' would match half the news in the country."""
    from stockbot.services.news import fold

    # Keywords come back folded, so they compare against Cyrillic headlines too.
    assert keywords_for("KVTS", "Kvarts AJ") == [fold("Kvarts")]
    assert fold("jamiyati") not in keywords_for("X", "Kvarts aksiyadorlik jamiyati")


def test_headlines_are_matched_to_the_right_companies():
    headlines = parse_feed(RSS, "gazeta.uz")
    matches = match_headlines(
        headlines, {"KVTS": "Kvarts AJ", "UZTL": "O'zbektelekom AK", "BIOK": "Biokimyo AJ"}
    )
    assert "Kvarts" in matches["KVTS"][0]
    assert "zbektelekom" in matches["UZTL"][0]
    assert "BIOK" not in matches  # no headline mentions it


def test_apostrophe_spellings_do_not_break_matching():
    headlines = [Headline(title="Oʻzbektelekom rekord foyda", source="x")]
    matches = match_headlines(headlines, {"UZTL": "O'zbektelekom AK"})
    assert "UZTL" in matches


async def test_a_company_is_not_repeated_across_sections(storage, monkeypatch):
    """The money section already made the case — saying it twice wastes the reader."""
    provider = make_provider(storage)
    storage.save_cache("quotes", QUOTES, "2026-08-10")
    storage.save_history_series("UZMT", {"2026-08-07": 49000.0, "2026-08-10": 51000.0})

    trades = (
        '{"data":{"trades":[{"security_code":"UZ7000000000UZMT",'
        '"volume":"1,240,000,000"}]}}'
    )

    async def fake_request(url):
        return trades if "from=" in url else None

    monkeypatch.setattr(provider, "_request", fake_request)
    report = await Scout(storage, provider).build("weekly")

    assert [r.ticker for r in report.turnover_leaders] == ["UZMT"]
    assert "UZMT" not in [r.ticker for r in report.movers]


async def test_noise_never_appears_in_the_money_ranking(storage, monkeypatch):
    provider = make_provider(storage)
    storage.save_cache("quotes", QUOTES, "2026-08-10")
    storage.save_history_series("KASU", {"2026-08-07": 0.10, "2026-08-10": 0.19})

    trades = '{"data":{"trades":[{"security_code":"UZ7000000000KASU","volume":"190,000"}]}}'

    async def fake_request(url):
        return trades if "from=" in url else None

    monkeypatch.setattr(provider, "_request", fake_request)
    report = await Scout(storage, provider).build("weekly")

    assert report.turnover_leaders == []
    assert [r.ticker for r in report.noise] == ["KASU"]


async def test_a_young_database_does_not_call_every_share_newly_listed(storage, monkeypatch):
    """Everything looks new on day one; that is our history, not market news."""
    provider = make_provider(storage)
    storage.save_cache("quotes", QUOTES, "2026-08-10")
    for ticker in ("KVTS", "UZMT", "UZMK"):
        storage.save_history_series(ticker, {"2026-08-07": 100.0, "2026-08-10": 110.0})

    async def fake_request(url):
        return None

    monkeypatch.setattr(provider, "_request", fake_request)
    report = await Scout(storage, provider).build("weekly")

    tags = [tag for row in report.movers for tag in row.tags]
    assert "newly listed" not in tags


# -- /add with several tickers ----------------------------------------------


def test_add_result_lists_what_happened_to_each_ticker():
    from stockbot.handlers.commands import _render_add_result
    from stockbot.services.prices import Quote

    class Cfg:
        max_tickers_per_user = 25

    added = [
        Quote(ticker="ONTO", name="Onto Innovation", market="US"),
        Quote(ticker="KVTS", name="Kvarts AJ", market="UZSE"),
    ]
    text = _render_add_result(
        added, ["NOW"], [("MANE", "unknown ticker or no data", "US")], [], Cfg
    )
    assert "Now following 2" in text
    assert "Onto Innovation" in text and "Kvarts AJ" in text
    assert "Already on your list: NOW" in text
    assert "MANE" in text and "unknown ticker" in text


def test_add_blames_the_provider_when_every_ticker_fails():
    """Seven failures at once is a data source problem, not seven typos."""
    from stockbot.handlers.commands import _render_add_result

    class Cfg:
        max_tickers_per_user = 25

    failed = [(t, "data temporarily unavailable", "US") for t in ("ONTO", "CRDO", "ALAB")]
    text = _render_add_result([], [], failed, [], Cfg)
    assert "price provider is probably" in text


def test_add_says_which_tickers_did_not_fit():
    from stockbot.handlers.commands import _render_add_result
    from stockbot.services.prices import Quote

    class Cfg:
        max_tickers_per_user = 2

    text = _render_add_result(
        [Quote(ticker="ONTO", name="Onto", market="US")], [], [], [("NOW", "US")], Cfg
    )
    assert "list is full at 2" in text and "NOW" in text


# -- per-company news, keyless ----------------------------------------------

GOOGLE_NEWS = """<?xml version="1.0"?>
<rss version="2.0"><channel>
  <item><title>D-Wave Quantum stock plunges after share offering - Reuters</title>
        <link>https://news.google.com/a</link>
        <pubDate>Mon, 10 Aug 2026 18:04:00 GMT</pubDate></item>
  <item><title>Quantum computing outlook for 2025 - Barron's</title>
        <link>https://news.google.com/b</link>
        <pubDate>Mon, 03 Mar 2025 10:00:00 GMT</pubDate></item>
  <item><title>Headline with no date</title>
        <link>https://news.google.com/c</link></item>
</channel></rss>"""


def test_publisher_suffix_is_stripped_from_google_news_titles():
    from stockbot.services.news import _strip_source_suffix

    assert (
        _strip_source_suffix("D-Wave stock plunges after share offering - Reuters")
        == "D-Wave stock plunges after share offering"
    )
    # A hyphenated company name must survive.
    assert _strip_source_suffix("D-Wave rises") == "D-Wave rises"


def test_stale_headlines_are_rejected_and_fresh_ones_kept():
    """A story from last year cannot explain today's 22% drop.

    Dates are built relative to now rather than hard-coded, so the test does
    not start failing once the fixture's dates age.
    """
    from datetime import datetime, timedelta, timezone

    from stockbot.services.news import Headline, _is_recent

    now = datetime.now(timezone.utc)
    assert _is_recent(Headline("Fresh", "x", published=now - timedelta(days=1))) is True
    assert _is_recent(Headline("Stale", "x", published=now - timedelta(days=30))) is False


def test_undated_headlines_are_kept():
    """Feeds vary; a missing date is not evidence the story is old."""
    from stockbot.services.news import _is_recent, parse_feed

    undated = [h for h in parse_feed(GOOGLE_NEWS, "x") if h.published is None]
    assert undated and _is_recent(undated[0]) is True


def test_pubdate_is_parsed_from_rfc822():
    from stockbot.services.news import parse_feed

    first = parse_feed(GOOGLE_NEWS, "news.google.com")[0]
    assert first.published is not None
    assert first.published.year == 2026 and first.published.month == 8


def test_per_company_news_needs_no_configured_feeds():
    """Uzbek feeds are configuration; per-company lookups are built in."""
    from stockbot.services.news import NewsProvider

    assert NewsProvider([]).enabled is True


# -- matching Russian news to Uzbek company names ---------------------------
# The exchange lists companies in Uzbek Latin; the press writes in Russian
# Cyrillic. Without folding the two onto common ground, nothing ever matches.


def test_russian_headlines_match_uzbek_company_names():
    from stockbot.services.news import fold, keywords_for

    pairs = [
        ("Кварц", "Kvarts AJ"),
        ("Узбектелеком", "O'zbektelekom AK"),
        ("УзАвто Моторс", "UzAuto Motors AJ"),
        ("Олмалик", "Olmaliq KMK AJ"),
        ("Ипотека-банк", "Ipoteka-bank ATIB"),
        ("Кизилкумцемент", "Qizilqumsement AJ"),
        ("Хамкорбанк", "Hamkorbank ATB"),
    ]
    for russian, uzbek in pairs:
        keywords = keywords_for("X", uzbek)
        assert any(k in fold(russian) for k in keywords), f"{russian} vs {uzbek}"


def test_folding_is_not_loose_enough_to_match_unrelated_news():
    """Over-collapsing would tag every headline onto some company."""
    from stockbot.services.news import Headline, match_headlines

    unrelated = [
        Headline("Доллар на 13 августа вырос на 58,63 сума", "kursiv"),
        Headline("Глава Xbox поиграла в The Elder Scrolls VI", "kursiv"),
        Headline("Сколько узбекистанцы отдохнут на День независимости", "kursiv"),
    ]
    matches = match_headlines(
        unrelated,
        {"KVTS": "Kvarts AJ", "UZMT": "UzAuto Motors AJ", "HMKB": "Hamkorbank ATB"},
    )
    assert matches == {}


def test_latin_headlines_still_match():
    """Uzbek-language outlets exist too; folding must not break them."""
    from stockbot.services.news import Headline, match_headlines

    news = [Headline("Kvarts oyna zavodi ishlab chiqarishni kengaytirdi", "spot")]
    assert "KVTS" in match_headlines(news, {"KVTS": "Kvarts AJ"})
