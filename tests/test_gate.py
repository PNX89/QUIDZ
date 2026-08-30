from __future__ import annotations

import typing
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


def test_blocking_by_currency_names_the_currencies_rather_than_the_payments() -> None:
    # GateDecision documents what blocked_ids holds under each scope, and the payment scope was
    # the only one anything ran, so the other two branches were prose with code behind them.
    decision = gate(
        report(
            finding(payment_id="pay_eur", currency="EUR"),
            finding(payment_id="pay_jpy", currency="JPY"),
        ),
        policy=GatePolicy(scope="currency"),
        now=NOW,
    )
    assert (decision.outbound_blocked, decision.blocked_ids) == (True, ("EUR", "JPY"))


def test_blocking_the_batch_names_the_batch_and_nothing_finer() -> None:
    # The whole payout stops, so naming the payments would invite somebody to release the rest.
    decision = gate(
        report(finding(payment_id="pay_1"), finding(payment_id="pay_2")),
        policy=GatePolicy(scope="batch"),
        now=NOW,
    )
    assert (decision.outbound_blocked, decision.blocked_ids) == (True, ("*",))


def test_every_scope_the_policy_offers_has_a_case_of_its_own_above() -> None:
    """A fourth scope must not be free to add.

    The three names are written out here rather than read off the annotation and fed back into
    a loop: a test parametrised from the type it is checking covers one case fewer the moment
    somebody deletes an entry, and stays green while it does it.
    """
    scope = typing.get_type_hints(GatePolicy)["scope"]
    assert set(typing.get_args(scope)) == {"payment", "currency", "batch"}


def test_a_break_below_both_materiality_thresholds_does_not_block() -> None:
    decision = gate(
        report(finding(DriftKind.AMOUNT_MISMATCH, Severity.BREAK, delta=100)),
        policy=GatePolicy(materiality_count=5, materiality_value_minor=10_000),
        now=NOW,
    )
    assert (decision.outbound_blocked, decision.blocked_ids) == (False, ())


def test_two_immaterial_breaks_in_different_currencies_do_not_pool_into_a_material_one() -> None:
    """A total across currencies is the addition quidz.money exists to refuse.

    Neither of these crosses the value threshold on its own, and 6000 JPY and 5000 EUR are not
    11000 of anything: the ISO exponent alone is 0 for one and 2 for the other. Summed, they
    blocked two payments neither of which was material, which is the availability incident the
    threshold is there to avoid.
    """
    jpy = finding(
        DriftKind.AMOUNT_MISMATCH, Severity.BREAK, payment_id="pay_jpy", delta=-6000, currency="JPY"
    )
    eur = finding(
        DriftKind.AMOUNT_MISMATCH, Severity.BREAK, payment_id="pay_eur", delta=5000, currency="EUR"
    )
    policy = GatePolicy(materiality_count=5, materiality_value_minor=10_000)
    decision = gate(report(jpy, eur), policy=policy, now=NOW)
    assert (decision.outbound_blocked, decision.blocked_ids) == (False, ())


def test_a_currency_over_the_threshold_blocks_and_the_rationale_states_each_total() -> None:
    """The same two findings with the JPY one over the line on its own.

    The rationale carries a figure per currency rather than one number, because one number is
    a different amount of real money in every currency it is compared against: 10000 minor
    units is 100.00 EUR, 10.000 BHD and 10000 VND, and the last of those is small change.
    """
    jpy = finding(
        DriftKind.AMOUNT_MISMATCH,
        Severity.BREAK,
        payment_id="pay_jpy",
        delta=-12_000,
        currency="JPY",
    )
    eur = finding(
        DriftKind.AMOUNT_MISMATCH, Severity.BREAK, payment_id="pay_eur", delta=5000, currency="EUR"
    )
    policy = GatePolicy(materiality_count=5, materiality_value_minor=10_000)
    decision = gate(report(jpy, eur), policy=policy, now=NOW)
    assert (decision.outbound_blocked, decision.blocked_ids) == (True, ("pay_eur", "pay_jpy"))
    # The clause that carries the figures, not the sentence: every rationale contains digits.
    exposure = decision.rationale.split("exposure ", 1)[1].split(";", 1)[0]
    assert exposure == "12000 JPY, 5000 EUR"


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
