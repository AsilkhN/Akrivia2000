"""Which exchange a ticker belongs to.

Yahoo covers about seventy exchanges and encodes the venue in the ticker's
suffix: `BP.L` is London, `SAP.DE` is Frankfurt, `7203.T` is Tokyo, a bare
symbol is the US. That suffix is enough to group a report by market without
spending a single extra request.

Grouping matters because averaging across markets is meaningless: different
currencies, different trading calendars, different hours. A day when Tokyo rose
and New York fell is two facts, not one blended number.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Market:
    code: str
    label: str
    flag: str
    benchmark: str | None = None  # a Yahoo symbol for the local index

    @property
    def heading(self) -> str:
        return f"{self.flag} {self.label}"


US = Market("US", "US market", "🇺🇸", "SPY")
UZSE = Market("UZSE", "Uzbek exchange (UZSE)", "🇺🇿")

# Suffix → market. Only the venues Yahoo actually serves are listed; anything
# unknown falls back to a generic entry built from the suffix itself, so an
# unusual ticker still lands in its own section instead of being mislabelled.
_BY_SUFFIX: dict[str, Market] = {
    "L": Market("UK", "London", "🇬🇧", "^FTSE"),
    "DE": Market("DE", "Frankfurt", "🇩🇪", "^GDAXI"),
    "F": Market("DE", "Frankfurt", "🇩🇪", "^GDAXI"),
    "PA": Market("FR", "Paris", "🇫🇷", "^FCHI"),
    "AS": Market("NL", "Amsterdam", "🇳🇱", "^AEX"),
    "BR": Market("BE", "Brussels", "🇧🇪", "^BFX"),
    "MC": Market("ES", "Madrid", "🇪🇸", "^IBEX"),
    "MI": Market("IT", "Milan", "🇮🇹", "FTSEMIB.MI"),
    "LS": Market("PT", "Lisbon", "🇵🇹", "^PSI20"),
    "VI": Market("AT", "Vienna", "🇦🇹", "^ATX"),
    "SW": Market("CH", "Zurich", "🇨🇭", "^SSMI"),
    "ST": Market("SE", "Stockholm", "🇸🇪", "^OMX"),
    "OL": Market("NO", "Oslo", "🇳🇴", "^OSEAX"),
    "CO": Market("DK", "Copenhagen", "🇩🇰", "^OMXC25"),
    "HE": Market("FI", "Helsinki", "🇫🇮", "^OMXH25"),
    "IR": Market("IE", "Dublin", "🇮🇪", "^ISEQ"),
    "WA": Market("PL", "Warsaw", "🇵🇱", "^WIG20"),
    "PR": Market("CZ", "Prague", "🇨🇿", "^PX"),
    "IS": Market("TR", "Istanbul", "🇹🇷", "XU100.IS"),
    "TA": Market("IL", "Tel Aviv", "🇮🇱", "^TA125.TA"),
    "TO": Market("CA", "Toronto", "🇨🇦", "^GSPTSE"),
    "V": Market("CA", "Toronto", "🇨🇦", "^GSPTSE"),
    "NE": Market("CA", "Toronto", "🇨🇦", "^GSPTSE"),
    "MX": Market("MX", "Mexico City", "🇲🇽", "^MXX"),
    "SA": Market("BR", "São Paulo", "🇧🇷", "^BVSP"),
    "BA": Market("AR", "Buenos Aires", "🇦🇷", "^MERV"),
    "T": Market("JP", "Tokyo", "🇯🇵", "^N225"),
    "HK": Market("HK", "Hong Kong", "🇭🇰", "^HSI"),
    "SS": Market("CN", "Shanghai", "🇨🇳", "000001.SS"),
    "SZ": Market("CN", "Shenzhen", "🇨🇳", "399001.SZ"),
    "KS": Market("KR", "Seoul", "🇰🇷", "^KS11"),
    "KQ": Market("KR", "Seoul", "🇰🇷", "^KS11"),
    "TW": Market("TW", "Taipei", "🇹🇼", "^TWII"),
    "TWO": Market("TW", "Taipei", "🇹🇼", "^TWII"),
    "NS": Market("IN", "Mumbai (NSE)", "🇮🇳", "^NSEI"),
    "BO": Market("IN", "Mumbai (BSE)", "🇮🇳", "^BSESN"),
    "AX": Market("AU", "Sydney", "🇦🇺", "^AXJO"),
    "NZ": Market("NZ", "Auckland", "🇳🇿", "^NZ50"),
    "SI": Market("SG", "Singapore", "🇸🇬", "^STI"),
    "KL": Market("MY", "Kuala Lumpur", "🇲🇾", "^KLSE"),
    "BK": Market("TH", "Bangkok", "🇹🇭", "^SET.BK"),
    "JK": Market("ID", "Jakarta", "🇮🇩", "^JKSE"),
    "SR": Market("SA", "Riyadh", "🇸🇦", "^TASI.SR"),
    "AE": Market("AE", "Abu Dhabi", "🇦🇪", None),
    "QA": Market("QA", "Doha", "🇶🇦", None),
    "JO": Market("ZA", "Johannesburg", "🇿🇦", "^J203.JO"),
    "CA": Market("EG", "Cairo", "🇪🇬", "^CASE30"),
}

CRYPTO = Market("CRYPTO", "Crypto", "🪙")
INDEX = Market("INDEX", "Indices", "📊")


def detect_market(ticker: str) -> Market:
    """Work out the venue from a Yahoo symbol. No network, no cost."""
    symbol = ticker.strip().upper()
    if not symbol:
        return US
    if symbol.startswith("^"):
        return INDEX
    if symbol.endswith(("-USD", "-EUR", "-GBP", "-USDT")):
        return CRYPTO
    if "." not in symbol:
        return US

    suffix = symbol.rsplit(".", 1)[1]
    known = _BY_SUFFIX.get(suffix)
    if known is not None:
        return known
    # An exchange we have no entry for still deserves its own section rather
    # than being silently filed under "US market".
    return Market(suffix, f"{suffix} exchange", "🏛")


def benchmark_for(market: Market) -> str | None:
    return market.benchmark


def all_markets() -> list[Market]:
    return [US, UZSE, CRYPTO, INDEX, *dict.fromkeys(_BY_SUFFIX.values())]


def market_by_code(code: str) -> Market:
    """Resolve a stored market code back to its label, for rendering."""
    for market in all_markets():
        if market.code == code:
            return market
    return Market(code, f"{code} exchange", "🏛")
