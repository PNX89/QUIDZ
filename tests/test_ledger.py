from __future__ import annotations

import json
import random
import sqlite3
from pathlib import Path

import pytest

from conftest import (
    adyen_body,
    authorize_body,
    capture_body,
    effect_rows,
    insert_delivery,
    refund_body,
)
from quidz import store
from quidz.clock import FakeClock
from quidz.dlq import RetryPolicy
from quidz.events import CanonicalEvent, Provider, from_stripe
from quidz.inbox import claim, drain, drain_until_idle
from quidz.ledger import ApplyOutcome, apply_delivery, load_payment
from quidz.model import AmountInvariantViolation, EffectKind, PaymentState
from quidz.money import Money

# The two aggregates the permutation stream builds, plus every effect row behind them.
Outcome = tuple[PaymentState | None, PaymentState | None, tuple[tuple[object, ...], ...]]


def deliver(conn: sqlite3.Connection, delivery_id: str, raw: bytes) -> ApplyOutcome:
    insert_delivery(conn, delivery_id, raw)
    with store.write_tx(conn):
        return apply_delivery(conn, delivery_id, from_stripe(json.loads(raw)))


def test_five_deliveries_of_one_event_produce_exactly_one_effect_row(
    conn: sqlite3.Connection,
) -> None:
    clock = FakeClock()
    for _ in range(5):
        claim(
            conn,
            provider=Provider.STRIPE,
            identity="evt_auth",
            raw=authorize_body("evt_auth"),
            headers={},
            clock=clock,
        )
        drain(conn, clock=clock, policy=RetryPolicy())
        clock.advance(60.0)
    assert conn.execute("SELECT count(*) AS n FROM effects").fetchone()["n"] == 1


def _event(kind: EffectKind, *, occurred_at: float, sequence: int) -> CanonicalEvent:
    return CanonicalEvent(
        provider=Provider.STRIPE,
        identity=f"evt_{kind.value}_{sequence}",
        payment_id="pay_1",
        kind=kind,
        provider_ref=f"ref_{kind.value}_{sequence}",
        amount=Money(1000, "EUR"),
        occurred_at=occurred_at,
        sequence=sequence,
        raw_amount_value="1000",
        raw_currency="EUR",
    )


def test_updated_at_is_the_newest_provider_time_folded_in_not_whichever_applied_last(
    conn: sqlite3.Connection,
) -> None:
    """The upsert's own max() is an invariant enforced only in the SQL that writes it.

    updated_at is absent from _STATE_COLUMNS, so load_payment, list_payments, every report
    and every other test in this suite never read it back; deleting the max() and using
    excluded.updated_at outright leaves the full suite green. This applies a delivery stamped
    with a later provider time first and one stamped earlier second, the out of order delivery
    the comment beside the upsert names, and reads the column back with a raw query since
    load_payment has nothing to return it through.
    """
    later = _event(EffectKind.AUTHORIZE, occurred_at=2000.0, sequence=0)
    earlier = _event(EffectKind.CAPTURE_FAIL, occurred_at=1000.0, sequence=1)
    with store.write_tx(conn):
        insert_delivery(conn, "stripe:evt_later")
        apply_delivery(conn, "stripe:evt_later", later)
        insert_delivery(conn, "stripe:evt_earlier")
        apply_delivery(conn, "stripe:evt_earlier", earlier)
    row = conn.execute(
        "SELECT updated_at FROM payments WHERE payment_id = ?", ("pay_1",)
    ).fetchone()
    assert row["updated_at"] == 2000.0


def test_the_duplicate_path_reports_a_noop(conn: sqlite3.Connection) -> None:
    deliver(conn, "stripe:evt_auth", authorize_body("evt_auth"))
    assert deliver(conn, "stripe:evt_auth_again", authorize_body("evt_auth")) is (
        ApplyOutcome.NOOP_DUPLICATE
    )


def test_two_event_ids_for_one_effect_collide_on_the_business_key(
    conn: sqlite3.Connection,
) -> None:
    # Stripe documents that two Event objects can describe one underlying change, so event id
    # dedup alone is not enough and UNIQUE(payment_id, kind, provider_ref) is what catches it.
    deliver(conn, "stripe:evt_auth", authorize_body("evt_auth"))
    deliver(conn, "stripe:evt_cap_a", capture_body("evt_cap_a"))
    outcome = deliver(conn, "stripe:evt_cap_b", capture_body("evt_cap_b"))
    captures = conn.execute("SELECT count(*) AS n FROM effects WHERE kind = 'capture'").fetchone()[
        "n"
    ]
    assert (outcome, captures) == (ApplyOutcome.NOOP_DUPLICATE, 1)


def test_the_aggregate_matches_the_sum_of_its_effect_rows(conn: sqlite3.Connection) -> None:
    deliver(conn, "stripe:evt_auth", authorize_body("evt_auth", minor=1000))
    deliver(conn, "stripe:evt_cap", capture_body("evt_cap", minor=1000))
    deliver(conn, "stripe:evt_ref", refund_body("evt_ref", minor=400))
    summed = conn.execute(
        "SELECT sum(CASE WHEN kind = 'capture' THEN amount_minor ELSE 0 END) AS captured, "
        "sum(CASE WHEN kind = 'refund' THEN amount_minor ELSE 0 END) AS refunded FROM effects"
    ).fetchone()
    state = load_payment(conn, "pi_1")
    assert state is not None
    assert (state.captured_minor, state.refunded_minor) == (
        summed["captured"],
        summed["refunded"],
    )


