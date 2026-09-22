"""detect: find moments with small moving objects, so reviewers jump instead of watch.

Runs on the review copies prep made (local, deinterlaced, clock burned in):

  ffmpeg decodes each review copy to small greyscale frames (default 640 px wide,
  12.5 fps) -> numpy compares every frame against a slowly updating background ->
  pixels that changed by much more than the footage's own noise, and that have
  changed neighbours (lone noise pixels don't), count as motion -> frames with enough
  motion are joined into events.

Written per recording: <id>/detections_<id>.csv with the clock time, the offset
into the review copy (to jump to), duration, size and direction of travel.

What it is and isn't:
  * A reviewing aid, the "first pass". It flags candidate moments; it does not
    identify bats, count them, or decide emergence vs re-entry. Moths, rain and
    leaves also move. The ecologist's review is the record.
  * Tuned for the usual emergence set-up: a mostly static IR view with small
    bright or dark objects crossing. Whole-frame changes (headlights, auto-exposure,
    a bumped tripod) are logged as `scene-change` instead of flooding the list.
  * The burned-in clock (top-left) is masked out, since it changes every second.

Measure it with devtools/score_detections.py against fake cards' ground truth
before trusting any setting.
"""
from __future__ import annotations

import csv
import json
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Callable

from .prep import wall_clock
from .probe import ToolMissing, find_tool

DETECTION_COLUMNS = ["clock_time", "video_offset", "duration_s", "kind", "peak_pixels",
                     "direction", "x_start", "y_start", "x_end", "y_end"]


@dataclass
class DetectOptions:
    width: int = 640               # analysis width; height follows the aspect ratio
    fps: float = 12.5              # analysis frame rate
    # Defaults favour catching every bat over avoiding false alarms: a reviewer
    # dismisses a false alarm in seconds, a missed bat is a wrong count. Chosen by grid
    # search against fake-card ground truth (46/46 caught, 3 false alarms); recalibrate
    # on real footage before relying on them.
    sensitivity: float = 5.0       # threshold = sensitivity x each pixel's noise (lower = more)
    min_pixels: int = 4            # changed pixels (after neighbour check) to count
    min_peak: int = 12             # an event must reach this size at least once
    max_fraction: float = 0.15     # more than this share of the frame = scene change
    merge_gap_s: float = 0.4       # join motion separated by less than this
    min_duration_s: float = 0.16   # drop blips shorter than this (2 frames at 12.5 fps)
    bg_rate: float = 0.05          # background update per frame (0-1)
    mask_clock: bool = True
    only: list[str] = field(default_factory=list)


@dataclass
class Event:
    start_f: int
    end_f: int
    peak: int = 0
    kind: str = "motion"
    track: list[tuple[float, float]] = field(default_factory=list)


def _direction(track: list[tuple[float, float]]) -> str:
    if len(track) < 2:
        return ""
    (x0, y0), (x1, y1) = track[0], track[-1]
    dx, dy = x1 - x0, y1 - y0
    if abs(dx) < 3 and abs(dy) < 3:
        return "stationary"
    horiz = "right" if dx > 0 else "left"
    vert = "down" if dy > 0 else "up"
    if abs(dx) > 2 * abs(dy):
        return horiz
    if abs(dy) > 2 * abs(dx):
        return vert
    return f"{vert}-{horiz}"


