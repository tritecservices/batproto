"""Talking to the survey hub from the Emergence Review Kit (standard library only).

A review folder is *linked* to a hub survey by ``emergence-kit hub link``, which
writes ``<folder>/hub.json``. From then on:

* ``signoff`` and ``qa`` go through the hub first, so the hub enforces one reviewer at
  a time and QA by a different person across every laptop, not just this one;
* after each command the folder's audit trail is **anchored** at the hub (best effort:
  in the field with no signal the kit carries on, and anchors next time);
* ``hub verify`` compares the folder's audit trail with the anchored copy, catching a
  trail that was cut short or rebuilt.

Sign-in: Microsoft Entra ID. The token comes from, in order,
  EMERGENCE_HUB_TOKEN                     (automation; short-lived tokens only)
  the Azure CLI: ``az account get-access-token --scope <EMERGENCE_HUB_SCOPE>``
Settings: EMERGENCE_HUB_URL (https://...), EMERGENCE_HUB_SCOPE
(api://<app id>/access_as_user). Lock tokens are kept per user in
``~/.emergence-kit/locks.json``, never in the shared folder.
"""
from __future__ import annotations

import json
import os
import shutil
import ssl
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

LINK_FILE = "hub.json"
TIMEOUT_S = 20


class HubError(Exception):
    def __init__(self, message: str, status: int | None = None, body: dict | None = None):
        super().__init__(message)
        self.status, self.body = status, body or {}


# ------------------------------------------------------------------ config and auth
def _check_url(url: str) -> str:
    url = url.rstrip("/")
    local = url.startswith(("http://127.0.0.1", "http://localhost"))
    if not url.startswith("https://") and not local:
        raise HubError("the hub URL must start with https:// (tokens are never sent in clear)")
    return url


def get_token() -> str:
    tok = os.environ.get("EMERGENCE_HUB_TOKEN", "").strip()
    if tok:
        return tok
    scope = os.environ.get("EMERGENCE_HUB_SCOPE", "").strip()
    if not scope:
        raise HubError("set EMERGENCE_HUB_SCOPE (api://<app id>/access_as_user) so the kit "
                       "can sign you in with the Azure CLI")
    az = shutil.which("az") or shutil.which("az.cmd")
    if not az:
        raise HubError("Azure CLI not found: install it (winget install Microsoft.AzureCLI) "
                       "and run 'az login'")
    r = subprocess.run([az, "account", "get-access-token", "--scope", scope,
                        "--query", "accessToken", "-o", "tsv"],
                       capture_output=True, text=True, timeout=60)
    if r.returncode or not r.stdout.strip():
        raise HubError("could not get a sign-in token from the Azure CLI (run 'az login'): "
                       + (r.stderr.strip().splitlines() or ["no output"])[-1])
    return r.stdout.strip()


class Client:
    def __init__(self, url: str, token: str | None = None, opener=None):
        self.url = _check_url(url)
        self._token = token
        ctx = ssl.create_default_context(cafile=os.environ.get("EMERGENCE_HUB_CA") or None)
        self._open = opener or (lambda req: urllib.request.urlopen(req, timeout=TIMEOUT_S,
                                                                  context=ctx))

    def request(self, method: str, path: str, body: dict | None = None):
        if self._token is None:
            self._token = get_token()
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, method=method, headers={
            "Authorization": f"Bearer {self._token}", "Content-Type": "application/json",
            "Accept": "application/json"})
        try:
            with self._open(req) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            try:
                payload = json.loads(exc.read() or b"{}")
            except ValueError:
                payload = {}
            detail = payload.get("detail", exc.reason)
            if isinstance(detail, list):              # FastAPI validation errors
                detail = "; ".join(str(d.get("msg", d)) for d in detail)
            raise HubError(f"hub said {exc.code}: {detail}", exc.code, payload) from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise HubError(f"hub not reachable ({getattr(exc, 'reason', exc)})") from None

    # surveys and recordings
    def create_survey(self, sid, name=""):
        return self.request("POST", "/surveys", {"id": sid, "name": name})

    def register(self, sid, ids):
        return self.request("POST", f"/surveys/{sid}/recordings", {"ids": ids})

    def recordings(self, sid):
        return self.request("GET", f"/surveys/{sid}/recordings")

    # locks, sign-off, QA
    def lock(self, sid, rid, kind):
        return self.request("POST", f"/surveys/{sid}/recordings/{rid}/lock", {"kind": kind})

    def release(self, sid, rid, token):
        return self.request("POST", f"/surveys/{sid}/recordings/{rid}/lock/release",
                            {"token": token})

    def signoff(self, sid, rid, token, log_sha256, rows):
        return self.request("POST", f"/surveys/{sid}/recordings/{rid}/signoff",
                            {"token": token, "log_sha256": log_sha256, "rows": rows})

    def qa(self, sid, rid, token, decision, log_sha256, note=""):
        return self.request("POST", f"/surveys/{sid}/recordings/{rid}/qa",
                            {"token": token, "decision": decision, "log_sha256": log_sha256,
                             "note": note})

    # jobs
    def submit(self, sid, kind, params=None):
        return self.request("POST", f"/surveys/{sid}/jobs", {"kind": kind, "params": params or {}})

    def jobs(self, sid):
        return self.request("GET", f"/surveys/{sid}/jobs")

    # audit anchors
    def anchor(self, sid, entries):
        return self.request("POST", f"/surveys/{sid}/anchors", {"entries": entries})

    def verify(self, sid, entries):
        return self.request("POST", f"/surveys/{sid}/anchors/verify", {"entries": entries})


