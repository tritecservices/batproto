"""prep: get survey video off the share and into a form that reviews fast.

For every recording in a manifest (from `scan`):

1. Copy the original clips to local disk, hashing as they copy, into
   <dest>/originals/<card>/..., keeping the card's folder layout. Writes go to
   *.part and are renamed only when complete, so a half-copied file never looks
   finished. SHA-256 of every file goes in <dest>/checksums.sha256 (the format
   `sha256sum -c` reads), which is the chain-of-custody record.
2. Make a review copy <dest>/<id>/<id>_review.mp4 straight from the joined local
   clips (ffmpeg concat, no intermediate file): deinterlaced if needed, 720p, a
   keyframe every second so scrubbing is instant, and the real clock time burned
   into the picture.
3. Write an empty review log <dest>/<id>/review_log_<id>.csv for the reviewer.
4. Record what happened in <dest>/prep_report.json.

Originals are only ever read. Re-running skips anything already done, so an
interrupted night's prep can simply be started again.
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .probe import ToolMissing, find_tool

REVIEW_COLUMNS = ["clock_time", "event", "species_group", "count", "direction",
                  "observer", "notes"]
CHUNK = 8 * 1024 * 1024
REVIEW_BYTES_PER_HOUR = 0.6e9          # 720p H.264 CRF 23 on dark footage, generous


# ---------------------------------------------------------------- utilities
def wall_clock(start_iso: str) -> datetime:
    """The recording start as naive camera wall-clock time (same rule as scan)."""
    from .scan import _local_naive
    return _local_naive(datetime.fromisoformat(start_iso))


def clock_epoch(start_iso: str) -> int:
    """Seconds such that ffmpeg's `gmtime` renders the camera's wall clock."""
    return int(wall_clock(start_iso).replace(tzinfo=timezone.utc).timestamp())


def sha256_copy(src: Path, dst: Path) -> str:
    """Copy src -> dst via dst.part, hashing in the same pass; keep the mtime."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    part = dst.with_name(dst.name + ".part")
    h = hashlib.sha256()
    with src.open("rb") as fi, part.open("wb") as fo:
        while block := fi.read(CHUNK):
            h.update(block)
            fo.write(block)
    shutil.copystat(src, part)
    part.replace(dst)
    return h.hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while block := fh.read(CHUNK):
            h.update(block)
    return h.hexdigest()


