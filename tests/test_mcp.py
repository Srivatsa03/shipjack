"""The store as an MCP server: the ACP tool binding, the JSON-RPC layer, and a purchase over MCP.

The point is that a purchase over MCP reaches the same place as the same purchase over the in-process
API: AP2 bounds the money, not the destination. So the delivery finding does not depend on the
transport.
"""

import json

import pytest

from conftest import DOCK, GIFT, HOME, HUB, hub_intent
from shipjack.mandates import digest, jwt_payload
from shipjack.mcp import ACP_TOOLS, PROTOCOL_VERSION, TOOL_NAMES, MCPStore, ToolError
from shipjack.merchant import ACP_API_VERSION

META = {"api_version": ACP_API_VERSION}


@pytest.fixture
def store(world):
    return MCPStore(world.merchants["northwind"])


def ok(store, name, **args):
    return store.call_tool(name, {"meta": META, **args})


# ---- the tool layer ------------------------------------------------------------------------------


def test_the_five_acp_tools_are_named_exactly_as_the_binding_says():
    names = {t["name"] for t in ACP_TOOLS}
    assert {"create_checkout_session", "get_checkout_session", "update_checkout_session",
            "complete_checkout_session", "cancel_checkout_session"} <= names


def test_every_tool_requires_the_api_version(store):
    for tool in TOOL_NAMES:
        with pytest.raises(ToolError) as exc:
            store.call_tool(tool, {"payload": {}, "id": "x"})
        assert exc.value.body["code"] == "missing_api_version"


def test_an_unsupported_api_version_is_refused(store):
    with pytest.raises(ToolError) as exc:
        store.call_tool("search_products", {"meta": {"api_version": "1999-01-01"}})
    assert exc.value.body["code"] == "unsupported_api_version"


def test_create_get_update_cancel_round_trip(store):
    created = ok(store, "create_checkout_session", payload={"items": [{"id": HUB.id, "quantity": 1}],
                                                            "fulfillment_details": {"address": HOME}})
    sid = created["id"]
    assert created["status"] == "ready_for_payment"
    got = ok(store, "get_checkout_session", id=sid)
    assert got["id"] == sid
    updated = ok(store, "update_checkout_session", id=sid, payload={"items": [{"id": HUB.id, "quantity": 2}]})
    assert updated["totals"][-1]["amount"] == 3499 * 2
    canceled = ok(store, "cancel_checkout_session", id=sid)
    assert canceled["status"] == "canceled"


def test_a_bad_create_is_a_flat_acp_error_not_a_crash(store):
    for payload in ({"items": "hub"}, {"items": [{"id": "ghost", "quantity": 1}]}, {}):
        with pytest.raises(ToolError) as exc:
            ok(store, "create_checkout_session", payload=payload)
        assert set(exc.value.body) >= {"type", "code", "message"}


def test_an_id_is_required_where_the_binding_says_so(store):
    for tool in ("get_checkout_session", "complete_checkout_session", "get_ap2_checkout"):
        with pytest.raises(ToolError) as exc:
            store.call_tool(tool, {"meta": META, "payload": {}})
        assert exc.value.body["code"] == "invalid"


def test_idempotency_is_scoped_to_the_connection_and_tool(store):
    args = {"meta": {**META, "idempotency_key": "k1"}, "payload": {"items": [{"id": HUB.id, "quantity": 1}],
                                                                   "fulfillment_details": {"address": HOME}}}
    first = store.call_tool("create_checkout_session", args)
    again = store.call_tool("create_checkout_session", json.loads(json.dumps(args)))
    assert first["id"] == again["id"]  # replayed, one session
    changed = dict(args, payload={"items": [{"id": HUB.id, "quantity": 9}], "fulfillment_details": {"address": HOME}})
    with pytest.raises(ToolError) as exc:
        store.call_tool("create_checkout_session", changed)
    assert exc.value.body["code"] == "idempotency_conflict"


# ---- discovery -----------------------------------------------------------------------------------


def test_discovery_advertises_mcp_and_the_version(store):
    d = store.discovery()
    assert d["transports"] == ["mcp"] and ACP_API_VERSION in d["protocol"]["supported_versions"]
    assert d["seller"]["id"] == "northwind"


# ---- the JSON-RPC / MCP layer --------------------------------------------------------------------


def rpc(store, method, params=None, mid=1):
    msg = {"jsonrpc": "2.0", "id": mid, "method": method}
    if params is not None:
        msg["params"] = params
    return store.handle(msg)


def test_initialize_reports_the_protocol_and_tool_capability(store):
    r = rpc(store, "initialize", {"protocolVersion": PROTOCOL_VERSION, "capabilities": {}, "clientInfo": {}})
    assert r["result"]["protocolVersion"] == PROTOCOL_VERSION and "tools" in r["result"]["capabilities"]


def test_the_initialized_notification_gets_no_reply(store):
    assert store.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_tools_list_returns_the_tools(store):
    r = rpc(store, "tools/list")
    assert {t["name"] for t in r["result"]["tools"]} == TOOL_NAMES


def test_an_unknown_method_is_a_json_rpc_error(store):
    r = rpc(store, "resources/list")
    assert r["error"]["code"] == -32601


def test_a_non_jsonrpc_message_is_rejected(store):
    assert store.handle({"method": "tools/list"})["error"]["code"] == -32600


