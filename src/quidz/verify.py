"""Two real webhook signature schemes side by side.

Stripe signs the raw request bytes prefixed by the timestamp it also signs, so it can carry a
replay window. Adyen signs a colon joined string of eight fields and signs no timestamp at
all, so no window is possible there and identity dedup plus TLS plus IP allowlisting is what
is left. The contrast is the point of having both.

https://docs.stripe.com/webhooks/signature
https://docs.adyen.com/development-resources/webhooks/secure-webhooks/verify-hmac-signatures
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from collections.abc import Mapping, Sequence

__all__ = [
    "ADYEN_SIGNED_FIELDS",
    "STRIPE_DEFAULT_TOLERANCE_SECONDS",
    "STRIPE_SIGNATURE_HEADER",
    "BadSignature",
    "MalformedHeader",
    "SignatureError",
    "StaleTimestamp",
    "adyen_escape",
    "adyen_signing_fields",
    "adyen_signing_string",
    "parse_stripe_signature_header",
    "verify_adyen",
    "verify_stripe",
]

STRIPE_DEFAULT_TOLERANCE_SECONDS: int = 300
STRIPE_SIGNATURE_HEADER = "Stripe-Signature"

# "To prevent downgrade attacks, ignore all schemes that aren't v1." v0 is a deliberately
# fake scheme Stripe sends with test events, so a prefix match on "v" is exploitable.
_STRIPE_SCHEME = "v1"

ADYEN_SIGNED_FIELDS: tuple[str, ...] = (
    "pspReference",
    "originalReference",
    "merchantAccountCode",
    "merchantReference",
    "value",
    "currency",
    "eventCode",
    "success",
)


class SignatureError(Exception):
    """Base class for every rejection on the verification path."""


class MalformedHeader(SignatureError): ...


class BadSignature(SignatureError): ...


class StaleTimestamp(SignatureError): ...


def _header(headers: Mapping[str, str], name: str) -> str | None:
    wanted = name.lower()
    for key, value in headers.items():
        if key.lower() == wanted:
            return value
    return None


def parse_stripe_signature_header(value: str) -> tuple[int, tuple[str, ...]]:
    """Return the signed timestamp and every v1 signature in the header.

    Splitting on the first '=' only matters: a scheme value can itself contain '=' padding,
    and a naive split('=') either raises or silently mangles the value.
    """
    timestamp: str | None = None
    signatures: list[str] = []
    for part in value.split(","):
        scheme, separator, raw = part.strip().partition("=")
        if not separator:
            continue
        if scheme == "t":
            timestamp = raw
        elif scheme == _STRIPE_SCHEME:
            signatures.append(raw)
    if timestamp is None or not timestamp.isdigit():
        raise MalformedHeader(f"{STRIPE_SIGNATURE_HEADER} carries no usable t= timestamp")
    # The signed payload is rebuilt from the parsed integer, so a non canonical timestamp such
    # as "0123" has to be rejected rather than silently re-encoded as "123".
    if timestamp != str(int(timestamp)):
        raise MalformedHeader(f"{STRIPE_SIGNATURE_HEADER} timestamp {timestamp!r} is not canonical")
    if not signatures:
        raise MalformedHeader(f"{STRIPE_SIGNATURE_HEADER} carries no {_STRIPE_SCHEME} signature")
    return int(timestamp), tuple(signatures)


def verify_stripe(
    raw: bytes,
    headers: Mapping[str, str],
    secrets: Sequence[bytes],
    *,
    now: float,
    tolerance_seconds: int = STRIPE_DEFAULT_TOLERANCE_SECONDS,
) -> int:
    """Verify a Stripe delivery over the exact bytes received and return the signed timestamp.

    `secrets` is a sequence because during an endpoint secret rotation Stripe keeps the
    previous secret active for up to 24 hours and signs with every active secret, so the
    header carries one v1 value per secret and a match on any one of them is a pass.
    """
    if tolerance_seconds <= 0:
        raise ValueError(
            "tolerance_seconds must be positive; Stripe's guidance is that a tolerance of 0 "
            "disables the recency check entirely"
        )
    if not secrets:
        raise ValueError("at least one endpoint secret is required")

    header = _header(headers, STRIPE_SIGNATURE_HEADER)
    if header is None:
        raise MalformedHeader(f"missing {STRIPE_SIGNATURE_HEADER} header")
    timestamp, candidates = parse_stripe_signature_header(header)

    signed_payload = f"{timestamp}.".encode() + raw
    matched = False
    for secret in secrets:
        expected = hmac.new(secret, signed_payload, hashlib.sha256).digest()
        for candidate in candidates:
            try:
                given = bytes.fromhex(candidate)
            except ValueError:
                continue
            # Raw digest bytes on both sides, never the hex text and never the whole header.
            if hmac.compare_digest(expected, given):
                matched = True
    if not matched:
        raise BadSignature("no active endpoint secret reproduces a v1 signature in the header")

    # Checked at receipt time, never at processing time: a Stripe retry carries a new
    # timestamp and a new signature, so a queued delivery must not be re-checked later.
    #
    # Deliberately one sided. An attacker can only replay a signature the legitimate sender
    # already minted, and minting a future timestamp needs the endpoint secret, so rejecting a
    # future dated delivery buys no defence at all. What it does buy is that a receiving host
    # a few minutes slow reads valid live traffic as a replay, counts it as replay_rejected and
    # marks it non retryable. Stripe's own libraries check the past side only.
    if now - timestamp > tolerance_seconds:
        raise StaleTimestamp(
            f"signed timestamp {timestamp} is older than the {tolerance_seconds}s tolerance "
            f"at {now}"
        )
    return timestamp


def adyen_escape(value: str) -> str:
    """Escape a field value for the colon joined signing string.

    Without this, a merchantReference containing ':' shifts every later field by one and the
    signature never matches. https://github.com/Adyen/adyen-python-api-library/issues/117
    """
    return value.replace("\\", "\\\\").replace(":", "\\:")


def _text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def adyen_signing_fields(item: Mapping[str, object]) -> dict[str, str]:
    """Flatten a notification item to the eight signed fields, lifting amount.value."""
    amount = item.get("amount")
    nested: Mapping[str, object] = amount if isinstance(amount, Mapping) else {}
    fields = {name: _text(item.get(name)) for name in ADYEN_SIGNED_FIELDS}
    if not fields["value"]:
        fields["value"] = _text(nested.get("value"))
    if not fields["currency"]:
        fields["currency"] = _text(nested.get("currency"))
    return fields


def adyen_signing_string(item: Mapping[str, str]) -> str:
    """Join the eight signed fields. Takes the flat mapping adyen_signing_fields produces."""
    return ":".join(adyen_escape(_text(item.get(name))) for name in ADYEN_SIGNED_FIELDS)


def verify_adyen(item: Mapping[str, object], signature_b64: str, hmac_key_hex: str) -> None:
    """Verify additionalData.hmacSignature. No timestamp is signed, so no window is possible.

    The flattening happens here rather than at the call site. A notification item carries the
    amount nested under `amount`, so signing the item directly leaves the value and currency
    segments empty and every real item fails verification for a reason that looks like a bad
    key. Flattening an already flat mapping is a no-op, so both shapes verify.
    """
    try:
        key = bytes.fromhex(hmac_key_hex)
    except ValueError as exc:
        raise ValueError(
            "the Adyen HMAC key is the hexadecimal value from the Customer Area, "
            "decoded to raw bytes before use"
        ) from exc
    signing_string = adyen_signing_string(adyen_signing_fields(item))
    expected = hmac.new(key, signing_string.encode("utf-8"), hashlib.sha256).digest()
    try:
        given = base64.b64decode(signature_b64, validate=True)
    except ValueError as exc:
        raise BadSignature("hmacSignature is not valid base64") from exc
    if not hmac.compare_digest(expected, given):
        raise BadSignature("hmacSignature does not match the signed field string")
