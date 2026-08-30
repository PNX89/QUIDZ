from __future__ import annotations

import contextlib
import pathlib

import pytest

from conftest import EventFactory
from quidz.model import (
    STATUSES,
    AmountInvariantViolation,
    EffectKind,
    IllegalTransition,
    PaymentState,
    PrematureEvent,
    apply_effect,
    derive_status,
    state_rank,
)
from quidz.money import CurrencyMismatch

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


def test_a_capture_of_exactly_the_authorization_is_allowed_and_one_minor_unit_more_is_not(
    make_event: EventFactory,
) -> None:
    """The boundary, not a round number well past it.

    The test above captures 1200 against 1000, which passes just as happily against a guard
    relaxed by one minor unit. One minor unit is what an off by one in a money invariant is
    worth per payment, and it is the version of the mistake nobody notices.
    """
    state = apply_effect(FRESH, make_event(EffectKind.AUTHORIZE, 1000))
    exact = apply_effect(state, make_event(EffectKind.CAPTURE, 1000))
    assert (exact.captured_minor, derive_status(exact)) == (1000, "captured")
    with pytest.raises(AmountInvariantViolation, match="exceed the authorization"):
        apply_effect(state, make_event(EffectKind.CAPTURE, 1001, ref="ref_capture_2"))


def test_an_event_in_another_currency_never_folds_into_the_aggregate(
    make_event: EventFactory,
) -> None:
    """The aggregate holds one currency, and minor units of another are not units of it.

    money.add and money.sub refuse this, but no production module calls either: the aggregate
    is where every amount actually meets another amount, so this guard is the only thing in the
    package that can raise CurrencyMismatch, and dlq's currency_mismatch reason code is
    reachable only through it.
    """
    state = apply_effect(FRESH, make_event(EffectKind.AUTHORIZE, 1000))
    with pytest.raises(CurrencyMismatch, match="does not match payment"):
        apply_effect(state, make_event(EffectKind.CAPTURE, 1000, currency="USD"))


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


@pytest.mark.parametrize("kind", [EffectKind.REFUND_FAIL, EffectKind.REFUND_REVERSE])
def test_a_refund_correction_beyond_the_outstanding_refund_is_refused(
    kind: EffectKind, make_event: EventFactory
) -> None:
    """Both regressive kinds, because the guard names both and a tuple can lose one.

    A correction is money coming back to the merchant, so more of it than actually left drives
    refunded_minor negative and derive_status reads the payment as captured again while the
    child rows say otherwise. 400 exactly is the case the two tests below already cover, so the
    boundary here is one minor unit past it.
    """
    state = apply_effect(FRESH, make_event(EffectKind.AUTHORIZE, 1000))
    state = apply_effect(state, make_event(EffectKind.CAPTURE, 1000))
    state = apply_effect(state, make_event(EffectKind.REFUND, 400, ref="re_1"))
    with pytest.raises(AmountInvariantViolation, match="exceeds the outstanding refunded 400"):
        apply_effect(state, make_event(kind, 401, ref="re_1"))


def test_an_adjustment_below_the_money_already_taken_is_an_amount_violation(
    make_event: EventFactory,
) -> None:
    """An authorization cannot be adjusted below what has already been captured against it.

    Worth its own reason code as well as its own refusal: any adjustment on a captured payment
    is also a rank regression a few lines further down, and illegal_transition sends whoever
    reads the dead letter queue looking at delivery ordering rather than at an adjustment that
    contradicts money the merchant has already taken.
    """
    state = apply_effect(FRESH, make_event(EffectKind.AUTHORIZE, 1000, sequence=100))
    state = apply_effect(state, make_event(EffectKind.CAPTURE, 600, sequence=200))
    with pytest.raises(AmountInvariantViolation, match="below the captured 600"):
        apply_effect(state, make_event(EffectKind.ADJUST_AUTH, 599, ref="adj_1", sequence=300))


def test_a_negative_amount_is_refused_before_the_guards_below_it_ever_see_it(
    make_event: EventFactory,
) -> None:
    """Every conservation check downstream compares against a ceiling, and negatives clear them.

    A refund of -100 against a capture of 1000 is comfortably under the refund ceiling, so
    nothing else refuses it, and folding it leaves refunded_minor at -100: money handed back to
    the merchant with no refund_fail or refund_reverse row anywhere saying it was.
    """
    state = apply_effect(FRESH, make_event(EffectKind.AUTHORIZE, 1000))
    state = apply_effect(state, make_event(EffectKind.CAPTURE, 1000))
    with pytest.raises(AmountInvariantViolation, match="must not be negative"):
        apply_effect(state, make_event(EffectKind.REFUND, -100, ref="re_1"))


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


