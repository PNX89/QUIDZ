from __future__ import annotations

import pytest

from quidz.events import UnknownEventType, from_adyen, from_stripe
from quidz.model import EffectKind

STRIPE_CAPTURE = {
    "id": "evt_3Nn1x2",
    "type": "charge.captured",
    "created": 1_755_500_000,
    "data": {
        "object": {
            "id": "ch_3Nn1x2",
            "payment_intent": "pi_3Nn1x2",
            "amount": 2000,
            "amount_captured": 2000,
            "currency": "eur",
        }
    },
}

ADYEN_CAPTURE = {
    "pspReference": "8412534564722331",
    "originalReference": "8412534564722330",
    "merchantAccountCode": "TestMerchant",
    "merchantReference": "order-1",
    "amount": {"value": 1130, "currency": "EUR"},
    "eventCode": "CAPTURE",
    "success": "true",
    "eventDate": "2026-08-18T10:00:00+02:00",
}


def test_stripe_identity_is_the_event_id() -> None:
    assert from_stripe(STRIPE_CAPTURE).identity == "evt_3Nn1x2"


def test_adyen_identity_is_psp_reference_and_event_code() -> None:
    assert from_adyen(ADYEN_CAPTURE).identity == "8412534564722331:CAPTURE"


def test_capture_with_success_false_maps_to_capture_fail() -> None:
    event = from_adyen({**ADYEN_CAPTURE, "success": "false"})
    assert event.kind is EffectKind.CAPTURE_FAIL


def test_refund_with_success_true_maps_to_refund() -> None:
    item = {**ADYEN_CAPTURE, "eventCode": "REFUND", "success": "true"}
    assert from_adyen(item).kind is EffectKind.REFUND


def test_an_unrecognised_event_code_raises_unknown_event_type() -> None:
    with pytest.raises(UnknownEventType):
        from_adyen({**ADYEN_CAPTURE, "eventCode": "CHARGEBACK"})
