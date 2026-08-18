"""Integer minor units with a currency, and arithmetic that refuses to mix currencies.

An amount is always the pair (minor, currency). There is no float and no Decimal anywhere in
the ledger, and there is no rounding, because this repo has no split or allocation path.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

__all__ = [
    "ADYEN_EXPONENT",
    "THREE_DECIMAL",
    "ZERO_DECIMAL",
    "CurrencyMismatch",
    "Money",
    "add",
    "exponent",
    "format_major",
    "sub",
]

# ISO 4217 exponents, which are not universally 2, so a hardcoded * 100 is a defect.
ZERO_DECIMAL: frozenset[str] = frozenset(
    {
        "BIF",
        "CLP",
        "DJF",
        "GNF",
        "ISK",
        "JPY",
        "KMF",
        "KRW",
        "PYG",
        "RWF",
        "UGX",
        "VND",
        "VUV",
        "XAF",
        "XOF",
        "XPF",
    }
)
THREE_DECIMAL: frozenset[str] = frozenset({"BHD", "IQD", "JOD", "KWD", "LYD", "OMR", "TND"})

# Adyen's currency table deliberately disagrees with ISO 4217 on exactly four codes, and Adyen
# names them as the trap: "For CLP, CVE, IDR, and ISK the ISO 4217 standard has a different
# number of decimals than shown in our currency codes table". ISK is the sharpest of them, zero
# decimal under ISO and two decimal to Adyen, so reading an Adyen ISK amount as an ISO minor
# unit is wrong by a factor of 100 in the direction that overstates the money.
#
# This is deliberately a named constant and not a branch. The ledger holds one scale, the ISO
# one, and this repo consumes webhooks rather than submitting payments or payouts, so nothing
# here converts. Anything that did submit to Adyen would have to scale by this table on the way
# out, and an integration ingesting these four currencies from Adyen would have to scale on the
# way in, at the adapter, before the amount ever reaches an aggregate.
# https://docs.adyen.com/development-resources/currency-codes
ADYEN_EXPONENT: Mapping[str, int] = MappingProxyType({"CLP": 2, "CVE": 0, "IDR": 0, "ISK": 2})

_DEFAULT_EXPONENT = 2


class CurrencyMismatch(ValueError):
    """Raised when two amounts in different currencies would be combined."""


def _validate_currency(currency: object) -> str:
    if not isinstance(currency, str):
        got = type(currency).__name__
        raise ValueError(f"currency must be an ISO 4217 alpha-3 string, got {got}")
    ok = len(currency) == 3 and currency.isascii() and currency.isalpha() and currency.isupper()
    if not ok:
        raise ValueError(f"currency must be three uppercase ASCII letters, got {currency!r}")
    return currency


@dataclass(frozen=True, slots=True)
class Money:
    minor: int
    currency: str

    def __post_init__(self) -> None:
        # bool is an int subclass, so it has to be rejected explicitly.
        if isinstance(self.minor, bool) or not isinstance(self.minor, int):
            got = type(self.minor).__name__
            raise ValueError(f"minor must be an int in minor units, got {got}")
        _validate_currency(self.currency)


def exponent(currency: str) -> int:
    """The ISO 4217 exponent. See ADYEN_EXPONENT for the four codes Adyen scales differently."""
    _validate_currency(currency)
    if currency in ZERO_DECIMAL:
        return 0
    if currency in THREE_DECIMAL:
        return 3
    return _DEFAULT_EXPONENT


def add(a: Money, b: Money) -> Money:
    if a.currency != b.currency:
        raise CurrencyMismatch(f"cannot add {b.currency} to {a.currency}")
    return Money(a.minor + b.minor, a.currency)


def sub(a: Money, b: Money) -> Money:
    if a.currency != b.currency:
        raise CurrencyMismatch(f"cannot subtract {b.currency} from {a.currency}")
    return Money(a.minor - b.minor, a.currency)


def format_major(m: Money) -> str:
    """Render an amount for human display. The result never feeds arithmetic."""
    places = exponent(m.currency)
    sign = "-" if m.minor < 0 else ""
    digits = str(abs(m.minor)).rjust(places + 1, "0")
    if places == 0:
        return f"{sign}{digits} {m.currency}"
    return f"{sign}{digits[:-places]}.{digits[-places:]} {m.currency}"
