from __future__ import annotations

import base64
import hashlib
import hmac

import pytest

from quidz.verify import BadSignature, adyen_signing_string, verify_adyen

KEY_HEX = "00" * 32

DOCUMENTED = {
    "pspReference": "7914073381342284",
    "originalReference": "",
    "merchantAccountCode": "TestMerchant",
    "merchantReference": "TestPayment-1407325143704",
    "value": "1130",
    "currency": "EUR",
    "eventCode": "AUTHORISATION",
    "success": "true",
}
DOCUMENTED_STRING = (
    "7914073381342284::TestMerchant:TestPayment-1407325143704:1130:EUR:AUTHORISATION:true"
)


def signature(item: dict[str, str], key_hex: str = KEY_HEX) -> str:
    digest = hmac.new(
        bytes.fromhex(key_hex), adyen_signing_string(item).encode("utf-8"), hashlib.sha256
    ).digest()
    return base64.b64encode(digest).decode("ascii")


def test_documented_signing_string_is_reproduced_exactly() -> None:
    assert adyen_signing_string(DOCUMENTED) == DOCUMENTED_STRING


def test_empty_original_reference_yields_an_empty_segment() -> None:
    assert adyen_signing_string(DOCUMENTED).split(":")[1] == ""


def test_merchant_reference_containing_a_colon_is_escaped_and_verifies() -> None:
    item = {**DOCUMENTED, "merchantReference": "order:42"}
    assert "order\\:42" in adyen_signing_string(item)
    verify_adyen(item, signature(item), KEY_HEX)


def test_a_field_value_containing_a_backslash_is_escaped_and_verifies() -> None:
    item = {**DOCUMENTED, "merchantReference": "order\\42"}
    assert "order\\\\42" in adyen_signing_string(item)
    verify_adyen(item, signature(item), KEY_HEX)


def test_the_hex_key_is_decoded_to_bytes_before_hmac() -> None:
    key_hex = "DEADBEEF" * 8
    verify_adyen(DOCUMENTED, signature(DOCUMENTED, key_hex), key_hex)
    wrong = hmac.new(
        key_hex.encode("ascii"), DOCUMENTED_STRING.encode("utf-8"), hashlib.sha256
    ).digest()
    with pytest.raises(BadSignature):
        verify_adyen(DOCUMENTED, base64.b64encode(wrong).decode("ascii"), key_hex)


def test_the_signature_is_base64_not_hex() -> None:
    digest = hmac.new(
        bytes.fromhex(KEY_HEX), DOCUMENTED_STRING.encode("utf-8"), hashlib.sha256
    ).digest()
    verify_adyen(DOCUMENTED, base64.b64encode(digest).decode("ascii"), KEY_HEX)
    with pytest.raises(BadSignature):
        verify_adyen(DOCUMENTED, digest.hex(), KEY_HEX)


def test_a_tampered_value_fails() -> None:
    good = signature(DOCUMENTED)
    with pytest.raises(BadSignature):
        verify_adyen({**DOCUMENTED, "value": "1131"}, good, KEY_HEX)


def test_no_timestamp_is_consulted_anywhere_in_the_adyen_path() -> None:
    # Adyen signs no timestamp, so a Stripe style tolerance window is impossible here and
    # identity dedup plus TLS plus IP allowlisting is the whole replay defence.
    assert "eventDate" not in adyen_signing_string(DOCUMENTED)
    verify_adyen(DOCUMENTED, signature(DOCUMENTED), KEY_HEX)
