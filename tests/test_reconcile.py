from __future__ import annotations

from collections.abc import Sequence

from quidz.model import PaymentState
from quidz.reconcile import (
    Direction,
    DriftKind,
    ExceptionReport,
    Finding,
    GatePolicy,
    RemotePayment,
    SettlementRow,
    Severity,
    reconcile,
)

NOW = 1_787_011_200.0
# Older than the default one hour grace window and younger than the three day settlement SLA,
# so a pair built with it lands in neither the timing bucket nor the settlement bucket.
SETTLED_AGE = 7_200.0


def local(
    payment_id: str = "pay_1",
    *,
    currency: str = "EUR",
    authorized: int = 1000,
    captured: int = 1000,
    refunded: int = 0,
    capture_failed: int = 0,
) -> PaymentState:
    return PaymentState(
        payment_id=payment_id,
        currency=currency,
        authorized_minor=authorized,
        captured_minor=captured,
        capture_failed_minor=capture_failed,
        refunded_minor=refunded,
    )


def remote(
    payment_id: str = "pay_1",
    *,
    provider_ref: str = "ref_1",
    currency: str = "EUR",
    authorized: int = 1000,
    captured: int = 1000,
    refunded: int = 0,
    status: str = "captured",
    age: float = SETTLED_AGE,
) -> RemotePayment:
    return RemotePayment(
        payment_id=payment_id,
        provider_ref=provider_ref,
        currency=currency,
        authorized_minor=authorized,
        captured_minor=captured,
        refunded_minor=refunded,
        status=status,
        created_at=NOW - age,
    )


def run(
    *,
    ledger: Sequence[PaymentState] = (),
    provider: Sequence[RemotePayment] = (),
    settlement: Sequence[SettlementRow] = (),
    parked: Sequence[tuple[str, float]] = (),
    policy: GatePolicy | None = None,
) -> ExceptionReport:
    return reconcile(
        local=ledger,
        remote=provider,
        settlement=settlement,
        parked=parked,
        now=NOW,
        policy=policy or GatePolicy(),
    )


def only(report: ExceptionReport, kind: DriftKind) -> Finding:
    matches = [finding for finding in report.findings if finding.kind is kind]
    seen = [f.kind for f in report.findings]
    assert len(matches) == 1, f"expected exactly one {kind}, got {seen}"
    return matches[0]


def test_a_provider_payment_with_no_ledger_record_is_missing_locally() -> None:
    finding = only(run(provider=[remote()]), DriftKind.MISSING_LOCALLY)
    assert (finding.severity, finding.direction) == (Severity.CRITICAL, Direction.NONE)


def test_a_ledger_payment_the_provider_does_not_show_is_missing_remotely() -> None:
    finding = only(run(ledger=[local()]), DriftKind.MISSING_REMOTELY)
    assert (finding.severity, finding.direction) == (Severity.BREAK, Direction.NONE)


def test_two_local_aggregates_for_one_payment_id_are_a_local_duplicate() -> None:
    report = run(ledger=[local(), local()], provider=[remote()])
    finding = only(report, DriftKind.DUPLICATE_LOCAL)
    assert (finding.severity, finding.direction) == (Severity.BREAK, Direction.NONE)


def test_two_provider_records_for_one_payment_id_are_a_double_authorization() -> None:
    report = run(ledger=[local()], provider=[remote(), remote(provider_ref="ref_2")])
    finding = only(report, DriftKind.DUPLICATE_REMOTE)
    assert (finding.severity, finding.direction) == (Severity.CRITICAL, Direction.NONE)


def test_a_differing_net_position_is_an_amount_mismatch() -> None:
    report = run(ledger=[local(captured=1000)], provider=[remote(captured=800)])
    finding = only(report, DriftKind.AMOUNT_MISMATCH)
    assert (finding.severity, finding.direction, finding.delta_minor) == (
        Severity.BREAK,
        Direction.NONE,
        200,
    )


def test_an_equal_net_position_with_a_different_split_is_a_partial_divergence() -> None:
    # A scalar comparison of the net position sees nothing here. That is the whole category.
    report = run(
        ledger=[local(captured=1000, refunded=200)],
        provider=[remote(captured=800, refunded=0, status="partially_refunded")],
    )
    finding = only(report, DriftKind.PARTIAL_STATE_DIVERGENCE)
    assert (finding.severity, finding.delta_minor) == (Severity.WARN, 0)


def test_an_equal_vector_with_a_different_status_is_a_status_mismatch() -> None:
    report = run(ledger=[local()], provider=[remote(status="authorized")])
    finding = only(report, DriftKind.STATUS_MISMATCH)
    assert (finding.severity, finding.direction) == (Severity.BREAK, Direction.LOCAL_AHEAD)


def test_the_status_mismatch_direction_names_which_side_is_ahead() -> None:
    ahead = only(
        run(ledger=[local()], provider=[remote(status="authorized")]), DriftKind.STATUS_MISMATCH
    )
    behind = only(
        run(
            ledger=[local(captured=0)],
            provider=[remote(captured=0, status="captured")],
        ),
        DriftKind.STATUS_MISMATCH,
    )
    assert (ahead.direction, behind.direction) == (Direction.LOCAL_AHEAD, Direction.REMOTE_AHEAD)


def test_a_currency_disagreement_stops_the_comparison() -> None:
    report = run(ledger=[local(currency="EUR")], provider=[remote(currency="USD")])
    finding = only(report, DriftKind.CURRENCY_MISMATCH)
    assert (finding.severity, [f.kind for f in report.findings]) == (
        Severity.CRITICAL,
        [DriftKind.CURRENCY_MISMATCH],
    )


def test_a_divergence_inside_the_grace_window_is_merely_in_flight() -> None:
    finding = only(run(provider=[remote(age=60.0)]), DriftKind.IN_FLIGHT)
    assert (finding.severity, finding.direction) == (Severity.INFO, Direction.NONE)


def test_a_provider_row_with_no_payment_id_cannot_be_joined() -> None:
    finding = only(run(provider=[remote(payment_id="")]), DriftKind.UNKNOWN_REMOTE_ROW)
    assert (finding.severity, finding.provider_ref) == (Severity.WARN, "ref_1")


def test_a_settlement_net_inside_the_fee_tolerance_is_informational() -> None:
    row = SettlementRow("ref_1", 1000, 30, 970, "EUR", "2026-08-19", "Settled")
    finding = only(
        run(ledger=[local()], provider=[remote()], settlement=[row]), DriftKind.FEE_GROSS_NET
    )
    assert (finding.severity, finding.delta_minor) == (Severity.INFO, -30)


def test_a_capture_past_the_settlement_sla_and_absent_from_the_file_is_a_break() -> None:
    report = run(ledger=[local()], provider=[remote(age=400_000.0)])
    finding = only(report, DriftKind.UNSETTLED_PAST_SLA)
    assert (finding.severity, finding.delta_minor) == (Severity.BREAK, 1000)


def test_a_park_beyond_the_limit_is_a_break_and_below_it_is_in_flight() -> None:
    policy = GatePolicy(park_max_age_seconds=900.0)
    stale = only(run(parked=[("pay_1", NOW - 1000.0)], policy=policy), DriftKind.PARKED_STALE)
    fresh = only(run(parked=[("pay_1", NOW - 100.0)], policy=policy), DriftKind.IN_FLIGHT)
    assert (stale.severity, fresh.severity) == (Severity.BREAK, Severity.INFO)
