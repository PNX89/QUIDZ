from __future__ import annotations

import json
import random
import sqlite3
from pathlib import Path

import pytest

from conftest import authorize_body, capture_body, effect_rows, insert_delivery, refund_body
from quidz import store
from quidz.clock import FakeClock
from quidz.dlq import RetryPolicy
from quidz.events import Provider, from_stripe
from quidz.inbox import claim, drain, drain_until_idle
from quidz.ledger import ApplyOutcome, apply_delivery, load_payment
from quidz.model import AmountInvariantViolation


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
    """
    stream = [
        ("evt_auth", authorize_body("evt_auth", minor=1000)),
        ("evt_cap", capture_body("evt_cap", minor=1000)),
        ("evt_ref", refund_body("evt_ref", minor=400)),
        ("evt_cap_twin", capture_body("evt_cap_twin", minor=1000)),
        ("evt_ref_twin", refund_body("evt_ref_twin", minor=400)),
    ]
    rng = random.Random(0)
    outcomes: set[tuple[object, ...]] = set()
    for run in range(20):
        order = stream[:]
        rng.shuffle(order)
        connection = store.connect(tmp_path / f"run_{run}.db")
        store.init_schema(connection)
        clock = FakeClock()
        for identity, raw in order:
            claim(
                connection,
                provider=Provider.STRIPE,
                identity=identity,
                raw=raw,
                headers={},
                clock=clock,
            )
            clock.advance(0.05)
        drain_until_idle(connection, clock=clock, policy=RetryPolicy(), max_passes=12)
        state = load_payment(connection, "pi_1")
        outcomes.add((state, tuple(effect_rows(connection))))
        connection.close()
    assert len(outcomes) == 1
