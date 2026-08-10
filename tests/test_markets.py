from stockbot.markets import detect_market, market_by_code


def test_bare_symbols_are_us():
    assert detect_market("NVDA").code == "US"
    assert detect_market("ONTO").code == "US"


def test_suffixes_identify_the_exchange():
    assert detect_market("BP.L").label == "London"
    assert detect_market("SAP.DE").label == "Frankfurt"
    assert detect_market("7203.T").label == "Tokyo"
    assert detect_market("0700.HK").label == "Hong Kong"
    assert detect_market("SHOP.TO").code == "CA"
    assert detect_market("RELIANCE.NS").code == "IN"


def test_local_index_is_offered_per_market():
    assert detect_market("BP.L").benchmark == "^FTSE"
    assert detect_market("7203.T").benchmark == "^N225"
    assert detect_market("NVDA").benchmark == "SPY"


def test_crypto_and_indices_are_their_own_groups():
    assert detect_market("BTC-USD").code == "CRYPTO"
    assert detect_market("^GSPC").code == "INDEX"


def test_unknown_suffix_gets_its_own_section_rather_than_being_called_us():
    market = detect_market("XYZ.ZZ")
    assert market.code == "ZZ"
    assert market.code != "US"


def test_codes_resolve_back_to_labels_for_rendering():
    assert market_by_code("UZSE").flag == "🇺🇿"
    assert market_by_code("JP").label == "Tokyo"
    assert market_by_code("NOPE").label == "NOPE exchange"
