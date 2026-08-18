"""The payment aggregate and its transition rules. Pure, zero I/O, no sqlite3.

The stored truth is the aggregate's amounts plus its child effect rows. The status a human
reads is DERIVED from those amounts and is never stored as the source of truth, because a
single linear status chain cannot express a partially refunded payment and because a refund
does not move a PaymentIntent out of `succeeded`.

https://docs.stripe.com/payments/paymentintents/lifecycle
https://docs.stripe.com/refunds
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
from typing import TYPE_CHECKING

from quidz.money import CurrencyMismatch

# Type only import. events imports EffectKind from this module, so a runtime import here
# would be a cycle, and this module stays free of every dependency except money.
if TYPE_CHECKING:
    from quidz.events import CanonicalEvent

__all__ = [
    "STATUSES",
    "AmountInvariantViolation",
    "EffectKind",
    "IllegalTransition",
    "PaymentState",
    "PrematureEvent",
    "apply_effect",
    "derive_status",
    "state_rank",
]


class EffectKind(StrEnum):
    AUTHORIZE = "authorize"
    AUTHORIZE_FAIL = "authorize_fail"
    ADJUST_AUTH = "adjust_auth"
    CAPTURE = "capture"
    CAPTURE_FAIL = "capture_fail"
    VOID = "void"
    EXPIRE = "expire"
    REFUND = "refund"
    REFUND_FAIL = "refund_fail"
    REFUND_REVERSE = "refund_reverse"


# There is no `settled` status. Neither Stripe nor Adyen sends a per payment settled webhook;
# settlement is a fact posted from a settlement file and joined on a different key, which is
# exactly why reconciliation in this repo is two source.
STATUSES: tuple[str, ...] = (
    "pending",
    "authorized",
    "capture_failed",
    "voided",
    "expired",
    "partially_captured",
    "captured",
    "partially_refunded",
    "refunded",
)

_STATUS_RANK: dict[str, int] = {
    "pending": 0,
    "authorized": 10,
    "capture_failed": 20,
    "voided": 30,
    "expired": 30,
    "partially_captured": 40,
    "captured": 50,
    "partially_refunded": 60,
    "refunded": 70,
}

# The lowest status rank an effect of this kind implies. A payment never regresses below the
# rank it has already reached, which is how out of order delivery is tolerated without
# ordering on the event timestamp: Stripe does not guarantee delivery order and Adyen
# documents duplicates whose eventDate differs.
_KIND_RANK: dict[EffectKind, int] = {
    EffectKind.AUTHORIZE_FAIL: 0,
    EffectKind.AUTHORIZE: 10,
    EffectKind.ADJUST_AUTH: 10,
    EffectKind.CAPTURE_FAIL: 20,
    EffectKind.VOID: 30,
    EffectKind.EXPIRE: 30,
    EffectKind.CAPTURE: 40,
    EffectKind.REFUND: 60,
    EffectKind.REFUND_FAIL: 60,
    EffectKind.REFUND_REVERSE: 60,
}

# The rank the aggregate must already carry for the effect to be applicable at all. Below it
# the effect is premature and the caller parks it rather than failing it.
_PREREQUISITE_RANK: dict[EffectKind, int] = {
    EffectKind.ADJUST_AUTH: 10,
    EffectKind.CAPTURE: 10,
    EffectKind.CAPTURE_FAIL: 10,
    EffectKind.VOID: 10,
    EffectKind.EXPIRE: 10,
    EffectKind.REFUND: 40,
    EffectKind.REFUND_FAIL: 60,
    EffectKind.REFUND_REVERSE: 60,
}

_NEEDS_AMOUNT = frozenset(
    {
        EffectKind.AUTHORIZE,
        EffectKind.ADJUST_AUTH,
        EffectKind.CAPTURE,
        EffectKind.CAPTURE_FAIL,
        EffectKind.REFUND,
        EffectKind.REFUND_FAIL,
        EffectKind.REFUND_REVERSE,
    }
)
_MOVES_MONEY = frozenset({EffectKind.CAPTURE, EffectKind.REFUND})


@dataclass(frozen=True, slots=True)
class PaymentState:
    payment_id: str
    currency: str
    authorized_minor: int = 0
    captured_minor: int = 0
    capture_failed_minor: int = 0
    refunded_minor: int = 0
    refund_failed_minor: int = 0
    voided: bool = False
    expired: bool = False
    rank: int = 0
    last_sequence: int = -1


class IllegalTransition(Exception):
    """The effect contradicts something the aggregate already knows."""


class AmountInvariantViolation(Exception):
    """The effect would break amount conservation."""


class PrematureEvent(Exception):
    """The prerequisite effect has not arrived yet. The caller parks, it does not fail."""


def derive_status(state: PaymentState) -> str:
    if state.captured_minor > 0:
        if state.refunded_minor >= state.captured_minor:
            return "refunded"
        if state.refunded_minor > 0:
            return "partially_refunded"
        if state.captured_minor >= state.authorized_minor:
            return "captured"
        return "partially_captured"
    if state.expired:
        return "expired"
    if state.voided:
        return "voided"
    if state.capture_failed_minor > 0:
        return "capture_failed"
    if state.authorized_minor > 0:
        return "authorized"
    return "pending"


def state_rank(status: str) -> int:
    try:
        return _STATUS_RANK[status]
    except KeyError:
        raise ValueError(f"unknown payment status {status!r}") from None


def _amount_minor(event: CanonicalEvent, state: PaymentState) -> int:
    amount = event.amount
    if amount is None:
        raise IllegalTransition(f"{event.kind} requires an amount, delivery carried none")
    if amount.currency != state.currency:
        raise CurrencyMismatch(
            f"event currency {amount.currency} does not match payment {state.payment_id} "
            f"held in {state.currency}"
        )
    if amount.minor < 0:
        raise AmountInvariantViolation(f"{event.kind} amount must not be negative")
    return amount.minor


def apply_effect(state: PaymentState, event: CanonicalEvent) -> PaymentState:
    kind = event.kind
    minor = _amount_minor(event, state) if kind in _NEEDS_AMOUNT else 0

    if kind in _MOVES_MONEY and (state.voided or state.expired):
        reason = "expired" if state.expired else "voided"
        raise IllegalTransition(f"cannot {kind} a payment that is already {reason}")
    # A refund against a capture that definitively did not happen is a business error. A
    # refund against a capture that has merely not arrived yet is premature, handled below.
    capture_ruled_out = state.capture_failed_minor > 0 or state.voided or state.expired
    if kind is EffectKind.REFUND and state.captured_minor == 0 and capture_ruled_out:
        raise IllegalTransition(
            "cannot refund a payment that was never captured; cancel it instead"
        )

    prerequisite = _PREREQUISITE_RANK.get(kind)
    if prerequisite is not None and state.rank < prerequisite:
        raise PrematureEvent(
            f"{kind} arrived before its prerequisite on payment {state.payment_id}"
        )

    if kind is EffectKind.CAPTURE and state.captured_minor + minor > state.authorized_minor:
        raise AmountInvariantViolation(
            f"capture of {minor} would exceed the authorization of {state.authorized_minor} "
            f"on payment {state.payment_id}"
        )
    if kind is EffectKind.REFUND and state.refunded_minor + minor > state.captured_minor:
        raise AmountInvariantViolation(
            f"refund of {minor} would exceed the captured {state.captured_minor} "
            f"on payment {state.payment_id}"
        )
    if kind in (EffectKind.REFUND_FAIL, EffectKind.REFUND_REVERSE) and minor > state.refunded_minor:
        raise AmountInvariantViolation(
            f"{kind} of {minor} exceeds the outstanding refunded {state.refunded_minor} "
            f"on payment {state.payment_id}"
        )
    if kind is EffectKind.ADJUST_AUTH and minor < state.captured_minor:
        raise AmountInvariantViolation(
            f"adjusted authorization {minor} is below the captured {state.captured_minor}"
        )

    # One capture per payment. A partial capture releases the remainder of the hold, so there
    # is no coming back for the difference; multicapture is a separate opt in feature.
    # https://docs.stripe.com/payments/place-a-hold-on-a-payment-method
    if kind is EffectKind.CAPTURE and state.captured_minor > 0:
        raise IllegalTransition(
            f"payment {state.payment_id} is already captured and the remainder of a partial "
            "capture is released, so a second capture is not possible"
        )

    if _KIND_RANK[kind] < state.rank:
        raise IllegalTransition(
            f"{kind} would regress payment {state.payment_id} below rank {state.rank}"
        )

    updated = _fold(state, kind, minor)
    rank = max(state.rank, _KIND_RANK[kind], state_rank(derive_status(updated)))
    return replace(
        updated,
        rank=rank,
        last_sequence=max(state.last_sequence, event.sequence),
    )


def _fold(state: PaymentState, kind: EffectKind, minor: int) -> PaymentState:
    if kind is EffectKind.AUTHORIZE:
        return replace(state, authorized_minor=state.authorized_minor + minor)
    if kind is EffectKind.ADJUST_AUTH:
        # Adyen's AUTHORISATION_ADJUSTMENT carries the new total, not a delta.
        return replace(state, authorized_minor=minor)
    if kind is EffectKind.AUTHORIZE_FAIL:
        # A declined authorization leaves the payment pending so the shopper can retry. It is
        # recorded as a child effect and moves no amount on the aggregate.
        return state
    if kind is EffectKind.CAPTURE:
        return replace(state, captured_minor=state.captured_minor + minor)
    if kind is EffectKind.CAPTURE_FAIL:
        return replace(state, capture_failed_minor=state.capture_failed_minor + minor)
    if kind is EffectKind.VOID:
        return replace(state, voided=True)
    if kind is EffectKind.EXPIRE:
        return replace(state, expired=True)
    if kind is EffectKind.REFUND:
        return replace(state, refunded_minor=state.refunded_minor + minor)
    # A failed refund and a reversed refund have the same balance effect, money that left is
    # back with the merchant. The child effect row keeps the two distinguishable.
    return replace(
        state,
        refunded_minor=state.refunded_minor - minor,
        refund_failed_minor=state.refund_failed_minor + minor,
    )
