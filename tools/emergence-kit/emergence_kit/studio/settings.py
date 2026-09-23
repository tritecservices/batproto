"""Per-user settings and the recent-surveys list, in %APPDATA%\\Ecomsp\\SurveyStudio.

Nothing here is survey data: just the reviewer's initials, preferences and where their
survey folders are. Survey locations (lat/lon) are never stored.
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

DEFAULTS = {"observer": "", "default_species": "", "skip_seconds": 5,
            "survey_root": "", "quick_sync": False}
MAX_RECENT = 12


def home() -> Path:
    base = os.environ.get("SURVEY_STUDIO_HOME")
    if base:
        p = Path(base)
    elif os.name == "nt" and os.environ.get("APPDATA"):
        p = Path(os.environ["APPDATA"]) / "Ecomsp" / "SurveyStudio"
    else:
        p = Path.home() / ".config" / "ecomsp-survey-studio"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _read() -> dict:
    f = home() / "settings.json"
    try:
        return json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}
    except ValueError:
        return {}


def _write(data: dict) -> None:
    f = home() / "settings.json"
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, f)


def load() -> dict:
    return {**DEFAULTS, **{k: v for k, v in _read().get("settings", {}).items() if k in DEFAULTS}}


def save(new: dict) -> dict:
    data = _read()
    cur = load()
    for k, v in new.items():
        if k in DEFAULTS and isinstance(v, type(DEFAULTS[k])):
            cur[k] = v.strip()[:80] if isinstance(v, str) else v
    if cur["observer"]:
        cur["observer"] = cur["observer"].upper()[:6]
    data["settings"] = cur
    _write(data)
    return cur


def default_survey_root() -> Path:
    root = load().get("survey_root")
    if root:
        return Path(root)
    docs = Path.home() / "Documents"
    return (docs if docs.exists() else Path.home()) / "Survey reviews"


def recent_surveys() -> list[dict]:
    out = []
    for r in _read().get("recent", []):
        p = Path(r["path"])
        out.append({**r, "exists": (p / "prep_report.json").exists()})
    return out


def remember_survey(path: Path, name: str) -> None:
    data = _read()
    rec = [r for r in data.get("recent", []) if r.get("path") != str(path)]
    rec.insert(0, {"path": str(path), "name": name,
                   "opened": datetime.now().isoformat(timespec="minutes")})
    data["recent"] = rec[:MAX_RECENT]
    _write(data)


def forget_survey(path: str) -> None:
    data = _read()
    data["recent"] = [r for r in data.get("recent", []) if r.get("path") != path]
    _write(data)
