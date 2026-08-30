"""Two source reconciliation and a scoped fail closed gate.

Source A is the provider's payment list, joined to the ledger on the payment id. Source B is
the settlement report, joined on the psp reference, which is a different join on a different
key. That is the whole reason reconciliation here is two source: settlement is a fact posted
from a file, not an event, because neither Stripe nor Adyen sends a per payment settled
webhook.
https://docs.adyen.com/reporting/settlement-reconciliation/transaction-level/settlement-details-report

The output is an exception report, which is what the tooling in this domain is actually
called, and the gate it feeds blocks outbound money movement only. Ingest is never blocked:
stopping ingest widens the very gap the gate exists to close.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Literal

from quidz.model import PaymentState, derive_status, state_rank

__all__ = [
    "DEFAULT_SEVERITIES",
    "BreakGlass",
    "Direction",
    "DriftKind",
    "ExceptionReport",
    "Finding",
    "GateDecision",
    "GatePolicy",
    "RemotePayment",
    "SettlementRow",
    "Severity",
    "gate",
    "reconcile",
    "to_json",
    "to_text",
]


class Severity(StrEnum):
    INFO = "info"
    WARN = "warn"
    BREAK = "break"
    CRITICAL = "critical"


class Direction(StrEnum):
    NONE = "none"
    LOCAL_AHEAD = "local_ahead"
    REMOTE_AHEAD = "remote_ahead"


class DriftKind(StrEnum):
    MISSING_LOCALLY = "missing_locally"
    MISSING_REMOTELY = "missing_remotely"
    DUPLICATE_LOCAL = "duplicate_local"
    DUPLICATE_REMOTE = "duplicate_remote"
    AMOUNT_MISMATCH = "amount_mismatch"
    PARTIAL_STATE_DIVERGENCE = "partial_state_divergence"
    STATUS_MISMATCH = "status_mismatch"
    CURRENCY_MISMATCH = "currency_mismatch"
    IN_FLIGHT = "in_flight"
    UNKNOWN_REMOTE_ROW = "unknown_remote_row"
    FEE_GROSS_NET = "fee_gross_net"
    UNSETTLED_PAST_SLA = "unsettled_past_sla"
    PARKED_STALE = "parked_stale"


# The provider moved money against a payment this ledger has no record of at all.
SEVERITY_MISSING_LOCALLY = Severity.CRITICAL
# The ledger recorded an effect the provider does not show; real, but the exposure is bounded.
SEVERITY_MISSING_REMOTELY = Severity.BREAK
# Two aggregates for one reference means every later total on that payment is wrong.
SEVERITY_DUPLICATE_LOCAL = Severity.BREAK
# Two provider records for one order is a double authorization, real money and expensive.
SEVERITY_DUPLICATE_REMOTE = Severity.CRITICAL
# The net position differs, so the books and the provider disagree about money.
SEVERITY_AMOUNT_MISMATCH = Severity.BREAK
# The net position agrees and only the split does not, so nothing is missing yet.
SEVERITY_PARTIAL_STATE_DIVERGENCE = Severity.WARN
# A status disagreement is a process break; the direction is what makes it actionable.
SEVERITY_STATUS_MISMATCH = Severity.BREAK
# Comparing across currencies is never a legitimate reconciliation, so it stops the line.
SEVERITY_CURRENCY_MISMATCH = Severity.CRITICAL
# Younger than the grace window, so an ordinary in flight item, the largest bucket in any run.
SEVERITY_IN_FLIGHT = Severity.INFO
# A provider side row that cannot be joined at all; worth a look, not a money break.
SEVERITY_UNKNOWN_REMOTE_ROW = Severity.WARN
# Fees, interchange and FX mean settlement net almost never equals capture gross.
SEVERITY_FEE_WITHIN_TOLERANCE = Severity.INFO
SEVERITY_FEE_OUTSIDE_TOLERANCE = Severity.BREAK
# Captured, past the settlement SLA and still absent from the file: money that has not landed.
SEVERITY_UNSETTLED_PAST_SLA = Severity.BREAK
# Unbounded parking is a silent loss channel, the mirror image of an unbounded retry.
SEVERITY_PARKED_STALE = Severity.BREAK

DEFAULT_SEVERITIES: Mapping[DriftKind, Severity] = MappingProxyType(
    {
        DriftKind.MISSING_LOCALLY: SEVERITY_MISSING_LOCALLY,
        DriftKind.MISSING_REMOTELY: SEVERITY_MISSING_REMOTELY,
        DriftKind.DUPLICATE_LOCAL: SEVERITY_DUPLICATE_LOCAL,
        DriftKind.DUPLICATE_REMOTE: SEVERITY_DUPLICATE_REMOTE,
        DriftKind.AMOUNT_MISMATCH: SEVERITY_AMOUNT_MISMATCH,
        DriftKind.PARTIAL_STATE_DIVERGENCE: SEVERITY_PARTIAL_STATE_DIVERGENCE,
        DriftKind.STATUS_MISMATCH: SEVERITY_STATUS_MISMATCH,
        DriftKind.CURRENCY_MISMATCH: SEVERITY_CURRENCY_MISMATCH,
        DriftKind.IN_FLIGHT: SEVERITY_IN_FLIGHT,
        DriftKind.UNKNOWN_REMOTE_ROW: SEVERITY_UNKNOWN_REMOTE_ROW,
        DriftKind.FEE_GROSS_NET: SEVERITY_FEE_WITHIN_TOLERANCE,
        DriftKind.UNSETTLED_PAST_SLA: SEVERITY_UNSETTLED_PAST_SLA,
        DriftKind.PARKED_STALE: SEVERITY_PARKED_STALE,
    }
)

_SEVERITY_ORDER: Mapping[Severity, int] = MappingProxyType(
    {Severity.INFO: 0, Severity.WARN: 1, Severity.BREAK: 2, Severity.CRITICAL: 3}
)

# Handed to GatePolicy through default_factory rather than sat on the field as a default.
# Python 3.11 rejects any dataclass default whose class is unhashable, and mappingproxy is one,
# so a bare default here raises ValueError at import time on the oldest supported interpreter.
# 3.12 narrowed that check to list, dict and set, which is why the failure hides on 3.12 and up.
_NO_SEVERITY_OVERRIDES: Mapping[DriftKind, Severity] = MappingProxyType({})


@dataclass(frozen=True, slots=True)
class RemotePayment:
    payment_id: str
    provider_ref: str
    currency: str
    authorized_minor: int
    captured_minor: int
    refunded_minor: int
    status: str
    created_at: float


@dataclass(frozen=True, slots=True)
class SettlementRow:
    psp_reference: str
    gross_minor: int
    fee_minor: int
    net_minor: int
    currency: str
    payout_date: str
    journal_type: str


@dataclass(frozen=True, slots=True)
class BreakGlass:
    approver: str
    reason: str
    expires_at: float
    scope_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class GatePolicy:
    in_flight_grace_seconds: float = 3600.0
    settlement_sla_seconds: float = 259200.0
    park_max_age_seconds: float = 900.0
    fee_tolerance_bps: int = 500
    materiality_count: int = 5
    materiality_value_minor: int = 10_000
    scope: Literal["payment", "currency", "batch"] = "payment"
    break_glass: BreakGlass | None = None
    severity_overrides: Mapping[DriftKind, Severity] = field(
        default_factory=lambda: _NO_SEVERITY_OVERRIDES
    )


@dataclass(frozen=True, slots=True)
class Finding:
    kind: DriftKind
    severity: Severity
    direction: Direction
    payment_id: str | None
    provider_ref: str | None
    local: Mapping[str, object] | None
    remote: Mapping[str, object] | None
    delta_minor: int | None
    currency: str | None
    age_seconds: float | None
    detail: str


@dataclass(frozen=True, slots=True)
class ExceptionReport:
    generated_at: float
    findings: tuple[Finding, ...]
    counts_by_severity: Mapping[Severity, int]
    counters: Mapping[str, int]


@dataclass(frozen=True, slots=True)
class GateDecision:
    outbound_blocked: bool
    # Always False. Halting ingest while the books disagree widens the gap it is meant to close.
    ingest_blocked: bool
    # Payment ids under scope=payment, currency codes under scope=currency, ("*",) for a batch.
    blocked_ids: tuple[str, ...]
    suppressed_by: BreakGlass | None
    rationale: str


def _vector(authorized: int, captured: int, refunded: int) -> dict[str, object]:
    return {"authorized_minor": authorized, "captured_minor": captured, "refunded_minor": refunded}


def _rank_or_none(status: str) -> int | None:
    try:
        return state_rank(status)
    except ValueError:
        return None


def _direction(local_status: str, remote_status: str) -> Direction:
    local_rank, remote_rank = _rank_or_none(local_status), _rank_or_none(remote_status)
    if local_rank is None or remote_rank is None or local_rank == remote_rank:
        return Direction.NONE
    return Direction.LOCAL_AHEAD if local_rank > remote_rank else Direction.REMOTE_AHEAD


class _Collector:
    def __init__(self, policy: GatePolicy) -> None:
        self.policy = policy
        self.findings: list[Finding] = []

    def add(
        self,
        kind: DriftKind,
        detail: str,
        *,
        severity: Severity | None = None,
        direction: Direction = Direction.NONE,
        payment_id: str | None = None,
        provider_ref: str | None = None,
        local: Mapping[str, object] | None = None,
        remote: Mapping[str, object] | None = None,
        delta_minor: int | None = None,
        currency: str | None = None,
        age_seconds: float | None = None,
    ) -> None:
        base = DEFAULT_SEVERITIES[kind] if severity is None else severity
        self.findings.append(
            Finding(
                kind=kind,
                severity=self.policy.severity_overrides.get(kind, base),
                direction=direction,
                payment_id=payment_id,
                provider_ref=provider_ref,
                local=local,
                remote=remote,
                delta_minor=delta_minor,
                currency=currency,
                age_seconds=age_seconds,
                detail=detail,
            )
        )


def _compare_pair(
    out: _Collector, local: PaymentState, remote: RemotePayment, *, age: float, grace: float
) -> None:
    if local.currency != remote.currency:
        out.add(
            DriftKind.CURRENCY_MISMATCH,
            f"ledger holds {local.payment_id} in {local.currency}, the provider reports "
            f"{remote.currency}; amounts in different currencies are not comparable",
            payment_id=local.payment_id,
            provider_ref=remote.provider_ref,
            currency=local.currency,
            age_seconds=age,
        )
        return

    local_vector = (local.authorized_minor, local.captured_minor, local.refunded_minor)
    remote_vector = (remote.authorized_minor, remote.captured_minor, remote.refunded_minor)
    local_status = derive_status(local)
    if local_vector == remote_vector and local_status == remote.status:
        return

    if age < grace:
        # The single largest source of false CRITICALs is treating an ordinary in flight item
        # as drift, so the timing bucket is checked before any divergence is classified.
        out.add(
            DriftKind.IN_FLIGHT,
            f"{local.payment_id} diverges but is {int(age)}s old, inside the "
            f"{int(grace)}s grace window",
            payment_id=local.payment_id,
            provider_ref=remote.provider_ref,
            currency=local.currency,
            age_seconds=age,
        )
        return

    if local_vector != remote_vector:
        local_net = local.captured_minor - local.refunded_minor
        remote_net = remote.captured_minor - remote.refunded_minor
        # A scalar comparison of the net position sees nothing here, which is exactly why the
        # partial bucket exists: same money, different composition.
        partial = local_net == remote_net and local.authorized_minor == remote.authorized_minor
        kind = DriftKind.PARTIAL_STATE_DIVERGENCE if partial else DriftKind.AMOUNT_MISMATCH
        detail = (
            f"{local.payment_id} capture and refund split disagrees at an equal net position"
            if partial
            else f"{local.payment_id} net position differs by {local_net - remote_net} "
            f"{local.currency} minor units"
        )
        out.add(
            kind,
            detail,
            payment_id=local.payment_id,
            provider_ref=remote.provider_ref,
            local=_vector(*local_vector),
            remote=_vector(*remote_vector),
            delta_minor=0 if partial else local_net - remote_net,
            currency=local.currency,
            age_seconds=age,
        )

    if local_status != remote.status:
        out.add(
            DriftKind.STATUS_MISMATCH,
            f"{local.payment_id} is {local_status} in the ledger and {remote.status} "
            "at the provider",
            direction=_direction(local_status, remote.status),
            payment_id=local.payment_id,
            provider_ref=remote.provider_ref,
            local={"status": local_status},
            remote={"status": remote.status},
            currency=local.currency,
            age_seconds=age,
        )


def _reconcile_events(
    out: _Collector,
    local_by_id: Mapping[str, list[PaymentState]],
    remote_by_id: Mapping[str, list[RemotePayment]],
    *,
    now: float,
    policy: GatePolicy,
) -> None:
    for payment_id in sorted(set(local_by_id) | set(remote_by_id)):
        locals_ = local_by_id.get(payment_id, [])
        remotes = remote_by_id.get(payment_id, [])
        if len(locals_) > 1:
            out.add(
                DriftKind.DUPLICATE_LOCAL,
                f"{len(locals_)} local aggregates carry the payment id {payment_id}",
                payment_id=payment_id,
                provider_ref=remotes[0].provider_ref if remotes else None,
                currency=locals_[0].currency,
            )
        if len(remotes) > 1:
            out.add(
                DriftKind.DUPLICATE_REMOTE,
                f"{len(remotes)} provider records carry the payment id {payment_id}, "
                "which is a double authorization",
                payment_id=payment_id,
                provider_ref=remotes[0].provider_ref,
                currency=remotes[0].currency,
                age_seconds=now - remotes[0].created_at,
            )
        if not remotes:
            local = locals_[0]
            out.add(
                DriftKind.MISSING_REMOTELY,
                f"{payment_id} exists in the ledger and not in the provider payment list",
                payment_id=payment_id,
                local=_vector(local.authorized_minor, local.captured_minor, local.refunded_minor),
                delta_minor=local.captured_minor - local.refunded_minor,
                currency=local.currency,
            )
            continue
        remote = remotes[0]
        age = now - remote.created_at
        if not locals_:
            in_flight = age < policy.in_flight_grace_seconds
            out.add(
                DriftKind.IN_FLIGHT if in_flight else DriftKind.MISSING_LOCALLY,
                f"{payment_id} is at the provider and not in the ledger, {int(age)}s old",
                payment_id=payment_id,
                provider_ref=remote.provider_ref,
                remote=_vector(
                    remote.authorized_minor, remote.captured_minor, remote.refunded_minor
                ),
                delta_minor=remote.captured_minor - remote.refunded_minor,
                currency=remote.currency,
                age_seconds=age,
            )
            continue
        _compare_pair(out, locals_[0], remote, age=age, grace=policy.in_flight_grace_seconds)


def _reconcile_settlement(
    out: _Collector,
    local_by_id: Mapping[str, list[PaymentState]],
    remote_by_id: Mapping[str, list[RemotePayment]],
    settlement: Sequence[SettlementRow],
    *,
    now: float,
    policy: GatePolicy,
) -> None:
    # The settlement join is on the psp reference, never on the payment id. They are different
    # keys and joining on the wrong one silently matches nothing, or worse, matches by luck.
    by_ref = {r.provider_ref: r for group in remote_by_id.values() for r in group if r.provider_ref}
    settled: set[str] = set()

    for row in sorted(settlement, key=lambda r: r.psp_reference):
        remote = by_ref.get(row.psp_reference)
        locals_ = local_by_id.get(remote.payment_id, []) if remote is not None else []
        if remote is None or not locals_:
            out.add(
                DriftKind.UNKNOWN_REMOTE_ROW,
                f"settlement row {row.psp_reference} joins to no ledger payment",
                provider_ref=row.psp_reference,
                remote={"net_minor": row.net_minor, "journal_type": row.journal_type},
                currency=row.currency,
            )
            continue
        local = locals_[0]
        settled.add(row.psp_reference)
        if row.currency != local.currency:
            out.add(
                DriftKind.CURRENCY_MISMATCH,
                f"settlement row {row.psp_reference} is in {row.currency} and the ledger holds "
                f"{local.payment_id} in {local.currency}",
                payment_id=local.payment_id,
                provider_ref=row.psp_reference,
                currency=local.currency,
            )
            continue
        gross = local.captured_minor
        delta = row.net_minor - gross
        bps = 10_000 if gross <= 0 else round(abs(delta) * 10_000 / gross)
        within = bps <= policy.fee_tolerance_bps
        out.add(
            DriftKind.FEE_GROSS_NET,
            f"settlement net {row.net_minor} against capture gross {gross} on "
            f"{local.payment_id} is {bps} bps, tolerance {policy.fee_tolerance_bps} bps",
            severity=(SEVERITY_FEE_WITHIN_TOLERANCE if within else SEVERITY_FEE_OUTSIDE_TOLERANCE),
            payment_id=local.payment_id,
            provider_ref=row.psp_reference,
            local={"captured_minor": gross},
            remote={"net_minor": row.net_minor, "fee_minor": row.fee_minor},
            delta_minor=delta,
            currency=local.currency,
        )

    for payment_id, locals_ in sorted(local_by_id.items()):
        local = locals_[0]
        remotes = remote_by_id.get(payment_id, [])
        if local.captured_minor <= 0 or not remotes:
            continue
        remote = remotes[0]
        age = now - remote.created_at
        if remote.provider_ref in settled or age <= policy.settlement_sla_seconds:
            continue
        out.add(
            DriftKind.UNSETTLED_PAST_SLA,
            f"{payment_id} captured {local.captured_minor} {local.currency}, {int(age)}s old "
            f"and absent from the settlement report",
            payment_id=payment_id,
            provider_ref=remote.provider_ref,
            local={"captured_minor": local.captured_minor},
            delta_minor=local.captured_minor,
            currency=local.currency,
            age_seconds=age,
        )


def reconcile(
    *,
    local: Sequence[PaymentState],
    remote: Sequence[RemotePayment],
    settlement: Sequence[SettlementRow],
    parked: Sequence[tuple[str, float]],
    now: float,
    policy: GatePolicy,
    counters: Mapping[str, int] | None = None,
) -> ExceptionReport:
    """Classify every disagreement between the ledger, the payment list and the settlement file.

    `parked` is (payment_id, received_at) per delivery still parked in the inbox.
    """
    out = _Collector(policy)

    local_by_id: dict[str, list[PaymentState]] = {}
    for state in local:
        local_by_id.setdefault(state.payment_id, []).append(state)
    remote_by_id: dict[str, list[RemotePayment]] = {}
    for row in remote:
        if not row.payment_id:
            out.add(
                DriftKind.UNKNOWN_REMOTE_ROW,
                f"provider row {row.provider_ref} carries no payment id to join on",
                provider_ref=row.provider_ref,
                remote=_vector(row.authorized_minor, row.captured_minor, row.refunded_minor),
                currency=row.currency,
                age_seconds=now - row.created_at,
            )
            continue
        remote_by_id.setdefault(row.payment_id, []).append(row)

    _reconcile_events(out, local_by_id, remote_by_id, now=now, policy=policy)
    _reconcile_settlement(out, local_by_id, remote_by_id, settlement, now=now, policy=policy)

    for payment_id, parked_since in sorted(parked):
        age = now - parked_since
        stale = age > policy.park_max_age_seconds
        out.add(
            DriftKind.PARKED_STALE if stale else DriftKind.IN_FLIGHT,
            f"a delivery for {payment_id} has been parked {int(age)}s waiting for its "
            f"prerequisite, limit {int(policy.park_max_age_seconds)}s",
            payment_id=payment_id,
            age_seconds=age,
        )

    findings = tuple(
        sorted(
            out.findings,
            key=lambda f: (
                -_SEVERITY_ORDER[f.severity],
                str(f.kind),
                f.payment_id or "",
                f.provider_ref or "",
            ),
        )
    )
    counts = {severity: 0 for severity in Severity}
    for finding in findings:
        counts[finding.severity] += 1
    unresolved = [f.age_seconds or 0.0 for f in findings if f.severity is not Severity.INFO]
    merged = dict(counters or {})
    merged.update(
        drift_info=counts[Severity.INFO],
        drift_warn=counts[Severity.WARN],
        drift_break=counts[Severity.BREAK],
        drift_critical=counts[Severity.CRITICAL],
        oldest_unresolved_drift_seconds=int(max(unresolved, default=0.0)),
    )
    return ExceptionReport(
        generated_at=now,
        findings=findings,
        counts_by_severity=MappingProxyType(counts),
        counters=MappingProxyType(merged),
    )


def _exposure_by_currency(breaks: Sequence[Finding]) -> dict[str, int]:
    """Drift totals per currency, never one number across them.

    Minor units of two currencies are not units of anything: the ISO exponent alone is 0 for
    JPY and 3 for BHD before an exchange rate is considered, so adding them is precisely the
    arithmetic money.add refuses, and quidz.money exists to keep that refusal in one place. A
    single number would also pool unrelated drift in unrelated currencies into a total that
    blocks payments none of which crossed the threshold on its own.

    No reference currency is converted to, deliberately: rates are listed under Limitations as
    absent, and a materiality threshold read per currency is honest without one.
    """
    totals: dict[str, int] = {}
    for finding in breaks:
        if finding.delta_minor and finding.currency:
            totals[finding.currency] = totals.get(finding.currency, 0) + abs(finding.delta_minor)
    return totals


def _exposure_text(exposure: Mapping[str, int]) -> str:
    """Largest first, so the rationale opens on the number that decided it."""
    if not exposure:
        return "no measured exposure"
    ordered = sorted(exposure.items(), key=lambda item: (-item[1], item[0]))
    return ", ".join(f"{total} {currency}" for currency, total in ordered)


def gate(report: ExceptionReport, *, policy: GatePolicy, now: float) -> GateDecision:
    """Decide whether outbound money movement is blocked, and for exactly which ids.

    A CRITICAL stands on its own: money moved that the ledger cannot account for is not a
    question of size. BREAK findings have to clear a materiality threshold by count or by
    value first, because a global halt on one small drifting row is a self inflicted
    availability incident.

    The count is a property of the finding set and is read across all of them. The value is
    money, so it is read against one currency at a time and the threshold means what it says in
    each of them.
    """
    criticals = [f for f in report.findings if f.severity is Severity.CRITICAL]
    breaks = [f for f in report.findings if f.severity is Severity.BREAK]
    exposure = _exposure_by_currency(breaks)
    material = len(breaks) >= policy.materiality_count or any(
        total >= policy.materiality_value_minor for total in exposure.values()
    )
    effective = criticals + (breaks if material else [])

    if policy.scope == "batch":
        ids: tuple[str, ...] = ("*",) if effective else ()
    elif policy.scope == "currency":
        ids = tuple(sorted({f.currency for f in effective if f.currency}))
    else:
        ids = tuple(sorted({f.payment_id for f in effective if f.payment_id}))
    if effective and not ids:
        ids = tuple(sorted({f.provider_ref for f in effective if f.provider_ref})) or ("*",)

    suppressed: BreakGlass | None = None
    glass = policy.break_glass
    if glass is not None and glass.expires_at > now and ids:
        remaining = tuple(i for i in ids if i not in glass.scope_ids)
        if remaining != ids:
            suppressed, ids = glass, remaining

    blocked = bool(effective) and bool(ids)
    if not effective:
        rationale = (
            f"{len(breaks)} break findings below the materiality thresholds "
            f"(count {policy.materiality_count}, value {policy.materiality_value_minor})"
            if breaks
            else "no findings at or above break"
        )
    elif blocked:
        rationale = (
            f"{len(criticals)} critical and {len(breaks) if material else 0} break findings, "
            f"exposure {_exposure_text(exposure)}; outbound blocked for {len(ids)} id(s) "
            f"under scope={policy.scope}"
        )
    else:
        approver = suppressed.approver if suppressed else "unknown"
        rationale = (
            f"{len(effective)} blocking findings suppressed by break glass from {approver} "
            f"until {suppressed.expires_at if suppressed else 0}"
        )
    return GateDecision(
        outbound_blocked=blocked,
        ingest_blocked=False,
        blocked_ids=ids,
        suppressed_by=suppressed,
        rationale=rationale,
    )


def _finding_json(finding: Finding) -> dict[str, object]:
    return {
        "kind": str(finding.kind),
        "severity": str(finding.severity),
        "direction": str(finding.direction),
        "payment_id": finding.payment_id,
        "provider_ref": finding.provider_ref,
        "local": None if finding.local is None else dict(finding.local),
        "remote": None if finding.remote is None else dict(finding.remote),
        "delta_minor": finding.delta_minor,
        "currency": finding.currency,
        "age_seconds": None if finding.age_seconds is None else round(finding.age_seconds, 3),
        "detail": finding.detail,
    }


def to_json(report: ExceptionReport, decision: GateDecision) -> str:
    """Sorted keys and a stable finding order, so the report diffs cleanly in CI."""
    glass = decision.suppressed_by
    payload = {
        "generated_at": report.generated_at,
        "counts_by_severity": {str(k): v for k, v in report.counts_by_severity.items()},
        "counters": dict(report.counters),
        "findings": [_finding_json(f) for f in report.findings],
        "gate": {
            "outbound_blocked": decision.outbound_blocked,
            "ingest_blocked": decision.ingest_blocked,
            "blocked_ids": list(decision.blocked_ids),
            "rationale": decision.rationale,
            "suppressed_by": None
            if glass is None
            else {
                "approver": glass.approver,
                "reason": glass.reason,
                "expires_at": glass.expires_at,
                "scope_ids": list(glass.scope_ids),
            },
        },
    }
    return json.dumps(payload, sort_keys=True, indent=2)


def to_text(report: ExceptionReport, decision: GateDecision) -> str:
    counts = " ".join(
        f"{str(s).upper()}={report.counts_by_severity[s]}"
        for s in (Severity.CRITICAL, Severity.BREAK, Severity.WARN, Severity.INFO)
    )
    lines = [
        "EXCEPTION REPORT",
        f"  generated_at  {report.generated_at:.0f}",
        f"  findings      {len(report.findings)}  ({counts})",
        f"  outbound      {'BLOCKED' if decision.outbound_blocked else 'open'}",
        f"  ingest        {'BLOCKED' if decision.ingest_blocked else 'open'}",
        f"  blocked_ids   {', '.join(decision.blocked_ids) or '-'}",
        f"  rationale     {decision.rationale}",
    ]
    if report.findings:
        lines.append("")
        lines.append(f"  {'SEVERITY':<9}{'KIND':<26}{'PAYMENT':<18}{'DELTA':>10}  DETAIL")
        for finding in report.findings:
            delta = "-" if finding.delta_minor is None else str(finding.delta_minor)
            direction = "" if finding.direction is Direction.NONE else f" [{finding.direction}]"
            lines.append(
                f"  {str(finding.severity).upper():<9}{finding.kind!s:<26}"
                f"{finding.payment_id or finding.provider_ref or '-':<18}{delta:>10}  "
                f"{finding.detail}{direction}"
            )
    return "\n".join(lines)
