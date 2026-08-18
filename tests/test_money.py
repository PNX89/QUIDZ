from __future__ import annotations

import pytest

from quidz.money import CurrencyMismatch, Money, add, exponent, format_major, sub


def test_minor_must_be_an_int_and_bool_is_rejected() -> None:
    for bad in (1.0, True, "100", None):
        with pytest.raises(ValueError, match="minor must be an int"):
            Money(bad, "EUR")  # type: ignore[arg-type]


def test_currency_must_be_three_uppercase_ascii_letters() -> None:
    for bad in ("eur", "eUR", "EU", "EURO", "E1R", 978):
        with pytest.raises(ValueError, match="currency must be"):
            Money(100, bad)  # type: ignore[arg-type]


def test_zero_decimal_currencies_have_exponent_zero() -> None:
    assert (exponent("JPY"), exponent("KRW")) == (0, 0)


def test_three_decimal_currencies_have_exponent_three() -> None:
    assert (exponent("BHD"), exponent("KWD"), exponent("TND")) == (3, 3, 3)


def test_ordinary_currencies_have_exponent_two() -> None:
    assert (exponent("EUR"), exponent("USD")) == (2, 2)


def test_arithmetic_refuses_to_cross_currencies() -> None:
    with pytest.raises(CurrencyMismatch):
        add(Money(100, "EUR"), Money(100, "USD"))
    with pytest.raises(CurrencyMismatch):
        sub(Money(100, "EUR"), Money(100, "USD"))


def test_format_major_is_display_only_and_never_feeds_arithmetic() -> None:
    assert format_major(Money(123456, "EUR")) == "1234.56 EUR"
    assert format_major(Money(1130, "JPY")) == "1130 JPY"
    assert format_major(Money(-5, "EUR")) == "-0.05 EUR"
    with pytest.raises(ValueError, match="minor must be an int"):
        Money(format_major(Money(123456, "EUR")), "EUR")  # type: ignore[arg-type]
