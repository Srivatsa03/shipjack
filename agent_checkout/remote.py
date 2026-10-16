"""A merchant reached over its ACP REST API. The wallet can pay through it exactly as in-process."""

from __future__ import annotations

import uuid

import httpx
from jwcrypto.jwk import JWK

from .merchant import ACP_API_VERSION, CheckoutError


class RemoteMerchant:
    def __init__(self, client: httpx.Client, token: str):
        self.client = client
        self.headers = {"Authorization": f"Bearer {token}", "API-Version": ACP_API_VERSION}
        published = client.get("/.well-known/ap2-merchant-key").json()
        self.info = published["merchant"]
        self._key = JWK(**published["jwk"])

    @property
    def id(self) -> str:
        return self.info["id"]

    def verification_key(self) -> JWK:
        return self._key

    def _send(self, method: str, path: str, body: dict | None = None) -> dict:
        headers = dict(self.headers)
        if method == "POST":
            headers["Idempotency-Key"] = str(uuid.uuid4())
        resp = self.client.request(method, path, json=body if method == "POST" else None, headers=headers)
        data = resp.json()
        if resp.status_code >= 400:
            raise CheckoutError(data.get("code", "error"), data.get("message", ""), data.get("param"))
        return data

    def search(self, query: str = "") -> list[dict]:
        return self._send("GET", f"/products?q={query}")["data"]

    def product(self, product_id: str) -> dict:
        return self._send("GET", f"/products/{product_id}")

    def create_session(self, items, fulfillment_details=None, buyer=None) -> dict:
        return self._send("POST", "/checkout_sessions", {"items": items, "fulfillment_details": fulfillment_details,
                                                          "buyer": buyer})

    def update_session(self, session_id, items=None, fulfillment_details=None, buyer=None) -> dict:
        body = {k: v for k, v in (("items", items), ("fulfillment_details", fulfillment_details), ("buyer", buyer))
                if v is not None}
        return self._send("POST", f"/checkout_sessions/{session_id}", body)

    def get_session(self, session_id) -> dict:
        return self._send("GET", f"/checkout_sessions/{session_id}")

    def complete_session(self, session_id, payment_data: dict) -> dict:
        return self._send("POST", f"/checkout_sessions/{session_id}/complete", {"payment_data": payment_data})

    def cancel_session(self, session_id) -> dict:
        return self._send("POST", f"/checkout_sessions/{session_id}/cancel", {})

    def ap2_checkout(self, session_id) -> dict:
        return self._send("POST", f"/checkout_sessions/{session_id}/ap2", {})
