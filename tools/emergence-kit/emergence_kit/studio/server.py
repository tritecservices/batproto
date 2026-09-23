"""Survey Studio's local server: the app window talks to this, and this talks to the engine.

It listens on 127.0.0.1 only, on a random port, and accepts requests only from the
window it opened. That window gets a one-time secret in its first URL, which becomes an
HttpOnly, SameSite=Strict cookie. Requests also need the right Host header (to block DNS
rebinding) and, if they change anything, an ``X-Studio`` header (to block forged
cross-site form posts). Files are only served from survey folders the user opened.
"""
from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import secrets
import sys
import threading
import time
from datetime import datetime
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .. import __version__, audit
from . import reviewlog, settings
from .tasks import Tasks

STATIC = Path(__file__).with_name("static")
REPORT_FILES = {"report.html", "summary.csv", "events.csv", "summary.json"}
IDLE_EXIT_S = 90
SPECIES = [
    "Pipistrellus pipistrellus", "Pipistrellus pygmaeus", "Pipistrellus nathusii",
    "Pipistrellus sp.", "Nyctalus noctula", "Nyctalus leisleri", "Nyctalus sp.",
    "Eptesicus serotinus", "Myotis daubentonii", "Myotis nattereri", "Myotis sp.",
    "Plecotus auritus", "Plecotus sp.", "Barbastella barbastellus",
    "Rhinolophus hipposideros", "Rhinolophus ferrumequinum", "Bat (unidentified)",
]


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


# ------------------------------------------------------------------ app state
class AcousticMixin:
    """Acoustic workspace: a folder of bat detector WAV files."""

    def open_acoustic(self, path: str) -> dict:
        from .. import acoustic as A
        p = Path(path).expanduser()
        if not p.is_dir():
            raise ApiError(404, "That folder doesn't exist.")
        files = A.list_folder(p)
        if not files:
            raise ApiError(404, "No WAV recordings in that folder (or its sub-folders).")
        key = "a" + hashlib.sha256(str(p.resolve()).lower().encode()).hexdigest()[:15]
        self.acoustic[key] = {"folder": p.resolve(), "files": files, "recs": {}}
        settings.remember_acoustic(p.resolve())
        return self.acoustic_view(key)

    def acoustic_rec(self, key: str, i: int):
        from .. import acoustic as A
        a = self.acoustic.get(key)
        if not a:
            raise ApiError(404, "recordings folder not open")
        if not 0 <= i < len(a["files"]):
            raise ApiError(404, "no such recording")
        if i not in a["recs"]:
            try:
                a["recs"][i] = A.open_recording(a["files"][i])
            except (A.WavError, OSError) as exc:
                raise ApiError(422, f"{a['files'][i].name}: {exc}") from None
        return a, a["recs"][i]

    def acoustic_view(self, key: str) -> dict:
        from .. import acoustic as A
        a = self.acoustic[key]
        labels = A.read_labels(a["folder"])
        out = []
        for i, f in enumerate(a["files"]):
            try:
                _, r = self.acoustic_rec(key, i)
                info = r.summary()
            except ApiError as exc:
                info = {"name": f.name, "error": exc.message}
            lab = labels.get(f.name, {})
            out.append({**info, "i": i, "rel": f.relative_to(a["folder"]).as_posix(),
                        "manual_id": lab.get("manual_id") or "", "notes": lab.get("notes") or "",
                        "calls": lab.get("calls") or None})
        return {"key": key, "folder": str(a["folder"]), "name": a["folder"].name, "files": out}


