"""The load tool against a real HTTP server on a SQLite store: it reports purchases, storms and the ledger."""

import threading
import time

import pytest
from test_webhooks import free_port

from shipjack import load
from shipjack.http import demo_app


@pytest.fixture
def server(tmp_path):
    uvicorn = pytest.importorskip("uvicorn")
    db = str(tmp_path / "shop.db")
    port = free_port()
    srv = uvicorn.Server(uvicorn.Config(demo_app(db=db), host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not srv.started and time.monotonic() < deadline:
        time.sleep(0.02)
    yield f"http://127.0.0.1:{port}", db
    srv.should_exit = True
    thread.join(timeout=5)


def test_the_load_tool_counts_purchases_storms_and_charges(server):
    url, db = server
    report = load.run(url, purchases=40, concurrency=4, clients=2, storms=3, storm_size=6, db=db)
    assert report["purchases"]["completed"] == 40 and report["purchases"]["errors"] == 0
    assert report["storms"]["storms_with_one_paid_answer"] == 3 and report["storms"]["server_errors"] == 0
    assert report["ledger"] == {"charges": 43, "orders": 43, "checkouts_charged_more_than_once": 0,
                                "storm_sessions_charged_exactly_once": 3}


def test_the_ledger_check_finds_a_double_charge(tmp_path):
    import json
    import sqlite3

    from shipjack.store import Database
    db = str(tmp_path / "x.db")
    Database(db)
    conn = sqlite3.connect(db)
    for i, ref in enumerate(["cs_a", "cs_a", "cs_b"]):
        conn.execute("INSERT INTO ledger_charges (id, data) VALUES (?, ?)", (f"ch_{i}", json.dumps({"reference": ref})))
    conn.commit()
    conn.close()
    assert load.ledger(db, ["cs_a", "cs_b"]) == {"charges": 3, "orders": 0, "checkouts_charged_more_than_once": 1,
                                                 "storm_sessions_charged_exactly_once": 1}
