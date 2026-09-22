"""Find survey video and group split clips into whole recordings.

Sony AVCHD cameras split one long recording into several .MTS files
(PRIVATE/AVCHD/BDMV/STREAM/00000.MTS, 00001.MTS, ...). For review and reporting the
unit that matters is the *recording*: one camera, one continuous watch. A clip joins
the previous one when it starts where that one ended, within a small tolerance, and
has the same picture format.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable

from . import __version__
from .probe import ClipInfo, ToolMissing, find_tool, probe

VIDEO_EXT = {".mts", ".m2ts", ".mp4", ".mov", ".avi"}
SKIP_DIRS = {"$RECYCLE.BIN", "System Volume Information", ".Trashes", ".Spotlight-V100",
             ".fseventsd", "@eaDir", "__MACOSX", ".git"}
MANIFEST_SCHEMA = "emergence-kit/manifest@1"


@dataclass
class Recording:
    id: str
    card: str
    clips: list[ClipInfo] = field(default_factory=list)

    @property
    def start(self) -> datetime | None:
        return self.clips[0].start_dt() if self.clips else None

    @property
    def duration_s(self) -> float:
        return round(sum(c.duration_s or 0 for c in self.clips), 3)

    def to_dict(self) -> dict:
        first = self.clips[0]
        start = self.start
        end = start + timedelta(seconds=self.duration_s) if start else None
        sources = sorted({c.start_source for c in self.clips if c.start_source})
        warnings = sorted({w for c in self.clips for w in c.warnings})
        return {
            "id": self.id,
            "card": self.card,
            "start": start.isoformat(timespec="seconds") if start else None,
            "end": end.isoformat(timespec="seconds") if end else None,
            "start_source": first.start_source,
            "time_sources": sources,
            "duration_s": self.duration_s,
            "clip_count": len(self.clips),
            "width": first.width, "height": first.height, "fps": first.fps,
            "interlaced": first.interlaced, "codec": first.codec,
            "camera_model": first.camera_model, "camera_serial": first.camera_serial,
            "size_bytes": sum(c.size_bytes for c in self.clips),
            "warnings": warnings,
            "clips": [c.to_dict() for c in self.clips],
        }


# ------------------------------------------------------------------ discovery
# Folder names that mark where a camera's card starts, most specific first:
#   Sony AVCHD      <card>/PRIVATE/AVCHD/BDMV/STREAM/00000.MTS
#   Sony XAVC S     <card>/PRIVATE/M4ROOT/CLIP/C0001.MP4
#   everyone else   <card>/DCIM/100MEDIA/VID_0001.MP4
CARD_MARKERS = (("PRIVATE", ("AVCHD", "M4ROOT")), ("AVCHD", ()), ("M4ROOT", ()),
                ("DCIM", ()))


def card_root(path: Path, scan_root: Path) -> Path:
    """The folder that represents one camera card: the folder holding PRIVATE/,
    AVCHD/, M4ROOT/ or DCIM/ (whichever the card was copied with). Anything else:
    the folder the file is in."""
    parts = [p.upper() for p in path.parts[:-1]]
    for marker, needs_after in CARD_MARKERS:
        if marker not in parts:
            continue
        idx = len(parts) - 1 - parts[::-1].index(marker)
        if needs_after and not any(n in parts[idx + 1:] for n in needs_after):
            continue
        root = Path(*path.parts[:idx])
        try:
            root.relative_to(scan_root)
            return root
        except ValueError:
            return scan_root
    return path.parent


def find_videos(roots: Iterable[Path]) -> list[tuple[Path, Path]]:
    """(file, scan_root) pairs, sorted, junk folders and macOS ._ files skipped."""
    found: list[tuple[Path, Path]] = []
    for root in roots:
        root = Path(root)
        if root.is_file():
            if root.suffix.lower() in VIDEO_EXT:
                found.append((root, root.parent))
            continue
        for p in sorted(root.rglob("*")):
            if any(part in SKIP_DIRS for part in p.parts):
                continue
            if p.is_file() and p.suffix.lower() in VIDEO_EXT and not p.name.startswith("._"):
                found.append((p, root))
    return found


# ------------------------------------------------------------------- grouping
def _local_naive(dt: datetime) -> datetime:
    """Wall-clock time at the survey, without a timezone.

    - naive (AVCHD via exiftool, most cameras): already the camera's wall clock.
    - a non-zero offset (Sony XML "21:05+01:00"): the camera's wall clock too, so
      keep it as written rather than shifting it into the laptop's timezone.
    - UTC ("...Z" in MP4 headers, file times): convert to this machine's local time.
    """
    if dt.tzinfo is None:
        return dt
    if dt.utcoffset() and dt.utcoffset().total_seconds() != 0:
        return dt.replace(tzinfo=None)
    return dt.astimezone().replace(tzinfo=None)


def _same_format(a: ClipInfo, b: ClipInfo) -> bool:
    return (a.width, a.height, a.fps, a.codec) == (b.width, b.height, b.fps, b.codec)


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower() or "card"


def group_clips(clips_by_card: dict[str, list[ClipInfo]],
                gap_tolerance_s: float = 5.0) -> list[Recording]:
    recordings: list[Recording] = []
    for card, clips in sorted(clips_by_card.items()):
        ordered = sorted(clips, key=lambda c: (_local_naive(c.start_dt()) if c.start
                                               else datetime.max, Path(c.path).name))
        current: Recording | None = None
        for clip in ordered:
            joins = False
            if current and clip.start and current.clips[-1].start:
                prev = current.clips[-1]
                expected = (_local_naive(prev.start_dt())
                            + timedelta(seconds=prev.duration_s or 0))
                gap = abs((_local_naive(clip.start_dt()) - expected).total_seconds())
                joins = gap <= gap_tolerance_s and _same_format(prev, clip)
            if joins:
                current.clips.append(clip)
            else:
                start = clip.start_dt()
                stamp = _local_naive(start).strftime("%Y%m%d-%H%M%S") if start else "unknown"
                current = Recording(id=f"{_slug(card)}-{stamp}", card=card, clips=[clip])
                recordings.append(current)
    # ids must be unique even if two cards share a name and a start second
    seen: dict[str, int] = {}
    for r in recordings:
        n = seen.get(r.id, 0)
        seen[r.id] = n + 1
        if n:
            r.id = f"{r.id}-{n + 1}"
    return recordings


# ----------------------------------------------------------------------- scan
def scan(roots: list[Path], gap_tolerance_s: float = 5.0, use_exiftool: bool = True,
         progress: Callable[[str], None] | None = None) -> dict:
    ffprobe_tool = find_tool("ffprobe")
    if not ffprobe_tool:          # say so once, rather than failing every file
        raise ToolMissing("ffprobe not found. Install ffmpeg (Windows: "
                          "winget install Gyan.FFmpeg), or set EMERGENCE_FFPROBE "
                          "to the full path of ffprobe.exe")
    exiftool_tool = find_tool("exiftool") if use_exiftool else None
    by_card: dict[str, list[ClipInfo]] = {}
    failed: list[dict] = []

    files = find_videos(roots)
    for i, (path, scan_root) in enumerate(files, 1):
        if progress:
            progress(f"[{i}/{len(files)}] {path.name}")
        try:
            info = probe(path, ffprobe_tool, exiftool_tool or False)
        except Exception as exc:                       # keep going; report at the end
            failed.append({"path": str(path), "error": f"{exc.__class__.__name__}: {exc}"})
            continue
        root = card_root(path, scan_root)
        try:
            label = root.relative_to(scan_root).as_posix()
        except ValueError:
            label = root.name
        label = label if label not in ("", ".") else (scan_root.name or str(scan_root))
        by_card.setdefault(label, []).append(info)

    recordings = group_clips(by_card, gap_tolerance_s)
    return {
        "schema": MANIFEST_SCHEMA,
        "tool_version": __version__,
        "created": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "roots": [str(Path(r)) for r in roots],
        "tools": {"ffprobe": ffprobe_tool, "exiftool": exiftool_tool},
        "gap_tolerance_s": gap_tolerance_s,
        "summary": {
            "files": len(files),
            "recordings": len(recordings),
            "failed": len(failed),
            "hours": round(sum(r.duration_s for r in recordings) / 3600, 2),
            "needs_time_check": sum(1 for r in recordings
                                    if r.clips[0].start_source == "file-mtime"),
        },
        "recordings": [r.to_dict() for r in recordings],
        "failed": failed,
    }


def write_manifest(manifest: dict, out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + ".tmp")
    tmp.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    tmp.replace(out)                      # never leave a half-written manifest


def format_table(manifest: dict) -> str:
    rows = [("recording", "start", "length", "clips", "format", "time from")]
    for r in manifest["recordings"]:
        secs = int(round(r["duration_s"]))
        fmt = f"{r['width']}x{r['height']}" if r["width"] else "?"
        if r["fps"]:
            fmt += f" {r['fps']:g}{'i' if r['interlaced'] else 'p'}"
        start = (r["start"] or "?").replace("T", " ")[:19]
        rows.append((r["id"], start, f"{secs // 3600}:{secs // 60 % 60:02d}:{secs % 60:02d}",
                     str(r["clip_count"]), fmt, r["start_source"] or "?"))
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    lines = ["  ".join(cell.ljust(w) for cell, w in zip(row, widths)) for row in rows]
    lines.insert(1, "  ".join("-" * w for w in widths))
    s = manifest["summary"]
    lines.append("")
    lines.append(f"{s['files']} files -> {s['recordings']} recordings, "
                 f"{s['hours']} h of video, {s['failed']} failed")
    if s["needs_time_check"]:
        lines.append(f"WARNING: {s['needs_time_check']} recording(s) timed from file dates "
                     "only - install exiftool or check the camera clock")
    for f in manifest["failed"]:
        lines.append(f"FAILED {f['path']}: {f['error']}")
    return "\n".join(lines)
