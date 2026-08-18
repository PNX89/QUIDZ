from __future__ import annotations

import pytest

from conftest import EventFactory
from quidz.model import (
    AmountInvariantViolation,
    EffectKind,
    IllegalTransition,
    PaymentState,
    PrematureEvent,
    apply_effect,
    derive_status,
)

FRESH = PaymentState(payment_id="pay_1", currency="EUR")


def test_authorize_then_full_capture_derives_captured(make_event: EventFactory) -> None:
    state = apply_effect(FRESH, make_event(EffectKind.AUTHORIZE, 1000))
    state = apply_effect(state, make_event(EffectKind.CAPTURE, 1000))
    assert derive_status(state) == "captured"


def test_partial_capture_derives_partially_captured_and_releases_the_remainder(
    make_event: EventFactory,
) -> None:
    state = apply_effect(FRESH, make_event(EffectKind.AUTHORIZE, 1000))
    state = apply_effect(state, make_event(EffectKind.CAPTURE, 400))
    assert derive_status(state) == "partially_captured"
    with pytest.raises(IllegalTransition, match="released"):
        apply_effect(state, make_event(EffectKind.CAPTURE, 300, ref="ref_capture_2"))


def test_over_capture_violates_amount_conservation(make_event: EventFactory) -> None:
    state = apply_effect(FRESH, make_event(EffectKind.AUTHORIZE, 1000))
    with pytest.raises(AmountInvariantViolation, match="exceed the authorization"):
        apply_effect(state, make_event(EffectKind.CAPTURE, 1200))


def test_two_partial_refunds_summing_to_the_capture_derive_refunded(
    make_event: EventFactory,
) -> None:
    state = apply_effect(FRESH, make_event(EffectKind.AUTHORIZE, 1000))
    state = apply_effect(state, make_event(EffectKind.CAPTURE, 1000))
    state = apply_effect(state, make_event(EffectKind.REFUND, 600, ref="re_1"))
    state = apply_effect(state, make_event(EffectKind.REFUND, 400, ref="re_2"))
    assert derive_status(state) == "refunded"


def test_over_refund_violates_amount_conservation(make_event: EventFactory) -> None:
    state = apply_effect(FRESH, make_event(EffectKind.AUTHORIZE, 1000))
    state = apply_effect(state, make_event(EffectKind.CAPTURE, 1000))
    state = apply_effect(state, make_event(EffectKind.REFUND, 600, ref="re_1"))
    with pytest.raises(AmountInvariantViolation, match="exceed the captured"):
        apply_effect(state, make_event(EffectKind.REFUND, 600, ref="re_2"))


def test_refund_on_an_uncaptured_payment_says_cancel_instead(make_event: EventFactory) -> None:
    state = apply_effect(FRESH, make_event(EffectKind.AUTHORIZE, 1000))
    state = apply_effect(state, make_event(EffectKind.CAPTURE_FAIL, 1000))
    with pytest.raises(IllegalTransition, match="cancel it instead"):
        apply_effect(state, make_event(EffectKind.REFUND, 1000))


def test_capture_on_a_voided_payment_is_illegal(make_event: EventFactory) -> None:
    state = apply_effect(FRESH, make_event(EffectKind.AUTHORIZE, 1000))
    state = apply_effect(state, make_event(EffectKind.VOID))
    with pytest.raises(IllegalTransition, match="voided"):
        apply_effect(state, make_event(EffectKind.CAPTURE, 1000))


def test_capture_on_an_expired_payment_is_illegal(make_event: EventFactory) -> None:
    state = apply_effect(FRESH, make_event(EffectKind.AUTHORIZE, 1000))
    state = apply_effect(state, make_event(EffectKind.EXPIRE))
    with pytest.raises(IllegalTransition, match="expired"):
        apply_effect(state, make_event(EffectKind.CAPTURE, 1000))


def test_capture_fail_leaves_captured_at_zero_and_capture_can_still_succeed(
    make_event: EventFactory,
) -> None:
    state = apply_effect(FRESH, make_event(EffectKind.AUTHORIZE, 1000))
    state = apply_effect(state, make_event(EffectKind.CAPTURE_FAIL, 1000))
    assert (state.captured_minor, derive_status(state)) == (0, "capture_failed")
    retried = apply_effect(state, make_event(EffectKind.CAPTURE, 1000))
    assert derive_status(retried) == "captured"


def test_a_failed_authorization_leaves_the_payment_pending_and_retryable(
    make_event: EventFactory,
) -> None:
    state = apply_effect(FRESH, make_event(EffectKind.AUTHORIZE_FAIL, 1000))
    assert (state.authorized_minor, derive_status(state)) == (0, "pending")
    retried = apply_effect(state, make_event(EffectKind.AUTHORIZE, 1000))
    assert derive_status(retried) == "authorized"


def test_refund_fail_after_a_succeeded_refund_reduces_refunded(make_event: EventFactory) -> None:
    state = apply_effect(FRESH, make_event(EffectKind.AUTHORIZE, 1000))
    state = apply_effect(state, make_event(EffectKind.CAPTURE, 1000))
    state = apply_effect(state, make_event(EffectKind.REFUND, 400, ref="re_1"))
    state = apply_effect(state, make_event(EffectKind.REFUND_FAIL, 400, ref="re_1"))
    assert (state.refunded_minor, state.refund_failed_minor) == (0, 400)


def test_refund_reverse_after_a_succeeded_refund_reduces_refunded(
    make_event: EventFactory,
) -> None:
    state = apply_effect(FRESH, make_event(EffectKind.AUTHORIZE, 1000))
    state = apply_effect(state, make_event(EffectKind.CAPTURE, 1000))
    state = apply_effect(state, make_event(EffectKind.REFUND, 400, ref="re_1"))
    state = apply_effect(state, make_event(EffectKind.REFUND_REVERSE, 400, ref="re_1"))
    assert (state.refunded_minor, derive_status(state)) == (0, "captured")


def test_rank_never_regresses_on_a_late_arriving_earlier_event(
    make_event: EventFactory,
) -> None:
    state = apply_effect(FRESH, make_event(EffectKind.AUTHORIZE, 1000))
    state = apply_effect(state, make_event(EffectKind.CAPTURE, 1000))
    with pytest.raises(IllegalTransition, match="regress"):
        apply_effect(state, make_event(EffectKind.VOID))


def test_capture_before_authorize_is_premature_not_a_failure(make_event: EventFactory) -> None:
    with pytest.raises(PrematureEvent):
        apply_effect(FRESH, make_event(EffectKind.CAPTURE, 1000))