class Studio(AcousticMixin):
    def __init__(self, token: str | None = None):
        self.token = token or secrets.token_urlsafe(24)
        self.tasks = Tasks()
        self.surveys: dict[str, Path] = {}      # survey key -> folder (opened this session)
        self.acoustic: dict[str, dict] = {}     # folder key -> {"folder", "files", "recs"}
        self.last_ping = time.time()
        self.port = 0
        self._lock = threading.Lock()

    # surveys ---------------------------------------------------------------
    @staticmethod
    def key_for(path: Path) -> str:
        return hashlib.sha256(str(Path(path).resolve()).lower().encode()).hexdigest()[:16]

    def open_survey(self, path: str) -> dict:
        p = Path(path).expanduser()
        if not (p / "prep_report.json").exists():
            raise ApiError(404, "That folder isn't a prepared survey (no prep_report.json). "
                                "Use 'Import camera cards' to prepare one.")
        key = self.key_for(p)
        with self._lock:
            self.surveys[key] = p.resolve()
        settings.remember_survey(p.resolve(), self.survey_name(p))
        return self.survey(key)

    def folder(self, key: str) -> Path:
        p = self.surveys.get(key)
        if not p:
            raise ApiError(404, "survey not open")
        return p

    @staticmethod
    def survey_name(p: Path) -> str:
        meta = p / "studio.json"
        if meta.exists():
            try:
                return json.loads(meta.read_text(encoding="utf-8")).get("name") or p.name
            except ValueError:
                pass
        return p.name

    def recordings(self, p: Path) -> list[dict]:
        rep = json.loads((p / "prep_report.json").read_text(encoding="utf-8"))
        return rep.get("recordings", [])

    def survey(self, key: str) -> dict:
        from ..qa import status
        p = self.folder(key)
        problems, last = audit.verify(p)
        recs = []
        for e in self.recordings(p):
            rid = e["id"]
            rec = e.get("recording") or {}
            d = p / rid
            review = d / f"{rid}_review.mp4"
            st = status(p, rid)
            dets = d / f"detections_{rid}.csv"
            recs.append({
                "id": rid,
                "start": wall_clock_iso(rec.get("start")),
                "start_source": rec.get("start_source"),
                "duration_s": rec.get("duration_s"),
                "camera": " ".join(x for x in (rec.get("camera_model"), rec.get("camera_serial")) if x),
                "has_video": review.exists(),
                "log_rows": len(reviewlog.read(d / f"review_log_{rid}.csv")),
                "qa_rows": len(reviewlog.read(d / f"qa_log_{rid}.csv")),
                "detections": _count_rows(dets) if dets.exists() else None,
                "signed_off_by": st["signed_off_by"], "signed_off_at": st["signed_off_at"],
                "changed_after_signoff": st["changed_after_signoff"],
                "qa": st["qa"], "qa_by": st["qa_by"], "qa_agreement": st["qa_agreement"],
            })
        from .. import hub_client
        try:
            link = hub_client.read_link(p)
        except hub_client.HubError:
            link = None
        report = p / "report.html"
        return {"key": key, "name": self.survey_name(p), "folder": str(p),
                "recordings": recs, "audit": {"ok": not problems, "problems": problems,
                                              "entries": last["seq"] if last else 0},
                "hub": {"survey": link["survey"], "url": link["url"]} if link else None,
                "report": report.exists(),
                "detect_done": (p / "detect_report.json").exists()}


def wall_clock_iso(start: str | None) -> str | None:
    """The camera's wall-clock start as naive ISO (what the burned-in clock shows)."""
    if not start:
        return None
    from ..prep import wall_clock
    return wall_clock(start).strftime("%Y-%m-%dT%H:%M:%S")


def _count_rows(path: Path) -> int:
    with path.open(encoding="utf-8") as fh:
        return max(0, sum(1 for _ in fh) - 1)


