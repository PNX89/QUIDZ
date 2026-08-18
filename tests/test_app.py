from __future__ import annotations

import json
from collections.abc import Iterator
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
def client(tmp_path: Path, clock: FakeClock) -> Iterator[TestClient]:
    app = create_app(
        db_path=str(tmp_path / "quidz.db"),
        secrets=SIM.stripe_secrets,
        adyen_hmac_key_hex=SIM.adyen_hmac_key_hex,
        clock=clock,
    )
    with TestClient(app) as test_client:
        yield test_client


def first(provider: Provider) -> Delivery:
    return next(d for d in SIM.deliveries("happy") if d.provider is provider)


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


def test_the_report_route_returns_stable_parseable_json(client: TestClient) -> None:
    delivery = first(Provider.STRIPE)
    client.post("/webhooks/stripe", content=delivery.raw, headers=delivery.headers)
    first_body = client.get("/reconcile/report").text
    second_body = client.get("/reconcile/report").text
    assert (json.loads(first_body)["gate"]["ingest_blocked"], first_body == second_body) == (
        False,
        True,
    )
