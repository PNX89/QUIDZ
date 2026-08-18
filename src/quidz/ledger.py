"""Transactional application of one canonical event to the ledger.

apply_delivery runs inside the caller's transaction, never its own, because the dedup claim
and the ledger effect are only exactly once together. Claim the key, crash before applying,
and a unique constraint on its own gives you at most once: the retry sees the key, answers
200, and the effect never lands. That is silent money loss with a green dashboard.
"""

from __future__ import annotations

import sqlite3
from enum import StrEnum

from quidz.events import CanonicalEvent
from quidz.model import PaymentState, PrematureEvent, apply_effect
from quidz.reconcile import SettlementRow

__all__ = [
    "ApplyOutcome",
    "apply_delivery",
    "list_payments",
    "list_settlement_rows",
    "load_payment",
]

_STATE_COLUMNS = (
    "payment_id",
    "currency",
    "authorized_minor",
    "captured_minor",
    "capture_failed_minor",
    "refunded_minor",
    "refund_failed_minor",
    "voided",
    "expired",
    "rank",
    "last_sequence",
)


class ApplyOutcome(StrEnum):
    APPLIED = "applied"
    NOOP_DUPLICATE = "noop_duplicate"
    PARKED = "parked"
    REJECTED = "rejected"


def _row_to_state(row: sqlite3.Row) -> PaymentState:
    return PaymentState(
        payment_id=row["payment_id"],
        currency=row["currency"],
        authorized_minor=row["authorized_minor"],
        captured_minor=row["captured_minor"],
        capture_failed_minor=row["capture_failed_minor"],
        refunded_minor=row["refunded_minor"],
        refund_failed_minor=row["refund_failed_minor"],
        voided=bool(row["voided"]),
        expired=bool(row["expired"]),
        rank=row["rank"],
        last_sequence=row["last_sequence"],
    )


def load_payment(conn: sqlite3.Connection, payment_id: str) -> PaymentState | None:
    columns = ", ".join(_STATE_COLUMNS)
    row = conn.execute(
        f"SELECT {columns} FROM payments WHERE payment_id = ?", (payment_id,)
    ).fetchone()
    return None if row is None else _row_to_state(row)


def list_payments(conn: sqlite3.Connection) -> list[PaymentState]:
    columns = ", ".join(_STATE_COLUMNS)
    rows = conn.execute(f"SELECT {columns} FROM payments ORDER BY payment_id")
    return [_row_to_state(row) for row in rows]


def apply_delivery(
    conn: sqlite3.Connection, delivery_id: str, event: CanonicalEvent
) -> ApplyOutcome:
    """Insert the child effect row and fold it into the aggregate, in the caller's transaction.

    Returns PARKED when the effect's prerequisite has not arrived yet. Domain rejections raise
    so the caller can classify them into a reason code rather than losing the reason.

    The effect row is inserted BEFORE the aggregate is folded, so a re-delivery is decided by
    the unique index rather than by the state machine. Fold first and a repeated capture looks
    like an illegal second capture and lands in the dead letter queue instead of resolving to
    the no-op it actually is. The savepoint is what lets the insert stand for the duplicate
    check and still leave no row behind when the effect turns out to be premature.
    """
    state = load_payment(conn, event.payment_id)
    if state is None:
        if event.amount is None:
            # An amountless effect (void, expire) cannot open an aggregate because it carries
            # no currency, and every one of them requires an authorization first anyway.
            return ApplyOutcome.PARKED
        state = PaymentState(payment_id=event.payment_id, currency=event.amount.currency)

    conn.execute("SAVEPOINT quidz_effect")
    try:
        conn.execute(
            "INSERT INTO effects (payment_id, kind, provider_ref, amount_minor, currency, "
            "occurred_at, sequence, delivery_id, raw_amount_value, raw_currency) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event.payment_id,
                str(event.kind),
                event.provider_ref,
                None if event.amount is None else event.amount.minor,
                None if event.amount is None else event.amount.currency,
                event.occurred_at,
                event.sequence,
                delivery_id,
                event.raw_amount_value,
                event.raw_currency,
            ),
        )
        updated = apply_effect(state, event)
    except (sqlite3.IntegrityError, PrematureEvent) as exc:
        conn.execute("ROLLBACK TO quidz_effect")
        conn.execute("RELEASE quidz_effect")
        if isinstance(exc, PrematureEvent):
            return ApplyOutcome.PARKED
        return ApplyOutcome.NOOP_DUPLICATE
    conn.execute("RELEASE quidz_effect")

    conn.execute(
        "INSERT INTO payments (payment_id, currency, authorized_minor, captured_minor, "
        "capture_failed_minor, refunded_minor, refund_failed_minor, voided, expired, rank, "
        "last_sequence, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(payment_id) DO UPDATE SET "
        "authorized_minor = excluded.authorized_minor, "
        "captured_minor = excluded.captured_minor, "
        "capture_failed_minor = excluded.capture_failed_minor, "
        "refunded_minor = excluded.refunded_minor, "
        "refund_failed_minor = excluded.refund_failed_minor, "
        "voided = excluded.voided, expired = excluded.expired, rank = excluded.rank, "
        "last_sequence = excluded.last_sequence, "
        # The aggregate carries no wall clock of its own; this is the provider time of the
        # newest event folded in, and out of order deliveries must not drag it backwards.
        "updated_at = max(payments.updated_at, excluded.updated_at)",
        (
            updated.payment_id,
            updated.currency,
            updated.authorized_minor,
            updated.captured_minor,
            updated.capture_failed_minor,
            updated.refunded_minor,
            updated.refund_failed_minor,
            int(updated.voided),
            int(updated.expired),
            updated.rank,
            updated.last_sequence,
            event.occurred_at,
        ),
    )
    return ApplyOutcome.APPLIED


def list_settlement_rows(conn: sqlite3.Connection) -> list[SettlementRow]:
    """Source B, read back in a stable order so the report diffs cleanly."""
    return [
        SettlementRow(
            psp_reference=row["psp_reference"],
            gross_minor=row["gross_minor"],
            fee_minor=row["fee_minor"],
            net_minor=row["net_minor"],
            currency=row["currency"],
            payout_date=row["payout_date"],
            journal_type=row["journal_type"],
        )
        for row in conn.execute(
            "SELECT psp_reference, gross_minor, fee_minor, net_minor, currency, payout_date, "
            "journal_type FROM settlement_rows ORDER BY psp_reference"
        )
    ]
