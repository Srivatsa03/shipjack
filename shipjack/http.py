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
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .merchant import ACP_API_VERSION, CheckoutError, Merchant

SUPPORTED_VERSIONS = [ACP_API_VERSION]


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
        headers = {}
        if request.method == "POST":
            headers["Idempotency-Key"] = key
            if code < 500:  # 5xx responses are never cached against the key
                stored[slot] = (_fingerprint(body), code, payload)
        if request.headers.get("request-id"):
            headers["Request-Id"] = request.headers["request-id"]
        return JSONResponse(payload, status_code=code, headers=headers)

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
            b.get("items") or [], fulfillment_details=b.get("fulfillment_details"), buyer=b.get("buyer")))

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
        return await guarded(request, 200, lambda b: merchant.complete_session(session_id, b.get("payment_data") or {}))

    @app.post("/checkout_sessions/{session_id}/cancel")
    async def cancel(request: Request, session_id: str):
        def act(_):
            session = merchant.sessions.get(session_id)
            if session is not None and session.status in ("completed", "canceled"):
                raise _NotAllowed(session.status)
            return merchant.cancel_session(session_id)
        try:
            return await guarded(request, 200, act)
        except _NotAllowed as exc:
            return _error(405, "invalid", f"session is already {exc}")

    @app.post("/checkout_sessions/{session_id}/ap2")
    async def ap2(request: Request, session_id: str):
        return await guarded(request, 200, lambda _: merchant.ap2_checkout(session_id))

    return app


class _NotAllowed(Exception):
    pass
