"""argparse entry point: demo, replay, reconcile.

Exit codes are the contract: 0 pass, 1 the gate closed or findings landed at or above
--fail-on, 2 a usage or argument error. `quidz demo` always exits 0 because it is a
demonstration; `quidz reconcile` is the command whose exit code means something in CI.

The demo drives the whole pipeline in process with no socket. A bound server would introduce
a startup readiness race against the simulator's first request and a port collision risk under
parallel CI, which is exactly the flakiness this repo argues against.
"""

from __future__ import annotations

import argparse
import base64
import json
import sqlite3
import sys
import tempfile
from collections.abc import Sequence
from contextlib import closing
from dataclasses import asdict
from pathlib import Path

from quidz import __version__, ledger, metrics, store
from quidz.clock import FakeClock
from quidz.dlq import RetryPolicy
from quidz.events import Provider
from quidz.inbox import claim, drain_until_idle, parked_events, receive_adyen, receive_stripe
from quidz.model import derive_status
from quidz.money import Money, format_major
from quidz.reconcile import (
    ExceptionReport,
    GateDecision,
    GatePolicy,
    RemotePayment,
    SettlementRow,
    Severity,
    gate,
    reconcile,
    to_json,
    to_text,
)
from quidz.sim import SCENARIOS, BreakMode, Delivery, Simulator
from quidz.verify import SignatureError, StaleTimestamp

__all__ = ["build_parser", "main"]

# Deliveries arrive at distinct moments, so receipt order is the drain order. Without this
# every delivery shares one received_at and the queue drains in identifier order instead.
_RECEIPT_INTERVAL = 0.05
# How far the demo advances its clock between the last delivery and the reconciliation run,
# so that parked deliveries age past park_max_age_seconds and the report has something to say.
_RECONCILE_AFTER = 1_200.0
_SEVERITY_ORDER = {Severity.INFO: 0, Severity.WARN: 1, Severity.BREAK: 2, Severity.CRITICAL: 3}


