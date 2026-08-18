from __future__ import annotations

from types import MappingProxyType

from quidz.reconcile import (
    BreakGlass,
    Direction,
    DriftKind,
    ExceptionReport,
    Finding,
    GatePolicy,
    Severity,
    gate,
)

NOW = 1_787_011_200.0


def finding(
    kind: DriftKind = DriftKind.MISSING_LOCALLY,
    severity: Severity = Severity.CRITICAL,
    *,
    payment_id: str | None = "pay_1",
    delta: int | None = 0,
    currency: str | None = "EUR",
) -> Finding:
    return Finding(
        kind=kind,
        severity=severity,
        direction=Direction.NONE,
        payment_id=payment_id,
        provider_ref="ref_1",
        local=None,
        remote=None,
        delta_minor=delta,
        currency=currency,
        age_seconds=None,
        detail="constructed for the gate test",
    )


def report(*findings: Finding) -> ExceptionReport:
    counts = {severity: 0 for severity in Severity}
    for item in findings:
        counts[item.severity] += 1
    return ExceptionReport(
        generated_at=NOW,
        findings=findings,
        counts_by_severity=MappingProxyType(counts),
        counters=MappingProxyType({}),
    )


def test_one_critical_finding_blocks_outbound_movement() -> None:
    decision = gate(report(finding()), policy=GatePolicy(), now=NOW)
    assert (decision.outbound_blocked, decision.blocked_ids) == (True, ("pay_1",))


def test_ingest_is_never_blocked_whatever_the_findings() -> None:
    # Halting ingest while the books disagree widens the gap the gate exists to close.
    cases = [
        report(),
        report(finding(severity=Severity.INFO)),
        report(finding(), finding(payment_id="pay_2"), finding(payment_id="pay_3")),
    ]
    assert [gate(case, policy=GatePolicy(), now=NOW).ingest_blocked for case in cases] == [
        False,
        False,
        False,
    ]


def test_blocking_is_scoped_to_the_affected_payment_and_spares_the_rest() -> None:
    decision = gate(
        report(
            finding(payment_id="pay_bad"),
            finding(DriftKind.AMOUNT_MISMATCH, Severity.BREAK, payment_id="pay_ok", delta=100),
        ),
        policy=GatePolicy(),
        now=NOW,
    )
    assert decision.blocked_ids == ("pay_bad",)


def test_a_break_below_both_materiality_thresholds_does_not_block() -> None:
    decision = gate(
        report(finding(DriftKind.AMOUNT_MISMATCH, Severity.BREAK, delta=100)),
        policy=GatePolicy(materiality_count=5, materiality_value_minor=10_000),
        now=NOW,
    )
    assert (decision.outbound_blocked, decision.blocked_ids) == (False, ())


def test_an_in_flight_item_inside_the_grace_window_does_not_block() -> None:
    decision = gate(
        report(finding(DriftKind.IN_FLIGHT, Severity.INFO, delta=None)),
        policy=GatePolicy(),
        now=NOW,
    )
    assert (decision.outbound_blocked, decision.rationale) == (
        False,
        "no findings at or above break",
    )


def test_a_live_break_glass_suppresses_the_block_and_an_expired_one_is_inert() -> None:
    live = BreakGlass("ops-lead", "known provider backfill", NOW + 3600.0, ("pay_1",))
    expired = BreakGlass("ops-lead", "known provider backfill", NOW - 1.0, ("pay_1",))
    suppressed = gate(report(finding()), policy=GatePolicy(break_glass=live), now=NOW)
    stands = gate(report(finding()), policy=GatePolicy(break_glass=expired), now=NOW)
    assert (suppressed.outbound_blocked, suppressed.suppressed_by, stands.outbound_blocked) == (
        False,
        live,
        True,
    )
