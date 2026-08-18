"""Operational counters, persisted in the counters table.

The names are an allowlist because a typo in a counter name is a metric that silently never
fires, which is the worst failure mode a dashboard has.
"""

from __future__ import annotations

import sqlite3

__all__ = ["COUNTER_NAMES", "bump", "set_gauge", "snapshot"]

COUNTER_NAMES: tuple[str, ...] = (
    "events_received",
    "signature_rejected",
    "replay_rejected",
    "deduped",
    "applied",
    "parked",
    "dead_lettered",
    "drift_info",
    "drift_warn",
    "drift_break",
    "drift_critical",
    "oldest_unresolved_drift_seconds",
)

_UPSERT = (
    "INSERT INTO counters (name, value) VALUES (?, ?) "
    "ON CONFLICT(name) DO UPDATE SET value = value + excluded.value"
)


def _check(name: str) -> str:
    if name not in COUNTER_NAMES:
        raise KeyError(f"unknown counter {name!r}; the counter surface is a fixed allowlist")
    return name


def bump(conn: sqlite3.Connection, name: str, delta: int = 1) -> None:
    conn.execute(_UPSERT, (_check(name), delta))


def set_gauge(conn: sqlite3.Connection, name: str, value: int) -> None:
    conn.execute(
        "INSERT INTO counters (name, value) VALUES (?, ?) "
        "ON CONFLICT(name) DO UPDATE SET value = excluded.value",
        (_check(name), value),
    )


def snapshot(conn: sqlite3.Connection) -> dict[str, int]:
    """Every allowlisted name, zero filled, so a quiet counter is visibly zero and not absent."""
    stored = {row["name"]: row["value"] for row in conn.execute("SELECT name, value FROM counters")}
    return {name: int(stored.get(name, 0)) for name in COUNTER_NAMES}
