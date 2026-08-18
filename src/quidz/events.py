"""Provider payloads to one canonical event.

Delivery identity uses stable identity fields only. Adyen documents that duplicates carry the
same eventCode and pspReference while eventDate and other fields may differ, so any key that
hashes the whole payload hashes differently for a true duplicate and the effect is applied
twice. Stripe's identity is the event id.

https://docs.adyen.com/development-resources/webhooks/handle-webhook-events
https://docs.stripe.com/webhooks
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from quidz.model import EffectKind
from quidz.money import Money

__all__ = [
    "CanonicalEvent",
    "EventError",
    "MalformedEvent",
    "Provider",
    "UnknownEventType",
    "from_adyen",
    "from_stripe",
]


class EventError(Exception):
    """Base class for adapter rejections."""


class MalformedEvent(EventError): ...


class UnknownEventType(EventError):
    """A recognised provider sent an event this ledger does not model.

    Deliberately not a silent drop. dlq.classify maps this to a retryable with cap reason so
    an unrecognised new event type ends up in the dead letter queue where a human sees it.
    """


class Provider(StrEnum):
    STRIPE = "stripe"
    ADYEN = "adyen"


@dataclass(frozen=True, slots=True)
class CanonicalEvent:
    provider: Provider
    identity: str
    payment_id: str
    kind: EffectKind
    provider_ref: str
    amount: Money | None
    occurred_at: float
    sequence: int
    # The provider's own strings, kept verbatim so reconciliation compares like for like.
    raw_amount_value: str | None
    raw_currency: str | None


_STRIPE_KINDS: Mapping[str, EffectKind] = {
    "payment_intent.amount_capturable_updated": EffectKind.AUTHORIZE,
    "payment_intent.payment_failed": EffectKind.AUTHORIZE_FAIL,
    "payment_intent.canceled": EffectKind.VOID,
    "charge.captured": EffectKind.CAPTURE,
    "charge.expired": EffectKind.EXPIRE,
}
# data.object here is the Refund, so each refund carries its own provider_ref and two refunds
# on one charge never collide on UNIQUE(payment_id, kind, provider_ref).
_STRIPE_REFUND_TYPE = "charge.refund.updated"

# Stripe has no capture failed webhook because its capture is synchronous. Adyen's capture is
# asynchronous and it emits CAPTURE_FAILED, which is why captured is not a terminal state.
_ADYEN_KINDS: Mapping[str, EffectKind] = {
    "AUTHORISATION": EffectKind.AUTHORIZE,
    "AUTHORISATION_ADJUSTMENT": EffectKind.ADJUST_AUTH,
    "CANCELLATION": EffectKind.VOID,
    "CAPTURE": EffectKind.CAPTURE,
    "CAPTURE_FAILED": EffectKind.CAPTURE_FAIL,
    "REFUND": EffectKind.REFUND,
    "REFUND_FAILED": EffectKind.REFUND_FAIL,
    "REFUNDED_REVERSED": EffectKind.REFUND_REVERSE,
    "EXPIRE": EffectKind.EXPIRE,
}
# The boolean success field carries the outcome, so eventCode alone is not a transition. The
# pair is. https://docs.adyen.com/development-resources/webhooks/webhook-types
_ADYEN_FAILURE_KINDS: Mapping[str, EffectKind] = {
    "AUTHORISATION": EffectKind.AUTHORIZE_FAIL,
    "CAPTURE": EffectKind.CAPTURE_FAIL,
    "REFUND": EffectKind.REFUND_FAIL,
}


def _require_str(source: Mapping[str, object], key: str) -> str:
    value = source.get(key)
    if not isinstance(value, str) or not value:
        raise MalformedEvent(f"field {key!r} must be a non empty string, got {value!r}")
    return value


def _require_int(source: Mapping[str, object], key: str) -> int:
    value = source.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise MalformedEvent(f"field {key!r} must be an integer, got {value!r}")
    return value


def _stripe_object(payload: Mapping[str, object]) -> Mapping[str, object]:
    data = payload.get("data")
    if not isinstance(data, Mapping):
        raise MalformedEvent("Stripe event has no data object")
    obj = data.get("object")
    if not isinstance(obj, Mapping):
        raise MalformedEvent("Stripe event data carries no object")
    return obj


def _stripe_amount(
    kind: EffectKind, obj: Mapping[str, object]
) -> tuple[str | None, str | None, Money | None]:
    if kind in (EffectKind.VOID, EffectKind.EXPIRE):
        return None, None, None
    if kind is EffectKind.AUTHORIZE:
        field = "amount_capturable" if "amount_capturable" in obj else "amount"
    elif kind is EffectKind.CAPTURE:
        field = "amount_captured" if "amount_captured" in obj else "amount"
    else:
        field = "amount"
    minor = _require_int(obj, field)
    currency = _require_str(obj, "currency")
    # Stripe sends lowercase currency codes. The verbatim string is kept for reconciliation
    # and the uppercase ISO 4217 code is what the ledger does arithmetic on.
    return str(minor), currency, Money(minor, currency.upper())


def from_stripe(payload: Mapping[str, object]) -> CanonicalEvent:
    identity = _require_str(payload, "id")
    event_type = _require_str(payload, "type")
    created = _require_int(payload, "created")
    obj = _stripe_object(payload)

    if event_type == _STRIPE_REFUND_TYPE:
        failed = obj.get("status") == "failed"
        kind = EffectKind.REFUND_FAIL if failed else EffectKind.REFUND
    else:
        try:
            kind = _STRIPE_KINDS[event_type]
        except KeyError:
            raise UnknownEventType(f"unsupported Stripe event type {event_type!r}") from None

    # Stripe's own duplicate guidance: the id of the object in data.object plus the event
    # type identifies the effect, which is the second unique constraint in the schema.
    provider_ref = _require_str(obj, "id")
    payment_intent = obj.get("payment_intent")
    payment_id = payment_intent if isinstance(payment_intent, str) else provider_ref
    raw_value, raw_currency, amount = _stripe_amount(kind, obj)
    return CanonicalEvent(
        provider=Provider.STRIPE,
        identity=identity,
        payment_id=payment_id,
        kind=kind,
        provider_ref=provider_ref,
        amount=amount,
        occurred_at=float(created),
        sequence=created,
        raw_amount_value=raw_value,
        raw_currency=raw_currency,
    )


def _adyen_success(value: object) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("true", "false"):
        return text == "true"
    raise MalformedEvent(f"Adyen success must be true or false, got {value!r}")


def _adyen_kind(event_code: str, success: bool) -> EffectKind:
    table = _ADYEN_KINDS if success else _ADYEN_FAILURE_KINDS
    kind = table.get(event_code)
    if kind is None:
        outcome = "true" if success else "false"
        raise UnknownEventType(f"unsupported Adyen event pair ({event_code}, {outcome})")
    return kind


def _adyen_moment(item: Mapping[str, object]) -> float:
    text = _require_str(item, "eventDate")
    try:
        moment = datetime.fromisoformat(text)
    except ValueError as exc:
        raise MalformedEvent(f"eventDate {text!r} is not ISO 8601") from exc
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.timestamp()


def _adyen_amount(item: Mapping[str, object]) -> tuple[str | None, str | None, Money | None]:
    amount = item.get("amount")
    if not isinstance(amount, Mapping):
        return None, None, None
    minor = _require_int(amount, "value")
    currency = _require_str(amount, "currency")
    return str(minor), currency, Money(minor, currency)


def from_adyen(item: Mapping[str, object]) -> CanonicalEvent:
    psp_reference = _require_str(item, "pspReference")
    event_code = _require_str(item, "eventCode")
    kind = _adyen_kind(event_code, _adyen_success(item.get("success")))
    occurred_at = _adyen_moment(item)
    raw_value, raw_currency, amount = _adyen_amount(item)
    return CanonicalEvent(
        provider=Provider.ADYEN,
        identity=f"{psp_reference}:{event_code}",
        # merchantReference is the merchant's own order reference and is the one field that
        # is the same across the authorisation, the capture and every refund.
        payment_id=_require_str(item, "merchantReference"),
        kind=kind,
        provider_ref=psp_reference,
        amount=amount,
        occurred_at=occurred_at,
        sequence=int(occurred_at),
        raw_amount_value=raw_value,
        raw_currency=raw_currency,
    )
