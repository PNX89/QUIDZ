from __future__ import annotations

import sqlite3

from conftest import authorize_body, capture_body
from quidz.clock import FakeClock
from quidz.dlq import RetryPolicy
from quidz.events import Provider
from quidz.inbox import DeliveryState, claim, drain, drain_until_idle, parked_events
from quidz.reconcile import DriftKind, GatePolicy, reconcile


def take(conn: sqlite3.Connection, identity: str, raw: bytes, clock: FakeClock, **kwargs: float):
    return claim(
        conn,
        provider=Provider.STRIPE,
        identity=identity,
        raw=raw,
        headers={},
        clock=clock,
        **kwargs,
    )


def state_of(conn: sqlite3.Connection, delivery_id: str) -> str:
    return conn.execute(
        "SELECT state FROM deliveries WHERE delivery_id = ?", (delivery_id,)
    ).fetchone()["state"]


def test_the_first_claim_of_an_identity_is_new(conn: sqlite3.Connection) -> None:
    result = take(conn, "evt_1", authorize_body("evt_1"), FakeClock())
    assert (result.accepted, result.reason) == (True, "new")


def test_a_second_claim_after_the_work_finished_is_a_duplicate(conn: sqlite3.Connection) -> None:
    clock = FakeClock()
    take(conn, "evt_1", authorize_body("evt_1"), clock)
    drain(conn, clock=clock, policy=RetryPolicy())
    result = take(conn, "evt_1", authorize_body("evt_1"), clock)
    assert (result.accepted, result.reason) == (False, "duplicate")


def test_a_claim_against_a_live_lease_reports_in_progress(conn: sqlite3.Connection) -> None:
    # The loser of the unique constraint race must not answer 200 while the work is still
    # executing; this is the case the HTTP layer maps to 409.
    clock = FakeClock()
    take(conn, "evt_1", authorize_body("evt_1"), clock, lease_seconds=30.0)
    clock.advance(5.0)
    result = take(conn, "evt_1", authorize_body("evt_1"), clock)
    assert (result.accepted, result.reason) == (False, "in_progress")


def test_an_expired_lease_is_reclaimable(conn: sqlite3.Connection) -> None:
    clock = FakeClock()
    take(conn, "evt_1", authorize_body("evt_1"), clock, lease_seconds=30.0)
    clock.advance(31.0)
    result = take(conn, "evt_1", authorize_body("evt_1"), clock)
    assert (result.accepted, result.reason) == (True, "lease_reclaimed")


def test_a_crash_between_claim_and_apply_leaves_work_to_redo_not_a_false_success(
    conn: sqlite3.Connection,
) -> None:
    clock = FakeClock()
    result = take(conn, "evt_1", authorize_body("evt_1"), clock, lease_seconds=30.0)
    assert state_of(conn, result.delivery_id) == DeliveryState.CLAIMED
    assert conn.execute("SELECT count(*) AS n FROM effects").fetchone()["n"] == 0

    clock.advance(31.0)
    retry = take(conn, "evt_1", authorize_body("evt_1"), clock)
    drain(conn, clock=clock, policy=RetryPolicy())
    applied = conn.execute("SELECT count(*) AS n FROM effects").fetchone()["n"]
    assert (retry.accepted, state_of(conn, result.delivery_id), applied) == (
        True,
        DeliveryState.APPLIED,
        1,
    )


def test_a_premature_event_is_parked_with_a_parked_until(conn: sqlite3.Connection) -> None:
    clock = FakeClock()
    take(conn, "evt_cap", capture_body("evt_cap"), clock)
    report = drain(conn, clock=clock, policy=RetryPolicy())
    row = conn.execute("SELECT state, parked_until FROM deliveries").fetchone()
    assert (report.parked, row["state"], row["parked_until"]) == (1, DeliveryState.PARKED, 1.0)


def test_a_parked_event_resolves_once_its_prerequisite_arrives(conn: sqlite3.Connection) -> None:
    clock = FakeClock()
    take(conn, "evt_cap", capture_body("evt_cap"), clock)
    clock.advance(0.05)
    take(conn, "evt_auth", authorize_body("evt_auth"), clock)
    drain_until_idle(conn, clock=clock, policy=RetryPolicy())
    states = {row["state"] for row in conn.execute("SELECT state FROM deliveries")}
    assert states == {DeliveryState.APPLIED}


def test_a_park_beyond_the_limit_reconciles_as_parked_stale(conn: sqlite3.Connection) -> None:
    clock = FakeClock()
    take(conn, "evt_cap", capture_body("evt_cap"), clock)
    drain(conn, clock=clock, policy=RetryPolicy())
    policy = GatePolicy(park_max_age_seconds=900.0)
    report = reconcile(
        local=[],
        remote=[],
        settlement=[],
        parked=parked_events(conn),
        now=clock.now() + 1000.0,
        policy=policy,
    )
    assert [f.kind for f in report.findings] == [DriftKind.PARKED_STALE]


def test_drain_respects_its_limit(conn: sqlite3.Connection) -> None:
    clock = FakeClock()
    for index in range(5):
        take(conn, f"evt_{index}", authorize_body(f"evt_{index}", f"pi_{index}"), clock)
        clock.advance(0.05)
    assert drain(conn, clock=clock, policy=RetryPolicy(), limit=2).total == 2
