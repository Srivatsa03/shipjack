"""The store as an MCP server, so any MCP agent can shop there.

This follows the ACP MCP transport binding (docs/mcp-binding.md): the five checkout operations are
MCP tools named exactly as the spec says, each called with ``{meta, id?, payload?}``, with the
API version in ``meta.api_version``, idempotency in ``meta.idempotency_key``, and authentication
handled at the connection, not per call. A successful call returns the full checkout session as the
tool result; an error returns a flat ACP error with ``isError``.

Three more tools (``search_products``, ``get_product``, ``get_ap2_checkout``) are shipjack
extensions beyond the ACP binding, so an agent can discover products and fetch the merchant-signed
checkout it needs for an AP2 or direct payment. They are marked as extensions in their descriptions.

Transport: a minimal JSON-RPC 2.0 loop over newline-delimited stdio (``initialize``, ``tools/list``,
``tools/call``), which is MCP's base transport, with no extra dependency. ``MCPStore.handle`` takes
one JSON-RPC message and returns one reply, so it is tested in process; ``serve_stdio`` is the thin
runner, and ``shipjack-mcp`` is its console entry point.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from .merchant import ACP_API_VERSION, CheckoutError, Merchant

PROTOCOL_VERSION = "2025-11-25"

_ADDRESS = {"type": "object", "properties": {k: {"type": "string"} for k in
            ("name", "line_one", "line_two", "city", "state", "postal_code", "country")}}
_ITEMS = {"type": "array", "items": {"type": "object", "required": ["id", "quantity"], "properties": {
    "id": {"type": "string"}, "quantity": {"type": "integer", "minimum": 1}}}}
_META = {"type": "object", "description": "Protocol metadata. api_version is required.",
         "properties": {"api_version": {"type": "string"}, "idempotency_key": {"type": "string"},
                        "request_id": {"type": "string"}},
         "required": ["api_version"]}


def _tool(name: str, description: str, props: dict, required: list[str]) -> dict:
    return {"name": name, "description": description,
            "inputSchema": {"type": "object", "properties": props, "required": required}}


_CREATE_PAYLOAD = {"type": "object", "properties": {"items": _ITEMS, "fulfillment_details": {
    "type": "object", "properties": {"address": _ADDRESS, "email": {"type": "string"},
                                     "name": {"type": "string"}, "phone": {"type": "string"}}},
    "buyer": {"type": "object"}}}

ACP_TOOLS = [
    _tool("create_checkout_session", "Create a checkout session (ACP). payload has items and optional "
          "fulfillment_details.", {"meta": _META, "payload": _CREATE_PAYLOAD}, ["meta", "payload"]),
    _tool("get_checkout_session", "Retrieve the current state of a checkout session (ACP).",
          {"meta": _META, "id": {"type": "string"}}, ["meta", "id"]),
    _tool("update_checkout_session", "Update a session's items or fulfillment details (ACP).",
          {"meta": _META, "id": {"type": "string"}, "payload": _CREATE_PAYLOAD}, ["meta", "id", "payload"]),
    _tool("complete_checkout_session", "Submit payment and finalize the order (ACP). payload.payment_data "
          "has handler_id ('card', 'ap2' or 'direct') and the handler's fields.",
          {"meta": _META, "id": {"type": "string"}, "payload": {"type": "object"}}, ["meta", "id", "payload"]),
    _tool("cancel_checkout_session", "Cancel a session that is not yet completed (ACP).",
          {"meta": _META, "id": {"type": "string"}}, ["meta", "id"]),
    # shipjack extensions beyond the ACP binding
    _tool("search_products", "[shipjack extension] Search the store's catalog.",
          {"meta": _META, "payload": {"type": "object", "properties": {"query": {"type": "string"}}}}, ["meta"]),
    _tool("get_product", "[shipjack extension] Open a product page: price, description and reviews.",
          {"meta": _META, "id": {"type": "string"}}, ["meta", "id"]),
    _tool("get_ap2_checkout", "[shipjack extension] Get the merchant-signed checkout JWT and a single-use "
          "nonce for an AP2 or direct payment.", {"meta": _META, "id": {"type": "string"}}, ["meta", "id"]),
]
TOOL_NAMES = {t["name"] for t in ACP_TOOLS}


class ToolError(Exception):
    """An ACP error to return as the tool result with isError set."""

    def __init__(self, body: dict):
        super().__init__(body.get("message", ""))
        self.body = body


class MCPStore:
    """One merchant behind the MCP binding. One connection is one authenticated caller.

    Idempotency keys are scoped to this instance (the connection) and the tool path, as the REST API
    scopes them to the caller and path.
    """

    def __init__(self, merchant: Merchant, *, api_version: str = ACP_API_VERSION,
                 server_name: str = "shipjack-store"):
        self.merchant = merchant
        self.api_version = api_version
        self.server_name = server_name
        self._seen: dict[tuple[str, str], dict] = {}

    # ---- discovery -------------------------------------------------------------------------------
    def discovery(self) -> dict:
        """The ``/.well-known/acp.json`` document: who the seller is and which transports it supports."""
        return {"seller": dict(self.merchant.info), "protocol": {"supported_versions": [self.api_version]},
                "transports": ["mcp"]}

    # ---- the ACP tool layer ----------------------------------------------------------------------
    def call_tool(self, name: str, arguments: dict) -> dict:
        """Run one tool call and return its ACP result. Raises ToolError for a flat ACP error."""
        if name not in TOOL_NAMES:
            raise ToolError({"type": "invalid_request", "code": "unknown_tool", "message": f"no tool {name!r}"})
        if not isinstance(arguments, dict):
            raise ToolError({"type": "invalid_request", "code": "invalid", "message": "arguments must be an object"})
        meta = arguments.get("meta")
        if not isinstance(meta, dict) or "api_version" not in meta:
            raise ToolError({"type": "invalid_request", "code": "missing_api_version",
                             "message": "meta.api_version is required", "supported_versions": [self.api_version]})
        if meta["api_version"] != self.api_version:
            raise ToolError({"type": "invalid_request", "code": "unsupported_api_version",
                             "message": f"api_version {meta['api_version']} is not supported",
                             "supported_versions": [self.api_version]})
        key = meta.get("idempotency_key")
        mutating = name in ("create_checkout_session", "update_checkout_session", "complete_checkout_session",
                            "cancel_checkout_session")
        if key is not None and mutating:
            slot = (name, str(key))
            fingerprint = json.dumps(_canonical(arguments), separators=(",", ":"))
            if slot in self._seen:
                stored_fp, stored = self._seen[slot]
                if stored_fp != fingerprint:
                    raise ToolError({"type": "invalid_request", "code": "idempotency_conflict",
                                     "message": "idempotency_key already used with a different request"})
                return stored
        result = self._dispatch(name, arguments, meta)
        if key is not None and mutating:
            self._seen[(name, str(key))] = (json.dumps(_canonical(arguments), separators=(",", ":")), result)
        return result

    def _dispatch(self, name: str, arguments: dict, meta: dict) -> dict:
        m = self.merchant
        sid = arguments.get("id")
        payload = arguments.get("payload") or {}
        if name in ("get_checkout_session", "update_checkout_session", "complete_checkout_session",
                    "cancel_checkout_session", "get_product", "get_ap2_checkout") and not isinstance(sid, str):
            raise ToolError({"type": "invalid_request", "code": "invalid", "message": "id is required", "param": "id"})
        if not isinstance(payload, dict):
            raise ToolError({"type": "invalid_request", "code": "invalid", "message": "payload must be an object"})
        try:
            if name == "create_checkout_session":
                return m.create_session(payload.get("items"), fulfillment_details=payload.get("fulfillment_details"),
                                        buyer=payload.get("buyer"))
            if name == "get_checkout_session":
                return m.get_session(sid)
            if name == "update_checkout_session":
                return m.update_session(sid, items=payload.get("items"),
                                        fulfillment_details=payload.get("fulfillment_details"),
                                        buyer=payload.get("buyer"))
            if name == "complete_checkout_session":
                return m.complete_session(sid, payload.get("payment_data"))
            if name == "cancel_checkout_session":
                return m.cancel_session(sid)
            if name == "search_products":
                return {"data": m.search(str(payload.get("query", "")))}
            if name == "get_product":
                return m.product(sid)
            if name == "get_ap2_checkout":
                return m.ap2_checkout(sid)
        except CheckoutError as exc:
            raise ToolError(exc.body()) from None
        raise ToolError({"type": "invalid_request", "code": "unknown_tool", "message": f"no tool {name!r}"})

    # ---- the JSON-RPC 2.0 / MCP layer ------------------------------------------------------------
    def handle(self, message: dict) -> dict | None:
        """Handle one JSON-RPC message. Returns a reply, or None for a notification."""
        if message.get("jsonrpc") != "2.0" or "method" not in message:
            return _rpc_error(message.get("id"), -32600, "invalid JSON-RPC request")
        method, mid = message["method"], message.get("id")
        if method == "initialize":
            return _rpc_ok(mid, {"protocolVersion": PROTOCOL_VERSION, "capabilities": {"tools": {}},
                                 "serverInfo": {"name": self.server_name, "version": "0.1.0"}})
        if method in ("notifications/initialized", "initialized"):
            return None
        if method == "ping":
            return _rpc_ok(mid, {})
        if method == "tools/list":
            return _rpc_ok(mid, {"tools": ACP_TOOLS})
        if method == "tools/call":
            params = message.get("params") or {}
            name, arguments = params.get("name"), params.get("arguments") or {}
            try:
                result = self.call_tool(name, arguments)
                return _rpc_ok(mid, {"content": [{"type": "text", "text": json.dumps(result)}], "isError": False})
            except ToolError as exc:
                return _rpc_ok(mid, {"content": [{"type": "text", "text": json.dumps(exc.body)}], "isError": True})
        return _rpc_error(mid, -32601, f"method not found: {method}")

    def serve_stdio(self, stdin=None, stdout=None) -> None:
        """Read newline-delimited JSON-RPC from stdin and write replies to stdout."""
        stdin = stdin or sys.stdin
        stdout = stdout or sys.stdout
        for line in stdin:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except ValueError:
                stdout.write(json.dumps(_rpc_error(None, -32700, "parse error")) + "\n")
                stdout.flush()
                continue
            reply = self.handle(message)
            if reply is not None:
                stdout.write(json.dumps(reply) + "\n")
                stdout.flush()


def _canonical(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _canonical(v) for k, v in sorted(value.items())}
    if isinstance(value, list):
        return [_canonical(v) for v in value]
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def _rpc_ok(mid, result) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _rpc_error(mid, code, message) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def main(argv: list[str] | None = None) -> int:
    """Run a demo store as an MCP server over stdio (one store, Northwind)."""
    from .scenarios import stores
    from .world import build
    world = build(stores())
    MCPStore(world.merchants["northwind"]).serve_stdio()
    return 0


if __name__ == "__main__":
    sys.exit(main())
