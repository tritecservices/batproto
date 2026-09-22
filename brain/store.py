"""Local knowledge store: SQLite + FTS5 keyword index + optional sqlite-vec vectors.

One file on disk, no server. Keep it on LOCAL disk: SQLite WAL locking over SMB/NFS
corrupts the database. Copy snapshots to the NAS with scripts/promote-db.sh instead.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import struct
import threading
import time
from dataclasses import dataclass, field
from typing import Iterable, Sequence

DEFAULT_DB = os.environ.get("BRAIN_DB", "data/brain.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id           TEXT PRIMARY KEY,
    source       TEXT NOT NULL,        -- discord | stackexchange | discourse | nightarc | manual
    origin       TEXT,                 -- guild/channel, site, server name
    url          TEXT,
    title        TEXT,
    author       TEXT,
    created_at   INTEGER,              -- unix seconds
    ingested_at  INTEGER NOT NULL,
    tags         TEXT,                 -- json list
    meta         TEXT,                 -- json object
    text         TEXT NOT NULL,
    text_sha     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_doc_source  ON documents(source);
CREATE INDEX IF NOT EXISTS ix_doc_created ON documents(created_at);

CREATE TABLE IF NOT EXISTS chunks (
    id       TEXT PRIMARY KEY,
    doc_id   TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    ord      INTEGER NOT NULL,
    text     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_chunk_doc ON chunks(doc_id);

CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    text, chunk_id UNINDEXED, tokenize='porter unicode61'
);

CREATE TABLE IF NOT EXISTS embeddings (
    chunk_id TEXT PRIMARY KEY REFERENCES chunks(id) ON DELETE CASCADE,
    model    TEXT NOT NULL,
    dim      INTEGER NOT NULL,
    vec      BLOB NOT NULL
);

-- incremental ingest bookmarks, e.g. last message id per discord channel
CREATE TABLE IF NOT EXISTS cursors (
    key     TEXT PRIMARY KEY,
    value   TEXT,
    updated INTEGER
);
"""


@dataclass
class Document:
    source: str
    text: str
    id: str | None = None
    origin: str | None = None
    url: str | None = None
    title: str | None = None
    author: str | None = None
    created_at: int | None = None
    tags: list[str] = field(default_factory=list)
    meta: dict = field(default_factory=dict)

    def key(self) -> str:
        if self.id:
            return self.id
        basis = self.url or f"{self.source}:{self.title}:{self.text[:200]}"
        return hashlib.sha1(basis.encode("utf-8")).hexdigest()


