"""FastAPI adapter. The ONLY module in this package permitted to import fastapi.

Both webhook routes claim and return. Neither drains inline, because Adyen marks a webhook
Failing if it does not get a 2xx within ten seconds and then queues it for retry, so any work
done on the request thread is work that can turn one slow dependency into a redelivery storm.

The Stripe route reads the raw body before anything parses it. Any layer that re-encodes,
reorders keys or normalises whitespace breaks the signature, which is what the express.json(),
Next.js bodyParser and API Gateway rawBody traps all are.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from contextlib import closing
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse

from quidz import __version__, ledger, metrics, store
from quidz.clock import Clock, SystemClock
from quidz.events import EventError
from quidz.inbox import parked_events, receive_adyen, receive_stripe
from quidz.reconcile import GatePolicy, RemotePayment, gate, reconcile, to_json
from quidz.verify import SignatureError

__all__ = ["create_app"]


def _provider_snapshot(db_path: str) -> tuple[float, list[RemotePayment]] | None:
    """Read the payment list a demo run dropped beside the database.

    A deployed service would pull this from the provider API on a schedule. This adapter
    reads whatever snapshot is on disk and says nothing when there is none, rather than
    reconciling against an empty provider and reporting every payment as missing remotely.
    """
    path = Path(f"{db_path}.remote.json")
    if not path.exists():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    return float(payload["as_of"]), [RemotePayment(**row) for row in payload["payments"]]


def _adyen_items(payload: object) -> list[Mapping[str, object]]:
    """One Adyen notification can carry several items, each with its own identity."""
    if not isinstance(payload, Mapping):
        raise EventError("an Adyen notification must be a JSON object")
    wrapped = payload.get("notificationItems")
    if wrapped is None:
        return [payload]
    if not isinstance(wrapped, Sequence) or isinstance(wrapped, (str, bytes)):
        raise EventError("notificationItems must be a list")
    items: list[Mapping[str, object]] = []
    for entry in wrapped:
        item = entry.get("NotificationRequestItem") if isinstance(entry, Mapping) else None
        if not isinstance(item, Mapping):
            raise EventError("each notificationItems entry needs a NotificationRequestItem")
        items.append(item)
    return items


def create_app(
    *,
    db_path: str,
    secrets: Sequence[bytes],
    adyen_hmac_key_hex: str,
    clock: Clock | None = None,
) -> FastAPI:
    ticker: Clock = SystemClock() if clock is None else clock
    # A connection per request rather than a pool. SQLite connections are not shareable across
    # threads, and a portfolio adapter has no business shipping a pool it never load tested.
    with closing(store.connect(db_path)) as conn:
        store.init_schema(conn)

    app = FastAPI(title="quidz", version=__version__)

    @app.get("/healthz")
    async def healthz() -> Response:
        return JSONResponse({"status": "ok", "schema_version": store.SCHEMA_VERSION})

    @app.post("/webhooks/stripe")
    async def stripe_webhook(request: Request) -> Response:
        raw = await request.body()
        with closing(store.connect(db_path)) as conn:
            try:
                result = receive_stripe(conn, raw, request.headers, secrets=secrets, clock=ticker)
            except SignatureError as exc:
                return JSONResponse({"error": str(exc)}, status_code=400)
            except EventError as exc:
                return JSONResponse({"error": str(exc)}, status_code=400)
        if result.reason == "in_progress":
            return JSONResponse({"error": "delivery is still being processed"}, status_code=409)
        return JSONResponse({"received": True})

    @app.post("/webhooks/adyen")
    async def adyen_webhook(request: Request) -> Response:
        raw = await request.body()
        try:
            items = _adyen_items(json.loads(raw))
        except (ValueError, EventError) as exc:
            return PlainTextResponse(str(exc), status_code=400)
        in_progress = False
        with closing(store.connect(db_path)) as conn:
            for item in items:
                # Re-serialising an item is safe only because Adyen signs a joined field
                # string rather than the request bytes. The same move on a Stripe body would
                # invalidate its signature.
                body = json.dumps(item, sort_keys=True, separators=(",", ":")).encode()
                try:
                    result = receive_adyen(
                        conn, body, hmac_key_hex=adyen_hmac_key_hex, clock=ticker
                    )
                except SignatureError as exc:
                    return PlainTextResponse(str(exc), status_code=401)
                except EventError as exc:
                    return PlainTextResponse(str(exc), status_code=400)
                in_progress = in_progress or result.reason == "in_progress"
        if in_progress:
            return PlainTextResponse("delivery is still being processed", status_code=409)
        # The legacy acknowledgement Adyen documents. Any 2xx is accepted.
        return PlainTextResponse("[accepted]")

    @app.get("/reconcile/report")
    async def reconcile_report() -> Response:
        snapshot = _provider_snapshot(db_path)
        now, remote = (ticker.now(), []) if snapshot is None else snapshot
        policy = GatePolicy()
        with closing(store.connect(db_path)) as conn:
            report = reconcile(
                local=ledger.list_payments(conn),
                remote=remote,
                settlement=ledger.list_settlement_rows(conn),
                parked=parked_events(conn),
                now=now,
                policy=policy,
                counters=metrics.snapshot(conn),
            )
        decision = gate(report, policy=policy, now=now)
        return Response(content=to_json(report, decision), media_type="application/json")

    return app
