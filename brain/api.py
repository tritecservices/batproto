"""HTTP + OpenAPI face of the brain.

This is the bridge to Foundry: Foundry Agent Service registers OpenAPI 3.0 tools,
so exposing the archive over HTTP is what lets a Foundry agent query it. The same
server is handy for Copilot Studio actions and for any internal tooling you sell.

Run:  uvicorn brain.api:app --host 0.0.0.0 --port 8077
Auth: set BRAIN_API_KEY and send it as the `X-API-Key` header.
"""
from __future__ import annotations

import hmac
import re
import os
import threading
import time
from collections import defaultdict, deque

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from .search import search as hybrid_search
from .sensitivity import RESTRICTED_TAGS
from .store import Store

# Sources that are already public on the internet. Nothing else - not Discord, not
# the file share, not NightArc - may ever be served by the unauthenticated demo.
PUBLIC_SOURCES = frozenset({"stackexchange", "discourse", "github"})
PLACEHOLDER_KEYS = {"", "change-me", "changeme"}

app = FastAPI(
    title="Ecology MSP Brain",
    version="1.0.0",
    description="Retrieval over Discord threads, GIS Q&A forums and the NightArc schema.",
    servers=[{"url": os.environ.get("BRAIN_PUBLIC_URL", "http://localhost:8077")}],
)

# Browsers on the Netlify site call the API directly (not via Netlify rewrites, which
# would burn bandwidth credits). Only the listed origins may do so.
_cors = [o.strip() for o in os.environ.get("BRAIN_CORS_ORIGINS", "").split(",") if o.strip()]
if _cors:
    app.add_middleware(CORSMiddleware, allow_origins=_cors, allow_methods=["GET"],
                       allow_headers=["x-api-key"], max_age=3600)

_store: Store | None = None


def store() -> Store:
    global _store
    if _store is None:
        _store = Store()
    return _store


def auth(x_api_key: str | None = Header(default=None)) -> None:
    """Fail CLOSED. An unset or placeholder key used to mean "no auth", which turns a
    forgotten .env into a public copy of every Discord thread and client file.
    Set BRAIN_ALLOW_NO_KEY=1 to opt out deliberately (local dev only)."""
    expected = os.environ.get("BRAIN_API_KEY", "")
    if expected.strip() in PLACEHOLDER_KEYS:
        if os.environ.get("BRAIN_ALLOW_NO_KEY") == "1":
            return
        raise HTTPException(status_code=503,
                            detail="server has no BRAIN_API_KEY configured")
    if not x_api_key or not hmac.compare_digest(x_api_key, expected):
        raise HTTPException(status_code=401, detail="bad or missing X-API-Key")


class Hit(BaseModel):
    doc_id: str
    source: str
    origin: str | None = None
    title: str | None = None
    url: str | None = None
    author: str | None = None
    created_at: int | None = None
    score: float
    tags: list[str] = Field(default_factory=list)
    excerpt: str


class SearchResponse(BaseModel):
    query: str
    count: int
    hits: list[Hit]


@app.get("/health", operation_id="health", summary="Liveness check")
def health() -> dict:
    # unauthenticated, so it says nothing about what is in the store
    return {"ok": True}


@app.get("/search", operation_id="searchBrain", summary="Search the knowledge base",
         response_model=SearchResponse, dependencies=[Depends(auth)])
def search_endpoint(
    q: str = Query(..., description="Natural-language question or error message"),
    limit: int = Query(8, ge=1, le=25),
    source: str | None = Query(None, description="discord | stackexchange | discourse | github | nightarc"),
) -> SearchResponse:
    """Hybrid keyword + semantic search. Always cite the returned url in answers."""
    hits = hybrid_search(store(), q, limit=limit, source=source)
    return SearchResponse(query=q, count=len(hits), hits=[Hit(**h) for h in hits])


class DocResponse(BaseModel):
    doc_id: str
    source: str
    title: str | None = None
    url: str | None = None
    author: str | None = None
    tags: list[str] = Field(default_factory=list)
    text: str


@app.get("/document/{doc_id:path}", operation_id="getDocument",
         summary="Fetch one full document", response_model=DocResponse,
         dependencies=[Depends(auth)])
def get_document(doc_id: str) -> DocResponse:
    d = store().get(doc_id)
    if not d:
        raise HTTPException(status_code=404, detail="no such document")
    return DocResponse(doc_id=d["id"], source=d["source"], title=d["title"],
                       url=d["url"], author=d["author"], tags=d["tags"], text=d["text"])


@app.get("/sources", operation_id="listSources", summary="Corpus inventory",
         dependencies=[Depends(auth)])
def sources() -> dict:
    return {"sources": store().sources()}


