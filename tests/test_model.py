from __future__ import annotations

import contextlib
import pathlib

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


def test_refund_fail_after_a_full_refund_still_applies(make_event: EventFactory) -> None:
    # A full refund is the common shape and the one the rank ratchet used to make impossible:
    # the aggregate reached refunded, and every refund correction ranks below it.
    state = apply_effect(FRESH, make_event(EffectKind.AUTHORIZE, 1000))
    state = apply_effect(state, make_event(EffectKind.CAPTURE, 1000))
    state = apply_effect(state, make_event(EffectKind.REFUND, 1000, ref="re_1"))
    assert derive_status(state) == "refunded"
    state = apply_effect(state, make_event(EffectKind.REFUND_FAIL, 1000, ref="re_1"))
    assert (state.refunded_minor, state.refund_failed_minor, derive_status(state)) == (
        0,
        1000,
        "captured",
    )


def test_refund_reverse_after_a_full_refund_still_applies(make_event: EventFactory) -> None:
    state = apply_effect(FRESH, make_event(EffectKind.AUTHORIZE, 1000))
    state = apply_effect(state, make_event(EffectKind.CAPTURE, 1000))
    state = apply_effect(state, make_event(EffectKind.REFUND, 1000, ref="re_1"))
    state = apply_effect(state, make_event(EffectKind.REFUND_REVERSE, 1000, ref="re_1"))
    assert (state.refunded_minor, derive_status(state)) == (0, "captured")


def test_a_reversed_refund_still_refuses_a_late_void(make_event: EventFactory) -> None:
    # The rank follows the money back down, so it must not fall far enough to let a stale
    # cancellation land on a payment that is still captured.
    state = apply_effect(FRESH, make_event(EffectKind.AUTHORIZE, 1000))
    state = apply_effect(state, make_event(EffectKind.CAPTURE, 1000))
    state = apply_effect(state, make_event(EffectKind.REFUND, 1000, ref="re_1"))
    state = apply_effect(state, make_event(EffectKind.REFUND_REVERSE, 1000, ref="re_1"))
    with pytest.raises(IllegalTransition, match="regress"):
        apply_effect(state, make_event(EffectKind.VOID))


def test_a_second_authorization_is_a_double_authorization_not_a_larger_hold(
    make_event: EventFactory,
) -> None:
    # Two AUTHORISATION events for one merchant reference is the real incident. Summing them
    # gives one aggregate holding 2000 that then permits a capture of 2000.
    state = apply_effect(FRESH, make_event(EffectKind.AUTHORIZE, 1000))
    with pytest.raises(IllegalTransition, match="double authorization"):
        apply_effect(state, make_event(EffectKind.AUTHORIZE, 1000, ref="ref_auth_2"))
    assert state.authorized_minor == 1000


def test_an_adjustment_is_how_a_hold_legitimately_changes(make_event: EventFactory) -> None:
    state = apply_effect(FRESH, make_event(EffectKind.AUTHORIZE, 1000))
    state = apply_effect(state, make_event(EffectKind.ADJUST_AUTH, 1500))
    assert state.authorized_minor == 1500


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


def test_a_missing_parent_directory_says_so_instead_of_unable_to_open_database_file(
    tmp_path: pathlib.Path,
) -> None:
    """sqlite3 blames the file for a missing directory, which sends you looking in the wrong place.

    Found by running the adapter example the README now shows: `create_app` on a path inside a
    directory that does not exist failed with "unable to open database file", which reads as a
    permissions or corruption problem. The CLI makes its own directory, so only a caller building
    the app directly ever saw it, and that caller is the reader following the README.
    """
    from quidz import store

    missing = tmp_path / "not-created-yet" / "quidz.db"
    with pytest.raises(NotADirectoryError) as caught:
        store.connect(missing)
    assert str(missing.parent) in str(caught.value)

    # And the ordinary case is untouched: an existing directory still opens.
    missing.parent.mkdir()
    with contextlib.closing(store.connect(missing)) as conn:
        assert conn.execute("select 1").fetchone()[0] == 1