# ------------------------------------------------------------------ HTTP
class Handler(BaseHTTPRequestHandler):
    studio: Studio                               # set by make_server
    server_version = f"SurveyStudio/{__version__}"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):           # quiet: no console in the app
        if os.environ.get("STUDIO_DEBUG"):
            sys.stderr.write("studio: " + fmt % args + "\n")

    # --- security checks
    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").lower()
        return host in (f"127.0.0.1:{self.studio.port}", f"localhost:{self.studio.port}")

    def _cookie_ok(self) -> bool:
        c = SimpleCookie(self.headers.get("Cookie") or "")
        got = c.get("studio")
        return bool(got) and secrets.compare_digest(got.value, self.studio.token)

    # --- responses
    APP_CSP = ("default-src 'self'; img-src 'self' data:; media-src 'self'; style-src 'self'; "
               "script-src 'self'; frame-ancestors 'none'")
    # reports are self-contained HTML with inline styles, and never run scripts
    REPORT_CSP = "default-src 'none'; style-src 'unsafe-inline'; img-src data:"

    def _send(self, status: int, body: bytes, ctype: str, extra: dict | None = None,
              csp: str | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", csp or self.APP_CSP)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, status: int = 200) -> None:
        self._send(status, json.dumps(obj, default=str).encode(), "application/json")

    def _error(self, status: int, message: str) -> None:
        self._json({"error": message}, status)

    def _read_body(self) -> None:
        """Read the whole request body up front, so it can't be left unread on a kept-alive
        connection (where it would be taken for the start of the next request)."""
        n = int(self.headers.get("Content-Length") or 0)
        if n > 5_000_000:
            self.close_connection = True
            raise ApiError(413, "request too large")
        self._raw = self.rfile.read(n) if n else b""

    def _body(self) -> dict:
        try:
            data = json.loads(getattr(self, "_raw", b"") or b"{}")
        except ValueError:
            raise ApiError(400, "invalid JSON") from None
        if not isinstance(data, dict):
            raise ApiError(400, "expected a JSON object")
        return data

    # --- routing
    def do_GET(self):
        self._dispatch("GET")

    def do_HEAD(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PUT(self):
        self._dispatch("PUT")

    def _dispatch(self, method: str) -> None:
        try:
            self._read_body()
        except ApiError as exc:
            return self._error(exc.status, exc.message)
        if not self._host_ok():
            return self._error(403, "bad host")
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        path = url.path
        if method == "GET" and path == "/" and "t" in q:
            # first load: swap the one-time URL secret for a cookie, then clean the URL
            if not secrets.compare_digest(q["t"], self.studio.token):
                return self._error(403, "bad token")
            return self._send(303, b"", "text/plain", {
                "Location": "/", "Set-Cookie": f"studio={self.studio.token}; HttpOnly; "
                                               "SameSite=Strict; Path=/"})
        if not self._cookie_ok():
            return self._send(403, b"Open Survey Studio from its shortcut.", "text/plain")
        if method != "GET" and self.headers.get("X-Studio") != "1":
            return self._error(403, "missing X-Studio header")
        self.studio.last_ping = time.time()
        try:
            if method == "GET" and (path == "/" or path.startswith("/static/")):
                return self._static(path)
            if method == "GET" and path.startswith("/media/"):
                return self._media(path)
            if method == "GET" and path.startswith("/files/"):
                return self._report_file(path)
            if method == "GET" and path.startswith("/acoustic/"):
                return self._acoustic_media(path, q)
            if path.startswith("/api/"):
                return self._api(method, path[5:], q)
            self._error(404, "not found")
        except ApiError as exc:
            self._error(exc.status, exc.message)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:                   # noqa: BLE001 - report, don't crash the app
            self._error(500, f"{exc.__class__.__name__}: {exc}")

    # --- static app
    def _static(self, path: str) -> None:
        name = "index.html" if path == "/" else path[len("/static/"):]
        f = (STATIC / name).resolve()
        if not f.is_relative_to(STATIC.resolve()) or not f.is_file():
            raise ApiError(404, "not found")
        ctype = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
        if f.suffix == ".js":
            ctype = "text/javascript"
        self._send(200, f.read_bytes(), ctype + ("; charset=utf-8" if ctype.startswith("text") else ""))

    # --- video with HTTP range requests (needed for instant seeking)
    def _media(self, path: str) -> None:
        m = re.fullmatch(r"/media/([0-9a-f]{16})/([A-Za-z0-9._-]+)", path)
        if not m:
            raise ApiError(404, "not found")
        folder, rid = self.studio.folder(m.group(1)), m.group(2)
        f = (folder / rid / f"{rid}_review.mp4").resolve()
        if not f.is_relative_to(folder) or not f.is_file():
            raise ApiError(404, "no review copy for this recording (run prep)")
        size = f.stat().st_size
        with f.open("rb") as fh:
            head = fh.read(4)
        ctype = "video/webm" if head == b"\x1a\x45\xdf\xa3" else "video/mp4"
        rng = re.fullmatch(r"bytes=(\d*)-(\d*)", self.headers.get("Range") or "")
        start, end, status = 0, size - 1, 200
        if rng:
            if rng.group(1):
                start = int(rng.group(1))
                end = int(rng.group(2)) if rng.group(2) else size - 1
            else:
                start = max(0, size - int(rng.group(2) or 0))
            end = min(end, size - 1)
            if start > end:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            status = 206
        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if self.command == "HEAD":
            return
        with f.open("rb") as fh:
            fh.seek(start)
            left = length
            while left > 0:
                chunk = fh.read(min(1 << 20, left))
                if not chunk:
                    break
                self.wfile.write(chunk)
                left -= len(chunk)

    def _acoustic_media(self, path: str, q: dict) -> None:
        from .. import acoustic as A
        m = re.fullmatch(r"/acoustic/(a[0-9a-f]{15})/(\d+)/(spec\.png|te\.wav)", path)
        if not m:
            raise ApiError(404, "not found")
        _, rec = self.studio.acoustic_rec(m.group(1), int(m.group(2)))

        def num(name, default, lo, hi):
            try:
                return max(lo, min(hi, float(q.get(name, default))))
            except ValueError:
                return default
        if m.group(3) == "spec.png":
            dur = q.get("dur")
            png, _meta = A.spectrogram_png(
                rec, fmax_hz=num("fmax", 125_000, 5_000, 500_000),
                height=int(num("h", 360, 120, 900)), start_s=num("start", 0, 0, 3600),
                dur_s=num("dur", 1, 0.01, 60) if dur else None)
            return self._send(200, png, "image/png")
        wav = A.time_expanded_wav(rec, factor=int(num("x", 10, 1, 40)))
        return self._send(200, wav, "audio/wav")

    def _report_file(self, path: str) -> None:
        m = re.fullmatch(r"/files/([0-9a-f]{16})/([a-z_.]+)", path)
        if not m or m.group(2) not in REPORT_FILES:
            raise ApiError(404, "not found")
        f = self.studio.folder(m.group(1)) / m.group(2)
        if not f.is_file():
            raise ApiError(404, "not made yet")
        ctype = {"html": "text/html; charset=utf-8", "csv": "text/csv; charset=utf-8",
                 "json": "application/json"}[f.suffix[1:]]
        self._send(200, f.read_bytes(), ctype, csp=self.REPORT_CSP)

    # --- JSON API
    def _api(self, method: str, route: str, q: dict) -> None:
        st = self.studio
        parts = route.strip("/").split("/")
        if method == "GET" and route == "state":
            from ..audit import current_actor
            return self._json({"version": __version__, "settings": settings.load(),
                               "recent": settings.recent_surveys(), "user": current_actor(),
                               "species": SPECIES, "directions": reviewlog.DIRECTIONS,
                               "events": list(_event_keys().items()),
                               "tools": _tools(), "busy": st.tasks.busy()})
        if method == "POST" and route == "ping":
            return self._json({"ok": True})
        if method == "POST" and route == "settings":
            return self._json(settings.save(self._body()))
        if method == "POST" and route == "pick-folder":
            b = self._body()
            path = pick_folder(b.get("title") or "Choose a folder", b.get("start"))
            return self._json({"path": path})
        if method == "POST" and route == "surveys/open":
            return self._json(st.open_survey(self._body().get("path") or ""))
        if method == "POST" and route == "surveys/forget":
            settings.forget_survey(self._body().get("path") or "")
            return self._json({"recent": settings.recent_surveys()})
        if method == "POST" and route == "import":
            return self._json(start_import(st, self._body()))
        if method == "POST" and route == "acoustic/open":
            return self._json(st.open_acoustic(self._body().get("path") or ""))
        if method == "GET" and route == "acoustic/recent":
            return self._json({"recent": settings.recent_acoustic()})
        if parts[0] == "a" and len(parts) >= 2:
            return self._acoustic_api(method, parts[1], parts[2:])
        if parts[0] == "tasks" and len(parts) == 2 and method == "GET":
            t = st.tasks.get(parts[1])
            if not t:
                raise ApiError(404, "no such task")
            return self._json(t.view())
        if parts[0] == "s" and len(parts) >= 2:
            key = parts[1]
            folder = st.folder(key)
            rest = parts[2:]
            if method == "GET" and not rest:
                return self._json(st.survey(key))
            if method == "POST" and rest == ["open-folder"]:
                open_in_explorer(folder)
                return self._json({"ok": True})
            if method == "POST" and rest == ["detect"]:
                return self._json(start_detect(st, key, folder))
            if method == "POST" and rest == ["report"]:
                return self._json(start_report(st, key, folder, self._body()))
            if method == "POST" and rest == ["verify"]:
                from ..prep import verify
                problems = verify(folder, say=lambda m: None)
                return self._json({"ok": not problems, "problems": problems[:50]})
            if len(rest) >= 2 and rest[0] == "r":
                rid = rest[1]
                if rid not in {e["id"] for e in st.recordings(folder)}:
                    raise ApiError(404, "no such recording")
                return self._recording_api(method, folder, rid, rest[2:], q)
        raise ApiError(404, f"no route {method} /api/{route}")

    def _recording_api(self, method: str, folder: Path, rid: str, rest: list[str], q: dict):
        from .. import actions
        d = folder / rid
        if rest == ["log"]:
            kind = q.get("kind", "review")
            if kind not in ("review", "qa"):
                raise ApiError(400, "kind must be review or qa")
            path = d / f"{kind}_log_{rid}.csv"
            if method == "GET":
                if kind == "qa":
                    self._qa_allowed(folder, rid)
                return self._json({"rows": reviewlog.read(path)})
            if method == "PUT":
                if kind == "qa":
                    self._qa_allowed(folder, rid)
                rows = self._body().get("rows")
                if not isinstance(rows, list):
                    raise ApiError(400, "rows must be a list")
                try:
                    saved = reviewlog.write(path, rows)
                except reviewlog.LogError as exc:
                    raise ApiError(422, str(exc)) from None
                return self._json({"rows": saved, "saved_at": datetime.now().strftime("%H:%M:%S")})
        if rest == ["detections"] and method == "GET":
            f = d / f"detections_{rid}.csv"
            if not f.exists():
                return self._json({"rows": None})
            import csv
            with f.open(encoding="utf-8", newline="") as fh:
                return self._json({"rows": list(csv.DictReader(fh))})
        if rest == ["signoff"] and method == "POST":
            try:
                e = actions.do_signoff(folder, rid, self._body().get("note") or "")
            except actions.ActionError as exc:
                raise ApiError(409, str(exc)) from None
            return self._json({"ok": True, "by": e["actor"]["user"], "rows": e["details"]["rows"]})
        if rest == ["qa"] and method == "POST":
            b = self._body()
            if b.get("decision") not in ("approve", "reject"):
                raise ApiError(400, "decision must be approve or reject")
            try:
                e = actions.do_qa(folder, rid, b["decision"], b.get("note") or "")
            except actions.ActionError as exc:
                raise ApiError(409, str(exc)) from None
            return self._json({"ok": True, "comparison": e["details"]["comparison"]})
        if rest == ["lock"] and method == "POST":
            b = self._body()
            return self._json(hub_lock(folder, rid, b.get("kind") or "review",
                                       release=bool(b.get("release"))))
        raise ApiError(404, "no such recording action")

    def _acoustic_api(self, method: str, key: str, rest: list[str]):
        from .. import acoustic as A
        from ..audit import current_actor
        st = self.studio
        if not rest and method == "GET":
            if key not in st.acoustic:
                raise ApiError(404, "recordings folder not open")
            return self._json(st.acoustic_view(key))
        if rest == ["open-folder"] and method == "POST":
            a = st.acoustic.get(key) or {}
            if not a:
                raise ApiError(404, "recordings folder not open")
            open_in_explorer(a["folder"])
            return self._json({"ok": True})
        if len(rest) == 3 and rest[0] == "f" and rest[1].isdigit():
            a, rec = st.acoustic_rec(key, int(rest[1]))
            if rest[2] == "calls" and method == "GET":
                calls = A.find_calls(rec)
                return self._json({"calls": calls, "summary": rec.summary()})
            if rest[2] == "label" and method == "PUT":
                b = self._body()
                manual = str(b.get("manual_id") or "").strip()[:80]
                notes = str(b.get("notes") or "").replace("\n", " ").strip()[:300]
                labels = A.read_labels(a["folder"])
                name = rec.path.name
                labels[name] = {"file": name, "timestamp": rec.timestamp or "",
                                "auto_id": rec.guano.get("Species Auto ID", ""),
                                "manual_id": manual, "calls": b.get("calls", ""),
                                "observer": settings.load().get("observer", ""),
                                "labelled_at": datetime.now().isoformat(timespec="seconds"),
                                "notes": notes}
                A.write_labels(a["folder"], labels)
                audit.record(a["folder"], "acoustic.label", {
                    "file": name, "manual_id": manual, "auto_id": labels[name]["auto_id"],
                    "labels_sha256": audit.sha256_file(a["folder"] / A.LABEL_FILE)},
                    actor=current_actor())
                return self._json({"ok": True, "saved_at": datetime.now().strftime("%H:%M:%S")})
        raise ApiError(404, "no such acoustic route")

    def _qa_allowed(self, folder: Path, rid: str) -> None:
        """QA is independent: the reviewer who signed off can't open or write the QA log."""
        from ..audit import current_actor, latest
        so = latest(folder, "review.signoff", rid)
        if not so:
            raise ApiError(409, "QA starts after the reviewer has signed off this recording.")
        if so["actor"]["user"].lower() == current_actor()["user"].lower():
            raise ApiError(403, "You reviewed this recording, so QA must be done by someone else.")


# ------------------------------------------------------------------ work
def _event_keys() -> dict:
    return {"e": "emergence", "r": "re-entry", "p": "pass", "f": "foraging", "o": "other"}


def _tools() -> dict:
    from ..probe import find_tool
    return {t: bool(find_tool(t)) for t in ("ffmpeg", "ffprobe", "exiftool")}


def start_import(st: Studio, b: dict) -> dict:
    """Camera cards -> prepared survey folder (scan + prep), in the background."""
    from ..prep import PrepOptions, prep
    from ..scan import scan, write_manifest
    cards = [Path(c) for c in (b.get("cards") or []) if c]
    if not cards:
        raise ApiError(400, "add at least one camera card or folder")
    missing = [str(c) for c in cards if not c.exists()]
    if missing:
        raise ApiError(404, f"not found: {', '.join(missing)}")
    name = (b.get("name") or "").strip()
    if not name:
        raise ApiError(400, "give the survey a name")
    safe = re.sub(r"[^A-Za-z0-9 ._-]+", "", name).strip()[:80] or "survey"
    dest = Path(b.get("dest") or settings.default_survey_root()) / safe
    if (dest / "prep_report.json").exists():
        raise ApiError(409, f"a survey already exists at {dest}; open it instead")
    hw = "qsv" if b.get("quick_sync") else "none"

    def run(task):
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "studio.json").write_text(json.dumps(
            {"name": name, "created": datetime.now().isoformat(timespec="seconds"),
             "cards": [str(c) for c in cards]}, indent=2), encoding="utf-8")
        task.next_step("Finding recordings on the cards")
        manifest = scan(cards, progress=task.say)
        n = manifest["summary"]["recordings"]
        if not n:
            raise RuntimeError("No survey video found on the cards. Check you chose the card "
                               "itself (the drive letter) or a copy of its folders.")
        write_manifest(manifest, dest / "manifest.json")
        audit.record(dest, "scan", {"roots": manifest["roots"], "files": manifest["summary"]["files"],
                                    "recordings": n, "manifest": "manifest.json",
                                    "manifest_sha256": audit.sha256_file(dest / "manifest.json")})
        task.steps = 1 + n
        task.stage = f"Found {n} recording(s): copying them to this computer"
        task.say(task.stage)

        done = [0]

        def say(m):
            if "making review copy" in m:
                done[0] += 1
                task.next_step(f"Making review copy {done[0]} of {n}")
            else:
                task.say(m)
        prep(manifest, PrepOptions(dest=dest, hw=hw), say=say)
        info = st.open_survey(str(dest))
        from ..hub_client import sync_anchors
        sync_anchors(dest, say=lambda m: None)
        return {"key": info["key"]}
    t = st.tasks.start(f"Importing {name}", run, steps=2)
    return {"task": t.id}


def start_detect(st: Studio, key: str, folder: Path) -> dict:
    from ..detect import DetectOptions, run as detect_run
    try:
        import numpy  # noqa: F401
    except ImportError:
        raise ApiError(501, "Motion detection isn't available in this installation (numpy "
                            "missing).") from None
    recs = [e["id"] for e in st.recordings(folder)
            if (folder / e["id"] / f"{e['id']}_review.mp4").exists()]
    done = [0]

    def run(task):
        def say(m):
            if m.endswith(": analysing"):
                done[0] += 1
                task.next_step(f"Checking recording {done[0]} of {len(recs)}")
            else:
                task.say(m)
        out = detect_run(folder, DetectOptions(), say=say)
        from ..hub_client import sync_anchors
        sync_anchors(folder, say=lambda m: None)
        return {"motion": sum(r["motion"] for r in out["recordings"])}
    t = st.tasks.start("Finding movement", run, steps=max(1, len(recs)))
    return {"task": t.id}


def start_report(st: Studio, key: str, folder: Path, b: dict) -> dict:
    from ..report import run as report_run
    try:
        lat, lon = float(b.get("lat")), float(b.get("lon"))
    except (TypeError, ValueError):
        raise ApiError(400, "enter the site's latitude and longitude as numbers") from None
    if not (49 <= lat <= 61 and -9 <= lon <= 2.5):
        if not b.get("outside_uk"):
            raise ApiError(400, "That location isn't in Great Britain or Northern Ireland. "
                                "Check the numbers (latitude first, west is negative).")
    site = (b.get("site") or "").strip() or folder.name
    off = b.get("utc_offset")
    off = float(off) if off not in (None, "") else None

    def run(task):
        task.next_step("Building the report")
        summary = report_run(folder, lat, lon, site, off)
        from ..hub_client import sync_anchors
        sync_anchors(folder, say=lambda m: None)
        return {"issues": summary["issues"], "url": f"/files/{key}/report.html"}
    t = st.tasks.start("Report", run)
    return {"task": t.id}


def hub_lock(folder: Path, rid: str, kind: str, release: bool = False) -> dict:
    """Take (or release) the hub lock when a recording is opened, if the survey is linked."""
    from .. import hub_client as H
    found = H.client_for(folder)
    if not found:
        return {"hub": False}
    client, sid = found
    try:
        if release:
            held = H.load_lock(client.url, sid, rid)
            if held:
                client.release(sid, rid, held["token"])
                H.forget_lock(client.url, sid, rid)
            return {"hub": True, "released": True}
        lock = client.lock(sid, rid, kind)
        H.save_lock(client.url, sid, rid, lock)
        return {"hub": True, "locked": True, "expires_at": lock["expires_at"]}
    except H.HubError as exc:
        if exc.status == 409:
            return {"hub": True, "locked": False, "message": str(exc).split(": ", 1)[-1]}
        return {"hub": True, "locked": False, "offline": True, "message": str(exc)}


def pick_folder(title: str, start: str | None = None) -> str | None:
    """The Windows folder picker (via Tk), shown on top of the app window."""
    try:
        import tkinter
        from tkinter import filedialog
    except ImportError:
        raise ApiError(501, "No folder picker available; type or paste the path instead") from None
    root = tkinter.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        path = filedialog.askdirectory(title=title, initialdir=start or str(Path.home()),
                                       mustexist=True, parent=root)
    finally:
        root.destroy()
    return str(Path(path)) if path else None


def open_in_explorer(folder: Path) -> None:
    if os.name == "nt":
        os.startfile(str(folder))                 # noqa: S606 - the user's own folder
    else:
        import subprocess
        subprocess.Popen(["xdg-open", str(folder)])


# ------------------------------------------------------------------ server
def make_server(studio: Studio, port: int = 0) -> ThreadingHTTPServer:
    handler = type("StudioHandler", (Handler,), {"studio": studio})
    httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
    httpd.daemon_threads = True
    studio.port = httpd.server_address[1]
    return httpd


def serve_until_idle(httpd: ThreadingHTTPServer, studio: Studio,
                     idle_s: int = IDLE_EXIT_S) -> None:
    """Run until the window has been closed for a while (no pings) and nothing is busy."""
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        while True:
            time.sleep(2)
            if time.time() - studio.last_ping > idle_s and not studio.tasks.busy():
                break
    finally:
        httpd.shutdown()

