"""HTTP + OpenAPI face of the brain.

This is the bridge to Foundry: Foundry Agent Service registers OpenAPI 3.0 tools,
so exposing the archive over HTTP is what lets a Foundry agent query it. The same
server is handy for Copilot Studio actions and for any internal tooling you sell.

Run:  uvicorn brain.api:app --host 0.0.0.0 --port 8077
Auth: set BRAIN_API_KEY and send it as the `X-API-Key` header.
"""
from __future__ import annotations

import re
import os
import threading
import time
from collections import defaultdict, deque

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from .search import search as hybrid_search
from . import audit as access_audit
from .identity import AuthError, Principal, authenticate, auth_mode, require, tenant_db_path
from .sensitivity import RESTRICTED_TAGS
from .store import Store
from . import secrets as _secrets

# Pull secrets from Azure Key Vault (if configured) before anything reads the
# environment below. Fails closed: a configured but unreachable vault stops start-up.
_secrets.ensure_loaded()

# Sources that are already public on the internet. Nothing else - not Discord, not
# the file share, not NightArc - may ever be served by the unauthenticated demo.
PUBLIC_SOURCES = frozenset({"stackexchange", "discourse", "github", "manuals"})
# (manuals are only public when their config says so: everything else carries the
#  `internal-only` tag, which the public endpoint drops along with sensitive data)

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


def principal(authorization: str | None = Header(default=None),
              x_api_key: str | None = Header(default=None)) -> Principal:
    """Who is calling. Fails closed: an unset or placeholder key, or Entra sign-in
    without its settings, is a 503, never an open door. See brain/identity.py."""
    try:
        return authenticate(authorization, x_api_key)
    except AuthError as exc:
        headers = {"WWW-Authenticate": "Bearer"} if exc.status == 401 else None
        raise HTTPException(status_code=exc.status, detail=exc.message, headers=headers)


def reader(p: Principal = Depends(principal)) -> Principal:
    try:
        return require(p, "Brain.Read")
    except AuthError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.message)


_tenant_stores: dict[str, Store] = {}
_tenant_lock = threading.Lock()


def store_for(p: Principal) -> Store:
    """Tenant isolation: Entra callers get their own tenant's database when
    BRAIN_TENANT_DB_DIR is set; everything else uses the default store."""
    default = store()
    try:
        path = tenant_db_path(p, default.path)
    except AuthError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.message)
    if path == default.path:
        return default
    with _tenant_lock:
        if path not in _tenant_stores:
            _tenant_stores[path] = Store(path)
        return _tenant_stores[path]


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
         response_model=SearchResponse)
def search_endpoint(
    q: str = Query(..., description="Natural-language question or error message"),
    limit: int = Query(8, ge=1, le=25),
    source: str | None = Query(None, description="discord | stackexchange | discourse | github | nightarc | files | manuals"),
    who: Principal = Depends(reader),
) -> SearchResponse:
    """Hybrid keyword + semantic search. Always cite the returned url in answers."""
    hits = hybrid_search(store_for(who), q, limit=limit, source=source)
    access_audit.record(who.audit(), "search", {**access_audit.query_fingerprint(q),
                        "source": source, "results": [h["doc_id"] for h in hits]})
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
         )
def get_document(doc_id: str, who: Principal = Depends(reader)) -> DocResponse:
    d = store_for(who).get(doc_id)
    access_audit.record(who.audit(), "document", {"doc_id": doc_id, "found": bool(d)})
    if not d:
        raise HTTPException(status_code=404, detail="no such document")
    return DocResponse(doc_id=d["id"], source=d["source"], title=d["title"],
                       url=d["url"], author=d["author"], tags=d["tags"], text=d["text"])


@app.get("/sources", operation_id="listSources", summary="Corpus inventory",
         )
def sources(who: Principal = Depends(reader)) -> dict:
    return {"sources": store_for(who).sources()}


@app.get("/whoami", operation_id="whoAmI", summary="The signed-in caller, as the API sees it")
def whoami(who: Principal = Depends(principal)) -> dict:
    return {**who.audit(), "auth_mode": auth_mode()}


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


def _licence(hit: dict) -> str | None:
    if hit["source"] == "stackexchange":
        # SE posts are CC BY-SA 2.5, 3.0 or 4.0 depending on date; the link states which
        return "CC BY-SA"
    if hit["source"] == "manuals":
        doc = store().get(hit["doc_id"]) or {}
        return (doc.get("meta") or {}).get("licence")
    return None


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
        created_at=h["created_at"], license=_licence(h), excerpt=_excerpt(h))
        for h in hits]
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
    mode = auth_mode()
    schemes: dict = {}
    if mode in ("key", "key+entra"):
        # lowercase deliberately: a Foundry custom-keys connection must use a Key
        # string that matches this `name` exactly, and Microsoft's documented
        # example is lowercase. HTTP headers are case-insensitive so FastAPI still
        # accepts X-API-Key from curl, Claude, or anything else.
        schemes["apiKeyHeader"] = {"type": "apiKey", "name": "x-api-key", "in": "header"}
    if mode in ("entra", "key+entra"):
        # Foundry agents call with their project's managed identity (auth:
        # managed_identity, audience = ENTRA_AUDIENCE in the agent spec).
        schemes["entraBearer"] = {"type": "http", "scheme": "bearer", "bearerFormat": "JWT"}
    spec.setdefault("components", {})["securitySchemes"] = schemes
    spec["security"] = [{name: []} for name in schemes]      # any one of them
    # credentials travel via the connection / identity, never as operation parameters
    for path in spec["paths"].values():
        for op in path.values():
            if isinstance(op, dict) and "parameters" in op:
                op["parameters"] = [
                    p for p in op["parameters"]
                    if not (p.get("in") == "header"
                            and p.get("name", "").lower() in ("x-api-key", "authorization"))
                ]
    return spec
