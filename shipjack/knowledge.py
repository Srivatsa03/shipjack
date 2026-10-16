"""The store's knowledge base: its products and policy pages, searchable by meaning and by keywords.

Agents reach it through the store's MCP server (``search_store_docs``) and product search. Every hit
carries its source, so an answer can cite the page it came from.

On Postgres, each chunk has a pgvector embedding and a full-text vector. Three modes: ``vector``
(the default), ``keyword`` (full-text ranking), and ``hybrid``, which merges both rankings with
weighted reciprocal rank fusion (RRF) so an exact term (a SKU) still counts when the embedding misses
it. The default was chosen by measurement: on the store's 48-question eval, vector alone beat every
hybrid weighting (docs/RETRIEVAL.md); hybrid is there for catalogs with many SKUs. On SQLite or in memory, it falls back to BM25 keyword
ranking only.

Embedding models are pluggable (``embedder_from_env``):

* ``model2vec:minishlab/potion-base-8M`` (default): static embeddings, about 140 MB of memory, so it
  fits a 512 MB server next to the store;
* ``fastembed:BAAI/bge-small-en-v1.5``: a small transformer, better on paraphrases, about 340 MB;
* ``hash``: a deterministic bag-of-words hash, for tests (no download, no model).
"""

from __future__ import annotations

import hashlib
import math
import os
import re
import threading
from collections.abc import Sequence
from dataclasses import dataclass

RRF_K = 60  # the usual reciprocal-rank-fusion constant: a hit at rank r scores 1 / (60 + r)
KEYWORD_WEIGHT = float(os.environ.get("SHIPJACK_KEYWORD_WEIGHT", "0.25"))  # how much the keyword ranking counts in hybrid
# Vector by default: on the store's eval (docs/RETRIEVAL.md) it beat hybrid; hybrid stays one setting away.
SEARCH_MODE = os.environ.get("SHIPJACK_SEARCH_MODE", "vector")

_STOP = frozenset(["a", "an", "and", "are", "as", "at", "be", "by", "can", "do", "does", "for", "from", "has", "have", "how", "i", "if", "in", "is", "it", "its", "my", "of", "on", "or", "so", "that", "the", "their", "them", "they", "this", "to", "was", "we", "what", "when", "where", "which", "who", "will", "with", "you", "your", "me", "our", "us"])


def words(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in _STOP and len(w) > 1]


def stem(w: str) -> str:
    for suffix in ("ing", "ed", "es", "s"):
        if w.endswith(suffix) and len(w) - len(suffix) >= 3:
            return w[: -len(suffix)]
    return w


# ---- embedders -------------------------------------------------------------------------------------


class HashEmbedder:
    """A deterministic bag-of-words embedding (stemmed words hashed into buckets). For tests only:
    it knows nothing about meaning, but it is stable and needs no download."""

    name = "hash"

    def __init__(self, dims: int = 256):
        self.dims = dims

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        out = []
        for text in texts:
            v = [0.0] * self.dims
            for w in words(text):
                h = int.from_bytes(hashlib.blake2b(stem(w).encode(), digest_size=4).digest(), "big")
                v[h % self.dims] += 1.0
            out.append(_unit(v))
        return out


class Model2VecEmbedder:
    def __init__(self, model: str = "minishlab/potion-base-8M"):
        from model2vec import StaticModel
        self.name = f"model2vec:{model}"
        self._model = StaticModel.from_pretrained(model)
        self.dims = int(self._model.encode(["x"]).shape[1])

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [_unit(list(map(float, v))) for v in self._model.encode(list(texts))]


class FastEmbedder:
    def __init__(self, model: str = "BAAI/bge-small-en-v1.5"):
        from fastembed import TextEmbedding
        self.name = f"fastembed:{model}"
        self._model = TextEmbedding(model)
        self.dims = len(next(iter(self._model.embed(["x"]))))

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [_unit(list(map(float, v))) for v in self._model.embed(list(texts))]


_cache: dict[str, object] = {}
_cache_lock = threading.Lock()