# ------------------------------------------------------------ public demo
class _RateLimiter:
    """Tiny sliding-window limiter. One process, in memory - enough for a demo box
    on the website, not a substitute for Cloudflare rate-limiting rules."""

    def __init__(self, per_minute: int):
        self.per_minute = per_minute
        self.hits: dict[str, deque] = defaultdict(deque)
        self.lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self.lock:
            q = self.hits[key]
            while q and now - q[0] > 60:
                q.popleft()
            if len(q) >= self.per_minute:
                return False
            q.append(now)
            return True


_limiter = _RateLimiter(int(os.environ.get("BRAIN_PUBLIC_RPM", "10")))


class PublicHit(BaseModel):
    source: str
    title: str | None = None
    url: str | None = None
    author: str | None = None
    created_at: int | None = None
    license: str | None = None
    excerpt: str


class PublicSearchResponse(BaseModel):
    query: str
    count: int
    hits: list[PublicHit]
    notice: str


def _excerpt(hit: dict, n: int = 280) -> str:
    """Short plain-text teaser. The full answer lives at the source link, which is
    both better for the reader and what CC BY-SA attribution expects."""
    text = hit["excerpt"]
    title = hit.get("title") or ""
    # chunks are stored with the title prepended; don't show it twice
    while title and text.lstrip("# ").startswith(title):
        text = text.lstrip("# ")[len(title):].lstrip()
    text = re.sub(r"```|^\s*(#+|>+|-{3,}.*?-{3,})\s*", " ", text, flags=re.M)
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= n else text[:n].rsplit(" ", 1)[0] + "…"


@app.get("/public/search", operation_id="publicSearch", include_in_schema=False,
         response_model=PublicSearchResponse)
def public_search(request: Request,
                  q: str = Query(..., min_length=3, max_length=200)) -> PublicSearchResponse:
    """Unauthenticated demo for the marketing site.

    Hard limits, enforced here rather than trusted to the caller: public sources only,
    anything tagged sensitive or carrying a grid reference is dropped, short excerpts,
    attribution kept (Stack Exchange content is CC BY-SA), off unless BRAIN_PUBLIC_DEMO=1.
    """
    if os.environ.get("BRAIN_PUBLIC_DEMO") != "1":
        raise HTTPException(status_code=404, detail="not found")
    client = (request.headers.get("cf-connecting-ip")
              or (request.client.host if request.client else "unknown"))
    if not _limiter.allow(client):
        raise HTTPException(status_code=429, detail="slow down - try again in a minute")
    hits = hybrid_search(store(), q, limit=5, source=PUBLIC_SOURCES,
                         exclude_tags=RESTRICTED_TAGS)
    out = [PublicHit(
        source=h["source"], title=h["title"], url=h["url"], author=h["author"],
        created_at=h["created_at"],
        # SE posts are CC BY-SA 2.5, 3.0 or 4.0 depending on date; the link states which
        license="CC BY-SA" if h["source"] == "stackexchange" else None,
        excerpt=_excerpt(h)) for h in hits]
    return PublicSearchResponse(
        query=q, count=len(out), hits=out,
        notice="Public GIS community sources only. Follow each link for the full "
               "answer and its author.")


@app.get("/openapi-for-foundry", operation_id="openapiForFoundry", include_in_schema=False)
def openapi_for_foundry() -> dict:
    """OpenAPI shaped the way Foundry Agent Service wants it.

    Foundry requires OpenAPI 3.0/3.1, an explicit `servers` entry, and -- for API key
    auth -- a `securitySchemes` entry whose header name matches the key name in the
    Foundry project connection. FastAPI won't emit that from a Header dependency, so
    it is injected here.
    """
    spec = dict(app.openapi())
    spec["openapi"] = "3.0.3"
    spec["paths"] = {p: v for p, v in spec["paths"].items() if p != "/openapi-for-foundry"}
    spec.setdefault("components", {})["securitySchemes"] = {
        # lowercase deliberately: a Foundry custom-keys connection must use a Key
        # string that matches this `name` exactly, and Microsoft's documented
        # example is lowercase. HTTP headers are case-insensitive so FastAPI still
        # accepts X-API-Key from curl, Claude, or anything else.
        "apiKeyHeader": {"type": "apiKey", "name": "x-api-key", "in": "header"}
    }
    spec["security"] = [{"apiKeyHeader": []}]
    # the key travels via the project connection, so it must not also be a parameter
    for path in spec["paths"].values():
        for op in path.values():
            if isinstance(op, dict) and "parameters" in op:
                op["parameters"] = [
                    p for p in op["parameters"]
                    if not (p.get("in") == "header"
                            and p.get("name", "").lower() == "x-api-key")
                ]
    return spec
