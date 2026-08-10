from stockbot.formatting import (
    money,
    percent,
    quote_lines,
    render_report,
    render_watchlist,
    split_message,
    trend_emoji,
)
from stockbot.services.prices import Quote
from stockbot.storage import WatchlistEntry


def make_quote(**overrides) -> Quote:
    defaults = dict(
        ticker="ONTO",
        name="Onto Innovation",
        price=123.456,
        day_change=2.1,
        day_change_pct=1.73,
        week_change_pct=-3.4,
        session_date="2026-08-07",
    )
    defaults.update(overrides)
    return Quote(**defaults)


def test_money_and_percent_formatting():
    assert money(1234.5) == "$1,234.50"
    assert money(None) == "—"
    assert money(10.0, "EUR") == "€10.00"
    assert percent(1.73) == "+1.7%"
    assert percent(-0.04) == "-0.0%"
    assert percent(None) == "—"


def test_trend_emoji_thresholds():
    assert trend_emoji(2.0) == "📈"
    assert trend_emoji(-2.0) == "📉"
    assert trend_emoji(0.0) == "▪️"
    assert trend_emoji(None) == "▪️"


def test_quote_lines_include_ticker_price_and_changes():
    rendered = quote_lines(make_quote())
    assert "ONTO" in rendered
    assert "$123.46" in rendered
    assert "+1.7% day" in rendered
    assert "-3.4% week" in rendered


def test_broken_quote_renders_a_single_warning_line():
    rendered = quote_lines(Quote(ticker="MANE", error="unknown ticker or no data"))
    assert rendered.startswith("⚠️")
    assert "unknown ticker" in rendered
    assert "\n" not in rendered


def test_html_special_characters_are_escaped():
    rendered = quote_lines(make_quote(name="A & B <Corp>"))
    assert "&amp;" in rendered and "&lt;Corp&gt;" in rendered


def test_report_averages_only_usable_quotes():
    quotes = [
        make_quote(ticker="AAA", day_change_pct=4.0),
        make_quote(ticker="BBB", day_change_pct=-2.0),
        Quote(ticker="CCC", error="no data"),
    ]
    text = render_report(quotes, None, None, "2026-08-07", is_live=False)
    assert "+1.0%" in text  # (4.0 - 2.0) / 2
    assert "market close" in text
    assert "CCC" in text


def test_report_marks_live_prices_and_includes_ai_comment():
    text = render_report([make_quote()], None, "Chips rallied.", "2026-08-07", is_live=True)
    assert "live prices" in text
    assert "Chips rallied." in text
    assert "Not investment advice" in text


def test_empty_watchlist_message_explains_next_step():
    assert "/add" in render_watchlist([], "09:00 UTC")


def test_watchlist_lists_every_entry():
    entries = [
        WatchlistEntry("ONTO", "Onto Innovation", "US"),
        WatchlistEntry("NOW", None, "US"),
    ]
    text = render_watchlist(entries, "09:00 UTC")
    assert "ONTO" in text and "NOW" in text and "09:00 UTC" in text


def test_watchlist_separates_the_two_exchanges():
    entries = [
        WatchlistEntry("ONTO", "Onto Innovation", "US"),
        WatchlistEntry("KVTS", "Kvarts", "UZSE"),
    ]
    text = render_watchlist(entries, "09:00 UTC")
    assert "US market" in text and "UZSE" in text
    assert text.index("ONTO") < text.index("KVTS")


def test_uzs_prices_have_no_decimals():
    assert money(12500.0, "UZS") == "12 500 UZS"


def test_report_keeps_the_two_markets_in_separate_sections():
    quotes = [
        make_quote(ticker="ONTO", day_change_pct=1.5),
        make_quote(
            ticker="KVTS", name="Kvarts", price=12500.0, currency="UZS",
            day_change_pct=-2.0, market="UZSE",
        ),
    ]
    text = render_report(quotes, None, None, "2026-08-07", is_live=False)
    assert "US market" in text and "UZSE" in text
    assert "12 500 UZS" in text
    # The averages line must cover US stocks only — different currency, calendar.
    assert "Your US stocks on average: <b>+1.5%</b>" in text
    assert "parse.bot" in text


def test_report_without_uzse_does_not_mention_parsebot():
    text = render_report([make_quote()], None, None, "2026-08-07", is_live=False)
    assert "parse.bot" not in text


def test_split_message_respects_limit_and_keeps_content():
    text = "\n".join(f"line {i}" for i in range(500))
    chunks = split_message(text, limit=200)
    assert all(len(chunk) <= 200 for chunk in chunks)
    assert "line 499" in chunks[-1]


def test_split_message_leaves_short_text_alone():
    assert split_message("hello") == ["hello"]
