"""Pluggable embedders. Pick with BRAIN_EMBEDDER=hash|openai|azure|local."""
from __future__ import annotations

import hashlib
import math
import os
import re
from typing import Protocol, Sequence


class Embedder(Protocol):
    name: str
    dim: int

    def encode(self, texts: Sequence[str]) -> list[list[float]]: ...


class HashEmbedder:
    """Zero-dependency hashed bag-of-words. No API key, no network, works offline.

    Weaker than a real model but good enough to smoke-test the pipeline and to
    run hybrid search where FTS5 carries most of the signal.
    """

    name = "hash-1024"
    dim = 1024

    _token = re.compile(r"[a-z0-9_]+")

    def encode(self, texts: Sequence[str]) -> list[list[float]]:
        out = []
        for t in texts:
            v = [0.0] * self.dim
            toks = self._token.findall(t.lower())
            for tok in toks:
                h = int.from_bytes(hashlib.md5(tok.encode()).digest()[:4], "little")
                v[h % self.dim] += 1.0
            n = math.sqrt(sum(x * x for x in v)) or 1.0
            out.append([x / n for x in v])
        return out


class OpenAIEmbedder:
    """OpenAI or any OpenAI-compatible endpoint (also covers Azure OpenAI)."""

    def __init__(self, model: str | None = None, dim: int = 1536):
        from openai import OpenAI  # lazy import

        self.name = model or os.environ.get("BRAIN_EMBED_MODEL", "text-embedding-3-small")
        self.dim = dim
        self.client = OpenAI(
            api_key=os.environ.get("OPENAI_API_KEY"),
            base_url=os.environ.get("OPENAI_BASE_URL") or None,
        )

    def encode(self, texts: Sequence[str]) -> list[list[float]]:
        res = self.client.embeddings.create(model=self.name, input=list(texts))
        return [d.embedding for d in res.data]


class AzureOpenAIEmbedder:
    def __init__(self, deployment: str | None = None, dim: int = 1536):
        from openai import AzureOpenAI

        self.name = deployment or os.environ["AZURE_EMBED_DEPLOYMENT"]
        self.dim = dim
        self.client = AzureOpenAI(
            api_key=os.environ.get("AZURE_OPENAI_API_KEY"),
            azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
            api_version=os.environ.get("AZURE_OPENAI_API_VERSION", "2024-10-21"),
        )

    def encode(self, texts: Sequence[str]) -> list[list[float]]:
        res = self.client.embeddings.create(model=self.name, input=list(texts))
        return [d.embedding for d in res.data]


class LocalEmbedder:
    """sentence-transformers, fully offline. Best privacy/quality trade-off."""

    def __init__(self, model: str = "BAAI/bge-small-en-v1.5"):
        from sentence_transformers import SentenceTransformer

        self.name = model
        self.st = SentenceTransformer(model)
        self.dim = self.st.get_sentence_embedding_dimension()

    def encode(self, texts: Sequence[str]) -> list[list[float]]:
        return self.st.encode(list(texts), normalize_embeddings=True).tolist()


def get_embedder(kind: str | None = None) -> Embedder:
    kind = (kind or os.environ.get("BRAIN_EMBEDDER", "hash")).lower()
    return {
        "hash": HashEmbedder,
        "openai": OpenAIEmbedder,
        "azure": AzureOpenAIEmbedder,
        "local": LocalEmbedder,
    }[kind]()


def embed_pending(store, embedder: Embedder | None = None, batch: int = 128) -> int:
    """Fill in embeddings for any chunk that lacks one. Safe to re-run."""
    emb = embedder or get_embedder()
    total = 0
    while True:
        rows = store.pending_chunks(emb.name, limit=batch)
        if not rows:
            return total
        vecs = emb.encode([r["text"] for r in rows])
        store.save_embeddings(emb.name, list(zip([r["id"] for r in rows], vecs)))
        total += len(rows)
