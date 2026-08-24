"""SQLite schema, connection settings and the one write transaction shape this repo uses.

Two composite unique constraints carry the whole idempotency story and both sit on readable
columns, never on a hashed opaque key: a reviewer has to be able to read the event id, the
kind and the provider reference straight out of a row.

WAL gives one writer at a time while readers and writers do not block each other, and a query
against a WAL database can still return SQLITE_BUSY in obscure cases, so busy_timeout is set on
every connection. https://www.sqlite.org/wal.html
"""

from __future__ import annotations

import os
import pathlib
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

__all__ = ["SCHEMA_VERSION", "connect", "init_schema", "write_tx"]

SCHEMA_VERSION: int = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS deliveries (
    delivery_id      TEXT PRIMARY KEY,
    provider         TEXT NOT NULL,
    raw              BLOB NOT NULL,
    headers          TEXT NOT NULL,
    received_at      REAL NOT NULL,
    state            TEXT NOT NULL,
    attempts         INTEGER NOT NULL DEFAULT 0,
    next_attempt_at  REAL,
    lease_expires_at REAL,
    parked_until     REAL,
    reason_code      TEXT
);
CREATE TABLE IF NOT EXISTS effects (
    id               INTEGER PRIMARY KEY,
    payment_id       TEXT NOT NULL,
    kind             TEXT NOT NULL,
    provider_ref     TEXT NOT NULL,
    amount_minor     INTEGER,
    currency         TEXT,
    occurred_at      REAL NOT NULL,
    sequence         INTEGER NOT NULL,
    delivery_id      TEXT NOT NULL REFERENCES deliveries(delivery_id),
    raw_amount_value TEXT,
    raw_currency     TEXT,
    UNIQUE(payment_id, kind, provider_ref)
);
CREATE TABLE IF NOT EXISTS payments (
    payment_id           TEXT PRIMARY KEY,
    currency             TEXT NOT NULL,
    authorized_minor     INTEGER NOT NULL DEFAULT 0,
    captured_minor       INTEGER NOT NULL DEFAULT 0,
    capture_failed_minor INTEGER NOT NULL DEFAULT 0,
    refunded_minor       INTEGER NOT NULL DEFAULT 0,
    refund_failed_minor  INTEGER NOT NULL DEFAULT 0,
    voided               INTEGER NOT NULL DEFAULT 0,
    expired              INTEGER NOT NULL DEFAULT 0,
    rank                 INTEGER NOT NULL DEFAULT 0,
    last_sequence        INTEGER NOT NULL DEFAULT -1,
    updated_at           REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS settlement_rows (
    psp_reference TEXT PRIMARY KEY,
    gross_minor   INTEGER NOT NULL,
    fee_minor     INTEGER NOT NULL,
    net_minor     INTEGER NOT NULL,
    currency      TEXT NOT NULL,
    payout_date   TEXT NOT NULL,
    journal_type  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS counters (
    name  TEXT PRIMARY KEY,
    value INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS effects_by_payment ON effects(payment_id);
CREATE INDEX IF NOT EXISTS deliveries_due ON deliveries(state, next_attempt_at);
"""


def connect(path: str | os.PathLike[str], *, busy_timeout_ms: int = 5000) -> sqlite3.Connection:
    """Open a connection with the pragmas every writer in this repo depends on.

    isolation_level=None hands transaction control to write_tx, which always uses
    BEGIN IMMEDIATE: a deferred transaction that upgrades to a write lock later raises
    SQLITE_BUSY under contention, which is a flaky CI leg rather than a real defence.
    """
    # sqlite3 reports a missing PARENT directory as "unable to open database file", which reads
    # as a permissions or corruption problem and sends you looking in the wrong place. The CLI
    # creates the directory itself, so this only ever bit a caller constructing the app directly,
    # which is exactly what the README's adapter example asks a reader to do.
    parent = pathlib.Path(path).expanduser().parent
    if str(parent) and not parent.is_dir():
        raise NotADirectoryError(
            f"the directory for the database does not exist: {parent}. "
            "Create it, or pass a path inside one that does."
        )
    conn = sqlite3.connect(path, timeout=busy_timeout_ms / 1000, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")


@contextmanager
def write_tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """BEGIN IMMEDIATE, then commit or roll back.

    Nesting is refused rather than silently flattened: the dedup claim and the ledger effect
    are only atomic together if the caller owns exactly one transaction boundary.
    """
    if conn.in_transaction:
        raise RuntimeError("write_tx is already open on this connection; do not nest it")
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")