def test_the_providers_own_amount_strings_are_stored_verbatim(conn: sqlite3.Connection) -> None:
    deliver(conn, "stripe:evt_auth", authorize_body("evt_auth", minor=1000))
    row = conn.execute("SELECT raw_amount_value, raw_currency FROM effects").fetchone()
    assert (row["raw_amount_value"], row["raw_currency"]) == ("1000", "eur")


def test_a_rejected_effect_rolls_back_the_whole_write_tx(conn: sqlite3.Connection) -> None:
    deliver(conn, "stripe:evt_auth", authorize_body("evt_auth", minor=1000))
    with pytest.raises(AmountInvariantViolation):
        deliver(conn, "stripe:evt_cap", capture_body("evt_cap", minor=5000))
    kinds = [row[1] for row in effect_rows(conn)]
    assert kinds == ["authorize"]


def test_a_rejected_effect_unwinds_its_savepoint_without_a_write_tx(
    conn: sqlite3.Connection,
) -> None:
    """apply_delivery is exported and documented as raising, so a caller may well commit.

    Deliberately no write_tx here. Its blanket ROLLBACK is what makes the test above pass
    whether or not the savepoint is unwound, so it proves the wrapper rather than the function
    it wraps. Committing after the rejection is the case that put an over-capture of 99999
    against an authorization of 1000 into effects while payments still read captured 0.
    """
    deliver(conn, "stripe:evt_auth", authorize_body("evt_auth", minor=1000))
    raw = capture_body("evt_cap", minor=99999)
    insert_delivery(conn, "stripe:evt_cap", raw)
    conn.execute("BEGIN IMMEDIATE")
    with pytest.raises(AmountInvariantViolation):
        apply_delivery(conn, "stripe:evt_cap", from_stripe(json.loads(raw)))
    conn.execute("COMMIT")

    state = load_payment(conn, "pi_1")
    assert state is not None
    assert ([row[1] for row in effect_rows(conn)], state.captured_minor) == (["authorize"], 0)


def test_twenty_seeded_permutations_reach_one_terminal_aggregate(tmp_path: Path) -> None:
    """The invariant hypothesis would have bought, obtained deterministically instead.

    A fixed stream with duplicates injected, replayed in twenty seeded permutations, has to
    land on the same aggregate and the same effect rows every time.

    The stream carries an authorisation and two adjustments of it because adjust_auth is the
    only fold whose result depends on the order it is processed in: authorize and capture are
    refused after the first, void and expire set a flag, and every other kind is additive. A
    stream without one cannot fail this test however the shuffle lands, which is what let the
    aggregate reach two different holds while this test, the one the README names as pinning
    ordering tolerance, stayed green.
    """
    stream = [
        (Provider.STRIPE, "evt_auth", authorize_body("evt_auth", minor=1000)),
        (Provider.STRIPE, "evt_cap", capture_body("evt_cap", minor=1000)),
        (Provider.STRIPE, "evt_ref", refund_body("evt_ref", minor=400)),
        (Provider.STRIPE, "evt_cap_twin", capture_body("evt_cap_twin", minor=1000)),
        (Provider.STRIPE, "evt_ref_twin", refund_body("evt_ref_twin", minor=400)),
        (
            Provider.ADYEN,
            "PSPAUTH:AUTHORISATION",
            adyen_body("PSPAUTH", minor=1000, event_date="2026-08-18T10:00:00+00:00"),
        ),
        (
            Provider.ADYEN,
            "PSPADJA:AUTHORISATION_ADJUSTMENT",
            adyen_body(
                "PSPADJA",
                "AUTHORISATION_ADJUSTMENT",
                minor=500,
                event_date="2026-08-18T10:05:00+00:00",
            ),
        ),
        (
            Provider.ADYEN,
            "PSPADJB:AUTHORISATION_ADJUSTMENT",
            adyen_body(
                "PSPADJB",
                "AUTHORISATION_ADJUSTMENT",
                minor=1500,
                event_date="2026-08-18T10:10:00+00:00",
            ),
        ),
    ]
    rng = random.Random(0)
    outcomes: set[Outcome] = set()
    for run in range(20):
        order = stream[:]
        rng.shuffle(order)
        connection = store.connect(tmp_path / f"run_{run}.db")
        store.init_schema(connection)
        clock = FakeClock()
        for provider, identity, raw in order:
            claim(
                connection,
                provider=provider,
                identity=identity,
                raw=raw,
                headers={},
                clock=clock,
            )
            clock.advance(0.05)
        drain_until_idle(connection, clock=clock, policy=RetryPolicy(), max_passes=12)
        outcomes.add(
            (
                load_payment(connection, "pi_1"),
                load_payment(connection, "order-1"),
                tuple(effect_rows(connection)),
            )
        )
        connection.close()
    assert len(outcomes) == 1

    # One outcome is only the right outcome if it is the one the stream describes. Without
    # these, a bug that parked every adjustment for ever would agree with itself twenty times
    # over and read exactly like a pass.
    stripe_state, adyen_state, rows = next(iter(outcomes))
    assert stripe_state is not None and adyen_state is not None
    assert (stripe_state.captured_minor, stripe_state.refunded_minor) == (1000, 400)
    assert adyen_state.authorized_minor == 1500
    assert len(rows) == 6
