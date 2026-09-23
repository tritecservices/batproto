"""Start Survey Studio: local server + app window.

    survey-studio.exe                 (Start menu shortcut; no console)
    emergence-kit studio              (same, from a terminal)
    emergence-kit studio --no-window  (just print the address; for testing)

One instance per user: starting it again opens another window onto the running one.
The app closes itself about a minute after its last window closes, once any import or
detection still running has finished.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

from . import settings
from .server import Studio, make_server, serve_until_idle


def _edge() -> str | None:
    for base in (os.environ.get("PROGRAMFILES(X86)"), os.environ.get("PROGRAMFILES"),
                 os.environ.get("LOCALAPPDATA")):
        if base:
            p = Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe"
            if p.exists():
                return str(p)
    return shutil.which("msedge") or shutil.which("microsoft-edge")


def open_window(url: str) -> subprocess.Popen | None:
    """Open the app in its own window (Edge app mode, own profile), else the browser."""
    edge = _edge()
    if edge:
        profile = settings.home() / "window"
        return subprocess.Popen([edge, f"--app={url}", f"--user-data-dir={profile}",
                                 "--window-size=1480,920", "--no-first-run",
                                 "--no-default-browser-check", "--disable-sync",
                                 "--disable-features=Translate"])
    webbrowser.open(url)
    return None


def _running_instance() -> dict | None:
    f = settings.home() / "instance.json"
    if not f.exists():
        return None
    try:
        inst = json.loads(f.read_text(encoding="utf-8"))
        req = urllib.request.Request(f"http://127.0.0.1:{inst['port']}/?t={inst['token']}",
                                     method="HEAD")
        opener = urllib.request.build_opener(_NoRedirect)
        try:
            opener.open(req, timeout=2)
        except urllib.error.HTTPError as exc:
            if exc.code != 303:
                return None
        return inst
    except (OSError, ValueError, KeyError):
        return None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **kw):
        return None


def _windowed_setup() -> None:
    """survey-studio.exe has no console. Send its output to a log file (for the service
    desk), and stop ffmpeg, ffprobe and exiftool from each flashing up a console window."""
    log = settings.home() / "studio.log"
    try:
        if log.exists() and log.stat().st_size > 2_000_000:
            log.replace(log.with_suffix(".log.1"))
        fh = open(log, "a", encoding="utf-8", buffering=1)       # noqa: SIM115 - app lifetime
        sys.stdout = sys.stderr = fh
    except OSError:
        pass
    if os.name == "nt":
        base = subprocess.Popen

        class QuietPopen(base):                                   # type: ignore[misc, valid-type]
            def __init__(self, *a, **kw):
                kw.setdefault("creationflags", subprocess.CREATE_NO_WINDOW)
                if kw.get("stdin") is None:
                    kw["stdin"] = subprocess.DEVNULL
                super().__init__(*a, **kw)
        subprocess.Popen = QuietPopen                             # type: ignore[misc]


def main(argv: list[str] | None = None) -> int:
    if sys.stdout is None:                   # survey-studio.exe (windowed build)
        _windowed_setup()
    ap = argparse.ArgumentParser(prog="survey-studio")
    ap.add_argument("--no-window", action="store_true", help="don't open a window")
    ap.add_argument("--port", type=int, default=0)
    args = ap.parse_args(argv)

    inst = _running_instance()
    if inst and not args.no_window:
        open_window(f"http://127.0.0.1:{inst['port']}/?t={inst['token']}")
        return 0

    studio = Studio()
    httpd = make_server(studio, args.port)
    url = f"http://127.0.0.1:{studio.port}/?t={studio.token}"
    inst_file = settings.home() / "instance.json"
    inst_file.write_text(json.dumps({"port": studio.port, "token": studio.token,
                                     "pid": os.getpid()}), encoding="utf-8")
    try:
        if args.no_window:
            print(url, flush=True)
        else:
            open_window(url)
        studio.last_ping = time.time() + 60          # grace period while the window opens
        serve_until_idle(httpd, studio)
    finally:
        try:
            if json.loads(inst_file.read_text(encoding="utf-8")).get("pid") == os.getpid():
                inst_file.unlink()
        except (OSError, ValueError):
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
