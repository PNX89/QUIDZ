from __future__ import annotations

import random
import sqlite3

import pytest

from conftest import adyen_body, authorize_body
from quidz import inbox
from quidz.clock import FakeClock
from quidz.dlq import (
    REASON_CODES,
    RetryPolicy,
    classify,
    jitter_source,
    next_delay,
    should_retry,
)
from quidz.events import Provider
from quidz.inbox import DeliveryState, claim, drain, drain_until_idle
from quidz.ledger import apply_delivery, load_payment


def take(conn: sqlite3.Connection, identity: str, raw: bytes, clock: FakeClock) -> str:
    return claim(
        conn, provider=Provider.STRIPE, identity=identity, raw=raw, headers={}, clock=clock
    ).delivery_id


def delivery(conn: sqlite3.Connection) -> sqlite3.Row:
    return conn.execute("SELECT state, attempts, reason_code FROM deliveries").fetchone()


def test_a_transient_failure_backs_off_then_succeeds(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No real sleep anywhere: the FakeClock records the schedule the drain loop waited out."""
    attempts = {"count": 0}

    def flaky(connection: sqlite3.Connection, delivery_id: str, event: object) -> object:
        attempts["count"] += 1
        if attempts["count"] <= 2:
            raise sqlite3.OperationalError("database is locked")
        return apply_delivery(connection, delivery_id, event)  # type: ignore[arg-type]

    monkeypatch.setattr(inbox, "apply_delivery", flaky)
    clock = FakeClock()
    policy = RetryPolicy(seed=0)
    take(conn, "evt_auth", authorize_body("evt_auth"), clock)
    drain_until_idle(conn, clock=clock, policy=policy)

    # One generator across both retries, which is what a worker holds. Re-seeding per attempt
    # would draw the same number twice at two different ceilings.
    # approx, because the schedule is stored as an absolute next_attempt_at and the wait is
    # the difference of two floats rather than the delay itself.
    rng = random.Random(0)
    expected = [next_delay(policy, attempt, rng) for attempt in (1, 2)]
    effects = conn.execute("SELECT count(*) AS n FROM effects").fetchone()["n"]
    assert clock.slept == pytest.approx(expected)
    assert (delivery(conn)["state"], effects) == (DeliveryState.APPLIED, 1)


def test_a_permanent_failure_dead_letters_on_the_first_attempt(conn: sqlite3.Connection) -> None:
    clock = FakeClock()
    take(conn, "evt_bad", b'{"id":"evt_bad","type":"charge.captured"}', clock)
    report = drain(conn, clock=clock, policy=RetryPolicy())
    row = delivery(conn)
    assert (report.dead_lettered, row["state"], row["attempts"], row["reason_code"]) == (
        1,
        DeliveryState.DEAD_LETTERED,
        1,
        "schema_invalid",
    )


def test_retries_are_bounded_by_max_attempts() -> None:
    # The anti runaway regression: without this bound one malformed event class becomes a
    # self amplifying retry storm.
    policy = RetryPolicy(max_attempts=5)
    reason = REASON_CODES["downstream_unavailable"]
    allowed = [should_retry(policy, reason, attempt, budget_used=0) for attempt in (4, 5, 6)]
    assert allowed == [True, False, False]


def test_the_global_budget_stops_retries_below_max_attempts() -> None:
    policy = RetryPolicy(max_attempts=5, budget_per_minute=100)
    reason = REASON_CODES["downstream_unavailable"]
    assert should_retry(policy, reason, 1, budget_used=100) is False


def test_jitter_from_a_seeded_generator_reproduces_exactly() -> None:
    policy = RetryPolicy(seed=0)
    first = [
        next_delay(policy, attempt, rng) for rng in [jitter_source(policy)] for attempt in range(6)
    ]
    second = [
        next_delay(policy, attempt, rng) for rng in [jitter_source(policy)] for attempt in range(6)
    ]
    assert first == second


def test_an_unseeded_policy_does_not_hand_every_worker_the_same_schedule() -> None:
    """The default has to be unpredictable, or full jitter buys nothing.

    A seeded generator built from the policy inside the drain loop draws the identical
    sequence on every pass and on every worker in a fleet, which is the lockstep retry the
    jitter exists to break up. Determinism is a test facility, so it lives behind an explicit
    seed and never behind the default.
    """
    policy = RetryPolicy()
    first = [
        next_delay(policy, attempt, rng) for rng in [jitter_source(policy)] for attempt in range(8)
    ]
    second = [
        next_delay(policy, attempt, rng) for rng in [jitter_source(policy)] for attempt in range(8)
    ]
    assert (policy.seed, first == second) == (None, False)


def test_the_delay_never_exceeds_the_ceiling() -> None:
    policy = RetryPolicy(base_seconds=1.0, max_seconds=300.0)
    rng = random.Random(7)
    assert all(next_delay(policy, attempt, rng) <= 300.0 for attempt in range(40))


def test_an_unrecognised_failure_is_retryable_with_a_cap_and_never_dropped() -> None:
    reason = classify(RuntimeError("something new from the provider"))
    assert (reason.code, reason.retryable, reason.max_attempts) == ("unknown", True, 3)


def test_a_body_that_is_not_utf_8_is_permanent_and_not_retried() -> None:
    # UnicodeDecodeError is a ValueError, not a JSONDecodeError, so without an entry of its own
    # it lands in the retryable catch all and burns three attempts on stored bytes that cannot
    # change between them. It is the same permanent parse failure as malformed JSON.
    with pytest.raises(UnicodeDecodeError) as raised:
        b'{"id":"\xff"}'.decode("utf-8")
    reason = classify(raised.value)
    assert (reason.code, reason.retryable) == ("schema_invalid", False)


def test_a_stored_delivery_that_is_not_utf_8_dead_letters_on_the_first_attempt(
    conn: sqlite3.Connection,
) -> None:
    clock = FakeClock()
    take(conn, "evt_bad", b'{"id":"evt_bad","type":"\xff\xfe"}', clock)
    report = drain(conn, clock=clock, policy=RetryPolicy())
    row = delivery(conn)
    assert (report.dead_lettered, report.retried, row["reason_code"]) == (1, 0, "schema_invalid")


def test_a_double_authorization_dead_letters_instead_of_doubling_the_hold(
    conn: sqlite3.Connection,
) -> None:
    """Two AUTHORISATIONs for one merchantReference, which is the shape of a real double auth.

    Different pspReferences, so the unique index lets both effects through and the aggregate is
    the only thing that can refuse. Summing them would leave one aggregate holding 2000 that
    then permits a capture of 2000, and reconciliation would never see it: the ledger and the
    provider would agree on the total.
    """
    clock = FakeClock()
    for psp in ("PSPREFA", "PSPREFB"):
        claim(
            conn,
            provider=Provider.ADYEN,
            identity=f"{psp}:AUTHORISATION",
            raw=adyen_body(psp),
            headers={},
            clock=clock,
        )
        clock.advance(1.0)
    report = drain(conn, clock=clock, policy=RetryPolicy())
    state = load_payment(conn, "order-1")
    dead = conn.execute(
        "SELECT reason_code FROM deliveries WHERE state = ?", (DeliveryState.DEAD_LETTERED,)
    ).fetchall()
    assert state is not None
    assert (report.dead_lettered, state.authorized_minor, [row[0] for row in dead]) == (
        1,
        1000,
        ["illegal_transition"],
    )


def test_a_redelivered_message_resolves_to_a_no_op_not_a_second_effect(
    conn: sqlite3.Connection,
) -> None:
    clock = FakeClock()
    delivery_id = take(conn, "evt_auth", authorize_body("evt_auth"), clock)
    drain(conn, clock=clock, policy=RetryPolicy())
    # The delivery is put back on the queue, which is what a provider retry after a lost
    # acknowledgement looks like from this side.
    conn.execute(
        "UPDATE deliveries SET state = ?, next_attempt_at = NULL WHERE delivery_id = ?",
        (DeliveryState.CLAIMED, delivery_id),
    )
    report = drain(conn, clock=clock, policy=RetryPolicy())
    effects = conn.execute("SELECT count(*) AS n FROM effects").fetchone()["n"]
    assert (report.noop_duplicate, report.dead_lettered, effects) == (1, 0, 1)
