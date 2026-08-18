"""Fail-closed payment webhook reconciliation over synthetic data."""

from __future__ import annotations

from quidz.clock import Clock, FakeClock, SystemClock
from quidz.events import (
    CanonicalEvent,
    EventError,
    MalformedEvent,
    Provider,
    UnknownEventType,
    from_adyen,
    from_stripe,
)
from quidz.model import (
    AmountInvariantViolation,
    EffectKind,
    IllegalTransition,
    PaymentState,
    PrematureEvent,
    apply_effect,
    derive_status,
    state_rank,
)
from quidz.money import (
    THREE_DECIMAL,
    ZERO_DECIMAL,
    CurrencyMismatch,
    Money,
    add,
    exponent,
    format_major,
    sub,
)
from quidz.store import SCHEMA_VERSION, connect, init_schema, write_tx
from quidz.verify import (
    STRIPE_DEFAULT_TOLERANCE_SECONDS,
    BadSignature,
    MalformedHeader,
    SignatureError,
    StaleTimestamp,
    adyen_escape,
    adyen_signing_fields,
    adyen_signing_string,
    parse_stripe_signature_header,
    verify_adyen,
    verify_stripe,
)

__version__ = "0.1.0"

__all__ = [
    "SCHEMA_VERSION",
    "STRIPE_DEFAULT_TOLERANCE_SECONDS",
    "THREE_DECIMAL",
    "ZERO_DECIMAL",
    "AmountInvariantViolation",
    "BadSignature",
    "CanonicalEvent",
    "Clock",
    "CurrencyMismatch",
    "EffectKind",
    "EventError",
    "FakeClock",
    "IllegalTransition",
    "MalformedEvent",
    "MalformedHeader",
    "Money",
    "PaymentState",
    "PrematureEvent",
    "Provider",
    "SignatureError",
    "StaleTimestamp",
    "SystemClock",
    "UnknownEventType",
    "__version__",
    "add",
    "adyen_escape",
    "adyen_signing_fields",
    "adyen_signing_string",
    "apply_effect",
    "connect",
    "derive_status",
    "exponent",
    "format_major",
    "from_adyen",
    "from_stripe",
    "init_schema",
    "parse_stripe_signature_header",
    "state_rank",
    "sub",
    "verify_adyen",
    "verify_stripe",
    "write_tx",
]
