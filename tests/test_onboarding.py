"""Onboarding a merchant: catalog imports (Shopify export, simple CSV), the store's knowledge base, and
the rule that an order keeps what it was sold as, whatever the catalog becomes later.

Store-backed tests run on every store (memory, SQLite, Postgres when SHIPJACK_TEST_PG is set). On
Postgres the knowledge base is hybrid (pgvector + full text); elsewhere it is keyword only. The
embedder is the test hash (conftest sets SHIPJACK_EMBEDDER=hash), so nothing is downloaded.
"""

from importlib import resources

import pytest
from fastapi.testclient import TestClient

from conftest import HOME, HUB, stores
from shipjack.catalog import Product
from shipjack.http import create_app
from shipjack.knowledge import KnowledgeBase, policy_chunks, product_chunk
from shipjack.merchant import ACP_API_VERSION, CheckoutError
from shipjack.onboarding import MAX_BYTES, parse_catalog
from shipjack.world import build

SAMPLE = resources.files("shipjack").joinpath("data/northwind_shopify_export.csv").read_text()
CARD = {"handler_id": "card", "instrument": {"type": "card", "credential": {"number": "4242424242424242"}}}


def h(key="k", token="op"):
    return {"Authorization": f"Bearer {token}", "API-Version": ACP_API_VERSION, "Idempotency-Key": key}


@pytest.fixture
def api(world):
    return TestClient(create_app(world.merchants["northwind"], {"t": "platform"}, operators={"op": "ops"}))


# ---- parsing --------------------------------------------------------------------------------------------


def test_a_shopify_export_becomes_one_product_per_variant():
    out = parse_catalog(SAMPLE)
    by_id = {p.id: p for p in out.products}
    assert out.format == "shopify" and len(out.products) == 32
    assert sorted(out.skipped) == ["prototype-hub-x: draft", "usb-a-hub-2019: archived"]
    cable = [p for p in out.products if p.id.startswith("usb-c-cable-")]
    assert [(p.title, p.price) for p in cable] == [("Northwind USB-C to USB-C Cable (1 m)", 999),
                                                   ("Northwind USB-C to USB-C Cable (2 m)", 1299),
                                                   ("Northwind USB-C to USB-C Cable (3 m)", 1599)]
    assert by_id["northwind-gift-card-nw-gift50"].kind == "digital" and by_id["northwind-gift-card-nw-gift50"].price == 5000
    assert by_id["extended-warranty-2-years"].kind == "digital"  # doesn't require shipping
    assert by_id["thunderbolt-4-dock"].price == 18900
    hub = by_id["northwind-7-in-1-usb-c-hub"]
    assert "<" not in hub.description and "HDMI 4K60, gigabit" in hub.description and "By Northwind." in hub.description


def test_a_simple_csv_and_its_bad_rows():
    text = "id,title,price,kind\nhub-1,Hub One,34.99,\n,No Id,5,\ncard,Gift Card,$25,digital\nbad,Bad,free,\nhub-1,Again,1,\n"
    out = parse_catalog(text)
    assert out.format == "simple" and [(p.id, p.price, p.kind) for p in out.products] == [
        ("hub-1", 3499, "physical"), ("card", 2500, "digital")]
    assert out.skipped == ["row 3: id and title are required", "bad: price 'free' is not a price", "hub-1: duplicate id"]


@pytest.mark.parametrize("text", ["name,cost\nx,1\n", "", "just some text"])
def test_something_that_is_not_a_catalog_is_refused(text):
    with pytest.raises(ValueError, match="not a Shopify product export"):
        parse_catalog(text)


def test_an_oversized_file_is_refused():
    with pytest.raises(ValueError, match="larger than"):
        parse_catalog(b"x" * (MAX_BYTES + 1))


# ---- importing into a store ---------------------------------------------------------------------------------


def test_an_operator_imports_a_catalog_and_agents_can_buy_from_it(world, api):
    r = api.post("/admin/catalog", json={"csv": SAMPLE}, headers=h("imp"))
    assert r.status_code == 200 and r.json()["imported"] == 32 and r.json()["indexed"] == 32
    assert r.json()["skipped"] == ["prototype-hub-x: draft", "usb-a-hub-2019: archived"]
    platform = h("s1", token="t")
    assert api.get("/products/thunderbolt-4-dock", headers=platform).json()["price"] == 18900
    found = api.get("/products?q=thunderbolt dock", headers=platform).json()["data"]
    assert found[0]["id"] == "thunderbolt-4-dock"
    s = api.post("/checkout_sessions", json={"items": [{"id": "thunderbolt-4-dock"}],
                                             "fulfillment_details": {"address": HOME}}, headers=platform).json()
    assert s["status"] == "ready_for_payment" and s["totals"][-1]["amount"] == 18900


def test_only_an_operator_may_import(api):
    assert api.post("/admin/catalog", json={"csv": SAMPLE}, headers=h("x", token="t")).status_code == 403
    assert api.post("/admin/documents", json={"slug": "a", "markdown": "## A\nb"}, headers=h("y", token="t")).status_code == 403


@pytest.mark.parametrize("body", [{}, {"csv": ""}, {"csv": "name\nx\n"}])
def test_a_bad_import_is_a_400_and_changes_nothing(world, api, body):
    before = set(world.merchants["northwind"].products)
    r = api.post("/admin/catalog", json=body, headers=h(str(body)))
    assert r.status_code == 400 and set(world.merchants["northwind"].products) == before


