from __future__ import annotations

import pytest

from quidz.money import (
    ADYEN_EXPONENT,
    CurrencyMismatch,
    Money,
    add,
    exponent,
    format_major,
    sub,
)


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


def test_the_four_currencies_adyen_scales_differently_follow_iso_here() -> None:
    """CLP, CVE, IDR and ISK are the codes Adyen's own table disagrees with ISO 4217 on.

    The ledger holds one scale and it is the ISO one, so these four have to be right by ISO
    and knowingly different from Adyen rather than accidentally either. An integration that
    submitted to Adyen, or ingested these four from Adyen, would scale at the adapter.
    """
    iso = {"CLP": 0, "CVE": 2, "IDR": 2, "ISK": 0}
    assert {code: exponent(code) for code in ADYEN_EXPONENT} == iso
    # Every entry in the table earns its place by differing, or it is noise in a comment.
    assert all(exponent(code) != scale for code, scale in ADYEN_EXPONENT.items())


def test_the_zero_decimal_set_is_iso_and_not_a_provider_table() -> None:
    # ISK is the sharpest of the four: zero decimal by ISO, two decimal to Adyen, so getting
    # it from the wrong table overstates every ISK amount by a factor of a hundred.
    assert format_major(Money(1130, "ISK")) == "1130 ISK"
    assert format_major(Money(1130, "IDR")) == "11.30 IDR"


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