class CliError(Exception):
    """A user facing failure. Printed as one line, never as a traceback."""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="quidz", description="Fail-closed payment webhook reconciliation over synthetic data."
    )
    parser.add_argument("--version", action="version", version=f"quidz {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    demo = sub.add_parser("demo", help="run the whole pipeline over synthetic deliveries")
    demo.add_argument("--scenario", choices=SCENARIOS, default="happy")
    demo.add_argument(
        "--break",
        dest="breaks",
        action="append",
        default=[],
        choices=[mode.value for mode in BreakMode],
        metavar="MODE",
        help="inject a specific failure, repeatable: " + ", ".join(m.value for m in BreakMode),
    )
    demo.add_argument("--seed", type=int, default=0)
    demo.add_argument("--db", help="database path; defaults to a fresh temporary file per run")
    demo.add_argument("--json", dest="json_path", metavar="PATH", help="write the report as JSON")
    demo.add_argument("--http", action="store_true", help="route deliveries through the ASGI app")

    replay = sub.add_parser("replay", help="re-run a captured delivery log against a fresh ledger")
    replay.add_argument("file", help="a delivery log written by quidz demo")
    replay.add_argument("--db")
    replay.add_argument("--json", dest="json_path", metavar="PATH")
    replay.add_argument(
        "--assert-terminal",
        action="store_true",
        help="fail if the replayed terminal state differs from the log's recorded expectation",
    )

    recon = sub.add_parser("reconcile", help="reconcile an existing database and apply the gate")
    recon.add_argument("--db", help="database path produced by an earlier run")
    recon.add_argument("--as-of", type=float, metavar="UNIX", dest="as_of")
    recon.add_argument("--json", dest="json_path", metavar="PATH")
    recon.add_argument("--fail-on", choices=["break", "critical"], dest="fail_on")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "demo":
            return _demo(args)
        if args.command == "replay":
            return _replay(args)
        return _reconcile(args)
    except CliError as exc:
        print(f"quidz: {exc}", file=sys.stderr)
        return 2


def _paths(db: str | None) -> tuple[Path, Path, Path]:
    if db:
        base = Path(db)
        base.parent.mkdir(parents=True, exist_ok=True)
    else:
        base = Path(tempfile.mkdtemp(prefix="quidz-")) / "quidz.db"
    return base, Path(f"{base}.jsonl"), Path(f"{base}.remote.json")


def _demo(args: argparse.Namespace) -> int:
    db_path, log_path, snapshot_path = _paths(args.db)
    simulator = Simulator(seed=args.seed)
    breaks = [BreakMode(mode) for mode in args.breaks]
    deliveries = simulator.deliveries(args.scenario, breaks=breaks)
    clock = FakeClock(t=simulator.started_at)
    policy = GatePolicy()

    with closing(store.connect(db_path)) as conn:
        store.init_schema(conn)
        receipts = (
            _receive_over_http(deliveries, simulator, clock, db_path)
            if args.http
            else _receive_in_process(conn, deliveries, simulator, clock)
        )
        drained = drain_until_idle(conn, clock=clock, policy=RetryPolicy(seed=args.seed))
        _store_settlement(conn, simulator.settlement_report())
        clock.advance(_RECONCILE_AFTER)

        report, decision = _run_reconcile(conn, simulator.remote_payments(), clock.now(), policy)
        ledger_lines = _ledger_lines(conn)
        _write_log(log_path, conn, _terminal(conn))
    _write_snapshot(snapshot_path, simulator, args, clock.now())

    modes = ", ".join(mode.value for mode in simulator.applied_breaks) or "none"
    print("QUIDZ demo")
    print(f"  scenario      {args.scenario}")
    print(f"  seed          {args.seed}")
    print(f"  breaks        {modes}")
    print(f"  transport     {'in-process ASGI' if args.http else 'in-process, no socket'}")
    print(f"  database      {db_path}")
    print(f"  delivery log  {log_path}")
    print()
    print("RECEIPT")
    print(f"  deliveries    {len(deliveries)}")
    print(
        f"  accepted      {receipts['new'] + receipts['lease_reclaimed']}"
        f"   duplicate {receipts['duplicate']}   in_progress {receipts['in_progress']}"
    )
    print(
        f"  rejected      {receipts['signature_invalid'] + receipts['stale_timestamp']}"
        f"   (signature {receipts['signature_invalid']}, replay {receipts['stale_timestamp']})"
    )
    print()
    print("DRAIN")
    print(
        f"  applied {drained.applied}   noop_duplicate {drained.noop_duplicate}   "
        f"parked {drained.parked}   dead_lettered {drained.dead_lettered}   "
        f"retried {drained.retried}"
    )
    print()
    print(ledger_lines)
    print()
    print(to_text(report, decision))
    print()
    _print_counters(report)
    if args.json_path:
        Path(args.json_path).write_text(to_json(report, decision), encoding="utf-8")
    return 0


def _receive_in_process(
    conn: sqlite3.Connection,
    deliveries: Sequence[Delivery],
    simulator: Simulator,
    clock: FakeClock,
) -> dict[str, int]:
    tally = _empty_tally()
    for delivery in deliveries:
        try:
            if delivery.provider is Provider.STRIPE:
                result = receive_stripe(
                    conn,
                    delivery.raw,
                    delivery.headers,
                    secrets=simulator.stripe_secrets,
                    clock=clock,
                )
            else:
                result = receive_adyen(
                    conn, delivery.raw, hmac_key_hex=simulator.adyen_hmac_key_hex, clock=clock
                )
            tally[result.reason] += 1
        except StaleTimestamp:
            tally["stale_timestamp"] += 1
        except SignatureError:
            tally["signature_invalid"] += 1
        clock.advance(_RECEIPT_INTERVAL)
    return tally


def _receive_over_http(
    deliveries: Sequence[Delivery],
    simulator: Simulator,
    clock: FakeClock,
    db_path: Path,
) -> dict[str, int]:
    """The same deliveries through the ASGI app, still in process and still network free."""
    try:
        import asyncio

        import httpx

        from quidz.app import create_app
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise CliError(
            "--http needs the server extra; install it with: uv sync --all-extras"
        ) from exc

    app = create_app(
        db_path=str(db_path),
        secrets=simulator.stripe_secrets,
        adyen_hmac_key_hex=simulator.adyen_hmac_key_hex,
        clock=clock,
    )
    tally = _empty_tally()

    async def post_all() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://quidz.invalid") as http:
            for delivery in deliveries:
                if delivery.provider is Provider.STRIPE:
                    response = await http.post(
                        "/webhooks/stripe", content=delivery.raw, headers=delivery.headers
                    )
                else:
                    envelope = {
                        "live": "false",
                        "notificationItems": [
                            {"NotificationRequestItem": json.loads(delivery.raw)}
                        ],
                    }
                    response = await http.post("/webhooks/adyen", json=envelope)
                if response.status_code == 200:
                    tally["new"] += 1
                elif response.status_code == 409:
                    tally["in_progress"] += 1
                elif response.status_code == 401:
                    tally["signature_invalid"] += 1
                else:
                    stale = "timestamp" in response.text
                    tally["stale_timestamp" if stale else "signature_invalid"] += 1
                clock.advance(_RECEIPT_INTERVAL)

    asyncio.run(post_all())
    return tally


def _empty_tally() -> dict[str, int]:
    names = (
        "new",
        "duplicate",
        "in_progress",
        "lease_reclaimed",
        "signature_invalid",
        "stale_timestamp",
    )
    return dict.fromkeys(names, 0)


def _store_settlement(conn: sqlite3.Connection, rows: Sequence[SettlementRow]) -> None:
    with store.write_tx(conn):
        for row in rows:
            conn.execute(
                "INSERT OR REPLACE INTO settlement_rows (psp_reference, gross_minor, fee_minor, "
                "net_minor, currency, payout_date, journal_type) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    row.psp_reference,
                    row.gross_minor,
                    row.fee_minor,
                    row.net_minor,
                    row.currency,
                    row.payout_date,
                    row.journal_type,
                ),
            )


