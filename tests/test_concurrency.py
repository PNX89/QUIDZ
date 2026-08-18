from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from conftest import insert_delivery
from quidz import store

_INSERT = (
    "INSERT INTO effects (payment_id, kind, provider_ref, amount_minor, currency, occurred_at, "
    "sequence, delivery_id) VALUES ('pi_1', 'capture', 'ch_1', 1000, 'EUR', 0.0, 0, ?)"
)


def prepare(db_path: Path) -> str:
    setup = store.connect(db_path)
    store.init_schema(setup)
    delivery_id = insert_delivery(setup, "stripe:evt_1")
    setup.close()
    return delivery_id


def test_eight_racing_writers_leave_one_row_and_seven_integrity_errors(db_path: Path) -> None:
    """The unique index, not a prior SELECT, is the enforcement point.

    This proves arbitration at the INSERT boundary. It does not prove MVCC level write
    concurrency: SQLite has one writer at a time, which is exactly why the barrier is here.
    """
    delivery_id = prepare(db_path)
    barrier = threading.Barrier(8)
    lock = threading.Lock()
    wins: list[int] = []
    conflicts: list[int] = []

    def writer(index: int) -> None:
        connection = store.connect(db_path)
        try:
            barrier.wait(timeout=10.0)
            with store.write_tx(connection):
                connection.execute(_INSERT, (delivery_id,))
        except sqlite3.IntegrityError:
            with lock:
                conflicts.append(index)
        else:
            with lock:
                wins.append(index)
        finally:
            connection.close()

    threads = [threading.Thread(target=writer, args=(index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30.0)

    reader = store.connect(db_path)
    rows = reader.execute("SELECT count(*) AS n FROM effects").fetchone()["n"]
    reader.close()
    assert (len(wins), len(conflicts), rows) == (1, 7, 1)


def test_the_database_is_a_real_file_in_wal_mode(db_path: Path) -> None:
    prepare(db_path)
    connection = store.connect(db_path)
    mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
    connection.close()
    assert (db_path.exists(), mode) == (True, "wal")


def test_twenty_contended_rounds_never_report_the_database_as_locked(db_path: Path) -> None:
    delivery_id = prepare(db_path)
    failures: list[str] = []

    def writer(worker: int) -> None:
        connection = store.connect(db_path, busy_timeout_ms=5000)
        try:
            for round_number in range(20):
                try:
                    with store.write_tx(connection):
                        connection.execute(
                            "INSERT INTO effects (payment_id, kind, provider_ref, amount_minor, "
                            "currency, occurred_at, sequence, delivery_id) "
                            "VALUES (?, 'capture', ?, 1000, 'EUR', 0.0, 0, ?)",
                            (f"pi_{worker}", f"ch_{worker}_{round_number}", delivery_id),
                        )
                except sqlite3.OperationalError as exc:
                    failures.append(str(exc))
        finally:
            connection.close()

    threads = [threading.Thread(target=writer, args=(worker,)) for worker in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60.0)
    assert failures == []
