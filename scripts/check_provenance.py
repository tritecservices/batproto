"""Clean-room gate: fail if the repository contains data it must not ship.

    python scripts/check_provenance.py            # from the repo root; exit 1 on problems

Checks every tracked file (git ls-files; falls back to walking the tree):

1. Data-like files (databases, recordings, video, spreadsheets, documents, CSV,
   JSON...) must be listed in provenance.yaml -> repo_data_allowlist.
2. No file over 5 MB (recordings and databases hide in big files).
3. Text files must not contain: Discord channel links with real ids, private keys,
   or an API key / token / password assigned a real-looking value.

Runs in CI on every push (phase 7) and before every release. It is a backstop, not
a substitute for the clean-room policy in docs/CLEAN-ROOM.md.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

DATA_EXT = {".db", ".sqlite", ".sqlite3", ".db-wal", ".db-shm", ".csv", ".tsv", ".json",
            ".xlsx", ".xls", ".docx", ".doc", ".pdf", ".wav", ".zc", ".mts", ".m2ts",
            ".mp4", ".mov", ".avi", ".qgz", ".qgs", ".gpkg", ".shp", ".dbf", ".kml",
            ".kmz", ".bak", ".mdf", ".ldf", ".parquet", ".pkl"}
MAX_BYTES = 5 * 1024 * 1024
FORBIDDEN = [
    (re.compile(r"discord(?:app)?\.com/channels/\d{15,}/\d{15,}"), "Discord channel link with real ids"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "private key"),
    (re.compile(r"""(?i)\b(api[_-]?key|token|password|secret)\b\s*[:=]\s*["']?(?!change-me|<|\$|\{|os\.environ|None|""|'')[A-Za-z0-9_\-]{24,}"""),
     "credential with a real-looking value"),
]
SKIP_TEXT_SCAN = {"scripts/check_provenance.py", "tests/test_provenance.py"}


def tracked_files(root: Path) -> list[str]:
    try:
        out = subprocess.run(["git", "ls-files"], cwd=root, capture_output=True, text=True,
                             check=True).stdout
        return [line for line in out.splitlines() if line]
    except (OSError, subprocess.CalledProcessError):
        return [p.relative_to(root).as_posix() for p in root.rglob("*")
                if p.is_file() and ".git" not in p.parts and "node_modules" not in p.parts]


def load_allowlist(root: Path) -> set[str]:
    import yaml
    data = yaml.safe_load((root / "provenance.yaml").read_text(encoding="utf-8")) or {}
    return set(data.get("repo_data_allowlist") or [])


def check(root: Path) -> list[str]:
    allow = load_allowlist(root)
    problems = []
    for rel in tracked_files(root):
        p = root / rel
        if not p.is_file():
            continue
        ext = "".join(p.suffixes[-1:]).lower()
        if ext in DATA_EXT and rel not in allow:
            problems.append(f"{rel}: data-like file not in provenance.yaml repo_data_allowlist")
        if p.stat().st_size > MAX_BYTES:
            problems.append(f"{rel}: larger than 5 MB")
        if rel in SKIP_TEXT_SCAN or ext in DATA_EXT - {".json", ".csv", ".tsv"}:
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for rx, what in FORBIDDEN:
            if m := rx.search(text):
                line = text.count("\n", 0, m.start()) + 1
                problems.append(f"{rel}:{line}: {what}")
    return problems


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    problems = check(root)
    for p in problems:
        print(p)
    print(f"provenance check: {len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