def analyse(video: Path, opts: DetectOptions, ffmpeg: str,
            say: Callable[[str], None] = lambda m: None) -> tuple[list[Event], int, int]:
    """Return (events, frame width, frame height) for one video."""
    try:
        import numpy as np
    except ImportError as exc:                      # pragma: no cover
        raise ToolMissing("detect needs numpy: python -m pip install numpy") from exc

    ffprobe = find_tool("ffprobe")
    info = json.loads(subprocess.run(
        [ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=width,height", "-of", "json", str(video)],
        capture_output=True, text=True).stdout or "{}")
    st = (info.get("streams") or [{}])[0]
    src_w, src_h = st.get("width") or 1280, st.get("height") or 720
    w = opts.width
    h = int(round(src_h * w / src_w / 2)) * 2
    proc = subprocess.Popen(
        [ffmpeg, "-v", "error", "-i", str(video), "-an",
         "-vf", f"fps={opts.fps},scale={w}:{h},format=gray", "-f", "rawvideo", "pipe:1"],
        stdout=subprocess.PIPE)
    size = w * h
    mask = np.ones((h, w), dtype=bool)
    if opts.mask_clock:                             # prep draws the clock at (20,20)
        mask[: int(h * 0.12), : int(w * 0.30)] = False
    bg = None
    events: list[Event] = []
    current: Event | None = None
    gap_frames = max(1, int(round(opts.merge_gap_s * opts.fps)))
    min_frames = max(1, int(round(opts.min_duration_s * opts.fps)))
    ys, xs = np.mgrid[0:h, 0:w]
    f = -1
    assert proc.stdout
    while True:
        buf = proc.stdout.read(size)
        if len(buf) < size:
            break
        f += 1
        frame = np.frombuffer(buf, dtype=np.uint8).reshape(h, w).astype(np.float32)
        # 3x3 box blur: sensor noise and compression "breathing" at keyframes are
        # scattered pixel-level flicker and average away; a bat is a compact blob
        # and survives. Done with shifted sums (no scipy needed).
        blurred = np.zeros_like(frame)
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                blurred += np.roll(np.roll(frame, dy, 0), dx, 1)
        frame = blurred / 9.0
        if bg is None:
            bg = frame.copy()
            var = np.full(frame.shape, 16.0, dtype=np.float32)     # start: std 4
            warm = int(opts.fps * 2)                                # 2 s to learn noise
            continue
        if f <= warm:                                   # learning period: no detections
            d = frame - bg
            bg += 0.2 * d
            var += 0.2 * (np.minimum(np.abs(d), 60.0) ** 2 - var)
            continue
        diff = np.abs(frame - bg)
        # Each pixel has its own noise level (running variance): noisy sky and a
        # near-black roofline need very different thresholds.
        hot = (diff > np.maximum(12.0, opts.sensitivity * np.sqrt(var))) & mask
        # keep pixels with at least 2 hot neighbours: kills single-pixel noise
        n = np.zeros(hot.shape, dtype=np.int8)
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                if dy or dx:
                    n += np.roll(np.roll(hot, dy, 0), dx, 1)
        solid = hot & (n >= 2)
        count = int(solid.sum())
        # learn slowly, and barely at all where something is moving
        rate = np.where(solid, opts.bg_rate * 0.05, opts.bg_rate).astype(np.float32)
        bg += rate * (frame - bg)
        var += rate * (np.minimum(diff, 60.0) ** 2 - var)

        active = count >= opts.min_pixels
        scene = count > opts.max_fraction * mask.sum()
        if active:
            cx, cy = float(xs[solid].mean()), float(ys[solid].mean())
            if current and f - current.end_f <= gap_frames and (current.kind == "scene-change") == scene:
                current.end_f = f
                current.peak = max(current.peak, count)
                current.track.append((cx, cy))
            else:
                if current:
                    events.append(current)
                current = Event(f, f, count, "scene-change" if scene else "motion", [(cx, cy)])
            if scene:
                bg = frame.copy()                   # adopt the new scene immediately
                warm = f + int(opts.fps)            # and relearn its noise for 1 s
    if current:
        events.append(current)
    proc.wait()
    kept = [e for e in events if e.kind == "scene-change"
            or (e.end_f - e.start_f + 1 >= min_frames and e.peak >= opts.min_peak)]
    return kept, w, h


def _hms(seconds: float) -> str:
    s = int(round(seconds))
    return f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}"


def run(dest: Path, opts: DetectOptions,
        say: Callable[[str], None] = lambda m: print(m, file=sys.stderr)) -> dict:
    ffmpeg = find_tool("ffmpeg")
    if not ffmpeg:
        raise ToolMissing("ffmpeg not found (winget install Gyan.FFmpeg)")
    prep = json.loads((dest / "prep_report.json").read_text(encoding="utf-8"))
    out: dict = {"recordings": []}
    for entry in prep["recordings"]:
        rid = entry["id"]
        if opts.only and rid not in opts.only:
            continue
        review = dest / rid / f"{rid}_review.mp4"
        if not review.exists():
            say(f"{rid}: no review copy, skipped (run prep)")
            continue
        say(f"{rid}: analysing")
        events, w, h = analyse(review, opts, ffmpeg, say)
        start = wall_clock(entry["recording"]["start"])
        rows = []
        for e in events:
            t0 = e.start_f / opts.fps
            (x0, y0), (x1, y1) = e.track[0], e.track[-1]
            rows.append({
                "clock_time": f"{start + timedelta(seconds=t0):%H:%M:%S}",
                "video_offset": _hms(t0),
                "duration_s": round((e.end_f - e.start_f + 1) / opts.fps, 2),
                "kind": e.kind, "peak_pixels": e.peak,
                "direction": _direction(e.track) if e.kind == "motion" else "",
                # positions as % of the frame, so they mean the same at any resolution
                "x_start": round(100 * x0 / w), "y_start": round(100 * y0 / h),
                "x_end": round(100 * x1 / w), "y_end": round(100 * y1 / h),
            })
        path = dest / rid / f"detections_{rid}.csv"
        with path.open("w", newline="", encoding="utf-8") as fh:
            wri = csv.DictWriter(fh, fieldnames=DETECTION_COLUMNS)
            wri.writeheader()
            wri.writerows(rows)
        n_motion = sum(1 for r in rows if r["kind"] == "motion")
        out["recordings"].append({"id": rid, "motion": n_motion,
                                  "scene_changes": len(rows) - n_motion, "file": str(path)})
        say(f"    {n_motion} motion event(s), {len(rows) - n_motion} scene change(s)")
    rep = dest / "detect_report.json"
    options = {k: v for k, v in opts.__dict__.items() if k != "only"}
    rep.write_text(json.dumps({"options": options, **out}, indent=2), encoding="utf-8")
    from . import audit
    audit.record(dest, "detect", {"options": options,
                                  "recordings": {r["id"]: r["motion"] for r in out["recordings"]},
                                  "detect_report_sha256": audit.sha256_file(rep)})
    return out
