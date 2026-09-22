"""Read facts about a video file without decoding it.

Two external tools, both called rather than bundled (see README: licensing):

* ffprobe (part of ffmpeg) - duration, resolution, frame rate, interlacing, codec.
* exiftool (optional) - the camera's own recording date/time. Sony AVCHD cameras
  store it inside the H.264 stream (the "MDPM" block), where ffprobe can't see it.

Plus one file read directly: Sony XAVC S cameras (.MP4 under PRIVATE/M4ROOT/CLIP)
write an XML sidecar per clip (C0001.MP4 -> C0001M01.XML) holding the recording
start with its UTC offset, the camera model and serial. It is the best time source
of all, so it is tried first.

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


def _windows_candidates(name: str) -> list[Path]:
    """Where IT departments and package managers put ffmpeg/exiftool on Windows,
    so a managed install works even when PATH wasn't updated for this user."""
    exe = f"{name}.exe"
    env = os.environ
    roots = [
        Path(env.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Links",   # winget, per user
        Path(env.get("ProgramFiles", r"C:\Program Files")) / "WinGet" / "Links",  # winget --scope machine
        Path(env.get("ProgramFiles", r"C:\Program Files")) / "ffmpeg" / "bin",
        Path(env.get("ProgramFiles", r"C:\Program Files")) / "ExifTool",
        Path(env.get("ProgramData", r"C:\ProgramData")) / "chocolatey" / "bin",
        Path(r"C:\ffmpeg\bin"),
    ]
    return [r / exe for r in roots if str(r) not in ("", ".")]


def find_tool(name: str) -> str | None:
    """Find ffmpeg / ffprobe / exiftool.

    Order: EMERGENCE_<NAME> environment variable (IT can pin an exact binary), then
    PATH, then the standard Windows install locations."""
    override = os.environ.get(f"EMERGENCE_{name.upper()}")
    if override and Path(override).is_file():
        return override
    found = shutil.which(name)
    if found or os.name != "nt":
        return found
    for cand in _windows_candidates(name):
        if cand.is_file():
            return str(cand)
    return None


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
    start_source: str | None = None     # sony-xml | exiftool | ffprobe | file-mtime
    camera_model: str | None = None
    camera_serial: str | None = None
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
    """DateTimeOriginal as written by the camera. Returns None if absent.

    MP4/MOV headers store CreateDate in UTC by specification. Without being told,
    exiftool prints it as-is, which then reads as local time and is an hour out in
    summer. QuickTimeUTC makes exiftool convert it and append the UTC offset."""
    extra = (["-api", "QuickTimeUTC=1"]
             if path.suffix.lower() in (".mp4", ".mov", ".m4v") else [])
    data = _run_json([tool, "-json", "-api", "LargeFileSupport=1", *extra,
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


def sony_sidecar(path: Path) -> Path | None:
    """C0001.MP4 -> C0001M01.XML in the same folder, any letter case."""
    want = f"{path.stem}M01.XML".lower()
    try:
        for sib in path.parent.iterdir():
            if sib.name.lower() == want:
                return sib
    except OSError:
        pass
    return None


def read_sony_xml(xml_path: Path) -> dict:
    """Pull start time, camera model/serial and frame rate from a Sony
    NonRealTimeMeta sidecar. Namespace-agnostic; missing fields are simply absent."""
    import xml.etree.ElementTree as ET

    out: dict = {}
    root = ET.parse(xml_path).getroot()
    for el in root.iter():
        tag = el.tag.rsplit("}", 1)[-1]
        if tag == "CreationDate" and el.get("value"):
            try:
                out["start"] = datetime.fromisoformat(el.get("value"))
            except ValueError:
                pass
        elif tag == "Device":
            out["model"] = el.get("modelName")
            out["serial"] = el.get("serialNo")
        elif tag == "VideoFrame" and el.get("captureFps"):
            out["capture_fps"] = el.get("captureFps")
    return out


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
    if path.suffix.lower() in (".mp4", ".mov") and (xml := sony_sidecar(path)):
        try:
            meta = read_sony_xml(xml)
            info.camera_model, info.camera_serial = meta.get("model"), meta.get("serial")
            if meta.get("start"):
                start = meta["start"]
                info.start_source = "sony-xml"
        except Exception as exc:                    # corrupt sidecar: fall through
            info.warnings.append(f"sony sidecar unreadable: {exc}")
    if start is None and exiftool_tool:
        try:
            start = exif_datetime(path, exiftool_tool)
            if start:
                info.start_source = "exiftool"
                if path.suffix.lower() in (".mp4", ".mov", ".m4v"):
                    info.warnings.append(
                        "start time from the MP4 header, read as UTC - cameras that "
                        "write local time there will be an hour out in summer; check "
                        "one clip against a known clock")
        except (RuntimeError, subprocess.TimeoutExpired, ValueError) as exc:
            info.warnings.append(f"exiftool failed: {exc}")
    if start is None:
        tag = (fmt.get("tags") or {}).get("creation_time")
        if tag:
            try:
                start = datetime.fromisoformat(tag.replace("Z", "+00:00"))
                info.start_source = "ffprobe"
                # MP4 stores this as UTC by spec, but many cheap cameras write their
                # local clock into it. An hour's error moves every emergence time.
                info.warnings.append("start time from the MP4 header - some cameras "
                                     "write local time where UTC is expected; check "
                                     "one clip against a known clock")
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