def test_an_order_keeps_what_it_was_sold_as_after_the_catalog_changes(world, api):
    m = world.merchants["northwind"]
    s = m.create_session([{"id": HUB.id}], fulfillment_details={"address": HOME})
    oid = m.complete_session(s["id"], CARD)["order"]["id"]
    api.post("/admin/catalog", json={"csv": "id,title,price\nnew-thing,A New Thing,5\n"}, headers=h("imp2"))
    assert HUB.id not in m.products
    order = m.acp_order(oid)
    assert order["line_items"][0]["title"] == HUB.title and order["line_items"][0]["unit_price"] == 3499
    assert m.get_session(s["id"])["line_items"][0]["name"] == HUB.title
    m.fulfill_order(oid)  # still ships: the order knows its lines were physical


def test_an_open_cart_with_a_product_that_is_gone_cannot_be_paid(world, api):
    m = world.merchants["northwind"]
    s = m.create_session([{"id": HUB.id}], fulfillment_details={"address": HOME})
    api.post("/admin/catalog", json={"csv": "id,title,price\nnew-thing,A New Thing,5\n"}, headers=h("imp3"))
    with pytest.raises(CheckoutError, match="no longer sold"):
        m.complete_session(s["id"], CARD)
    assert not world.processor.charges


def test_an_imported_catalog_survives_a_restart(tmp_path):
    db = str(tmp_path / "s.db")
    first = build(stores(), db=db).merchants["northwind"]
    first.replace_catalog([Product("only-one", "The Only One", 1234)])
    again = build(stores(), db=db).merchants["northwind"]
    assert list(again.products) == ["only-one"] and again.products["only-one"].price == 1234


# ---- the knowledge base ----------------------------------------------------------------------------------


def test_policy_pages_split_on_headings_with_citable_sources():
    chunks = policy_chunks("# Title\n\n## Returns\nThirty days.\n\n## Gift cards\nNever expire.\n", "store-policies")
    assert [(c.title, c.source) for c in chunks] == [("Returns", "store-policies#returns"),
                                                     ("Gift cards", "store-policies#gift-cards")]
    long = policy_chunks("## Long\n" + "\n\n".join(f"Paragraph {i} " + "word " * 60 for i in range(5)), "p")
    assert len(long) > 1 and all(c.source == "p#long" for c in long) and len({c.id for c in long}) == len(long)


@pytest.mark.parametrize("query, want", [
    ("can I change the delivery address after it ships", "Changing the delivery address"),
    ("do gift cards expire", "Gift cards"),
    ("how long does a refund take", "Refunds"),
])
def test_the_store_answers_policy_questions_with_a_source(api, query, want):
    hits = api.get(f"/knowledge/search?q={query}&kind=policy", headers=h(token="t")).json()["data"]
    assert hits[0]["title"] == want and hits[0]["source"].startswith("northwind-policies#")


def test_a_new_policy_page_becomes_searchable_and_replaces_its_old_version(api):
    page = "## Student discount\nStudents get 10% off hubs with a .edu email.\n"
    r = api.post("/admin/documents", json={"slug": "student-offer", "markdown": page}, headers=h("d1"))
    assert r.status_code == 200 and r.json()["chunks"] == 1
    hits = api.get("/knowledge/search?q=student discount edu&kind=policy", headers=h(token="t")).json()["data"]
    assert hits[0]["source"] == "student-offer#student-discount"
    api.post("/admin/documents", json={"slug": "student-offer", "markdown": "## Teachers\nTeachers get 5% off.\n"},
             headers=h("d2"))
    docs = api.get("/knowledge/documents", headers=h()).json()["data"]
    assert {"kind": "policy", "doc": "student-offer", "chunks": 1} in docs
    hits = api.get("/knowledge/search?q=student discount edu&kind=policy", headers=h(token="t")).json()["data"]
    assert all(x["doc"] != "student-offer" or x["title"] == "Teachers" for x in hits)


@pytest.mark.parametrize("body", [{"slug": "Bad Slug", "markdown": "x"}, {"slug": "ok", "markdown": ""}])
def test_a_bad_document_is_a_400(api, body):
    assert api.post("/admin/documents", json=body, headers=h(str(body))).status_code == 400


def test_the_mcp_server_offers_document_search(world, api):
    call = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
        "name": "search_store_docs", "arguments": {"meta": {"api_version": ACP_API_VERSION},
                                                   "payload": {"query": "warranty on a hub", "kind": "policy"}}}}
    r = api.post("/mcp", json=call, headers={"Authorization": "Bearer t"}).json()
    import json
    hits = json.loads(r["result"]["content"][0]["text"])["data"]
    assert hits[0]["title"] == "Warranty" and not r["result"]["isError"]


def test_hybrid_ranks_a_chunk_found_both_ways_first():
    """The fusion itself, on a stub: a chunk found by vector and by keyword beats one found by either."""
    from shipjack.knowledge import Hit
    kb = KnowledgeBase("m")
    kb.vector = True
    a, b, c = (Hit(x, x, x, x, "policy", x, 0.0) for x in "abc")
    kb._vector = lambda q, kind, n: [Hit(**{**a.__dict__}), Hit(**{**b.__dict__})]
    kb._keyword_pg = lambda q, kind, n: [Hit(**{**c.__dict__}), Hit(**{**b.__dict__})]
    ranked = kb.search("anything", k=3, mode="hybrid", keyword_weight=1.0)
    assert [x.id for x in ranked] == ["b", "a", "c"] and (ranked[0].vector_rank, ranked[0].keyword_rank) == (2, 2)


def test_a_product_chunk_says_what_and_how_it_is_delivered():
    chunk = product_chunk(Product("g", "Gift Card", 2500, kind="digital", description="Emailed."))
    assert chunk.source == "catalog:g" and "$25.00" in chunk.text and "delivered by email" in chunk.text
