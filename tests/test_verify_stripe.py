from __future__ import annotations

import hashlib
import hmac
import json
import pathlib
from typing import Any

import pytest

from quidz.verify import (
    STRIPE_DEFAULT_TOLERANCE_SECONDS,
    BadSignature,
    MalformedHeader,
    StaleTimestamp,
    parse_stripe_signature_header,
    verify_stripe,
)

PRIMARY = b"whsec_synthetic_primary"
PREVIOUS = b"whsec_synthetic_previous"
RAW = b'{"id":"evt_1","type":"charge.captured"}'
NOW = 1_755_500_000
README = pathlib.Path(__file__).resolve().parent.parent / "README.md"


def sign(raw: bytes, timestamp: int, secret: bytes) -> str:
    return hmac.new(secret, f"{timestamp}.".encode() + raw, hashlib.sha256).hexdigest()


def header(raw: bytes, timestamp: int, secret: bytes) -> dict[str, str]:
    return {"Stripe-Signature": f"t={timestamp},v1={sign(raw, timestamp, secret)}"}


def test_valid_single_v1_signature_passes() -> None:
    assert verify_stripe(RAW, header(RAW, NOW, PRIMARY), [PRIMARY], now=NOW) == NOW


def test_tampered_body_fails() -> None:
    with pytest.raises(BadSignature):
        verify_stripe(RAW + b" ", header(RAW, NOW, PRIMARY), [PRIMARY], now=NOW)


def test_wrong_key_fails() -> None:
    with pytest.raises(BadSignature):
        verify_stripe(RAW, header(RAW, NOW, PREVIOUS), [PRIMARY], now=NOW)


def test_v0_scheme_is_ignored_so_a_downgrade_attempt_is_rejected() -> None:
    # v0 carries the signature that would verify; v1 is wrong. Accepting any scheme prefix
    # would pass this, which is the downgrade attack Stripe warns about.
    headers = {"Stripe-Signature": f"t={NOW},v0={sign(RAW, NOW, PRIMARY)},v1={'0' * 64}"}
    with pytest.raises(BadSignature):
        verify_stripe(RAW, headers, [PRIMARY], now=NOW)


def test_a_second_v1_from_the_rotated_secret_passes() -> None:
    headers = {
        "Stripe-Signature": (f"t={NOW},v1={sign(RAW, NOW, PREVIOUS)},v1={sign(RAW, NOW, PRIMARY)}")
    }
    assert verify_stripe(RAW, headers, [PRIMARY], now=NOW) == NOW


def test_header_values_containing_padding_parse_on_the_first_equals_only() -> None:
    timestamp, signatures = parse_stripe_signature_header("t=1,v1=deadbeef,sig=YWJjZA==")
    assert (timestamp, signatures) == (1, ("deadbeef",))


def test_unknown_scheme_is_discarded() -> None:
    assert parse_stripe_signature_header("t=1,v9=ffff,v1=aa")[1] == ("aa",)


def test_missing_header_raises_malformed_header() -> None:
    with pytest.raises(MalformedHeader):
        verify_stripe(RAW, {}, [PRIMARY], now=NOW)


def test_tolerance_boundary_passes_and_one_second_past_it_is_stale() -> None:
    headers = header(RAW, NOW, PRIMARY)
    edge = NOW + STRIPE_DEFAULT_TOLERANCE_SECONDS
    assert verify_stripe(RAW, headers, [PRIMARY], now=edge) == NOW
    with pytest.raises(StaleTimestamp):
        verify_stripe(RAW, headers, [PRIMARY], now=edge + 1)


def test_the_default_window_is_the_five_minutes_stripe_documents() -> None:
    """The boundary above pins the comparison, and this pins the number it compares against.

    `edge` is computed from the constant, so both sides of that test slide together: set the
    window to thirty seconds, or to a day, and it still passes. The width of a replay window is
    a security property rather than a taste, and Stripe's own libraries default to 300 seconds,
    so the literal belongs in a test where changing it has to be deliberate.
    https://docs.stripe.com/webhooks/signature
    """
    assert STRIPE_DEFAULT_TOLERANCE_SECONDS == 300

    # The row that carries the claim, never the whole README: a page this long contains the
    # digits 300 somewhere whatever the constant says, so searching it would prove nothing.
    row = next(
        line
        for line in README.read_text(encoding="utf-8").splitlines()
        if "a tolerance window is possible" in line
    )
    assert f"{STRIPE_DEFAULT_TOLERANCE_SECONDS} seconds by default" in row


def test_a_receiver_clock_behind_the_sender_does_not_read_live_traffic_as_a_replay() -> None:
    # The window is one sided on purpose. Forging a future timestamp needs the endpoint secret,
    # so a two sided check defends nothing, while a receiving host a few minutes slow rejects
    # perfectly good deliveries as replays and marks them non retryable.
    headers = header(RAW, NOW, PRIMARY)
    assert verify_stripe(RAW, headers, [PRIMARY], now=NOW - 3600) == NOW


def test_zero_tolerance_is_refused() -> None:
    with pytest.raises(ValueError, match="tolerance of 0"):
        verify_stripe(RAW, header(RAW, NOW, PRIMARY), [PRIMARY], now=NOW, tolerance_seconds=0)


def test_compare_digest_is_called_with_two_bytes_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Asserts the code path, not wall clock timing: a timing side channel assertion is flaky
    # under CI scheduling jitter and a quarantined test is worse than no test.
    calls: list[tuple[object, object]] = []
    real = hmac.compare_digest

    def spy(a: Any, b: Any) -> bool:
        calls.append((a, b))
        return bool(real(a, b))

    monkeypatch.setattr(hmac, "compare_digest", spy)
    verify_stripe(RAW, header(RAW, NOW, PRIMARY), [PRIMARY], now=NOW)
    assert calls and all(isinstance(a, bytes) and isinstance(b, bytes) for a, b in calls)


def test_reserialized_json_with_reordered_keys_fails() -> None:
    body = json.dumps({"id": "evt_1", "type": "charge.captured"}, separators=(",", ":")).encode()
    reordered = json.dumps(
        {"type": "charge.captured", "id": "evt_1"}, separators=(",", ":")
    ).encode()
    headers = header(body, NOW, PRIMARY)
    with pytest.raises(BadSignature):
        verify_stripe(reordered, headers, [PRIMARY], now=NOW)
