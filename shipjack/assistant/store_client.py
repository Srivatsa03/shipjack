"""How the assistant reaches the store: MCP for discovery, ACP REST for checkout and orders.

Every call that changes something carries an idempotency key the caller derives from the thread and
the step, so when LangGraph re-runs a step after a crash or a retry, the store answers with the first
result instead of doing the work twice. Network errors, timeouts and 5xx answers are retried with
backoff; any other error comes back as ``StoreError`` with the store's ACP error code.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass

import httpx

from ..merchant import ACP_API_VERSION

RETRYABLE = frozenset({408, 425, 429, 500, 502, 503, 504})


class StoreError(Exception):
    """The store refused, or could not be reached after the retries."""

    def __init__(self, code: str, message: str, status: int | None = None):
        super().__init__(message)
        self.code, self.status = code, status


@dataclass
class StoreClient:
    """One store, seen by one agent platform (``token``). ``operator_token`` is for merchant-side actions
    (refunds) that the platform may only request after a store operator approved them."""

    base_url: str
    token: str
    operator_token: str = ""
    attempts: int = 3
    backoff: float = 0.3
    timeout: float = 15.0
    transport: httpx.AsyncBaseTransport | None = None  # tests pass the store app in process

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=self.base_url, timeout=self.timeout, transport=self.transport)

    async def _send(self, method: str, path: str, body: dict | None = None, *, key: str | None = None,
                    operator: bool = False) -> dict:
        headers = {"Authorization": f"Bearer {self.operator_token if operator else self.token}",
                   "API-Version": ACP_API_VERSION}
        if key:
            headers["Idempotency-Key"] = key
        last: Exception | None = None
        for attempt in range(self.attempts):
            try:
                async with self._client() as client:
                    resp = await client.request(method, path, json=body, headers=headers)
            except httpx.TransportError as exc:
                last = StoreError("unreachable", f"store unreachable: {type(exc).__name__}")
            else:
                if resp.status_code < 400:
                    return resp.json()
                data = _json(resp)
                if resp.status_code not in RETRYABLE:
                    raise StoreError(data.get("code", "error"), data.get("message", resp.text[:200]), resp.status_code)
                last = StoreError(data.get("code", "unavailable"), data.get("message", "store unavailable"),
                                  resp.status_code)
            if attempt + 1 < self.attempts:
                await asyncio.sleep(self.backoff * 2 ** attempt)
        raise last  # type: ignore[misc]

    # ---- discovery, over REST (used by deterministic steps) ----------------------------------
    async def discovery(self) -> dict:
        async with self._client() as client:
            return (await client.get("/.well-known/acp.json")).json()

    async def product(self, product_id: str) -> dict:
        return await self._send("GET", f"/products/{product_id}")

    # ---- checkout ----------------------------------------------------------------------------
    async def create_checkout(self, items: list[dict], fulfillment: dict, key: str) -> dict:
        return await self._send("POST", "/checkout_sessions", {"items": items, "fulfillment_details": fulfillment},
                                key=key)

    async def complete_checkout(self, session_id: str, payment_data: dict, key: str) -> dict:
        return await self._send("POST", f"/checkout_sessions/{session_id}/complete", {"payment_data": payment_data},
                                key=key)

    async def cancel_checkout(self, session_id: str, key: str) -> dict:
        return await self._send("POST", f"/checkout_sessions/{session_id}/cancel", {}, key=key)

    # ---- after the order ---------------------------------------------------------------------
    async def order(self, order_id: str) -> dict:
        return await self._send("GET", f"/orders/{order_id}")

    async def fulfill(self, order_id: str, key: str, **tracking) -> dict:
        return await self._send("POST", f"/orders/{order_id}/fulfill", tracking, key=key, operator=True)

    async def deliver(self, order_id: str, key: str) -> dict:
        return await self._send("POST", f"/orders/{order_id}/deliver", {}, key=key, operator=True)

    # ---- onboarding and knowledge (operator) ----------------------------------------------------
    async def import_catalog(self, csv_text: str, key: str) -> dict:
        return await self._send("POST", "/admin/catalog", {"csv": csv_text}, key=key, operator=True)

    async def add_document(self, slug: str, markdown: str, key: str) -> dict:
        return await self._send("POST", "/admin/documents", {"slug": slug, "markdown": markdown}, key=key, operator=True)

    async def documents(self) -> dict:
        return await self._send("GET", "/knowledge/documents", operator=True)

    async def search_docs(self, q: str, kind: str | None, mode: str | None, k: int = 5) -> dict:
        from urllib.parse import urlencode
        params = {"q": q, "k": k, **({"kind": kind} if kind else {}), **({"mode": mode} if mode else {})}
        return await self._send("GET", f"/knowledge/search?{urlencode(params)}")

    async def cancel_order(self, order_id: str, key: str) -> dict:
        return await self._send("POST", f"/orders/{order_id}/cancel", {}, key=key, operator=True)

    async def refund(self, order_id: str, amount: int | None, key: str) -> dict:
        body = {} if amount is None else {"amount": amount}
        return await self._send("POST", f"/orders/{order_id}/refund", body, key=key, operator=True)


@dataclass
class StoreMCP:
    """The store's MCP server over Streamable HTTP, through the official MCP client.

    The tools the agents see are plain (``search_products(query)``); this adds the ACP ``meta`` each
    MCP call needs. A tool error comes back as ``StoreError``.
    """

    url: str
    token: str
    transport: httpx.AsyncBaseTransport | None = None  # tests pass the store app in process

    async def call(self, tool: str, *, id: str | None = None, payload: dict | None = None) -> dict:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client
        args: dict = {"meta": {"api_version": ACP_API_VERSION}}
        if id is not None:
            args["id"] = id
        if payload is not None:
            args["payload"] = payload
        http = httpx.AsyncClient(headers={"Authorization": f"Bearer {self.token}"}, timeout=15,
                                 transport=self.transport)
        try:
            async with http, streamable_http_client(self.url, http_client=http) as streams, \
                    ClientSession(streams[0], streams[1]) as session:  # 1.x yields 3 values, 2.x yields 2
                await session.initialize()
                result = await session.call_tool(tool, args)
        except StoreError:
            raise
        except Exception as exc:  # the MCP client wraps transport failures in several types
            raise StoreError("unreachable", f"store MCP server unreachable: {type(exc).__name__}") from None
        body = json.loads(result.content[0].text) if result.content else {}
        failed = getattr(result, "is_error", None)  # MCP 2.x
        if failed is None:
            failed = getattr(result, "isError", False)  # MCP 1.x
        if failed:
            raise StoreError(body.get("code", "error"), body.get("message", "tool error"))
        return body


def _json(resp: httpx.Response) -> dict:
    try:
        data = resp.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}
