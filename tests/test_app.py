from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Iterator
from dataclasses import asdict
from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="the server extra is not installed")

from fastapi.testclient import TestClient

from quidz.app import create_app
from quidz.clock import FakeClock
from quidz.events import Provider
from quidz.sim import Delivery, Simulator

SIM = Simulator(seed=0)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(t=SIM.started_at)


@pytest.fixture
def client(db_path: Path, clock: FakeClock) -> Iterator[TestClient]:
    app = create_app(
        db_path=str(db_path),
        secrets=SIM.stripe_secrets,
        adyen_hmac_key_hex=SIM.adyen_hmac_key_hex,
        clock=clock,
    )
    with TestClient(app) as test_client:
        yield test_client


def first(provider: Provider) -> Delivery:
    return next(d for d in SIM.deliveries("happy") if d.provider is provider)


def sign(raw: bytes) -> dict[str, str]:
    """Sign an arbitrary body the way the simulator does, at the batch receipt time."""
    timestamp = int(SIM.started_at)
    signed = f"{timestamp}.".encode() + raw
    parts = [f"t={timestamp}"] + [
        f"v1={hmac.new(secret, signed, hashlib.sha256).hexdigest()}"
        for secret in SIM.stripe_secrets
    ]
    return {"Stripe-Signature": ",".join(parts)}


def write_snapshot(db_path: Path) -> None:
    """The provider payment list a demo run drops beside the database."""
    payload = {
        "as_of": SIM.started_at,
        "payments": [asdict(payment) for payment in SIM.remote_payments()],
    }
    Path(f"{db_path}.remote.json").write_text(json.dumps(payload), encoding="utf-8")


def test_a_valid_stripe_delivery_is_accepted(client: TestClient) -> None:
    delivery = first(Provider.STRIPE)
    response = client.post("/webhooks/stripe", content=delivery.raw, headers=delivery.headers)
    assert (response.status_code, response.json()) == (200, {"received": True})


def test_a_tampered_stripe_body_is_rejected(client: TestClient) -> None:
    delivery = first(Provider.STRIPE)
    response = client.post(
        "/webhooks/stripe", content=delivery.raw + b" ", headers=delivery.headers
    )
    assert response.status_code == 400


def test_a_second_delivery_under_a_live_lease_is_a_conflict(client: TestClient) -> None:
    # Stripe answers 409 for an idempotency key that is still executing. Answering 200 here
    # would tell the provider the work is done while it is still in the queue.
    delivery = first(Provider.STRIPE)
    client.post("/webhooks/stripe", content=delivery.raw, headers=delivery.headers)
    response = client.post("/webhooks/stripe", content=delivery.raw, headers=delivery.headers)
    assert response.status_code == 409


def test_the_adyen_route_acknowledges_with_the_documented_body(client: TestClient) -> None:
    delivery = first(Provider.ADYEN)
    envelope = {
        "live": "false",
        "notificationItems": [{"NotificationRequestItem": json.loads(delivery.raw)}],
    }
    response = client.post("/webhooks/adyen", json=envelope)
    assert (response.status_code, response.text) == (200, "[accepted]")


def test_a_tampered_adyen_signature_is_a_401(client: TestClient) -> None:
    # SignatureError is the one branch of the Adyen route that answers 401, mirroring Stripe's
    # own 409 for in-flight work: every other test that reaches this route signs correctly, so
    # nothing had exercised it.
    delivery = first(Provider.ADYEN)
    item = json.loads(delivery.raw)
    item["additionalData"]["hmacSignature"] = "not-the-real-signature"
    envelope = {"live": "false", "notificationItems": [{"NotificationRequestItem": item}]}
    response = client.post("/webhooks/adyen", json=envelope)
    assert response.status_code == 401


def test_healthz_reports_the_schema_version_the_database_was_stamped_with(
    client: TestClient,
) -> None:
    from quidz import store

    response = client.get("/healthz")
    assert response.json() == {"status": "ok", "schema_version": store.SCHEMA_VERSION}


def test_a_body_that_verifies_but_will_not_parse_is_a_client_error(client: TestClient) -> None:
    # Signed with a live secret and still undecodable. json.loads raises JSONDecodeError here
    # and UnicodeDecodeError for the second body, and neither is an EventError, so both used
    # to escape the route as a 500.
    for raw in (b"not json at all", b'{"id":"evt_1","type":"\xff\xfe"}'):
        response = client.post("/webhooks/stripe", content=raw, headers=sign(raw))
        assert response.status_code == 400, raw


def test_the_report_route_returns_stable_parseable_json(client: TestClient, db_path: Path) -> None:
    write_snapshot(db_path)
    delivery = first(Provider.STRIPE)
    client.post("/webhooks/stripe", content=delivery.raw, headers=delivery.headers)
    first_body = client.get("/reconcile/report").text
    second_body = client.get("/reconcile/report").text
    assert (json.loads(first_body)["gate"]["ingest_blocked"], first_body == second_body) == (
        False,
        True,
    )


def test_the_report_route_refuses_rather_than_reconcile_against_no_provider(
    client: TestClient,
) -> None:
    # Nothing on disk to compare against. Falling back to an empty payment list would report
    # every payment in the ledger as missing remotely and close the gate on the whole book,
    # which is a self inflicted incident caused by a missing file.
    assert client.get("/reconcile/report").status_code == 503


def test_an_unreadable_provider_snapshot_is_refused_the_same_way_as_a_missing_one(
    client: TestClient, db_path: Path
) -> None:
    Path(f"{db_path}.remote.json").write_bytes(b'{"as_of": 1.0, "payments": [\xff]}')
    assert client.get("/reconcile/report").status_code == 503