# ------------------------------------------------------------------ folder link
def read_link(folder: Path) -> dict | None:
    p = Path(folder) / LINK_FILE
    if not p.exists():
        return None
    link = json.loads(p.read_text(encoding="utf-8"))
    if not link.get("url") or not link.get("survey"):
        raise HubError(f"{p} is incomplete; run 'hub link' again")
    return link


def write_link(folder: Path, url: str, survey: str) -> dict:
    link = {"url": _check_url(url), "survey": survey}
    (Path(folder) / LINK_FILE).write_text(json.dumps(link, indent=2) + "\n", encoding="utf-8")
    return link


def client_for(folder: Path, token: str | None = None) -> tuple[Client, str] | None:
    link = read_link(folder)
    if not link:
        return None
    return Client(link["url"], token), link["survey"]


def anchor_entries(entries: list[dict]) -> list[dict]:
    return [{"seq": e["seq"], "hash": e["hash"], "prev": e.get("prev"),
             "action": str(e.get("action", ""))[:64]}
            for e in entries if "seq" in e and "hash" in e]


# ------------------------------------------------------------------ lock tokens (per user)
def _lock_store() -> Path:
    base = Path(os.environ.get("EMERGENCE_HOME") or Path.home() / ".emergence-kit")
    base.mkdir(parents=True, exist_ok=True)
    return base / "locks.json"


def _key(url: str, sid: str, rid: str) -> str:
    return f"{url.rstrip('/')}|{sid}|{rid}"


def save_lock(url: str, sid: str, rid: str, lock: dict) -> None:
    p = _lock_store()
    data = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    data[_key(url, sid, rid)] = {"token": lock["token"], "kind": lock["kind"],
                                 "expires_at": lock["expires_at"]}
    p.write_text(json.dumps(data, indent=2), encoding="utf-8")


def load_lock(url: str, sid: str, rid: str) -> dict | None:
    p = _lock_store()
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8")).get(_key(url, sid, rid))


def forget_lock(url: str, sid: str, rid: str) -> None:
    p = _lock_store()
    if p.exists():
        data = json.loads(p.read_text(encoding="utf-8"))
        data.pop(_key(url, sid, rid), None)
        p.write_text(json.dumps(data, indent=2), encoding="utf-8")


# ------------------------------------------------------------------ best-effort sync
def sync_anchors(folder: Path, say=print) -> dict | None:
    """Anchor the folder's audit trail if linked. Never raises: offline work continues."""
    from . import audit
    try:
        found = client_for(folder)
        if not found:
            return None
        client, sid = found
        entries = audit.read_entries(folder)
        if not entries:
            return None
        res = client.anchor(sid, anchor_entries(entries))
        if res.get("conflicts"):
            say(f"WARNING: the hub reports this folder's audit trail differs from the copy it "
                f"holds ({len(res['conflicts'])} entries). Run 'hub verify' and tell the "
                "service desk.")
        return res
    except HubError as exc:
        say(f"note: audit trail not anchored at the hub this time ({exc}); it will be next time")
    except Exception as exc:                          # noqa: BLE001 - never break the command
        say(f"note: audit trail not anchored at the hub ({exc.__class__.__name__})")
    return None
