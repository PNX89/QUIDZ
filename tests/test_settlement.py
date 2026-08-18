from __future__ import annotations

from quidz.reconcile import DriftKind, GatePolicy, SettlementRow, Severity
from test_reconcile import local, only, remote, run


def settlement(psp_reference: str, net_minor: int, gross_minor: int = 10_000) -> SettlementRow:
    return SettlementRow(
        psp_reference=psp_reference,
        gross_minor=gross_minor,
        fee_minor=gross_minor - net_minor,
        net_minor=net_minor,
        currency="EUR",
        payout_date="2026-08-19",
        journal_type="Settled",
    )


def test_a_fee_inside_the_tolerance_is_informational() -> None:
    # Settlement net almost never equals capture gross once scheme fees and interchange apply,
    # so without this bucket an ordinary run flags every settled payment as broken.
    report = run(
        ledger=[local(authorized=10_000, captured=10_000)],
        provider=[remote(authorized=10_000, captured=10_000)],
        settlement=[settlement("ref_1", 9_710)],
    )
    finding = only(report, DriftKind.FEE_GROSS_NET)
    assert (finding.severity, finding.delta_minor) == (Severity.INFO, -290)


def test_a_fee_outside_the_tolerance_is_a_break() -> None:
    report = run(
        ledger=[local(authorized=10_000, captured=10_000)],
        provider=[remote(authorized=10_000, captured=10_000)],
        settlement=[settlement("ref_1", 9_000)],
        policy=GatePolicy(fee_tolerance_bps=500),
    )
    finding = only(report, DriftKind.FEE_GROSS_NET)
    assert (finding.severity, finding.delta_minor) == (Severity.BREAK, -1000)


def test_a_capture_absent_from_the_file_past_the_sla_is_unsettled() -> None:
    report = run(
        ledger=[local(authorized=10_000, captured=10_000)],
        provider=[remote(authorized=10_000, captured=10_000, age=400_000.0)],
        settlement=[settlement("ref_other", 9_710)],
    )
    finding = only(report, DriftKind.UNSETTLED_PAST_SLA)
    assert (finding.severity, finding.payment_id) == (Severity.BREAK, "pay_1")


def test_the_settlement_join_is_on_the_psp_reference_and_not_the_payment_id() -> None:
    # Keyed by the payment id on purpose. It must not match; the two are different keys and a
    # join on the wrong one either matches nothing or, worse, matches by luck.
    report = run(
        ledger=[local(authorized=10_000, captured=10_000)],
        provider=[remote(authorized=10_000, captured=10_000)],
        settlement=[settlement("pay_1", 9_710)],
    )
    kinds = [finding.kind for finding in report.findings]
    assert kinds == [DriftKind.UNKNOWN_REMOTE_ROW]