def pack(vec: Sequence[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


def unpack(blob: bytes) -> list[float]:
    return list(struct.unpack(f"{len(blob) // 4}f", blob))


def chunk_text(text: str, target: int = 1200, overlap: int = 150) -> list[str]:
    """Paragraph-aware chunking. Keeps code blocks and error messages intact."""
    text = text.strip()
    if not text:
        return []
    if len(text) <= target:
        return [text]
    # never split inside a fenced code block
    parts, buf = [], []
    size = 0
    for para in re.split(r"\n\s*\n", text):
        if size + len(para) > target and buf:
            parts.append("\n\n".join(buf))
            tail = "\n\n".join(buf)[-overlap:]
            buf, size = ([tail] if overlap else []), len(tail)
        buf.append(para)
        size += len(para) + 2
    if buf:
        parts.append("\n\n".join(buf))
    return [p for p in (p.strip() for p in parts) if p]


class Store:
    """Thread-safe by giving each thread its own connection.

    Matters in practice: uvicorn runs sync endpoints in a worker threadpool, and a
    single sqlite3 connection raises "created in a thread" errors there. WAL mode
    lets those connections read concurrently alongside one writer.
    """

    def __init__(self, path: str = DEFAULT_DB):
        self.path = path
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        self._local = threading.local()
        self._write_lock = threading.RLock()
        self.db.executescript(SCHEMA)          # first access creates the connection

    @property
    def db(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=30)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=30000")
            self._local.conn = conn
            self.vec_ok = self._load_vec(conn)
        return conn

    @staticmethod
    def _load_vec(conn: sqlite3.Connection) -> bool:
        try:
            import sqlite_vec  # noqa: F401

            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            conn.enable_load_extension(False)
            return True
        except Exception:
            return False  # brute-force cosine fallback; fine to ~200k chunks

    # ------------------------------------------------------------------ write
    def upsert(self, doc: Document) -> tuple[str, bool]:
        """Insert or update. Returns (doc_id, changed). Unchanged text is a no-op."""
        did = doc.key()
        sha = hashlib.sha1(doc.text.encode("utf-8")).hexdigest()
        row = self.db.execute("SELECT text_sha FROM documents WHERE id=?", (did,)).fetchone()
        if row and row["text_sha"] == sha:
            return did, False
        now = int(time.time())
        with self._write_lock:
            self._insert(did, doc, sha, now)
        return did, True

    def _insert(self, did: str, doc: Document, sha: str, now: int) -> None:
        self.db.execute(
            """INSERT INTO documents
               (id,source,origin,url,title,author,created_at,ingested_at,tags,meta,text,text_sha)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                 source=excluded.source, origin=excluded.origin, url=excluded.url,
                 title=excluded.title, author=excluded.author, created_at=excluded.created_at,
                 ingested_at=excluded.ingested_at, tags=excluded.tags, meta=excluded.meta,
                 text=excluded.text, text_sha=excluded.text_sha""",
            (did, doc.source, doc.origin, doc.url, doc.title, doc.author,
             doc.created_at, now, json.dumps(doc.tags), json.dumps(doc.meta), doc.text, sha),
        )
        self._reindex(did, doc)
        self.db.commit()

    def _reindex(self, did: str, doc: Document) -> None:
        old = [r["id"] for r in self.db.execute("SELECT id FROM chunks WHERE doc_id=?", (did,))]
        if old:
            qs = ",".join("?" * len(old))
            self.db.execute(f"DELETE FROM chunks_fts WHERE chunk_id IN ({qs})", old)
            self.db.execute(f"DELETE FROM embeddings WHERE chunk_id IN ({qs})", old)
            self.db.execute("DELETE FROM chunks WHERE doc_id=?", (did,))
        header = f"{doc.title}\n" if doc.title else ""
        for i, ch in enumerate(chunk_text(doc.text)):
            cid = f"{did}:{i}"
            body = header + ch
            self.db.execute("INSERT INTO chunks (id,doc_id,ord,text) VALUES (?,?,?,?)",
                            (cid, did, i, body))
            self.db.execute("INSERT INTO chunks_fts (text,chunk_id) VALUES (?,?)", (body, cid))

    def bulk_upsert(self, docs: Iterable[Document]) -> dict:
        added = skipped = 0
        for d in docs:
            _, changed = self.upsert(d)
            added += changed
            skipped += not changed
        return {"written": added, "unchanged": skipped}

    # -------------------------------------------------------------- embeddings
    def pending_chunks(self, model: str, limit: int = 500) -> list[sqlite3.Row]:
        return list(self.db.execute(
            """SELECT c.id, c.text FROM chunks c
               LEFT JOIN embeddings e ON e.chunk_id=c.id AND e.model=?
               WHERE e.chunk_id IS NULL LIMIT ?""", (model, limit)))

    def save_embeddings(self, model: str, rows: list[tuple[str, Sequence[float]]]) -> None:
        with self._write_lock:
            self.db.executemany(
                "INSERT OR REPLACE INTO embeddings (chunk_id,model,dim,vec) VALUES (?,?,?,?)",
                [(cid, model, len(v), pack(v)) for cid, v in rows])
            self.db.commit()

    # ------------------------------------------------------------------- read
    def get(self, doc_id: str) -> dict | None:
        r = self.db.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["tags"] = json.loads(d["tags"] or "[]")
        d["meta"] = json.loads(d["meta"] or "{}")
        return d

    def sources(self) -> list[dict]:
        return [dict(r) for r in self.db.execute(
            """SELECT source, COUNT(*) n, MIN(created_at) oldest, MAX(created_at) newest
               FROM documents GROUP BY source ORDER BY n DESC""")]

    def get_cursor(self, key: str) -> str | None:
        r = self.db.execute("SELECT value FROM cursors WHERE key=?", (key,)).fetchone()
        return r["value"] if r else None

    def set_cursor(self, key: str, value: str) -> None:
        with self._write_lock:
            self._set_cursor(key, value)

    def _set_cursor(self, key: str, value: str) -> None:
        self.db.execute(
            "INSERT INTO cursors (key,value,updated) VALUES (?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated=excluded.updated",
            (key, value, int(time.time())))
        self.db.commit()

    def delete_prefix(self, prefix: str, keep: set[str] | None = None) -> int:
        """Delete every document whose id starts with `prefix`, except those in
        `keep`. Used when a source is re-ingested and some documents no longer exist
        (a manual's new edition has fewer pages). Returns how many were removed."""
        keep = keep or set()
        with self._write_lock:
            ids = [r["id"] for r in self.db.execute(
                "SELECT id FROM documents WHERE id LIKE ? ESCAPE '\\'",
                (prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%",))
                if r["id"] not in keep]
            for did in ids:
                chunk_ids = [r["id"] for r in self.db.execute(
                    "SELECT id FROM chunks WHERE doc_id=?", (did,))]
                if chunk_ids:
                    qs = ",".join("?" * len(chunk_ids))
                    self.db.execute(f"DELETE FROM chunks_fts WHERE chunk_id IN ({qs})", chunk_ids)
                    self.db.execute(f"DELETE FROM embeddings WHERE chunk_id IN ({qs})", chunk_ids)
                self.db.execute("DELETE FROM chunks WHERE doc_id=?", (did,))
                self.db.execute("DELETE FROM documents WHERE id=?", (did,))
            self.db.commit()
        return len(ids)

    def close(self) -> None:
        if conn := getattr(self._local, "conn", None):
            conn.close()
            self._local.conn = None
