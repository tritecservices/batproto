"""GitHub issue ingestion -- the real QGIS bug/troubleshooting corpus.

Defaults to qgis/QGIS plus the plugin repos ecologists actually hit. Set
GITHUB_TOKEN to lift the rate limit from 60/hr to 5,000/hr.
"""
from __future__ import annotations

import os
import time
from typing import Iterator

import requests

from ..store import Document, Store

API = "https://api.github.com"
DEFAULT_REPOS = ["qgis/QGIS", "qgis/QGIS-Documentation", "macaodha/batdetect2"]


def _headers() -> dict:
    h = {"Accept": "application/vnd.github+json",
         "X-GitHub-Api-Version": "2022-11-28",
         "User-Agent": "ecomsp-brain/1.0"}
    if tok := os.environ.get("GITHUB_TOKEN"):
        h["Authorization"] = f"Bearer {tok}"
    return h


def _get(url: str, **params):
    for attempt in range(4):
        r = requests.get(url, params=params, headers=_headers(), timeout=45)
        if r.status_code == 200:
            return r.json()
        if r.status_code in (403, 429):        # secondary rate limit
            time.sleep(int(r.headers.get("retry-after", 20 * (attempt + 1))))
            continue
        r.raise_for_status()
    return []


def fetch(repos: list[str], state: str = "closed", labels: str | None = None,
          max_pages: int = 5, with_comments: bool = True) -> Iterator[Document]:
    """Closed issues by default: a closed issue usually contains the fix."""
    for repo in repos:
        for page in range(1, max_pages + 1):
            issues = _get(f"{API}/repos/{repo}/issues", state=state, per_page=100,
                          page=page, sort="comments", direction="desc",
                          **({"labels": labels} if labels else {}))
            if not issues:
                break
            for it in issues:
                if "pull_request" in it:
                    continue
                parts = [f"# {it['title']}", "", it.get("body") or ""]
                if with_comments and it.get("comments", 0):
                    for c in _get(it["comments_url"], per_page=30) or []:
                        parts += ["", f"--- {c['user']['login']} ---", c.get("body") or ""]
                yield Document(
                    id=f"gh:{repo}:{it['number']}",
                    source="github",
                    origin=repo,
                    url=it["html_url"],
                    title=it["title"],
                    author=(it.get("user") or {}).get("login"),
                    created_at=int(time.mktime(time.strptime(
                        it["created_at"], "%Y-%m-%dT%H:%M:%SZ"))),
                    tags=[l["name"] for l in it.get("labels", [])],
                    meta={"state": it["state"], "comments": it.get("comments"),
                          "number": it["number"]},
                    text="\n".join(parts)[:60_000],
                )


def run(store: Store, repos: list[str] | None = None, max_pages: int = 2) -> dict:
    return store.bulk_upsert(fetch(repos or DEFAULT_REPOS, max_pages=max_pages))
