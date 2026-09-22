"""GIS Stack Exchange ingestion via the official Stack Exchange API v2.3.

Content is CC BY-SA, so every document keeps its URL and author for attribution.
Register an app on Stack Apps and set STACKAPPS_KEY to raise the daily quota
(300/day anonymous -> 10,000/day with a key).
"""
from __future__ import annotations

import html
import os
import re
import time
from typing import Iterator

import requests

from ..store import Document, Store

API = "https://api.stackexchange.com/2.3"

# Bodies + answers in one call instead of N+1 requests. Built at runtime because
# hand-written filter ids silently drop fields.
INCLUDE = ";".join([
    ".items", ".has_more", ".quota_remaining", ".backoff",
    "question.body", "question.answers", "question.link", "question.tags",
    "question.score", "question.view_count", "question.is_answered",
    "question.creation_date", "question.title", "question.question_id", "question.owner",
    "answer.body", "answer.is_accepted", "answer.score", "answer.owner",
    "shallow_user.display_name",
])
_filter_cache: str | None = None

DEFAULT_TAGS = ["qgis", "pyqgis", "qgis-plugins", "qgis-processing",
                "arcgis-pro", "arcpy", "google-earth"]


def _strip(h: str) -> str:
    h = re.sub(r"<pre><code>(.*?)</code></pre>", r"\n```\n\1\n```\n", h or "", flags=re.S)
    h = re.sub(r"<code>(.*?)</code>", r"`\1`", h, flags=re.S)
    h = re.sub(r"<br\s*/?>", "\n", h)
    h = re.sub(r"</p>", "\n\n", h)
    h = re.sub(r"<[^>]+>", "", h)
    return re.sub(r"\n{3,}", "\n\n", html.unescape(h)).strip()


def build_filter() -> str:
    global _filter_cache
    if _filter_cache:
        return _filter_cache
    r = requests.get(f"{API}/filters/create",
                     params={"include": INCLUDE, "base": "none", "unsafe": "false"},
                     timeout=30)
    r.raise_for_status()
    _filter_cache = r.json()["items"][0]["filter"]
    return _filter_cache


def _get(path: str, **params) -> dict:
    params.setdefault("site", "gis.stackexchange")
    if key := os.environ.get("STACKAPPS_KEY"):
        params["key"] = key
    for attempt in range(5):
        r = requests.get(f"{API}/{path}", params=params, timeout=45)
        if r.status_code == 200:
            data = r.json()
            if backoff := data.get("backoff"):
                time.sleep(backoff + 1)      # API-mandated, ignoring it gets you blocked
            return data
        if r.status_code in (429, 502, 503):
            time.sleep(2 ** attempt * 3)
            continue
        r.raise_for_status()
    raise RuntimeError(f"stack exchange api failed: {path}")


def fetch(tags: list[str], min_score: int = 0, answered_only: bool = True,
          max_pages: int = 20, site: str = "gis.stackexchange") -> Iterator[Document]:
    for tag in tags:
        for page in range(1, max_pages + 1):
            data = _get("questions", tagged=tag, page=page, pagesize=100,
                        order="desc", sort="votes", filter=build_filter(), site=site)
            for q in data.get("items", []):
                if q.get("score", 0) < min_score:
                    continue
                answers = sorted(q.get("answers", []),
                                 key=lambda a: (a.get("is_accepted", False), a.get("score", 0)),
                                 reverse=True)
                if answered_only and not answers:
                    continue
                parts = [f"# {html.unescape(q.get('title',''))}", "", _strip(q.get("body", ""))]
                for a in answers[:5]:
                    mark = "ACCEPTED ANSWER" if a.get("is_accepted") else "Answer"
                    who = html.unescape((a.get("owner") or {}).get("display_name", "unknown"))
                    parts += ["", f"## {mark} (score {a.get('score',0)}, by {who})",
                              _strip(a.get("body", ""))]
                yield Document(
                    id=f"se:{site}:{q['question_id']}",
                    source="stackexchange",
                    origin=site,
                    url=q.get("link"),
                    title=html.unescape(q.get("title", "")),
                    author=html.unescape((q.get("owner") or {}).get("display_name", "")) or None,
                    created_at=q.get("creation_date"),
                    tags=q.get("tags", []),
                    meta={"score": q.get("score"), "views": q.get("view_count"),
                          "answers": len(answers), "is_answered": q.get("is_answered"),
                          "license": "CC BY-SA"},
                    text="\n".join(parts),
                )
            if not data.get("has_more"):
                break
            if data.get("quota_remaining", 1) < 20:
                print("stack exchange quota nearly exhausted; stopping")
                return


def run(store: Store, tags: list[str] | None = None, min_score: int = 1,
        max_pages: int = 10) -> dict:
    return store.bulk_upsert(fetch(tags or DEFAULT_TAGS, min_score=min_score,
                                   max_pages=max_pages))
