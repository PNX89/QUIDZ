"""A synthetic provider: signed Stripe and Adyen deliveries, a payment list, a settlement file.

Every secret here is a literal placeholder, nothing reaches a network, and no card like data
is generated: identifiers are letter seeded tokens and a test sweeps every payload for runs of
13 to 19 digits.

Deliveries are signed at receipt time rather than at event time, which is what Stripe does on
every retry, so the signature timestamp and the event's own created value are different
numbers on purpose.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import random
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

from quidz.events import Provider
from quidz.model import PaymentState, derive_status
from quidz.reconcile import RemotePayment, SettlementRow
from quidz.verify import adyen_signing_string

__all__ = ["SCENARIOS", "BreakMode", "Delivery", "Simulator"]

SCENARIOS: tuple[str, ...] = ("happy", "adversarial")

_LETTERS = "ABCDEFGHJKLMNPQRSTVWXYZ"
_ALNUM = _LETTERS + _LETTERS.lower() + "23456789"
_MERCHANT_ACCOUNT = "QUIDZ_SYNTHETIC"
# Basis points charged by the synthetic acquirer, plus a fixed authorization fee in minor units.
_FEE_BPS = 290
_FEE_FIXED_MINOR = 25


class BreakMode(StrEnum):
    DUPLICATE = "duplicate"
    REPLAY = "replay"
    REORDER = "reorder"
    DROP = "drop"
    AMOUNT_MISMATCH = "amount-mismatch"
    TAMPER = "tamper"


@dataclass(frozen=True, slots=True)
class Delivery:
    provider: Provider
    identity: str
    raw: bytes
    headers: dict[str, str]


@dataclass(frozen=True, slots=True)
class _Plan:
    provider: Provider
    currency: str
    capture_pct: int
    refund_pct: int
    capture_failed: bool
    age_days: float
    # The provider captured, and the webhook saying so has not arrived. Nothing is broken;
    # this is the ordinary in flight case that dominates every real reconciliation run.
    capture_webhook_pending: bool = False


# Index order is load bearing: each break mode targets a fixed payment so the demo output is
# explainable line by line rather than randomly scattered.
_PLAN: tuple[_Plan, ...] = (
    _Plan(Provider.STRIPE, "EUR", 100, 0, False, 4.0),
    _Plan(Provider.STRIPE, "USD", 66, 33, False, 4.0),
    _Plan(Provider.STRIPE, "JPY", 100, 0, False, 4.0),
    _Plan(Provider.ADYEN, "EUR", 100, 0, False, 4.0),
    _Plan(Provider.ADYEN, "EUR", 100, 0, True, 0.02, capture_webhook_pending=True),
    _Plan(Provider.ADYEN, "GBP", 100, 100, False, 4.0),
)
_REORDER_INDEX, _SKEW_INDEX, _TAMPER_INDEX, _DROP_INDEX, _DUPLICATE_INDEX = 0, 1, 2, 3, 5


@dataclass(frozen=True, slots=True)
class _Booked:
    payment_id: str
    provider: Provider
    currency: str
    authorized: int
    captured: int
    refunded: int
    capture_failed: bool
    capture_webhook_pending: bool
    created_at: float
    auth_ref: str
    capture_ref: str
    refund_ref: str


@dataclass
class Simulator:
    seed: int = 0
    stripe_secrets: tuple[bytes, ...] = (b"whsec_synthetic_primary", b"whsec_synthetic_previous")
    adyen_hmac_key_hex: str = "00" * 32
    # Receipt time for the whole batch. Injected rather than read from the clock so two runs
    # with the same seed are byte identical.
    started_at: float = 1_787_011_200.0
    _book: tuple[_Booked, ...] = field(default_factory=tuple, init=False, repr=False)
    _breaks: frozenset[BreakMode] = field(default_factory=frozenset, init=False, repr=False)
    _scenario: str = field(default="happy", init=False, repr=False)

    @property
    def applied_breaks(self) -> tuple[BreakMode, ...]:
        """The break modes in force, including the ones the adversarial scenario implies."""
        return tuple(sorted(self._breaks))

    def deliveries(self, scenario: str, *, breaks: Sequence[BreakMode] = ()) -> list[Delivery]:
        if scenario not in SCENARIOS:
            raise ValueError(f"unknown scenario {scenario!r}; expected one of {SCENARIOS}")
        modes = set(breaks) | (set(BreakMode) if scenario == "adversarial" else set())
        self._scenario, self._breaks = scenario, frozenset(modes)
        self._book = self._build_book()
        return self._build_deliveries()

    def remote_payments(self) -> list[RemotePayment]:
        """The provider's payment list, source A of the reconciliation."""
        self._ensure_book()
        out: list[RemotePayment] = []
        for index, booked in enumerate(self._book):
            captured = booked.captured
            if BreakMode.AMOUNT_MISMATCH in self._breaks and index == _SKEW_INDEX:
                captured += 1000
            state = PaymentState(
                payment_id=booked.payment_id,
                currency=booked.currency,
                authorized_minor=booked.authorized,
                captured_minor=captured,
                capture_failed_minor=0
                if captured
                else (booked.authorized if booked.capture_failed else 0),
                refunded_minor=booked.refunded,
            )
            out.append(
                RemotePayment(
                    payment_id=booked.payment_id,
                    provider_ref=booked.capture_ref if captured else booked.auth_ref,
                    currency=booked.currency,
                    authorized_minor=booked.authorized,
                    captured_minor=captured,
                    refunded_minor=booked.refunded,
                    status=derive_status(state),
                    created_at=booked.created_at,
                )
            )
        return out

    def settlement_report(self) -> list[SettlementRow]:
        """The settlement file, source B, keyed by psp reference and never by payment id."""
        self._ensure_book()
        rows: list[SettlementRow] = []
        for index, booked in enumerate(self._book):
            # Nothing settles within half an hour, so a capture still in flight has no row.
            if booked.captured <= 0 or booked.capture_webhook_pending:
                continue
            if self._scenario == "adversarial" and index == _DUPLICATE_INDEX:
                # Held back on purpose: captured, past the SLA, absent from the file.
                continue
            fee = booked.captured * _FEE_BPS // 10_000 + _FEE_FIXED_MINOR
            rows.append(
                SettlementRow(
                    psp_reference=booked.capture_ref,
                    gross_minor=booked.captured,
                    fee_minor=fee,
                    net_minor=booked.captured - fee,
                    currency=booked.currency,
                    payout_date=self._iso(self.started_at + 86_400.0)[:10],
                    journal_type="Settled",
                )
            )
        if self._scenario == "adversarial":
            # Settlement files carry journal rows that join to no payment at all.
            rows.append(
                SettlementRow(
                    psp_reference="PAYOUT_BATCH_0001",
                    gross_minor=0,
                    fee_minor=0,
                    net_minor=0,
                    currency="EUR",
                    payout_date=self._iso(self.started_at + 86_400.0)[:10],
                    journal_type="MerchantPayout",
                )
            )
        return rows

    def _ensure_book(self) -> None:
        if not self._book:
            self._book = self._build_book()

    def _build_book(self) -> tuple[_Booked, ...]:
        rng = random.Random(self.seed)
        book: list[_Booked] = []
        for index, plan in enumerate(_PLAN):
            # Kept above 3000 minor units so the synthetic acquirer's fixed fee stays inside
            # the default 500 bps tolerance for every seed rather than only for seed 0.
            authorized = rng.randrange(60, 400) * 50
            captured = authorized * plan.capture_pct // 100
            refunded = captured * plan.refund_pct // 100
            if plan.provider is Provider.STRIPE:
                payment_id = f"pi_{_token(rng, 16)}"
                refs = (payment_id, f"ch_{_token(rng, 16)}", f"re_{_token(rng, 16)}")
            else:
                # One merchant reference carries a colon on purpose: it is the field that
                # breaks a signer which joins the signed fields without escaping them.
                separator = ":" if index == _DUPLICATE_INDEX else "-"
                payment_id = f"order{separator}{_token(rng, 8).upper()}"
                refs = (_psp_reference(rng), _psp_reference(rng), _psp_reference(rng))
            book.append(
                _Booked(
                    payment_id=payment_id,
                    provider=plan.provider,
                    currency=plan.currency,
                    authorized=authorized,
                    captured=captured,
                    refunded=refunded,
                    capture_failed=plan.capture_failed,
                    capture_webhook_pending=plan.capture_webhook_pending,
                    created_at=self.started_at - plan.age_days * 86_400.0,
                    auth_ref=refs[0],
                    capture_ref=refs[1],
                    refund_ref=refs[2],
                )
            )
        return tuple(book)

    def _build_deliveries(self) -> list[Delivery]:
        out: list[Delivery] = []
        for index, booked in enumerate(self._book):
            batch = self._payment_deliveries(booked)
            if BreakMode.REORDER in self._breaks and index == _REORDER_INDEX and len(batch) > 1:
                batch[0], batch[1] = batch[1], batch[0]
            if BreakMode.DROP in self._breaks and index == _DROP_INDEX:
                batch = batch[1:]
            if BreakMode.TAMPER in self._breaks and index == _TAMPER_INDEX and len(batch) > 1:
                batch[1] = _tamper(batch[1])
            out.extend(batch)
        if BreakMode.DUPLICATE in self._breaks:
            out.extend(self._duplicate_deliveries())
        if BreakMode.REPLAY in self._breaks:
            out.append(self._replayed_authorization())
        return out

    def _payment_deliveries(self, booked: _Booked) -> list[Delivery]:
        created = int(booked.created_at)
        if booked.provider is Provider.STRIPE:
            batch = [
                self._stripe(
                    "payment_intent.amount_capturable_updated",
                    created,
                    {
                        "id": booked.payment_id,
                        "object": "payment_intent",
                        "amount": booked.authorized,
                        "amount_capturable": booked.authorized,
                        "currency": booked.currency.lower(),
                    },
                )
            ]
            if booked.captured and not booked.capture_webhook_pending:
                batch.append(
                    self._stripe(
                        "charge.captured",
                        created + 120,
                        {
                            "id": booked.capture_ref,
                            "object": "charge",
                            "payment_intent": booked.payment_id,
                            "amount": booked.authorized,
                            "amount_captured": booked.captured,
                            "currency": booked.currency.lower(),
                        },
                    )
                )
            if booked.refunded:
                batch.append(
                    self._stripe(
                        "charge.refund.updated",
                        created + 7_200,
                        {
                            "id": booked.refund_ref,
                            "object": "refund",
                            "payment_intent": booked.payment_id,
                            "amount": booked.refunded,
                            "currency": booked.currency.lower(),
                            "status": "succeeded",
                        },
                    )
                )
            return batch

        batch = [
            self._adyen(booked, booked.auth_ref, "AUTHORISATION", booked.authorized, created, "")
        ]
        if booked.capture_failed:
            batch.append(
                self._adyen(
                    booked,
                    booked.capture_ref,
                    "CAPTURE_FAILED",
                    booked.authorized,
                    created + 300,
                    booked.auth_ref,
                )
            )
        if booked.captured and not booked.capture_webhook_pending:
            batch.append(
                self._adyen(
                    booked,
                    booked.capture_ref,
                    "CAPTURE",
                    booked.captured,
                    created + 120,
                    booked.auth_ref,
                )
            )
        if booked.refunded:
            batch.append(
                self._adyen(
                    booked,
                    booked.refund_ref,
                    "REFUND",
                    booked.refunded,
                    created + 7_200,
                    booked.capture_ref,
                )
            )
        return batch

    def _duplicate_deliveries(self) -> list[Delivery]:
        """Both levels of the idempotency story, one per delivery.

        The first is a byte identical re-delivery, which the delivery identity catches at the
        door. The second is a distinct Event object describing the same charge, which only the
        UNIQUE(payment_id, kind, provider_ref) constraint catches. Stripe documents that second
        case: two Event objects for one underlying change. https://docs.stripe.com/webhooks
        """
        booked = self._book[_REORDER_INDEX]
        original = self._stripe(
            "charge.captured",
            int(booked.created_at) + 120,
            {
                "id": booked.capture_ref,
                "object": "charge",
                "payment_intent": booked.payment_id,
                "amount": booked.authorized,
                "amount_captured": booked.captured,
                "currency": booked.currency.lower(),
            },
        )
        second_event = self._stripe(
            "charge.captured",
            int(booked.created_at) + 121,
            {
                "id": booked.capture_ref,
                "object": "charge",
                "payment_intent": booked.payment_id,
                "amount": booked.authorized,
                "amount_captured": booked.captured,
                "currency": booked.currency.lower(),
            },
            salt="dup",
        )
        return [original, second_event]

    def _replayed_authorization(self) -> Delivery:
        """A correctly signed delivery captured earlier and re-sent outside the window."""
        booked = self._book[_REORDER_INDEX]
        return self._stripe(
            "payment_intent.amount_capturable_updated",
            int(booked.created_at),
            {
                "id": booked.payment_id,
                "object": "payment_intent",
                "amount": booked.authorized,
                "amount_capturable": booked.authorized,
                "currency": booked.currency.lower(),
            },
            signed_at=int(self.started_at) - 3_600,
        )

    def _stripe(
        self,
        event_type: str,
        created: int,
        obj: dict[str, object],
        *,
        salt: str = "",
        signed_at: int | None = None,
    ) -> Delivery:
        material = ":".join((event_type, str(obj["id"]), str(created), salt))
        event_id = f"evt_{_stable_token(material)}"
        payload = {"id": event_id, "type": event_type, "created": created, "data": {"object": obj}}
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        timestamp = int(self.started_at) if signed_at is None else signed_at
        signed = f"{timestamp}.".encode() + raw
        # One header carries a signature per active secret, which is what a rotation looks
        # like: the previous secret stays valid for up to 24 hours.
        parts = [f"t={timestamp}"] + [
            f"v1={hmac.new(secret, signed, hashlib.sha256).hexdigest()}"
            for secret in reversed(self.stripe_secrets)
        ]
        return Delivery(Provider.STRIPE, event_id, raw, {"Stripe-Signature": ",".join(parts)})

    def _adyen(
        self,
        booked: _Booked,
        psp: str,
        event_code: str,
        minor: int,
        created: int,
        original_reference: str,
    ) -> Delivery:
        item: dict[str, object] = {
            "pspReference": psp,
            "originalReference": original_reference,
            "merchantAccountCode": _MERCHANT_ACCOUNT,
            "merchantReference": booked.payment_id,
            "amount": {"value": minor, "currency": booked.currency},
            "eventCode": event_code,
            "success": "true",
            "eventDate": self._iso(float(created)),
            "paymentMethod": "visa",
        }
        fields = {
            "pspReference": psp,
            "originalReference": original_reference,
            "merchantAccountCode": _MERCHANT_ACCOUNT,
            "merchantReference": booked.payment_id,
            "value": str(minor),
            "currency": booked.currency,
            "eventCode": event_code,
            "success": "true",
        }
        item["additionalData"] = {"hmacSignature": self._adyen_signature(fields)}
        raw = json.dumps(item, sort_keys=True, separators=(",", ":")).encode()
        return Delivery(Provider.ADYEN, f"{psp}:{event_code}", raw, {})

    def _adyen_signature(self, fields: dict[str, str]) -> str:
        key = bytes.fromhex(self.adyen_hmac_key_hex)
        digest = hmac.new(key, adyen_signing_string(fields).encode("utf-8"), hashlib.sha256)
        return base64.b64encode(digest.digest()).decode("ascii")

    @staticmethod
    def _iso(moment: float) -> str:
        return datetime.fromtimestamp(moment, UTC).isoformat()


