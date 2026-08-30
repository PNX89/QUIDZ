from __future__ import annotations

import sqlite3

import pytest

from quidz import store


def test_init_schema_stamps_pragma_user_version_with_schema_version(
    conn: sqlite3.Connection,
) -> None:
    """The number app.py's /healthz route reports comes from nowhere else.

    init_schema writes SCHEMA_VERSION into PRAGMA user_version and nothing read it back.
    Freezing the write to a literal while SCHEMA_VERSION moves on, exactly the shape a real
    migration bump would take if the write site were missed, left the full suite green before
    this test existed.
    """
    stamped = conn.execute("PRAGMA user_version").fetchone()[0]
    assert stamped == store.SCHEMA_VERSION


def test_an_effect_against_an_unknown_delivery_id_is_refused_by_the_foreign_key(
    conn: sqlite3.Connection,
) -> None:
    """PRAGMA foreign_keys=ON is what makes the effects table's REFERENCES load bearing.

    SQLite leaves foreign key enforcement off by default on every connection unless it is
    turned on explicitly. Deleting that PRAGMA from connect() left the full suite green before
    this test existed: nothing else in the suite inserts an effect for a delivery that was
    never claimed.
    """
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO effects (payment_id, kind, provider_ref, amount_minor, currency, "
            "occurred_at, sequence, delivery_id, raw_amount_value, raw_currency) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "pay_1",
                "authorize",
                "ref_1",
                1000,
                "EUR",
                0.0,
                0,
                "stripe:no-such-delivery",
                "1000",
                "EUR",
            ),
        )
