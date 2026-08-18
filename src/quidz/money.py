"""Integer minor units with a currency, and arithmetic that refuses to mix currencies.

An amount is always the pair (minor, currency). There is no float and no Decimal anywhere in
the ledger, and there is no rounding, because this repo has no split or allocation path.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "THREE_DECIMAL",
    "ZERO_DECIMAL",
    "CurrencyMismatch",
    "Money",
    "add",
    "exponent",
    "format_major",
    "sub",
]

# ISO 4217 exponents are not universally 2, so a hardcoded * 100 is a defect.
# https://docs.adyen.com/development-resources/currency-codes
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
