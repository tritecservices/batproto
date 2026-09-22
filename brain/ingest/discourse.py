"""OSGeo Discourse ingestion (the official QGIS support forum).

Discourse exposes read-only JSON on every page by appending `.json`, which is a
documented, supported access path -- no HTML parsing, no session hijacking.
Rate limited politely; Discourse will 429 you otherwise.
"""
from __future__ import annotations

import html
import re
import time
from typing import Iterator

import requests

from ..store import Document, Store

BASE = "https://discourse.osgeo.org"
QGIS_PARENT_ID = 11          # the "QGIS" root category on OSGeo Discourse
HEADERS = {"User-Agent": "ecomsp-brain/1.0 (internal knowledge base; contact: admin)"}


def _clean(cooked: str) -> str:
    s = re.sub(r"<pre[^>]*><code[^>]*>(.*?)</code></pre>", r"\n```\n\1\n```\n",
               cooked or "", flags=re.S)
    s = re.sub(r"<code>(.*?)</code>", r"`\1`", s, flags=re.S)
    s = re.sub(r"<br\s*/?>", "\n", s)
    s = re.sub(r"</p>", "\n\n", s)
    s = re.sub(r"<[^>]+>", "", s)
    return re.sub(r"\n{3,}", "\n\n", html.unescape(s)).strip()


def _json(path: str, **params) -> dict:
    for attempt in range(4):
        r = requests.get(f"{BASE}{path}", params=params, headers=HEADERS, timeout=45)
        if r.status_code == 200:
            time.sleep(0.6)               # be a good citizen
            return r.json()
        if r.status_code in (429, 502, 503):
            time.sleep(5 * (attempt + 1))
            continue
        if r.status_code == 404:
            return {}
        r.raise_for_status()
    return {}


def discover_categories(parent_id: int = QGIS_PARENT_ID) -> list[tuple[str, int]]:
    """Find QGIS categories at runtime -- ids and slugs change, hardcoding rots."""
    found: dict[int, tuple[str, int]] = {}
    for page in range(4):
        data = _json("/categories.json", include_subcategories="true", page=page)
        cats = ((data.get("category_list") or {}).get("categories")) or []
        if not cats:
            break
        for c in cats:
            for cand in [c, *(c.get("subcategory_list") or [])]:
                if cand["id"] == parent_id or cand.get("parent_category_id") == parent_id:
                    found[cand["id"]] = (cand["slug"], cand.get("topic_count") or 0)
    # busiest first, skip empties
    ordered = sorted(found.items(), key=lambda kv: -kv[1][1])
    return [(slug, cid) for cid, (slug, n) in ordered if n > 0]


def fetch(categories: list[tuple[str, int]] | None = None, max_pages: int = 5,
          min_posts: int = 2) -> Iterator[Document]:
    categories = categories or discover_categories()
    for slug, cid in categories:
        for page in range(max_pages):
            listing = _json(f"/c/{slug}/{cid}.json", page=page)
            topics = (listing.get("topic_list") or {}).get("topics") or []
            if not topics:
                break
            for t in topics:
                if t.get("posts_count", 0) < min_posts:
                    continue
                topic = _json(f"/t/{t['id']}.json")
                posts = ((topic.get("post_stream") or {}).get("posts")) or []
                if not posts:
                    continue
                body = [f"# {t.get('title','')}", ""]
                for p in posts:
                    who = p.get("username", "?")
                    body.append(f"--- {who} ({p.get('created_at','')[:10]}) ---")
                    body.append(_clean(p.get("cooked", "")))
                yield Document(
                    id=f"discourse:osgeo:{t['id']}",
                    source="discourse",
                    origin=f"osgeo/{slug}",
                    url=f"{BASE}/t/{t.get('slug', '')}/{t['id']}",
                    title=t.get("title"),
                    author=(posts[0].get("username") if posts else None),
                    created_at=int(time.mktime(time.strptime(
                        t["created_at"][:19], "%Y-%m-%dT%H:%M:%S"))) if t.get("created_at") else None,
                    # newer Discourse returns tag objects, older returns strings
                    tags=[(x.get("name") or x.get("slug")) if isinstance(x, dict) else x
                          for x in t.get("tags", []) if x],
                    meta={"posts": len(posts), "views": t.get("views"),
                          "category": slug, "has_accepted": t.get("has_accepted_answer")},
                    text="\n".join(body)[:60_000],
                )


def run(store: Store, max_pages: int = 3) -> dict:
    cats = discover_categories()
    print("discourse categories:", ", ".join(f"{s}({i})" for s, i in cats))
    return store.bulk_upsert(fetch(cats, max_pages=max_pages))
