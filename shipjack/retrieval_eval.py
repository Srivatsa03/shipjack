"""How well the store's knowledge base finds the right page: keyword vs vector vs hybrid, per embedder.

The set (``data/retrieval_eval.jsonl``) is 40 questions in a shopper's words, 20 about products and 20
about policies, each with the pages that answer it. Most product questions avoid the product's own
words ("something to connect two monitors to my laptop"), which is where keyword search struggles.

Metrics, for the first relevant hit: hit@1, hit@3, and MRR over the top 5. It needs Postgres (pgvector
and full text); each run uses a throwaway schema.

    shipjack retrieval-eval --db postgresql://... --embedders hash,model2vec,fastembed
"""

from __future__ import annotations

import json
import time
import uuid
from importlib import resources

from .knowledge import (
    KnowledgeBase,
    embedder,
    load_policies,
    policy_chunks,
    product_chunk,
)
from .onboarding import parse_catalog

MODES = (("keyword", None), ("vector", None), ("hybrid", 1.0), ("hybrid", 0.5), ("hybrid", 0.25))
NAMES = {"hash": "hash", "model2vec": "model2vec:minishlab/potion-base-8M", "fastembed": "fastembed:BAAI/bge-small-en-v1.5"}


def load_set() -> list[dict]:
    text = resources.files("shipjack").joinpath("data/retrieval_eval.jsonl").read_text()
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def score(ranked: list[str], relevant: set[str]) -> tuple[int, int, float]:
    first = next((i for i, x in enumerate(ranked[:5], 1) if x in relevant), None)
    return int(first == 1), int(first is not None and first <= 3), (1.0 / first) if first else 0.0


def run(db_url: str, embedders: list[str]) -> dict:
    import psycopg

    from .store import PostgresDatabase
    queries = load_set()
    products = parse_catalog(resources.files("shipjack").joinpath("data/northwind_shopify_export.csv").read_text()).products
    chunks = {"product": [product_chunk(p) for p in products],
              "policy": policy_chunks(load_policies(), "northwind-policies")}
    report = {"queries": len(queries), "products": len(products), "policy_chunks": len(chunks["policy"]), "runs": []}
    for spec in embedders:
        schema = f"eval_{uuid.uuid4().hex[:8]}"
        with psycopg.connect(db_url, autocommit=True) as conn:
            conn.execute(f'CREATE SCHEMA "{schema}"')
        db = PostgresDatabase(db_url, schema=schema)
        try:
            emb = embedder(NAMES.get(spec, spec))
            kb = KnowledgeBase("eval", db=db, embed=emb)
            started = time.perf_counter()
            for kind, cs in chunks.items():
                kb.replace(kind, cs)
            index_s = time.perf_counter() - started
            for mode, weight in MODES:
                rows, lat = {"product": [], "policy": [], "exact": []}, []
                for q in queries:
                    t = time.perf_counter()
                    hits = kb.search(q["query"], kind=q["kind"], k=5, mode=mode, keyword_weight=weight)
                    lat.append((time.perf_counter() - t) * 1000)
                    ranked = [h.doc if q["kind"] == "product" else h.source for h in hits]
                    group = "exact" if q.get("style") == "exact" else q["kind"]
                    rows[group].append(score(ranked, set(q["relevant"])))
                result = {"embedder": emb.name, "mode": mode if weight is None else f"hybrid, keyword x{weight:g}",
                          "index_s": round(index_s, 2), "p50_ms": round(sorted(lat)[len(lat) // 2], 1)}
                for kind, rs in rows.items():
                    n = len(rs)
                    result[kind] = {"hit@1": round(sum(r[0] for r in rs) / n, 3), "hit@3": round(sum(r[1] for r in rs) / n, 3),
                                    "mrr@5": round(sum(r[2] for r in rs) / n, 3)}
                report["runs"].append(result)
        finally:
            db.close()
            with psycopg.connect(db_url, autocommit=True) as conn:
                conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
    return report


def table(report: dict) -> str:
    lines = ["| Embedder | Search | Products (paraphrased) hit@1 / hit@3 | Exact terms hit@1 / hit@3 | "
             "Policies hit@1 / hit@3 | p50 |", "|---|---|---|---|---|---|"]
    for r in report["runs"]:
        lines.append(f"| {r['embedder'].split(':')[-1]} | {r['mode']} | {_cell(r, 'product')} | {_cell(r, 'exact')} | "
                     f"{_cell(r, 'policy')} | {r['p50_ms']} ms |")
    return "\n".join(lines)


def _cell(run: dict, group: str) -> str:
    return f"{run[group]['hit@1']:.0%} / {run[group]['hit@3']:.0%}"


def add_parser(sub) -> None:
    p = sub.add_parser("retrieval-eval", help="score keyword, vector and hybrid search on the store's knowledge base")
    p.add_argument("--db", required=True, help="a Postgres URL (pgvector); a throwaway schema is used and dropped")
    p.add_argument("--embedders", default="hash,model2vec", help="comma-separated: hash, model2vec, fastembed")
    p.add_argument("--json", help="also write the report here")
    p.set_defaults(func=main)


def main(args) -> int:
    report = run(args.db, [e.strip() for e in args.embedders.split(",") if e.strip()])
    print(table(report))
    if args.json:
        with open(args.json, "w") as f:
            json.dump(report, f, indent=1)
    return 0
