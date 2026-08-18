"""The front door and the queue behind it: verify, claim, lease, park, drain.

Adyen marks a webhook Failing and queues it for retry if it does not get a 2xx within ten
seconds, and it instructs consumers to store the message first and process it later. That is
exactly this split: claim() runs on the request thread and writes one row, drain() does the
work afterwards. The Stripe tolerance window is checked at receipt for the same reason, since
a Stripe retry carries a new timestamp and a new signature.

https://docs.adyen.com/development-resources/webhooks/handle-webhook-events
"""

from __future__ import annotations

import json
import random
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

from quidz import metrics
from quidz.clock import Clock
from quidz.dlq import RetryPolicy, classify, jitter_source, next_delay, should_retry
from quidz.events import (
    CanonicalEvent,
    EventError,
    MalformedEvent,
    Provider,
    from_adyen,
    from_stripe,
)
from quidz.ledger import ApplyOutcome, apply_delivery
from quidz.store import write_tx
from quidz.verify import (
    STRIPE_DEFAULT_TOLERANCE_SECONDS,
    SignatureError,
    StaleTimestamp,
    verify_adyen,
    verify_stripe,
)

__all__ = [
    "ClaimResult",
    "DeliveryState",
    "DrainReport",
    "claim",
    "drain",
    "drain_until_idle",
    "park",
    "parked_events",
    "receive_adyen",
    "receive_stripe",
]

# Both counters live in the counters table but outside the metrics allowlist: they are the
# retry budget's own state, not part of the operational surface anyone dashboards.
_BUDGET_MINUTE = "retry_budget_minute"
_BUDGET_USED = "retry_budget_used"

_ADAPTERS = {Provider.STRIPE: from_stripe, Provider.ADYEN: from_adyen}


class DeliveryState(StrEnum):
    CLAIMED = "claimed"
    APPLIED = "applied"
    DUPLICATE = "duplicate"
    PARKED = "parked"
    DEAD_LETTERED = "dead_lettered"


@dataclass(frozen=True, slots=True)
class ClaimResult:
    delivery_id: str
    accepted: bool
    reason: Literal["new", "duplicate", "in_progress", "lease_reclaimed"]


@dataclass(frozen=True, slots=True)
class DrainReport:
    applied: int = 0
    noop_duplicate: int = 0
    parked: int = 0
    dead_lettered: int = 0
    retried: int = 0

    @property
    def total(self) -> int:
        return self.applied + self.noop_duplicate + self.parked + self.dead_lettered + self.retried

    def merge(self, other: DrainReport) -> DrainReport:
        return DrainReport(
            self.applied + other.applied,
            self.noop_duplicate + other.noop_duplicate,
            self.parked + other.parked,
            self.dead_lettered + other.dead_lettered,
            self.retried + other.retried,
        )


def _payload(raw: bytes) -> Mapping[str, object]:
    payload = json.loads(raw)
    if not isinstance(payload, Mapping):
        raise MalformedEvent("a webhook body must be a JSON object")
    return payload


def _decode(provider: Provider, raw: bytes) -> CanonicalEvent:
    return _ADAPTERS[provider](_payload(raw))


def _identity(provider: Provider, payload: Mapping[str, object]) -> str:
    """Read the delivery identity without canonicalising the whole event.

    An event type this ledger does not model must still be stored and dead lettered rather
    than rejected at the door, so the front door only reads the stable identity fields.
    """
    if provider is Provider.STRIPE:
        value = payload.get("id")
        if not isinstance(value, str) or not value:
            raise MalformedEvent("Stripe event carries no id")
        return value
    psp, code = payload.get("pspReference"), payload.get("eventCode")
    if not isinstance(psp, str) or not psp or not isinstance(code, str) or not code:
        raise MalformedEvent("Adyen item carries no pspReference and eventCode pair")
    return f"{psp}:{code}"


