from decimal import Decimal

from pricing.normalize import identity_hash, parse_date, parse_int, parse_price


def test_parse_price_string_forms():
    assert parse_price("$329,990") == Decimal("329990")
    assert parse_price("From $329,990") == Decimal("329990")
    assert parse_price("329990.00") == Decimal("329990.00")


def test_parse_price_numeric_forms():
    assert parse_price(228999) == Decimal("228999")
    assert parse_price(228999.0) == Decimal("228999.0")


def test_parse_price_missing_or_unpriced():
    assert parse_price(None) is None
    assert parse_price(0) is None  # OriginalPrice=0 means "no was-price"
    assert parse_price("Call for price") is None
    assert parse_price("") is None


def test_parse_int():
    assert parse_int("1,190") == 1190
    assert parse_int(1190.0) == 1190
    assert parse_int(None) is None


def test_parse_date():
    assert parse_date("2026-07-26").isoformat() == "2026-07-26"
    assert parse_date("2026-07-26T00:00:00").isoformat() == "2026-07-26"
    assert parse_date(None) is None
    assert parse_date("TBD") is None


def test_identity_hash_stable_and_sensitive():
    a = identity_hash(1190, None, None, 1)
    assert a == identity_hash(1190, None, None, 1)
    assert a != identity_hash(1191, None, None, 1)