def test_tools_call_wraps_the_result_as_mcp_content(store):
    r = rpc(store, "tools/call", {"name": "search_products", "arguments": {"meta": META, "payload": {"query": "hub"}}})
    assert r["result"]["isError"] is False
    data = json.loads(r["result"]["content"][0]["text"])
    assert any(p["id"] == HUB.id for p in data["data"])


def test_tools_call_reports_a_tool_error_as_iserror_not_a_crash(store):
    r = rpc(store, "tools/call", {"name": "get_product", "arguments": {"meta": META, "id": "ghost"}})
    assert r["result"]["isError"] is True and json.loads(r["result"]["content"][0]["text"])["code"] == "not_found"


# ---- a whole purchase over MCP -------------------------------------------------------------------


def complete(store, sid, payment_data):
    return ok(store, "complete_checkout_session", id=sid, payload={"payment_data": payment_data})


def test_a_card_purchase_over_mcp_pays_the_merchant(world, store):
    sid = ok(store, "create_checkout_session", payload={"items": [{"id": HUB.id, "quantity": 1}],
                                                        "fulfillment_details": {"address": HOME}})["id"]
    done = complete(store, sid, {"handler_id": "card", "instrument": {"type": "card",
                                                                      "credential": {"number": "4242424242424242"}}})
    assert done["status"] == "completed" and world.processor.charges[0].account == "acct_northwind"


def test_an_ap2_purchase_over_mcp_matches_the_in_process_path(world, store):
    from shipjack.wallet import Wallet
    sid = ok(store, "create_checkout_session", payload={"items": [{"id": HUB.id, "quantity": 1}],
                                                        "fulfillment_details": {"address": HOME}})["id"]
    wallet = Wallet("ap2+ext", world.credentials, surface=world.surface, directory=world.directory)
    wallet.delegation = world.surface.delegate(hub_intent(world), wallet.agent_public_key(), extensions=True)
    # the agent fetches the signed checkout over MCP, then pays through its wallet against the same merchant
    result = wallet.pay(world.merchants["northwind"], sid)
    assert result["status"] == "paid" and world.processor.charges[0].account == "acct_northwind"


def test_the_delivery_finding_holds_over_mcp(world, store):
    """Right store, right item, right price, paid once, shipped to the attacker; every ACP field is clean."""
    sid = ok(store, "create_checkout_session", payload={"items": [{"id": HUB.id, "quantity": 1}],
                                                        "fulfillment_details": {"address": DOCK}})["id"]
    done = complete(store, sid, {"handler_id": "card", "instrument": {"type": "card",
                                                                      "credential": {"number": "4242424242424242"}}})
    order = world.merchants["northwind"].orders[0]
    assert done["status"] == "completed" and order["amount"] == 3499
    assert order["fulfillment_details"]["address"]["line_one"] == "77 Dock St"  # ACP carried the redirect through


def test_get_ap2_checkout_returns_a_signed_checkout_and_a_nonce(world, store):
    sid = ok(store, "create_checkout_session", payload={"items": [{"id": HUB.id, "quantity": 1}],
                                                        "fulfillment_details": {"address": HOME}})["id"]
    challenge = ok(store, "get_ap2_checkout", id=sid)
    assert challenge["aud"] == "northwind" and challenge["nonce"]
    checkout = jwt_payload(challenge["checkout_jwt"])
    assert checkout["id"] == sid and digest(challenge["checkout_jwt"]) == digest(challenge["checkout_jwt"])


def test_a_gift_card_over_mcp_delivers_to_the_email_in_the_session(world, store):
    sid = ok(store, "create_checkout_session", payload={"items": [{"id": GIFT.id, "quantity": 1}],
                                                        "fulfillment_details": {"email": "codes@evil.test"}})["id"]
    complete(store, sid, {"handler_id": "card", "instrument": {"type": "card",
                                                               "credential": {"number": "4242424242424242"}}})
    assert world.merchants["northwind"].orders[0]["fulfillment_details"]["email"] == "codes@evil.test"


# ---- the stdio loop ------------------------------------------------------------------------------


def test_serve_stdio_runs_a_whole_session(world):
    import io
    store = MCPStore(world.merchants["northwind"])
    lines = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "create_checkout_session",
         "arguments": {"meta": META, "payload": {"items": [{"id": HUB.id, "quantity": 1}],
                                                 "fulfillment_details": {"address": HOME}}}}},
    ]
    stdin = io.StringIO("\n".join(json.dumps(m) for m in lines) + "\n")
    stdout = io.StringIO()
    store.serve_stdio(stdin, stdout)
    replies = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert [r["id"] for r in replies] == [1, 2, 3]  # the notification produced no reply
    assert replies[0]["result"]["serverInfo"]["name"] == "shipjack-store"
    session = json.loads(replies[2]["result"]["content"][0]["text"])
    assert session["status"] == "ready_for_payment"


def test_serve_stdio_survives_a_garbage_line(world):
    import io
    store = MCPStore(world.merchants["northwind"])
    stdout = io.StringIO()
    store.serve_stdio(io.StringIO('not json\n{"jsonrpc":"2.0","id":9,"method":"ping"}\n'), stdout)
    replies = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert replies[0]["error"]["code"] == -32700 and replies[1]["id"] == 9
