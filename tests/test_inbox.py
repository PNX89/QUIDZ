from __future__ import annotations

import json
import sqlite3

import pytest

from conftest import authorize_body, capture_body
from quidz.clock import FakeClock
from quidz.dlq import RetryPolicy
from quidz.events import Provider
from quidz.inbox import (
    ClaimResult,
    DeliveryState,
    claim,
    drain,
    drain_until_idle,
    parked_events,
    receive_adyen,
)
from quidz.reconcile import DriftKind, GatePolicy, reconcile


def take(
    conn: sqlite3.Connection, identity: str, raw: bytes, clock: FakeClock, **kwargs: float
) -> ClaimResult:
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
    row = conn.execute(
        "SELECT state FROM deliveries WHERE delivery_id = ?", (delivery_id,)
    ).fetchone()
    state: str = row["state"]
    return state


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


def state_of_metric(conn: sqlite3.Connection, name: str) -> int:
    """One counter, read through the module's own snapshot rather than by touching its table.

    Reading the table directly would make this test agree with a schema rather than with the
    interface an operator uses, and would break the day the table is renamed for a reason that
    has nothing to do with what is being asserted here.
    """
    from quidz import metrics

    return metrics.snapshot(conn).get(name, 0)


def test_a_forged_adyen_delivery_is_refused_at_the_front_door(
    conn: sqlite3.Connection,
) -> None:
    """Deleting the signature check left 148 of 148 green, and this is the money path.

    THE GAP WAS AT THE DOOR, NOT AT THE LOCK. `tests/test_verify_adyen.py` exercises
    `verify_adyen` thoroughly, against the documented signing string and a wrong key. What
    nothing did was drive `receive_adyen`, the function an HTTP route actually calls, with a body
    whose signature is wrong. Replacing that one call with `pass` was invisible to the whole
    suite, and a forged delivery was then accepted, claimed and queued for settlement.

    A well tested verifier that nobody proves is CALLED is the same defect as a lock nobody
    proves is fitted, and it is the shape this portfolio keeps finding: the guard is right, and
    the thing that was never checked is whether anything reaches it.

    Driven with a body the simulator genuinely signed, so the passing half is real rather than
    constructed to agree with the failing half.
    """
    from quidz.sim import Simulator
    from quidz.verify import BadSignature

    sim = Simulator()
    delivery = next(d for d in sim.deliveries("happy") if d.provider is Provider.ADYEN)
    item = json.loads(delivery.raw)

    # The honest body first, so a guard that refuses everything cannot pass this test.
    accepted = receive_adyen(
        conn,
        delivery.raw,
        hmac_key_hex=sim.adyen_hmac_key_hex,
        clock=FakeClock(),
    )
    assert accepted is not None

    # Now the same body with one signed field changed and the signature left alone, which is
    # exactly what an attacker replaying a captured notification would send.
    tampered = json.loads(delivery.raw)
    amount = tampered.get("amount")
    assert isinstance(amount, dict), "this delivery has no amount to tamper with"
    amount["value"] = int(amount["value"]) + 1
    forged = json.dumps(tampered, sort_keys=True, separators=(",", ":")).encode()

    before = state_of_metric(conn, "signature_rejected")
    with pytest.raises(BadSignature):
        receive_adyen(conn, forged, hmac_key_hex=sim.adyen_hmac_key_hex, clock=FakeClock())
    assert state_of_metric(conn, "signature_rejected") == before + 1, (
        "the delivery was refused without the refusal being counted, so an operator watching "
        "the metric would see nothing"
    )

    stored = conn.execute(
        "select count(*) from deliveries where provider = ?", (Provider.ADYEN.value,)
    ).fetchone()[0]
    assert stored == 1, (
        f"{stored} deliveries are stored where only the honest one should be. A forged body "
        f"reached the ledger"
    )
    assert item["pspReference"]  # the honest delivery really did carry an identity
