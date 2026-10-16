"""The merchant's ACP Agentic Checkout REST API, with the AP2 additions, as a FastAPI app.

Follows the ACP checkout RFC where it is specific: ``Authorization: Bearer`` and ``API-Version`` on
every request, ``Idempotency-Key`` on every POST with replay of the stored response (same key, same
body), 422 ``idempotency_conflict`` (same key, different body), no caching of 5xx responses, keys
scoped to the caller and the path, flat error objects, 201 on create, 405 on cancelling a finished
session.

AP2 additions (this project's convention; AP2 leaves the commerce-protocol binding open):
``POST /checkout_sessions/{id}/ap2`` returns the merchant-signed checkout JWT and a fresh nonce, and
``GET /.well-known/ap2-merchant-key`` publishes the key that signs checkout JWTs.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .mandates import FULFILLMENT
from .merchant import ACP_API_VERSION, CheckoutError, Merchant
from .psp import PaymentError

SUPPORTED_VERSIONS = [ACP_API_VERSION]
log = logging.getLogger("shipjack.http")


def _error(status: int, code: str, message: str, param: str | None = None, type_: str = "invalid_request",
           **extra: Any) -> JSONResponse:
    body = {"type": type_, "code": code, "message": message, **({"param": param} if param else {}), **extra}
    return JSONResponse(body, status_code=status)


def _canonical(value: Any) -> Any:
    """ACP request equivalence: key order and trailing zeros do not matter; null and absent differ."""
    if isinstance(value, dict):
        return {k: _canonical(v) for k, v in sorted(value.items())}
    if isinstance(value, list):
        return [_canonical(v) for v in value]
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def _fingerprint(body: Any) -> str:
    return hashlib.sha256(json.dumps(_canonical(body), separators=(",", ":")).encode()).hexdigest()


def create_app(merchant: Merchant, tokens: dict[str, str]) -> FastAPI:
    """``tokens`` maps bearer tokens to caller identities (one per agent platform)."""
    app = FastAPI(title=f"{merchant.info['name']} agentic checkout", version=ACP_API_VERSION)
    stored: dict[tuple[str, str, str], tuple[str, int, Any]] = {}

    async def guarded(request: Request, status: int, action) -> JSONResponse:
        auth = request.headers.get("authorization", "")
        caller = tokens.get(auth.removeprefix("Bearer ").strip()) if auth.startswith("Bearer ") else None
        if caller is None:
            return _error(401, "unauthorized", "a valid Authorization: Bearer token is required")
        version = request.headers.get("api-version")
        if not version:
            return _error(400, "missing_api_version", "API-Version header is required",
                          supported_versions=SUPPORTED_VERSIONS)
        if version not in SUPPORTED_VERSIONS:
            return _error(400, "unsupported_api_version", f"API-Version {version} is not supported",
                          supported_versions=SUPPORTED_VERSIONS)
        body: Any = None
        if request.method == "POST":
            raw = await request.body()
            try:
                body = json.loads(raw) if raw else {}
            except ValueError:
                return _error(400, "invalid", "request body is not valid JSON")
            if not isinstance(body, dict):
                return _error(400, "invalid", "request body must be a JSON object")
            key = request.headers.get("idempotency-key")
            if not key:
                return _error(400, "idempotency_key_required", "Idempotency-Key header is required on all POST requests")
            if len(key) > 255:
                return _error(400, "invalid", "Idempotency-Key is longer than 255 characters")
            slot = (caller, request.url.path, key)
            if slot in stored:
                fingerprint, code, payload = stored[slot]
                if fingerprint != _fingerprint(body):
                    return _error(422, "idempotency_conflict",
                                  "Idempotency-Key has already been used with a different request body")
                return JSONResponse(payload, status_code=code,
                                    headers={"Idempotent-Replayed": "true", "Idempotency-Key": key})
        try:
            code, payload = status, action(body)
        except CheckoutError as exc:
            code, payload = (404 if exc.code == "not_found" else 400), exc.body()
        except _NotAllowed as exc:
            code, payload = 405, {"type": "invalid_request", "code": "invalid", "message": f"session is already {exc}"}
        except Exception:  # a fault is a flat 500 that says nothing about the inside, and is never remembered
            log.exception("unhandled error in %s %s", request.method, request.url.path)
            return _error(500, "internal_error", "the request could not be processed", type_="processing_error")
        headers = {}
        if request.method == "POST":
            headers["Idempotency-Key"] = key
            if code < 500:  # 5xx responses are never cached against the key
                stored[slot] = (_fingerprint(body), code, payload)
        if request.headers.get("request-id"):
            headers["Request-Id"] = request.headers["request-id"]
        return JSONResponse(payload, status_code=code, headers=headers)

    @app.get("/.well-known/acp.json")
    def discovery() -> dict:
        """ACP discovery: who the seller is, the API versions it speaks, and the transports it offers.

        An agent reads this before its first call to choose REST or MCP and a compatible version.
        """
        return {"seller": dict(merchant.info), "protocol": {"supported_versions": SUPPORTED_VERSIONS},
                "transports": ["rest", "mcp"], "payment": {"handlers": ["card", "ap2", "direct"]},
                "extensions": [FULFILLMENT]}

    @app.get("/.well-known/ap2-merchant-key")
    def merchant_key() -> dict:
        return {"merchant": merchant.info, "jwk": json.loads(merchant.verification_key().export_public())}

    @app.get("/products")
    async def search(request: Request, q: str = ""):
        return await guarded(request, 200, lambda _: {"data": merchant.search(q)})

    @app.get("/products/{product_id}")
    async def product(request: Request, product_id: str):
        return await guarded(request, 200, lambda _: merchant.product(product_id))

    @app.post("/checkout_sessions")
    async def create(request: Request):
        return await guarded(request, 201, lambda b: merchant.create_session(
            b.get("items"), fulfillment_details=b.get("fulfillment_details"), buyer=b.get("buyer")))

    @app.post("/checkout_sessions/{session_id}")
    async def update(request: Request, session_id: str):
        return await guarded(request, 200, lambda b: merchant.update_session(
            session_id, items=b.get("items"), fulfillment_details=b.get("fulfillment_details"),
            buyer=b.get("buyer")))

    @app.get("/checkout_sessions/{session_id}")
    async def retrieve(request: Request, session_id: str):
        return await guarded(request, 200, lambda _: merchant.get_session(session_id))

    @app.post("/checkout_sessions/{session_id}/complete")
    async def complete(request: Request, session_id: str):
        return await guarded(request, 200, lambda b: merchant.complete_session(session_id, b.get("payment_data")))

    @app.post("/checkout_sessions/{session_id}/cancel")
    async def cancel(request: Request, session_id: str):
        def act(_):
            session = merchant.sessions.get(session_id)
            if session is not None and session.status in ("completed", "canceled"):
                raise _NotAllowed(session.status)
            return merchant.cancel_session(session_id)
        return await guarded(request, 200, act)

    @app.post("/checkout_sessions/{session_id}/ap2")
    async def ap2(request: Request, session_id: str):
        return await guarded(request, 200, lambda _: merchant.ap2_checkout(session_id))

    # ---- after the order: status, refunds, dispute evidence (merchant operations) ----------------

    def _order(order_id: str) -> dict:
        found = next((o for o in merchant.orders if o["id"] == order_id), None)
        if found is None:
            raise CheckoutError("not_found", f"no order {order_id!r}")
        return dict(found)

    @app.get("/orders/{order_id}")
    async def get_order(request: Request, order_id: str):
        return await guarded(request, 200, lambda _: _order(order_id))

    @app.post("/orders/{order_id}/refund")
    async def refund(request: Request, order_id: str):
        def act(body):
            amount = body.get("amount")
            if amount is not None and (isinstance(amount, bool) or not isinstance(amount, int)):
                raise CheckoutError("invalid", "amount must be a whole number of cents", "$.amount")
            try:
                return merchant.refund_order(order_id, amount)
            except PaymentError as exc:
                raise CheckoutError(exc.code, str(exc), "$.amount") from None
        return await guarded(request, 200, act)

    @app.get("/orders/{order_id}/evidence")
    async def evidence(request: Request, order_id: str):
        return await guarded(request, 200, lambda _: merchant.dispute_evidence(order_id))

    return app


class _NotAllowed(Exception):
    pass


def demo_app(token: str = "demo-token", *, stripe: bool = False) -> FastAPI:
    """A ready-to-run store (Northwind) with one demo platform token, for ``shipjack-serve``.

    With ``stripe=True`` the money settles through Stripe test mode (needs STRIPE_TEST_KEY and the
    'stripe' extra); otherwise the processor is simulated.
    """
    from .scenarios import stores
    from .world import build
    processor = None
    if stripe:
        from .stripe_psp import StripeProcessor
        processor = StripeProcessor.from_env()
    world = build(stores(), processor=processor)
    app = create_app(world.merchants["northwind"], {token: "demo-agent-platform"})
    app.state.world = world
    return app


def main(argv: list[str] | None = None) -> int:
    """Run the demo store over HTTP (the REST transport). Needs the optional 'serve' extra (uvicorn)."""
    import argparse
    parser = argparse.ArgumentParser(prog="shipjack-serve", description="Run the demo agent-ready store over HTTP.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--token", default="demo-token", help="the bearer token an agent platform presents")
    parser.add_argument("--stripe", action="store_true", help="settle through Stripe test mode (needs STRIPE_TEST_KEY)")
    args = parser.parse_args(argv)
    try:
        import uvicorn
    except ImportError:
        import sys
        print("shipjack-serve needs uvicorn: pip install 'shipjack[serve]'", file=sys.stderr)
        return 2
    try:
        app = demo_app(args.token, stripe=args.stripe)
    except ValueError as exc:
        import sys
        print(f"shipjack-serve: {exc}", file=sys.stderr)
        return 2
    rail = "Stripe test mode" if args.stripe else "simulated processor"
    print(f"shipjack store on http://{args.host}:{args.port}  ({rail}; ACP discovery at /.well-known/acp.json)")
    uvicorn.run(app, host=args.host, port=args.port)
    return 0
