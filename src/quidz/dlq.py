"""Poison message handling: typed reason codes, bounded jittered backoff, a global budget.

Every rejection is classified permanent or transient before anything is retried. A handler
that treats every failure as transient turns one malformed event class into a self amplifying
retry storm, which is the outage this repo is built around.
"""

from __future__ import annotations

import json
import random
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass

from quidz.events import MalformedEvent, UnknownEventType
from quidz.model import AmountInvariantViolation, IllegalTransition
from quidz.money import CurrencyMismatch
from quidz.verify import BadSignature, MalformedHeader, SignatureError, StaleTimestamp

__all__ = [
    "REASON_CODES",
    "ReasonCode",
    "RetryPolicy",
    "classify",
    "jitter_source",
    "next_delay",
    "should_retry",
]


@dataclass(frozen=True, slots=True)
class ReasonCode:
    code: str
    retryable: bool
    max_attempts: int | None


REASON_CODES: Mapping[str, ReasonCode] = {
    "signature_invalid": ReasonCode("signature_invalid", False, None),
    "stale_timestamp": ReasonCode("stale_timestamp", False, None),
    "schema_invalid": ReasonCode("schema_invalid", False, None),
    "illegal_transition": ReasonCode("illegal_transition", False, None),
    "amount_invariant": ReasonCode("amount_invariant", False, None),
    "currency_mismatch": ReasonCode("currency_mismatch", False, None),
    "unknown_event_type": ReasonCode("unknown_event_type", True, 3),
    "downstream_unavailable": ReasonCode("downstream_unavailable", True, 5),
    # An event type this ledger does not model yet must never be dropped on the floor, so the
    # catch all is retryable with a cap and ends up in the dead letter queue where a human
    # sees it. A non retryable default would silently discard a provider's new event type.
    "unknown": ReasonCode("unknown", True, 3),
}

# Order matters: CurrencyMismatch subclasses ValueError and StaleTimestamp subclasses
# SignatureError, so the specific entries have to be consulted first.
_CLASSIFIERS: tuple[tuple[type[BaseException], str], ...] = (
    (StaleTimestamp, "stale_timestamp"),
    (BadSignature, "signature_invalid"),
    (MalformedHeader, "signature_invalid"),
    (SignatureError, "signature_invalid"),
    (UnknownEventType, "unknown_event_type"),
    (MalformedEvent, "schema_invalid"),
    (json.JSONDecodeError, "schema_invalid"),
    # A body that is not UTF-8 fails the same parse for the same permanent reason, but
    # UnicodeDecodeError is a ValueError rather than a JSONDecodeError, so without this entry
    # it falls through to the retryable catch all and burns three attempts on a stored payload
    # that cannot change between them.
    (UnicodeDecodeError, "schema_invalid"),
    (CurrencyMismatch, "currency_mismatch"),
    (AmountInvariantViolation, "amount_invariant"),
    (IllegalTransition, "illegal_transition"),
    (sqlite3.OperationalError, "downstream_unavailable"),
)


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    base_seconds: float = 1.0
    max_seconds: float = 300.0
    max_attempts: int = 5
    # A per attempt cap alone still lets ten thousand poisoned deliveries retry in lockstep.
    budget_per_minute: int = 100
    # None everywhere except in a test that wants a fixed schedule to assert against. A seed
    # that reaches production is worse than no jitter at all in one respect: every worker in
    # the fleet draws the identical sequence and retries in lockstep, which is exactly the
    # thundering herd the jitter exists to break up. Determinism belongs to the tests only.
    seed: int | None = None


def jitter_source(policy: RetryPolicy) -> random.Random:
    """One generator per drain worker: unpredictable in production, reproducible under a seed."""
    return random.SystemRandom() if policy.seed is None else random.Random(policy.seed)


def next_delay(policy: RetryPolicy, attempt: int, rng: random.Random) -> float:
    """Full jitter backoff. The rng is passed in, never the global random module."""
    if attempt < 0:
        raise ValueError("attempt must not be negative")
    ceiling = min(policy.max_seconds, policy.base_seconds * 2**attempt)
    return rng.uniform(0.0, ceiling)


def classify(exc: BaseException) -> ReasonCode:
    for exc_type, code in _CLASSIFIERS:
        if isinstance(exc, exc_type):
            return REASON_CODES[code]
    return REASON_CODES["unknown"]


def should_retry(
    policy: RetryPolicy, reason: ReasonCode, attempt: int, *, budget_used: int
) -> bool:
    if not reason.retryable:
        return False
    if budget_used >= policy.budget_per_minute:
        return False
    cap = policy.max_attempts
    if reason.max_attempts is not None:
        cap = min(cap, reason.max_attempts)
    return attempt < cap