def read_checksums(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            m = re.match(r"([0-9a-f]{64}) [ *](.+)$", line.strip())
            if m:
                out[m.group(2)] = m.group(1)
    return out


def write_checksums(path: Path, sums: dict[str, str]) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("".join(f"{h} *{rel}\n" for rel, h in sorted(sums.items())),
                   encoding="utf-8")
    tmp.replace(path)


def _escape_concat(p: Path) -> str:
    return str(p).replace("\\", "/").replace("'", "'\\''")


def _font_option() -> str:
    """drawtext needs a font file on Windows; elsewhere fontconfig finds one."""
    if os.name == "nt":
        for name in ("arial.ttf", "segoeui.ttf", "consola.ttf"):
            f = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / name
            if f.exists():
                # filter-graph escaping: forward slashes, and the drive colon escaped
                return "fontfile='" + str(f).replace("\\", "/").replace(":", "\\:") + "':"
    return ""


def review_filter(rec: dict) -> str:
    parts = []
    if rec.get("interlaced"):
        parts.append("yadif=0:-1:0")
    if (rec.get("height") or 0) > 720:
        parts.append("scale=-2:720")
    epoch = clock_epoch(rec["start"])
    size = 36 if (rec.get("height") or 720) >= 720 else 20
    parts.append(
        f"drawtext={_font_option()}"
        f"text='%{{pts\\:gmtime\\:{epoch}\\:%H\\\\\\:%M\\\\\\:%S}}'"
        f":x=20:y=20:fontsize={size}:fontcolor=white:box=1:boxcolor=black@0.6")
    return ",".join(parts)


def ffmpeg_major(ffmpeg: str) -> int:
    """Major version from `ffmpeg -version`; 0 if unparseable (e.g. git builds)."""
    r = subprocess.run([ffmpeg, "-version"], capture_output=True, text=True)
    m = re.search(r"ffmpeg version n?(\d+)\.", r.stdout)
    return int(m.group(1)) if m else 0


def filter_file_args(ffmpeg: str, path: Path) -> list[str]:
    """Read the video filter from a file. ffmpeg 7+ spells this `-/filter:v <file>`;
    `-filter_script:v` was deprecated in 7 and removed in 8. Git/nightly builds
    report no version number and are assumed new."""
    major = ffmpeg_major(ffmpeg)
    if major == 0 or major >= 7:
        return ["-/filter:v", str(path)]
    return ["-filter_script:v", str(path)]


def probe_duration(path: Path, ffprobe: str) -> float | None:
    r = subprocess.run([ffprobe, "-v", "error", "-show_entries", "format=duration",
                        "-of", "default=nw=1:nk=1", str(path)],
                       capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return None


def run_ffmpeg_progress(cmd: list[str], total_s: float,
                        say: Callable[[str], None]) -> None:
    """Run ffmpeg with -progress and report every ~10 s. Raises on failure."""
    # stderr goes to a temporary file, not a pipe: a pipe nobody reads fills up on long
    # encodes with many warnings, and ffmpeg then blocks forever
    import tempfile
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as errf, \
            subprocess.Popen(cmd + ["-progress", "pipe:1", "-nostats"],
                             stdout=subprocess.PIPE, stderr=errf,
                             text=True, encoding="utf-8", errors="replace") as proc:
        last = 0.0
        assert proc.stdout
        for line in proc.stdout:
            if line.startswith("out_time_us=") or line.startswith("out_time_ms="):
                try:
                    secs = int(line.split("=", 1)[1]) / 1e6
                except ValueError:
                    continue
                if time.monotonic() - last > 10 and total_s:
                    say(f"    encoding {min(100, secs / total_s * 100):5.1f}%")
                    last = time.monotonic()
        code = proc.wait()
        errf.seek(0)
        err = errf.read()
    if code != 0:
        raise RuntimeError(f"ffmpeg failed: {err.strip()[-800:]}")


# --------------------------------------------------------------------- prep
@dataclass
class PrepOptions:
    dest: Path
    dry_run: bool = False
    hw: str = "none"                 # none | qsv
    crf: int = 23
    only: list[str] = field(default_factory=list)
    skip_review: bool = False
    force: bool = False


def _card_relative(clip_path: Path, card: str, roots: list[str]) -> Path:
    """Where under originals/<card>/ a clip goes: its path below the card folder."""
    for root in roots:
        card_dir = Path(root) / card
        try:
            return clip_path.relative_to(card_dir)
        except ValueError:
            continue
    return Path(clip_path.name)


def plan(manifest: dict, opts: PrepOptions) -> list[dict]:
    recs = [r for r in manifest["recordings"] if not opts.only or r["id"] in opts.only]
    return recs


def prep(manifest: dict, opts: PrepOptions,
         say: Callable[[str], None] = lambda m: print(m, file=sys.stderr)) -> dict:
    ffmpeg, ffprobe = find_tool("ffmpeg"), find_tool("ffprobe")
    if not ffmpeg or not ffprobe:
        raise ToolMissing("ffmpeg/ffprobe not found. Install ffmpeg (Windows: winget "
                          "install Gyan.FFmpeg), or set EMERGENCE_FFMPEG / EMERGENCE_FFPROBE")
    recs = plan(manifest, opts)
    dest = opts.dest
    need = sum(r["size_bytes"] for r in recs) + sum(
        r["duration_s"] / 3600 * REVIEW_BYTES_PER_HOUR for r in recs)
    report: dict = {"created": datetime.now().astimezone().isoformat(timespec="seconds"),
                    "manifest_created": manifest.get("created"),
                    "dest": str(dest), "recordings": [], "estimated_bytes": int(need)}

    if opts.dry_run:
        say(f"dry run: {len(recs)} recording(s), about {need / 1e9:.1f} GB needed at {dest}")
        for r in recs:
            say(f"  {r['id']}: {r['clip_count']} clip(s), {r['duration_s'] / 60:.0f} min, "
                f"time from {r['start_source']}")
        report["dry_run"] = True
        return report

    dest.mkdir(parents=True, exist_ok=True)
    from . import audit
    problems, _ = audit.verify(dest)
    if problems:
        raise RuntimeError(f"audit trail in {dest} failed verification: {problems[0]}")
    free = shutil.disk_usage(dest).free
    if free < need:
        raise RuntimeError(f"not enough space at {dest}: need ~{need / 1e9:.1f} GB, "
                           f"{free / 1e9:.1f} GB free")

    sums_path = dest / "checksums.sha256"
    sums = read_checksums(sums_path)

    for r in recs:
        t_rec = time.monotonic()
        entry: dict = {"id": r["id"], "card": r["card"], "copied": 0, "skipped_copy": 0,
                       "review": None, "warnings": list(r.get("warnings", []))}
        say(f"{r['id']}  ({r['clip_count']} clip(s), {r['duration_s'] / 60:.0f} min)")

        # 1. local copies with checksums
        local: list[Path] = []
        for c in r["clips"]:
            src = Path(c["path"])
            rel = Path("originals") / r["card"] / _card_relative(src, r["card"],
                                                                manifest.get("roots", []))
            dst = dest / rel
            key = rel.as_posix()
            st = src.stat()
            if (dst.exists() and key in sums and dst.stat().st_size == st.st_size
                    and int(dst.stat().st_mtime) == int(st.st_mtime)):
                entry["skipped_copy"] += 1
            else:
                say(f"    copying {src.name} ({st.st_size / 1e6:.0f} MB)")
                sums[key] = sha256_copy(src, dst)
                write_checksums(sums_path, sums)       # after every file: resumable
                entry["copied"] += 1
            local.append(dst)

        out_dir = dest / r["id"]
        out_dir.mkdir(exist_ok=True)

        # 2. review copy
        review = out_dir / f"{r['id']}_review.mp4"
        if opts.skip_review:
            entry["review"] = "skipped"
        elif review.exists() and not opts.force and abs(
                (probe_duration(review, ffprobe) or -99) - r["duration_s"]) <= 1.5:
            entry["review"] = {"path": str(review), "status": "already done"}
        else:
            listfile = out_dir / f"{r['id']}.concat.txt"
            listfile.write_text("".join(f"file '{_escape_concat(p.resolve())}'\n"
                                        for p in local), encoding="utf-8")
            vf = out_dir / f"{r['id']}.vf.txt"
            vf.write_text(review_filter(r), encoding="utf-8")
            gop = str(max(1, round(r.get("fps") or 25)))
            enc = (["-c:v", "h264_qsv", "-global_quality", str(opts.crf)]
                   if opts.hw == "qsv" else
                   ["-c:v", "libx264", "-preset", "veryfast", "-crf", str(opts.crf)])
            part = review.with_name(review.stem + ".part.mp4")
            cmd = [ffmpeg, "-v", "error", "-y", "-f", "concat", "-safe", "0",
                   "-i", str(listfile), "-map", "0:v:0", "-map", "0:a:0?",
                   *filter_file_args(ffmpeg, vf), *enc, "-pix_fmt", "yuv420p",
                   "-g", gop, "-keyint_min", gop, "-sc_threshold", "0",
                   "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(part)]
            say("    making review copy")
            t0 = time.monotonic()
            try:
                run_ffmpeg_progress(cmd, r["duration_s"], say)
            except RuntimeError as exc:
                # belt and braces: if this build rejects the filter-file syntax we
                # picked from its version number, try the other spelling once
                if "Unrecognized option" not in str(exc) and "Option not found" not in str(exc):
                    raise
                i = next(i for i, a in enumerate(cmd) if a in ("-/filter:v", "-filter_script:v"))
                cmd[i] = "-filter_script:v" if cmd[i] == "-/filter:v" else "-/filter:v"
                say(f"    retrying with {cmd[i]}")
                run_ffmpeg_progress(cmd, r["duration_s"], say)
            got = probe_duration(part, ffprobe) or 0
            if abs(got - r["duration_s"]) > 1.5:
                raise RuntimeError(f"{r['id']}: review copy is {got:.1f}s, recording is "
                                   f"{r['duration_s']:.1f}s - not keeping it")
            part.replace(review)
            entry["review"] = {"path": str(review), "status": "made",
                               "duration_s": round(got, 3),
                               "encode_s": round(time.monotonic() - t0, 1)}

        # 3. empty review log and QA log, never overwritten. The QA log is for a second,
        #    independent reviewer (see qa.py).
        log = out_dir / f"review_log_{r['id']}.csv"
        for path in (log, out_dir / f"qa_log_{r['id']}.csv"):
            if not path.exists():
                with path.open("w", newline="", encoding="utf-8") as fh:
                    csv.writer(fh).writerow(REVIEW_COLUMNS)
        entry["review_log"] = str(log)
        entry["recording"] = {k: r.get(k) for k in ("start", "end", "start_source",
                                                    "duration_s", "camera_model",
                                                    "camera_serial")}
        entry["seconds"] = round(time.monotonic() - t_rec, 1)
        report["recordings"].append(entry)

    rep = dest / "prep_report.json"
    rep.write_text(json.dumps(report, indent=2), encoding="utf-8")
    from . import audit
    audit.record(dest, "prep", {
        "manifest_created": manifest.get("created"), "roots": manifest.get("roots"),
        "recordings": [r["id"] for r in report["recordings"]],
        "copied": sum(r["copied"] for r in report["recordings"]),
        "checksums_sha256": audit.sha256_file(sums_path),
        "prep_report_sha256": audit.sha256_file(rep)})
    return report


def verify(dest: Path, say: Callable[[str], None] = print) -> list[str]:
    """Re-hash every local copy against checksums.sha256. Returns problems."""
    sums = read_checksums(dest / "checksums.sha256")
    problems = []
    for rel, h in sorted(sums.items()):
        p = dest / rel
        if not p.exists():
            problems.append(f"missing: {rel}")
        elif sha256_file(p) != h:
            problems.append(f"CHANGED: {rel}")
    say(f"verified {len(sums)} file(s), {len(problems)} problem(s)")
    from . import audit
    if not audit.verify(dest)[0]:
        audit.record(dest, "verify", {"files": len(sums), "problems": problems[:50]})
    return problems