def claim(
    conn: sqlite3.Connection,
    *,
    provider: Provider,
    identity: str,
    raw: bytes,
    headers: Mapping[str, str],
    clock: Clock,
    lease_seconds: float = 30.0,
) -> ClaimResult:
    """Record the delivery and take a lease on it, in one transaction, then return.

    The loser of the unique constraint race does not blindly answer 200. A live lease means
    the work is still executing, which the HTTP layer reports as 409 Conflict, mirroring
    Stripe's own behaviour for an idempotency key that is still in flight.
    https://docs.stripe.com/api/idempotent_requests
    """
    delivery_id = f"{provider}:{identity}"
    now = clock.now()
    encoded = json.dumps(dict(headers), sort_keys=True)
    with write_tx(conn):
        try:
            conn.execute(
                "INSERT INTO deliveries (delivery_id, provider, raw, headers, received_at, "
                "state, attempts, next_attempt_at, lease_expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)",
                (
                    delivery_id,
                    str(provider),
                    raw,
                    encoded,
                    now,
                    DeliveryState.CLAIMED,
                    now,
                    now + lease_seconds,
                ),
            )
            return ClaimResult(delivery_id, True, "new")
        except sqlite3.IntegrityError:
            pass
        row = conn.execute(
            "SELECT state, lease_expires_at FROM deliveries WHERE delivery_id = ?", (delivery_id,)
        ).fetchone()
        lease = row["lease_expires_at"]
        if row["state"] != DeliveryState.CLAIMED:
            return ClaimResult(delivery_id, False, "duplicate")
        if lease is not None and lease > now:
            return ClaimResult(delivery_id, False, "in_progress")
        # The lease expired, so the previous holder died before applying anything. Adyen's
        # instruction for a repeat delivery is to use the details from the latest event.
        conn.execute(
            "UPDATE deliveries SET raw = ?, headers = ?, lease_expires_at = ?, "
            "next_attempt_at = ? WHERE delivery_id = ?",
            (raw, encoded, now + lease_seconds, now, delivery_id),
        )
        return ClaimResult(delivery_id, True, "lease_reclaimed")


def park(conn: sqlite3.Connection, delivery_id: str, *, until: float) -> None:
    """Parking and dead lettering are two states of one inbox row, not two mechanisms."""
    with write_tx(conn):
        conn.execute(
            "UPDATE deliveries SET state = ?, parked_until = ?, lease_expires_at = NULL "
            "WHERE delivery_id = ?",
            (DeliveryState.PARKED, until, delivery_id),
        )


def parked_events(conn: sqlite3.Connection) -> list[tuple[str, float]]:
    """(payment_id, received_at) per parked delivery, the reconciler's view of the park.

    Age is measured from receipt rather than from the last park attempt, because what an
    operator needs to know is how long the effect has been unapplied in total.
    """
    out: list[tuple[str, float]] = []
    rows = conn.execute(
        "SELECT delivery_id, provider, raw, received_at FROM deliveries WHERE state = ? "
        "ORDER BY delivery_id",
        (DeliveryState.PARKED,),
    )
    for row in rows.fetchall():
        try:
            payment_id = _decode(Provider(row["provider"]), row["raw"]).payment_id
        except (ValueError, EventError):
            payment_id = row["delivery_id"]
        out.append((payment_id, row["received_at"]))
    return out


