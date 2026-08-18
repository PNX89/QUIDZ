from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from quidz import store
from quidz.events import CanonicalEvent, Provider
from quidz.model import EffectKind
from quidz.money import Money

EventFactory = Callable[..., CanonicalEvent]


def stripe_body(
    event_id: str,
    event_type: str,
    obj: dict[str, object],
    *,
    created: int = 1_787_011_200,
) -> bytes:
    payload = {"id": event_id, "type": event_type, "created": created, "data": {"object": obj}}
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


def authorize_body(event_id: str, payment_id: str = "pi_1", minor: int = 1000) -> bytes:
    return stripe_body(
        event_id,
        "payment_intent.amount_capturable_updated",
        {
            "id": payment_id,
            "object": "payment_intent",
            "amount": minor,
            "amount_capturable": minor,
            "currency": "eur",
        },
    )


def capture_body(
    event_id: str, payment_id: str = "pi_1", charge_id: str = "ch_1", minor: int = 1000
) -> bytes:
    return stripe_body(
        event_id,
        "charge.captured",
        {
            "id": charge_id,
            "object": "charge",
            "payment_intent": payment_id,
            "amount": minor,
            "amount_captured": minor,
            "currency": "eur",
        },
    )


def refund_body(
    event_id: str, payment_id: str = "pi_1", refund_id: str = "re_1", minor: int = 400
) -> bytes:
    return stripe_body(
        event_id,
        "charge.refund.updated",
        {
            "id": refund_id,
            "object": "refund",
            "payment_intent": payment_id,
            "amount": minor,
            "currency": "eur",
            "status": "succeeded",
        },
    )


def insert_delivery(conn: sqlite3.Connection, delivery_id: str, raw: bytes = b"{}") -> str:
    """A delivery row the effects table can reference, for tests that bypass the inbox."""
    conn.execute(
        "INSERT OR IGNORE INTO deliveries (delivery_id, provider, raw, headers, received_at, "
        "state) VALUES (?, 'stripe', ?, '{}', 0.0, 'claimed')",
        (delivery_id, raw),
    )
    return delivery_id


def effect_rows(conn: sqlite3.Connection) -> list[tuple[object, ...]]:
    rows = conn.execute(
        "SELECT payment_id, kind, provider_ref, amount_minor, currency FROM effects "
        "ORDER BY payment_id, kind, provider_ref"
    )
    return [tuple(row) for row in rows]


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    # A real file, never :memory:. WAL is unsupported in memory and two :memory: connections
    # are two independent databases, so an in memory setup passes without testing anything.
    return tmp_path / "quidz.db"


@pytest.fixture
def conn(db_path: Path) -> Iterator[sqlite3.Connection]:
    connection = store.connect(db_path)
    store.init_schema(connection)
    yield connection
    connection.close()


@pytest.fixture
def make_event() -> EventFactory:
    def factory(
        kind: EffectKind,
        minor: int | None = None,
        *,
        currency: str = "EUR",
        payment_id: str = "pay_1",
        ref: str | None = None,
        sequence: int = 0,
    ) -> CanonicalEvent:
        provider_ref = ref if ref is not None else f"ref_{kind.value}"
        amount = None if minor is None else Money(minor, currency)
        return CanonicalEvent(
            provider=Provider.STRIPE,
            identity=f"evt_{provider_ref}",
            payment_id=payment_id,
            kind=kind,
            provider_ref=provider_ref,
            amount=amount,
            occurred_at=float(sequence),
            sequence=sequence,
            raw_amount_value=None if minor is None else str(minor),
            raw_currency=None if minor is None else currency,
        )

    return factory
