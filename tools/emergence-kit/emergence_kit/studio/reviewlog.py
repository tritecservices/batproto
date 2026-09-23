"""Reading and writing review / QA logs for Survey Studio.

The CSV format is the same one the command line and ``report`` use, so a log made in
the app and one typed in Excel are interchangeable. Writes are atomic (temp file, then
rename), so a crash or a full disk never leaves half a log.
"""
from __future__ import annotations

import csv
import os
import tempfile
from pathlib import Path

from ..prep import REVIEW_COLUMNS
from ..report import EVENTS, TIME_RE

DIRECTIONS = ("", "N", "NE", "E", "SE", "S", "SW", "W", "NW", "in", "out")


class LogError(ValueError):
    pass


def read(path: Path) -> list[dict]:
    if not Path(path).exists():
        return []
    with Path(path).open(encoding="utf-8-sig", newline="") as fh:
        return [{c: (row.get(c) or "").strip() for c in REVIEW_COLUMNS}
                for row in csv.DictReader(fh)]


def clean(row: dict, n: int) -> dict:
    """Validate one row from the app. Raises LogError naming the row."""
    out = {c: str(row.get(c, "") if row.get(c) is not None else "").strip()
           for c in REVIEW_COLUMNS}
    m = TIME_RE.match(out["clock_time"])
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59 or int(m.group(3) or 0) > 59:
        raise LogError(f"row {n}: time must be HH:MM:SS")
    out["clock_time"] = f"{int(m.group(1)):02d}:{int(m.group(2)):02d}:{int(m.group(3) or 0):02d}"
    out["event"] = out["event"].lower()
    if out["event"] not in EVENTS:
        raise LogError(f"row {n}: event must be one of {', '.join(EVENTS)}")
    try:
        count = int(out["count"] or "1")
    except ValueError:
        raise LogError(f"row {n}: count must be a whole number") from None
    if not 1 <= count <= 9999:
        raise LogError(f"row {n}: count must be between 1 and 9999")
    out["count"] = str(count)
    for col in ("species_group", "direction", "observer", "notes"):
        out[col] = out[col].replace("\n", " ").replace("\r", " ")[:200]
    return out


def write(path: Path, rows: list[dict]) -> list[dict]:
    cleaned = [clean(r, i) for i, r in enumerate(rows, 1)]
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=REVIEW_COLUMNS)
            w.writeheader()
            w.writerows(cleaned)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return cleaned
