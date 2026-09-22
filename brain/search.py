"""Hybrid retrieval: FTS5 (BM25) + vector cosine, fused with Reciprocal Rank Fusion."""
from __future__ import annotations

import json
import math
import re

from .embed import get_embedder
from .store import Store, unpack

_FTS_SAFE = re.compile(r"[^\w\s]")


def _fts_query(q: str) -> str:
    """Turn free text into a safe FTS5 query. Drops operators users didn't mean."""
    words = _FTS_SAFE.sub(" ", q).split()
    return " OR ".join(f'"{w}"' for w in words) if words else '""'


def _source_clause(source) -> tuple[str, list]:
    """source may be None, one name, or a list/tuple/set of names."""
    if not source:
        return "", []
    if isinstance(source, str):
        return " AND d.source = ?", [source]
    names = sorted(source)
    return f" AND d.source IN ({','.join('?' * len(names))})", names


def keyword_search(store: Store, query: str, limit: int = 40, source: str | None = None):
    sql = """SELECT f.chunk_id, bm25(chunks_fts) AS score
             FROM chunks_fts f
             JOIN chunks c ON c.id = f.chunk_id
             JOIN documents d ON d.id = c.doc_id
             WHERE chunks_fts MATCH ?"""
    args: list = [_fts_query(query)]
    clause, extra = _source_clause(source)
    sql += clause
    args += extra
    sql += " ORDER BY score LIMIT ?"          # bm25 is negative; lower is better
    args.append(limit)
    return [(r["chunk_id"], -r["score"]) for r in store.db.execute(sql, args)]


def vector_search(store: Store, query: str, limit: int = 40, source: str | None = None,
                  embedder=None):
    emb = embedder or get_embedder()
    qv = emb.encode([query])[0]
    sql = """SELECT e.chunk_id, e.vec FROM embeddings e
             JOIN chunks c ON c.id = e.chunk_id
             JOIN documents d ON d.id = c.doc_id
             WHERE e.model = ?"""
    args: list = [emb.name]
    clause, extra = _source_clause(source)
    sql += clause
    args += extra
    rows = store.db.execute(sql, args).fetchall()
    if not rows:
        return []

    # This is a brute-force scan, which is correct and fine into the low hundreds of
    # thousands of chunks. Two things make it cheap: the query is normalised once
    # rather than per row, and numpy does the dot products in C when available.
    try:
        import numpy as np

        q = np.asarray(qv, dtype=np.float32)
        q /= np.linalg.norm(q) or 1.0
        mat = np.frombuffer(b"".join(r["vec"] for r in rows), dtype=np.float32)
        mat = mat.reshape(len(rows), -1)
        norms = np.linalg.norm(mat, axis=1)
        norms[norms == 0] = 1.0
        sims = (mat @ q) / norms
        order = np.argsort(-sims)[:limit]
        return [(rows[i]["chunk_id"], float(sims[i])) for i in order]
    except ImportError:
        pass

    nq = math.sqrt(sum(a * a for a in qv)) or 1.0
    qn = [a / nq for a in qv]                      # normalise once, not per row
    scored = []
    for r in rows:
        v = unpack(r["vec"])
        nv = math.sqrt(sum(b * b for b in v)) or 1.0
        scored.append((r["chunk_id"], sum(a * b for a, b in zip(qn, v)) / nv))
    scored.sort(key=lambda x: -x[1])
    return scored[:limit]


def _tag_names(raw) -> list[str]:
    """Tags are strings, except that newer Discourse versions return tag objects
    ({"id", "name", "slug"}) and older ingests stored them verbatim."""
    out = []
    for t in raw or []:
        if isinstance(t, dict):
            t = t.get("name") or t.get("slug")
        if t:
            out.append(str(t))
    return out


def rrf(*rankings: list[tuple[str, float]], k: int = 60) -> list[tuple[str, float]]:
    fused: dict[str, float] = {}
    for ranking in rankings:
        for rank, (cid, _) in enumerate(ranking, start=1):
            fused[cid] = fused.get(cid, 0.0) + 1.0 / (k + rank)
    return sorted(fused.items(), key=lambda x: -x[1])


def search(store: Store, query: str, limit: int = 8, source=None,
           embedder=None, use_vectors: bool = True,
           exclude_tags: set[str] | None = None) -> list[dict]:
    """Return deduplicated hits, best chunk per document, newest-first on ties."""
    kw = keyword_search(store, query, limit=limit * 5, source=source)
    rankings = [kw]
    if use_vectors:
        try:
            rankings.append(vector_search(store, query, limit=limit * 5,
                                          source=source, embedder=embedder))
        except Exception:
            pass  # no embeddings yet -> keyword only
    results, seen = [], set()
    for cid, score in rrf(*rankings):
        row = store.db.execute(
            """SELECT c.text AS chunk, c.ord, d.* FROM chunks c
               JOIN documents d ON d.id = c.doc_id WHERE c.id = ?""", (cid,)).fetchone()
        if not row or row["id"] in seen:
            continue
        seen.add(row["id"])
        tags = _tag_names(json.loads(row["tags"] or "[]"))
        if exclude_tags and exclude_tags.intersection(tags):
            continue
        results.append({
            "doc_id": row["id"],
            "source": row["source"],
            "origin": row["origin"],
            "title": row["title"],
            "url": row["url"],
            "author": row["author"],
            "created_at": row["created_at"],
            "score": round(score, 6),
            "tags": tags,
            "excerpt": row["chunk"][:1500],
        })
        if len(results) >= limit:
            break
    return results