def _budget_used(conn: sqlite3.Connection, now: float) -> int:
    rows = {
        row["name"]: row["value"]
        for row in conn.execute(
            "SELECT name, value FROM counters WHERE name IN (?, ?)", (_BUDGET_MINUTE, _BUDGET_USED)
        )
    }
    if rows.get(_BUDGET_MINUTE) != int(now // 60):
        return 0
    return int(rows.get(_BUDGET_USED, 0))


def _record_budget(conn: sqlite3.Connection, now: float, used: int) -> None:
    for name, value in ((_BUDGET_MINUTE, int(now // 60)), (_BUDGET_USED, used)):
        conn.execute(
            "INSERT INTO counters (name, value) VALUES (?, ?) "
            "ON CONFLICT(name) DO UPDATE SET value = excluded.value",
            (name, value),
        )


def drain(
    conn: sqlite3.Connection,
    *,
    clock: Clock,
    policy: RetryPolicy,
    limit: int = 100,
    rng: random.Random | None = None,
) -> DrainReport:
    """Apply every due delivery, one transaction each, and return what happened to them.

    One transaction per delivery, not one per batch: a poisoned delivery must not roll back
    the effects of the deliveries next to it.

    The generator is a parameter so a worker keeps one across passes. Building it here from the
    policy on every call restarts the sequence each pass, which turns full jitter into one
    fixed set of delays repeated forever, and identically on every worker in a fleet.
    """
    now = clock.now()
    if rng is None:
        rng = jitter_source(policy)
    budget_used = _budget_used(conn, now)
    rows = conn.execute(
        "SELECT delivery_id, provider, raw, attempts, state FROM deliveries "
        "WHERE state IN (?, ?) AND (next_attempt_at IS NULL OR next_attempt_at <= ?) "
        "AND (parked_until IS NULL OR parked_until <= ?) "
        "ORDER BY received_at, delivery_id LIMIT ?",
        (DeliveryState.CLAIMED, DeliveryState.PARKED, now, now, limit),
    ).fetchall()

    report = DrainReport()
    for row in rows:
        delivery_id = row["delivery_id"]
        try:
            with write_tx(conn):
                event = _decode(Provider(row["provider"]), row["raw"])
                outcome = apply_delivery(conn, delivery_id, event)
                report = report.merge(
                    _settle(conn, delivery_id, row["state"], outcome, now, policy)
                )
        # A queue consumer classifies whatever comes out of the handler; an unexpected error
        # that escapes here would stall every delivery behind it.
        except Exception as exc:
            reason = classify(exc)
            attempts = row["attempts"] + 1
            retryable = should_retry(policy, reason, attempts, budget_used=budget_used)
            with write_tx(conn):
                if retryable:
                    conn.execute(
                        "UPDATE deliveries SET state = ?, attempts = ?, next_attempt_at = ?, "
                        "reason_code = ?, lease_expires_at = NULL WHERE delivery_id = ?",
                        (
                            DeliveryState.CLAIMED,
                            attempts,
                            now + next_delay(policy, attempts, rng),
                            reason.code,
                            delivery_id,
                        ),
                    )
                    budget_used += 1
                    _record_budget(conn, now, budget_used)
                    report = report.merge(DrainReport(retried=1))
                else:
                    conn.execute(
                        "UPDATE deliveries SET state = ?, attempts = ?, reason_code = ?, "
                        "next_attempt_at = NULL, lease_expires_at = NULL WHERE delivery_id = ?",
                        (DeliveryState.DEAD_LETTERED, attempts, reason.code, delivery_id),
                    )
                    metrics.bump(conn, "dead_lettered")
                    report = report.merge(DrainReport(dead_lettered=1))
    return report


def _settle(
    conn: sqlite3.Connection,
    delivery_id: str,
    was: str,
    outcome: ApplyOutcome,
    now: float,
    policy: RetryPolicy,
) -> DrainReport:
    if outcome is ApplyOutcome.PARKED:
        conn.execute(
            "UPDATE deliveries SET state = ?, parked_until = ?, lease_expires_at = NULL "
            "WHERE delivery_id = ?",
            (DeliveryState.PARKED, now + policy.base_seconds, delivery_id),
        )
        # Counted on the transition into the park, never on each re-park, or a delivery whose
        # prerequisite never arrives inflates the counter once per drain pass forever.
        if was == DeliveryState.PARKED:
            return DrainReport()
        metrics.bump(conn, "parked")
        return DrainReport(parked=1)
    if outcome is ApplyOutcome.NOOP_DUPLICATE:
        conn.execute(
            "UPDATE deliveries SET state = ?, lease_expires_at = NULL, next_attempt_at = NULL "
            "WHERE delivery_id = ?",
            (DeliveryState.DUPLICATE, delivery_id),
        )
        metrics.bump(conn, "deduped")
        return DrainReport(noop_duplicate=1)
    conn.execute(
        "UPDATE deliveries SET state = ?, parked_until = NULL, lease_expires_at = NULL, "
        "next_attempt_at = NULL WHERE delivery_id = ?",
        (DeliveryState.APPLIED, delivery_id),
    )
    metrics.bump(conn, "applied")
    return DrainReport(applied=1)


def drain_until_idle(
    conn: sqlite3.Connection, *, clock: Clock, policy: RetryPolicy, max_passes: int = 8
) -> DrainReport:
    """Drain, then sleep the clock forward to the next due delivery, until nothing is due.

    A delivery whose prerequisite never arrives parks again on every pass, so the pass count
    is bounded rather than waiting for an idle state that will not come. That remainder is
    what reconciliation reports as PARKED_STALE.
    """
    total = DrainReport()
    rng = jitter_source(policy)
    for _ in range(max_passes):
        pass_report = drain(conn, clock=clock, policy=policy, rng=rng)
        total = total.merge(pass_report)
        pending = conn.execute(
            "SELECT next_attempt_at, parked_until FROM deliveries WHERE state IN (?, ?)",
            (DeliveryState.CLAIMED, DeliveryState.PARKED),
        ).fetchall()
        if not pending:
            break
        due = min(max(row["next_attempt_at"] or 0.0, row["parked_until"] or 0.0) for row in pending)
        wait = due - clock.now()
        if wait > 0:
            clock.sleep(wait)
        elif pass_report.total == 0:
            # Work is due now and the last pass changed nothing, so another pass cannot help.
            # Work due now after a pass that did change something is a batch the limit cut
            # short, and that does deserve another pass.
            break
    return total


def receive_stripe(
    conn: sqlite3.Connection,
    raw: bytes,
    headers: Mapping[str, str],
    *,
    secrets: Sequence[bytes],
    clock: Clock,
    lease_seconds: float = 30.0,
    tolerance_seconds: int = STRIPE_DEFAULT_TOLERANCE_SECONDS,
) -> ClaimResult:
    """Verify over the exact bytes received, then claim. Nothing else runs on this thread."""
    metrics.bump(conn, "events_received")
    try:
        verify_stripe(raw, headers, secrets, now=clock.now(), tolerance_seconds=tolerance_seconds)
    except StaleTimestamp:
        metrics.bump(conn, "replay_rejected")
        raise
    except SignatureError:
        metrics.bump(conn, "signature_rejected")
        raise
    identity = _identity(Provider.STRIPE, _payload(raw))
    result = claim(
        conn,
        provider=Provider.STRIPE,
        identity=identity,
        raw=raw,
        headers=headers,
        clock=clock,
        lease_seconds=lease_seconds,
    )
    if result.reason == "duplicate":
        metrics.bump(conn, "deduped")
    return result


def receive_adyen(
    conn: sqlite3.Connection,
    raw: bytes,
    *,
    hmac_key_hex: str,
    clock: Clock,
    lease_seconds: float = 30.0,
) -> ClaimResult:
    """Verify one notification item and claim it. No timestamp is signed, so no replay window.

    The bytes stored here may be a re-serialisation of the item as it arrived inside a
    notificationItems envelope, which is safe only because Adyen signs a joined field string.
    Re-serialising a Stripe body the same way would break its signature.
    """
    metrics.bump(conn, "events_received")
    item = _payload(raw)
    additional = item.get("additionalData")
    signature = additional.get("hmacSignature", "") if isinstance(additional, Mapping) else ""
    try:
        verify_adyen(item, str(signature), hmac_key_hex)
    except SignatureError:
        metrics.bump(conn, "signature_rejected")
        raise
    result = claim(
        conn,
        provider=Provider.ADYEN,
        identity=_identity(Provider.ADYEN, item),
        raw=raw,
        headers={},
        clock=clock,
        lease_seconds=lease_seconds,
    )
    if result.reason == "duplicate":
        metrics.bump(conn, "deduped")
    return result