def _run_reconcile(
    conn: sqlite3.Connection,
    remote: Sequence[RemotePayment],
    now: float,
    policy: GatePolicy,
) -> tuple[ExceptionReport, GateDecision]:
    report = reconcile(
        local=ledger.list_payments(conn),
        remote=remote,
        settlement=ledger.list_settlement_rows(conn),
        parked=parked_events(conn),
        now=now,
        policy=policy,
        counters=metrics.snapshot(conn),
    )
    return report, gate(report, policy=policy, now=now)


def _terminal(conn: sqlite3.Connection) -> dict[str, str]:
    return {state.payment_id: derive_status(state) for state in ledger.list_payments(conn)}


def _write_log(path: Path, conn: sqlite3.Connection, terminal: dict[str, str]) -> None:
    """Log what the inbox actually accepted, in receipt order, plus the terminal state.

    Deliveries the front door rejected are deliberately absent: a signature that did not
    verify never entered the system, so replaying it would rebuild a ledger this run never
    had. The log is the inbox's own record, not the provider's outbox.
    """
    rows = conn.execute(
        "SELECT delivery_id, provider, raw, headers FROM deliveries "
        "ORDER BY received_at, delivery_id"
    )
    lines = [
        json.dumps(
            {
                "kind": "delivery",
                "provider": row["provider"],
                "identity": row["delivery_id"].split(":", 1)[1],
                "headers": json.loads(row["headers"]),
                "raw_b64": base64.b64encode(row["raw"]).decode("ascii"),
            },
            sort_keys=True,
        )
        for row in rows.fetchall()
    ]
    lines.append(json.dumps({"kind": "terminal", "payments": terminal}, sort_keys=True))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_snapshot(
    path: Path, simulator: Simulator, args: argparse.Namespace, as_of: float
) -> None:
    """The provider's payment list, dropped beside the database.

    A real reconciliation job pulls this from the provider API. Here it is a file, so that
    `quidz reconcile --db` reproduces the demo's report exactly without re-running it.
    """
    path.write_text(
        json.dumps(
            {
                "as_of": as_of,
                "seed": args.seed,
                "scenario": args.scenario,
                "payments": [asdict(payment) for payment in simulator.remote_payments()],
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )


def _read_snapshot(path: Path) -> tuple[float, list[RemotePayment]] | None:
    """Read the provider payment list a demo run dropped beside its database.

    Returns None when the file is absent, so a caller can decide whether that is fatal.
    """
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return float(payload["as_of"]), [RemotePayment(**row) for row in payload["payments"]]
    # ValueError covers both decode failures: JSONDecodeError for a truncated file and
    # UnicodeDecodeError for a corrupt one. OSError is here because exists() is true for a
    # directory and for a file this process cannot open.
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise CliError(f"provider snapshot {path} is not readable: {exc}") from exc


def _ledger_lines(conn: sqlite3.Connection) -> str:
    header = f"  {'PAYMENT':<22}{'AUTHORIZED':>16}{'CAPTURED':>16}{'REFUNDED':>16}  STATUS"
    lines = ["LEDGER", header]
    for state in ledger.list_payments(conn):
        # format_major is display only and never feeds arithmetic. It is what makes the
        # per currency exponent visible: JPY prints without a fractional part.
        amounts = [
            format_major(Money(minor, state.currency))
            for minor in (state.authorized_minor, state.captured_minor, state.refunded_minor)
        ]
        lines.append(
            f"  {state.payment_id:<22}{amounts[0]:>16}{amounts[1]:>16}{amounts[2]:>16}  "
            f"{derive_status(state)}"
        )
    return "\n".join(lines)


def _print_counters(report: ExceptionReport) -> None:
    print("COUNTERS")
    for name, value in report.counters.items():
        print(f"  {name:<34}{value:>8}")


def _replay(args: argparse.Namespace) -> int:
    deliveries, expected = _read_log(Path(args.file))
    db_path, _, _ = _paths(args.db)
    # Signatures are not re-checked here. The tolerance window is a receipt time defence and
    # Stripe re-signs with a new timestamp on every retry, so verifying a captured log days
    # later would reject every line and teach the wrong lesson.
    clock = FakeClock()
    with closing(store.connect(db_path)) as conn:
        store.init_schema(conn)
        for provider, identity, raw, headers in deliveries:
            claim(
                conn,
                provider=provider,
                identity=identity,
                raw=raw,
                headers=headers,
                clock=clock,
            )
            clock.advance(_RECEIPT_INTERVAL)
        drained = drain_until_idle(conn, clock=clock, policy=RetryPolicy())
        terminal = _terminal(conn)
        ledger_lines = _ledger_lines(conn)

    print("QUIDZ replay")
    print(f"  log           {args.file}")
    print(f"  deliveries    {len(deliveries)}")
    print(
        f"  applied {drained.applied}   noop_duplicate {drained.noop_duplicate}   "
        f"parked {drained.parked}   dead_lettered {drained.dead_lettered}"
    )
    print()
    print(ledger_lines)
    matches = expected is None or expected == terminal
    if args.json_path:
        Path(args.json_path).write_text(
            json.dumps({"payments": terminal, "matches_log": matches}, indent=2, sort_keys=True),
            encoding="utf-8",
        )
    if not args.assert_terminal:
        return 0
    if expected is None:
        raise CliError(f"{args.file} records no terminal state to assert against")
    if not matches:
        print()
        print("TERMINAL STATE MISMATCH")
        for payment_id in sorted(set(expected) | set(terminal)):
            want, got = expected.get(payment_id, "absent"), terminal.get(payment_id, "absent")
            if want != got:
                print(f"  {payment_id:<22}log {want:<20}replay {got}")
        return 1
    print()
    print(f"terminal state matches the log for all {len(terminal)} payments")
    return 0


def _read_log(path: Path) -> tuple[list[tuple[Provider, str, bytes, dict[str, str]]], dict | None]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CliError(f"cannot read delivery log {path}: {exc.strerror}") from exc
    # A truncated or corrupt log is not an OSError. UnicodeDecodeError is a ValueError, so the
    # handler above does not see it and the command exits on a traceback, which for `replay`
    # means exit 1: the code that says the ledger did not reproduce its terminal state.
    except UnicodeDecodeError as exc:
        raise CliError(
            f"delivery log {path} is not valid UTF-8 at byte {exc.start}: {exc.reason}"
        ) from exc
    deliveries: list[tuple[Provider, str, bytes, dict[str, str]]] = []
    expected: dict[str, str] | None = None
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
            if record["kind"] == "terminal":
                expected = dict(record["payments"])
                continue
            deliveries.append(
                (
                    Provider(record["provider"]),
                    record["identity"],
                    base64.b64decode(record["raw_b64"], validate=True),
                    dict(record["headers"]),
                )
            )
        except (ValueError, KeyError, TypeError) as exc:
            raise CliError(f"{path} line {number} is not a valid delivery record: {exc}") from exc
    if not deliveries:
        raise CliError(f"{path} carries no delivery records")
    return deliveries, expected


def _reconcile(args: argparse.Namespace) -> int:
    if not args.db:
        raise CliError("reconcile needs --db pointing at a database an earlier run produced")
    db_path = Path(args.db)
    if not db_path.exists():
        raise CliError(f"no database at {db_path}")
    snapshot = _read_snapshot(Path(f"{db_path}.remote.json"))
    if snapshot is None:
        raise CliError(
            f"no provider snapshot beside {db_path}; run `quidz demo --db {db_path}` first "
            "so the reconciliation has a payment list to compare against"
        )
    as_of, remote = snapshot
    now = as_of if args.as_of is None else args.as_of
    policy = GatePolicy()
    with closing(store.connect(db_path)) as conn:
        store.init_schema(conn)
        report, decision = _run_reconcile(conn, remote, now, policy)
    print(to_text(report, decision))
    print()
    _print_counters(report)
    if args.json_path:
        Path(args.json_path).write_text(to_json(report, decision), encoding="utf-8")

    if decision.outbound_blocked:
        return 1
    if args.fail_on:
        threshold = Severity.BREAK if args.fail_on == "break" else Severity.CRITICAL
        if any(_SEVERITY_ORDER[f.severity] >= _SEVERITY_ORDER[threshold] for f in report.findings):
            return 1
    return 0
