"""Read facts about a video file without decoding it.

Two external tools, both called rather than bundled (see README: licensing):

* ffprobe (part of ffmpeg) - duration, resolution, frame rate, interlacing, codec.
* exiftool (optional) - the camera's own recording date/time. Sony AVCHD cameras
  store it inside the H.264 stream (the "MDPM" block), where ffprobe can't see it.

Start time is the thing that matters most for emergence surveys, so every value
records where it came from, and the weakest source is flagged rather than trusted.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from fractions import Fraction
from pathlib import Path


class ToolMissing(RuntimeError):
    pass


def find_tool(name: str) -> str | None:
    """EMERGENCE_FFPROBE / EMERGENCE_EXIFTOOL override PATH, for machines where
    ffmpeg lives in a folder nobody added to PATH (common on Windows)."""
    override = os.environ.get(f"EMERGENCE_{name.upper()}")
    if override and Path(override).is_file():
        return override
    return shutil.which(name)


@dataclass
class ClipInfo:
    path: str
    size_bytes: int
    duration_s: float | None = None
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    interlaced: bool | None = None
    codec: str | None = None
    start: str | None = None            # ISO 8601
    start_source: str | None = None     # exiftool | ffprobe | file-mtime
    warnings: list[str] = field(default_factory=list)

    def start_dt(self) -> datetime | None:
        return datetime.fromisoformat(self.start) if self.start else None

    def to_dict(self) -> dict:
        return asdict(self)


def _run_json(cmd: list[str], timeout: int = 60):
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                         encoding="utf-8", errors="replace")
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip()[-400:] or f"{cmd[0]} failed")
    return json.loads(out.stdout or "null")


def _fps(rate: str | None) -> float | None:
    try:
        f = Fraction(rate) if rate and rate != "0/0" else None
        return round(float(f), 3) if f else None
    except (ValueError, ZeroDivisionError):
        return None


def ffprobe(path: Path, tool: str) -> dict:
    return _run_json([tool, "-v", "error", "-print_format", "json",
                      "-show_format", "-show_streams", "-select_streams", "v:0",
                      str(path)])


def exif_datetime(path: Path, tool: str) -> datetime | None:
    """DateTimeOriginal as written by the camera. Returns None if absent."""
    data = _run_json([tool, "-json", "-api", "LargeFileSupport=1",
                      "-DateTimeOriginal", "-CreateDate", str(path)])
    if not data:
        return None
    rec = data[0]
    for key in ("DateTimeOriginal", "CreateDate"):
        raw = rec.get(key)
        if not raw or raw.startswith("0000"):
            continue
        return parse_exif_datetime(raw)
    return None


def parse_exif_datetime(raw: str) -> datetime | None:
    """'2026:06:14 21:03:55+01:00' or '2026:06:14 21:03:55 DST' -> datetime."""
    raw = raw.replace(" DST", "").strip()
    date, _, rest = raw.partition(" ")
    iso = date.replace(":", "-") + "T" + rest
    try:
        return datetime.fromisoformat(iso)
    except ValueError:
        try:
            return datetime.strptime(raw[:19], "%Y:%m:%d %H:%M:%S")
        except ValueError:
            return None


def probe(path: Path, ffprobe_tool: str | None = None,
          exiftool_tool: str | None | bool = None) -> ClipInfo:
    """exiftool_tool=False disables exiftool even if installed (tests, speed)."""
    ffprobe_tool = ffprobe_tool or find_tool("ffprobe")
    if not ffprobe_tool:
        raise ToolMissing("ffprobe not found. Install ffmpeg, or set EMERGENCE_FFPROBE "
                          "to the full path of ffprobe.exe")
    if exiftool_tool is None:
        exiftool_tool = find_tool("exiftool")

    st = path.stat()
    info = ClipInfo(path=str(path), size_bytes=st.st_size)

    data = ffprobe(path, ffprobe_tool)
    fmt = data.get("format") or {}
    streams = data.get("streams") or []
    if fmt.get("duration"):
        info.duration_s = round(float(fmt["duration"]), 3)
    if streams:
        v = streams[0]
        info.codec = v.get("codec_name")
        info.width, info.height = v.get("width"), v.get("height")
        info.fps = _fps(v.get("avg_frame_rate")) or _fps(v.get("r_frame_rate"))
        order = v.get("field_order")
        if order:
            info.interlaced = order not in ("progressive", "unknown")
    else:
        info.warnings.append("no video stream found")

    # --- start time, best source first
    start: datetime | None = None
    if exiftool_tool:
        try:
            start = exif_datetime(path, exiftool_tool)
            if start:
                info.start_source = "exiftool"
        except (RuntimeError, subprocess.TimeoutExpired, ValueError) as exc:
            info.warnings.append(f"exiftool failed: {exc}")
    if start is None:
        tag = (fmt.get("tags") or {}).get("creation_time")
        if tag:
            try:
                start = datetime.fromisoformat(tag.replace("Z", "+00:00"))
                info.start_source = "ffprobe"
            except ValueError:
                pass
    if start is None:
        # Last resort. On a card the modified time is usually when the camera
        # closed the file, i.e. the END of the clip, so step back by the duration.
        # Copying files can reset it entirely - hence the warning.
        end = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).astimezone()
        start = end - timedelta(seconds=info.duration_s or 0)
        info.start_source = "file-mtime"
        info.warnings.append("start time estimated from file modified time - "
                             "check against the camera clock or a slate shot")
    info.start = start.isoformat(timespec="seconds")
    return info