def test_the_newest_adjustment_is_the_hold_whichever_order_the_pair_arrives_in(
    make_event: EventFactory,
) -> None:
    """Ordering is a rank, and within a rank the newest event wins by its own sequence.

    adjust_auth is the only fold that reads the order it is processed in, so it is the only
    kind that can falsify the second half of that claim. Both arrival orders of one pair, so a
    guard that dropped every adjustment would fail the first line and one that dropped none
    would fail the second.
    """
    authorized = apply_effect(FRESH, make_event(EffectKind.AUTHORIZE, 1000, sequence=100))
    older = make_event(EffectKind.ADJUST_AUTH, 500, ref="adj_10_05", sequence=200)
    newer = make_event(EffectKind.ADJUST_AUTH, 1500, ref="adj_10_10", sequence=300)

    in_order = apply_effect(apply_effect(authorized, older), newer)
    out_of_order = apply_effect(apply_effect(authorized, newer), older)
    assert (in_order.authorized_minor, in_order.last_sequence) == (1500, 300)
    assert (out_of_order.authorized_minor, out_of_order.last_sequence) == (1500, 300)


def test_a_stale_adjustment_cannot_raise_the_ceiling_the_over_capture_guard_reads(
    make_event: EventFactory,
) -> None:
    """What the ordering costs when it goes wrong, in the direction that moves real money.

    Fold the stale adjustment and the aggregate says 1500 is available to take, so a capture of
    1500 clears the invariant against a hold the provider is only carrying 500 of. Neither
    source disagrees afterwards either: the ledger and the provider both show a capture, and
    reconciliation has nothing to compare the ceiling against.
    """
    state = apply_effect(FRESH, make_event(EffectKind.AUTHORIZE, 1000, sequence=100))
    # The provider's newer word first: as of 10:10 the hold is 500.
    state = apply_effect(
        state, make_event(EffectKind.ADJUST_AUTH, 500, ref="adj_10_10", sequence=300)
    )
    # Then the 10:05 adjustment, late, carrying the total that was correct before that.
    stale = apply_effect(
        state, make_event(EffectKind.ADJUST_AUTH, 1500, ref="adj_10_05", sequence=200)
    )

    assert stale.authorized_minor == 500
    with pytest.raises(AmountInvariantViolation, match="exceed the authorization"):
        apply_effect(stale, make_event(EffectKind.CAPTURE, 1500))


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


def test_every_declared_status_is_reachable_and_ranked() -> None:
    """STATUSES is exported as the definitive list, and nothing was reading it.

    Not derive_status, which returns the string literals directly. Not state_rank, which keeps
    its own ranking dict. Not the CLI, not the reconciler, not another test. The README's own
    nine-status paragraph and STATUSES could each drift from derive_status, and from each
    other, with nothing going red. This builds one fixture per status by construction rather
    than by replay, so it does not depend on apply_effect reaching every branch, and checks
    STATUSES against derive_status's actual range and against state_rank in one pass: a member
    state_rank cannot look up raises ValueError here instead of staying a promise nothing
    keeps.
    """
    fixtures: dict[str, PaymentState] = {
        "pending": PaymentState(payment_id="p", currency="EUR"),
        "authorized": PaymentState(payment_id="p", currency="EUR", authorized_minor=1000),
        "capture_failed": PaymentState(
            payment_id="p", currency="EUR", authorized_minor=1000, capture_failed_minor=1000
        ),
        "voided": PaymentState(payment_id="p", currency="EUR", authorized_minor=1000, voided=True),
        "expired": PaymentState(
            payment_id="p", currency="EUR", authorized_minor=1000, expired=True
        ),
        "partially_captured": PaymentState(
            payment_id="p", currency="EUR", authorized_minor=1000, captured_minor=400
        ),
        "captured": PaymentState(
            payment_id="p", currency="EUR", authorized_minor=1000, captured_minor=1000
        ),
        "partially_refunded": PaymentState(
            payment_id="p",
            currency="EUR",
            authorized_minor=1000,
            captured_minor=1000,
            refunded_minor=400,
        ),
        "refunded": PaymentState(
            payment_id="p",
            currency="EUR",
            authorized_minor=1000,
            captured_minor=1000,
            refunded_minor=1000,
        ),
    }
    assert set(fixtures) == set(STATUSES)
    for expected_status, state in fixtures.items():
        assert derive_status(state) == expected_status
        assert isinstance(state_rank(expected_status), int)