def embedder(spec: str):
    """``hash``, ``model2vec[:model]`` or ``fastembed[:model]``; loaded once per process."""
    with _cache_lock:
        if spec not in _cache:
            kind, _, model = spec.partition(":")
            if kind == "hash":
                _cache[spec] = HashEmbedder()
            elif kind == "model2vec":
                _cache[spec] = Model2VecEmbedder(model or "minishlab/potion-base-8M")
            elif kind == "fastembed":
                _cache[spec] = FastEmbedder(model or "BAAI/bge-small-en-v1.5")
            else:
                raise ValueError(f"unknown embedder {spec!r}: use hash, model2vec[:model] or fastembed[:model]")
        return _cache[spec]


def embedder_from_env():
    return embedder(os.environ.get("SHIPJACK_EMBEDDER", "model2vec:minishlab/potion-base-8M"))


def _unit(v: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


# ---- chunks ----------------------------------------------------------------------------------------


@dataclass
class Chunk:
    id: str
    kind: str      # "product" or "policy"
    doc: str       # product id, or the document's slug
    title: str
    source: str    # what an answer cites
    text: str


def policy_chunks(markdown: str, slug: str, max_words: int = 180) -> list[Chunk]:
    """Split a policy page on its ``##`` headings, and long sections on paragraphs, so each chunk is
    one topic an agent can quote."""
    out: list[Chunk] = []
    sections = re.split(r"^## ", markdown, flags=re.MULTILINE)
    head = sections[0].strip()
    if head and not head.startswith("# "):
        sections[0] = "Overview\n" + head
    else:
        sections = sections[1:]
    for block in sections:
        title, _, body = block.partition("\n")
        title = title.strip() or "Overview"
        paras = [" ".join(p.split()) for p in re.split(r"\n\s*\n", body) if p.strip()]
        part, size, n = [], 0, 0
        for para in paras + [None]:
            if para is None or (part and size + len(para.split()) > max_words):
                if part:
                    n += 1
                    anchor = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
                    out.append(Chunk(f"{slug}#{anchor}" + (f"-{n}" if n > 1 else ""), "policy", slug, title,
                                     f"{slug}#{anchor}", " ".join(part)))
                part, size = [], 0
            if para is not None:
                part.append(para)
                size += len(para.split())
    return out


def product_chunk(p) -> Chunk:
    price = f"${p.price / 100:,.2f}"
    kind = "digital, delivered by email" if p.kind == "digital" else "ships to an address"
    return Chunk(f"product:{p.id}", "product", p.id, p.title, f"catalog:{p.id}",
                 f"{p.title}. {p.description} Price {price}. {kind}.".strip())


# ---- the index ---------------------------------------------------------------------------------------

PG_KB = """
CREATE EXTENSION IF NOT EXISTS vector;
CREATE TABLE IF NOT EXISTS kb_meta (merchant TEXT PRIMARY KEY, embedder TEXT NOT NULL, dims INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS kb_chunks (merchant TEXT NOT NULL, id TEXT NOT NULL, kind TEXT NOT NULL, doc TEXT NOT NULL,
                                      title TEXT NOT NULL, source TEXT NOT NULL, text TEXT NOT NULL,
                                      embedding vector, PRIMARY KEY (merchant, id),
                                      tsv tsvector GENERATED ALWAYS AS
                                          (setweight(to_tsvector('english', title), 'A') ||
                                           setweight(to_tsvector('english', text), 'B')) STORED);
CREATE INDEX IF NOT EXISTS kb_tsv ON kb_chunks USING gin (tsv);
"""


@dataclass
class Hit:
    id: str
    source: str
    title: str
    text: str
    kind: str
    doc: str
    score: float
    vector_rank: int | None = None
    keyword_rank: int | None = None

    def as_dict(self) -> dict:
        return {"title": self.title, "text": self.text, "source": self.source, "kind": self.kind, "doc": self.doc,
                "score": round(self.score, 5), "vector_rank": self.vector_rank, "keyword_rank": self.keyword_rank}


class KnowledgeBase:
    """One merchant's chunks. ``db`` is a store Database (Postgres gets hybrid search) or None (memory)."""

    def __init__(self, merchant: str, db=None, embed=None):
        self.merchant, self.db = merchant, db
        self.vector = db is not None and getattr(db, "dialect", "") == "postgres"
        self._embed = embed  # an embedder, or None to load the configured one on first use
        self._chunks: dict[str, Chunk] = {}  # the memory and SQLite fallback keeps chunks here
        if self.vector:
            self.db.conn().raw.execute(PG_KB)

    @property
    def embed(self):
        if self._embed is None:
            self._embed = embedder_from_env()
        return self._embed

    # ---- writing ----------------------------------------------------------------------------------
    def replace(self, kind: str, chunks: list[Chunk], doc: str | None = None) -> int:
        """Replace every chunk of ``kind`` (or only one document's) with ``chunks``. Returns how many."""
        if not self.vector:
            for cid in [c for c, v in self._chunks.items() if v.kind == kind and (doc is None or v.doc == doc)]:
                del self._chunks[cid]
            self._chunks.update({c.id: c for c in chunks})
            return len(chunks)
        vectors = self.embed.embed([f"{c.title}. {c.text}" for c in chunks]) if chunks else []
        with self.db.transaction() as conn:
            row = conn.execute("SELECT embedder FROM kb_meta WHERE merchant = ?", (self.merchant,)).fetchone()
            if row is not None and row[0] != self.embed.name:  # a new model: the old vectors don't compare
                conn.execute("UPDATE kb_chunks SET embedding = NULL WHERE merchant = ?", (self.merchant,))
            conn.execute("INSERT INTO kb_meta (merchant, embedder, dims) VALUES (?, ?, ?) ON CONFLICT (merchant) "
                         "DO UPDATE SET embedder = excluded.embedder, dims = excluded.dims",
                         (self.merchant, self.embed.name, self.embed.dims))
            if doc is None:
                conn.execute("DELETE FROM kb_chunks WHERE merchant = ? AND kind = ?", (self.merchant, kind))
            else:
                conn.execute("DELETE FROM kb_chunks WHERE merchant = ? AND kind = ? AND doc = ?", (self.merchant, kind, doc))
            for c, v in zip(chunks, vectors, strict=True):
                conn.execute("INSERT INTO kb_chunks (merchant, id, kind, doc, title, source, text, embedding) VALUES "
                             "(?, ?, ?, ?, ?, ?, ?, ?::vector) ON CONFLICT (merchant, id) DO UPDATE SET kind = "
                             "excluded.kind, doc = excluded.doc, title = excluded.title, source = excluded.source, "
                             "text = excluded.text, embedding = excluded.embedding",
                             (self.merchant, c.id, c.kind, c.doc, c.title, c.source, c.text, _vec(v)))
        return len(chunks)

    def documents(self) -> list[dict]:
        if not self.vector:
            seen: dict[tuple, dict] = {}
            for c in self._chunks.values():
                seen.setdefault((c.kind, c.doc), {"kind": c.kind, "doc": c.doc, "chunks": 0})["chunks"] += 1
            return sorted(seen.values(), key=lambda d: (d["kind"], d["doc"]))
        rows = self.db.read("SELECT kind, doc, count(*) FROM kb_chunks WHERE merchant = ? GROUP BY kind, doc "
                            "ORDER BY kind, doc", (self.merchant,))
        return [{"kind": r[0], "doc": r[1], "chunks": r[2]} for r in rows]

    # ---- searching -----------------------------------------------------------------------------
    def search(self, query: str, *, kind: str | None = None, k: int = 4, mode: str | None = None,
               keyword_weight: float | None = None) -> list[Hit]:
        """``mode``: vector (the default, SHIPJACK_SEARCH_MODE), hybrid or keyword. Without Postgres it is
        always keyword. A vector search that finds nothing falls back to keywords."""
        mode = mode or SEARCH_MODE
        if not query.strip():
            return []
        if not self.vector:
            return self._keyword_memory(query, kind, k)
        depth = max(k * 5, 20)
        by_vector = self._vector(query, kind, depth) if mode in ("hybrid", "vector") else []
        by_keyword = self._keyword_pg(query, kind, depth) if mode in ("hybrid", "keyword") else []
        fused: dict[str, Hit] = {}
        for ranked, attr in ((by_vector, "vector_rank"), (by_keyword, "keyword_rank")):
            for rank, hit in enumerate(ranked, 1):
                setattr(fused.setdefault(hit.id, hit), attr, rank)
        if mode == "vector" and not by_vector:  # e.g. chunks indexed before an embedder change
            by_keyword = self._keyword_pg(query, kind, depth)
        w = KEYWORD_WEIGHT if keyword_weight is None else keyword_weight
        for hit in fused.values():  # weighted reciprocal rank fusion: found both ways ranks highest
            hit.score = (1.0 / (RRF_K + hit.vector_rank) if hit.vector_rank else 0.0) + \
                (w / (RRF_K + hit.keyword_rank) if hit.keyword_rank else 0.0)
        return sorted(fused.values(), key=lambda h: (-h.score, h.id))[:k]

    def _where(self, kind: str | None) -> tuple[str, tuple]:
        return ("merchant = ?" + (" AND kind = ?" if kind else ""), (self.merchant, kind) if kind else (self.merchant,))

    def _vector(self, query: str, kind: str | None, n: int) -> list[Hit]:
        where, args = self._where(kind)
        q = _vec(self.embed.embed([query])[0])
        rows = self.db.read(f"SELECT id, source, title, text, kind, doc, 1 - (embedding <=> ?::vector) FROM kb_chunks "
                            f"WHERE {where} AND embedding IS NOT NULL ORDER BY embedding <=> ?::vector LIMIT ?",
                            (q, *args, q, n))
        return [Hit(r[0], r[1], r[2], r[3], r[4], r[5], float(r[6])) for r in rows]

    def _keyword_pg(self, query: str, kind: str | None, n: int) -> list[Hit]:
        where, args = self._where(kind)
        terms = " | ".join(sorted({stem(w) for w in words(query)}))  # any word may match; ts_rank orders them
        if not terms:
            return []
        rows = self.db.read(f"SELECT id, source, title, text, kind, doc, ts_rank_cd(tsv, q) FROM kb_chunks, "
                            f"to_tsquery('english', ?) q WHERE {where} AND tsv @@ q ORDER BY 7 DESC, id LIMIT ?",
                            (terms, *args, n))
        return [Hit(r[0], r[1], r[2], r[3], r[4], r[5], float(r[6])) for r in rows]

    def _keyword_memory(self, query: str, kind: str | None, k: int) -> list[Hit]:
        """BM25 over the chunks (title words count twice): rare words weigh more, repeated words count."""
        want = {stem(w) for w in words(query)}
        docs = [c for c in self._chunks.values() if not kind or c.kind == kind]
        if not want or not docs:
            return []
        bags = [[stem(w) for w in words(c.title) * 2 + words(c.text)] for c in docs]
        avg = sum(len(b) for b in bags) / len(bags)
        df = {t: sum(1 for b in bags if t in b) for t in want}
        scored = []
        for c, bag in zip(docs, bags, strict=True):
            score = 0.0
            for t in want:
                tf = bag.count(t)
                if tf:
                    idf = math.log(1 + (len(docs) - df[t] + 0.5) / (df[t] + 0.5))
                    score += idf * tf * 2.2 / (tf + 1.2 * (0.25 + 0.75 * len(bag) / avg))  # k1 = 1.2, b = 0.75
            if score:
                scored.append(Hit(c.id, c.source, c.title, c.text, c.kind, c.doc, score))
        ranked = sorted(scored, key=lambda h: (-h.score, h.id))[:k]
        for rank, hit in enumerate(ranked, 1):
            hit.keyword_rank = rank
        return ranked


def _vec(v: Sequence[float]) -> str:
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]"


def load_policies(path: str | None = None) -> str:
    from importlib import resources
    if path:
        with open(path) as f:
            return f.read()
    return resources.files("shipjack").joinpath("data/northwind_policies.md").read_text()