def _token(rng: random.Random, size: int) -> str:
    # Every fourth character is forced to a letter, which caps any digit run at three and
    # keeps generated identifiers structurally unable to look like a card number.
    return "".join(
        rng.choice(_LETTERS if position % 4 == 0 else _ALNUM) for position in range(size)
    )


def _psp_reference(rng: random.Random) -> str:
    return _token(rng, 16).upper()


def _stable_token(material: str) -> str:
    """A content derived identifier, so a re-delivery of one logical event reproduces its id."""
    raw = base64.b32encode(hashlib.sha256(material.encode("utf-8")).digest()).decode("ascii")[:16]
    return "".join(
        _LETTERS[0] if index % 4 == 0 and char.isdigit() else char for index, char in enumerate(raw)
    )


def _tamper(delivery: Delivery) -> Delivery:
    """Flip one digit of the body after signing. The signature no longer covers these bytes."""
    body = bytearray(delivery.raw)
    position = body.find(b'"amount":')
    while position < len(body) and not chr(body[position]).isdigit():
        position += 1
    if position >= len(body):
        raise ValueError("delivery body carries no amount digit to tamper with")
    body[position] = ord("9") if body[position] != ord("9") else ord("8")
    return Delivery(delivery.provider, delivery.identity, bytes(body), dict(delivery.headers))
