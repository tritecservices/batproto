"""Generate fake camera cards of simulated emergence-survey video, for testing.

    python devtools/make_fake_card.py C:\\FakeCards                   # defaults below
    python devtools/make_fake_card.py C:\\FakeCards --small --clip-seconds 20
    python devtools/make_fake_card.py C:\\FakeCards --format xavc --cards 1

Card formats, matching what real cameras write:

  avchd  Sony AVCHD:   <card>/PRIVATE/AVCHD/BDMV/STREAM/00000.MTS
         1080i 25 fps interlaced H.264 + AC-3 in 192-byte-packet MPEG-TS.
         No usable timestamp inside the file (the real MDPM block can't be
         synthesised), so file dates are set the way a camera leaves them.
  xavc   Sony XAVC S:  <card>/PRIVATE/M4ROOT/CLIP/C0001.MP4 + C0001M01.XML
         1080p 25 fps H.264 + AAC; the XML sidecar carries the true start time.
  dcim   Generic IR / trail camera: <card>/DCIM/100MEDIA/VID_0001.MP4
         1080p MP4 with a creation_time header in UTC and nothing else.

Each clip is dark, noisy "night" footage with a roofline, and small bright "bats"
that cross at known times. Every crossing is written to ground_truth.csv in the
same columns as the review log, so it doubles as the expected answer when testing
`report`, and as a practice log for reviewers.

Card 2 onwards is stopped and restarted mid-survey (a 2-minute gap), so scan should
report two recordings for it. That is deliberate: it tests the grouping.

EVERYTHING HERE IS SYNTHETIC. It is not survey data and must never be used as such.
"""
from __future__ import annotations

import argparse
import csv
import os
import random
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

FORMATS = ("avchd", "xavc", "dcim")
REVIEW_COLUMNS = ["clock_time", "event", "species_group", "count", "direction",
                  "observer", "notes"]


@dataclass
class Bat:
    t: float            # seconds from recording start when the bat is mid-frame
    event: str          # emergence | re-entry | pass
    species: str
    count: int


def plan_bats(total_s: float, rng: random.Random) -> list[Bat]:
    """Emergence clusters early, passes throughout, a couple of re-entries late."""
    bats: list[Bat] = []
    t = rng.uniform(3, 8)
    while t < total_s - 3:
        frac = t / total_s
        roll = rng.random()
        if frac < 0.6 and roll < 0.7:
            event = "emergence"
        elif frac > 0.75 and roll < 0.25:
            event = "re-entry"
        else:
            event = "pass"
        species = "Pipistrellus sp." if rng.random() < 0.8 else "Myotis sp."
        bats.append(Bat(round(t, 2), event, species, 2 if rng.random() < 0.15 else 1))
        t += rng.uniform(2.5, 9) if frac < 0.6 else rng.uniform(6, 15)
    return bats


def sprites(bats: list[Bat], clip_offset: float, clip_len: float,
            w: int, h: int) -> list[tuple[str, str, float, float]]:
    """(x_expr, y_expr, t_on, t_off) per bat body in this clip, for ffmpeg's overlay
    filter, where `t` is time in seconds. A bat is on screen for 1.6 s centred on its
    crossing time. (Not drawbox: there `t` means line thickness, not time.)"""
    roof = int(h * 0.72)
    body_w = max(4, w // 160)
    out = []
    for i, b in enumerate(bats):
        t0 = b.t - 0.8 - clip_offset
        t1 = b.t + 0.8 - clip_offset
        if t1 < 0 or t0 > clip_len:
            continue                         # not in this clip
        x0 = int(w * (0.2 + (i * 0.137) % 0.6))
        vx = w * 0.18
        if b.event == "emergence":            # up and away from the roofline
            xe, ye = f"{x0}+(t-{t0:.2f})*{vx:.0f}", f"{roof}-(t-{t0:.2f})*{h * 0.35:.0f}"
        elif b.event == "re-entry":           # down onto the roofline
            xe, ye = f"{x0}-(t-{t0:.2f})*{vx:.0f}", f"{int(h * 0.2)}+(t-{t0:.2f})*{h * 0.32:.0f}"
        else:                                 # across the sky
            xe, ye = f"(t-{t0:.2f})*{w / 1.6:.0f}", f"{int(h * (0.15 + (i * 0.07) % 0.35))}"
        for k in range(b.count):
            dx = k * body_w * 3
            out.append((f"{xe}+{dx}", ye, max(t0, 0.0), t1))
    return out


def run_ffmpeg(args: list[str]) -> None:
    ff = shutil.which("ffmpeg") or os.environ.get("EMERGENCE_FFMPEG")
    if not ff:
        sys.exit("ffmpeg not found - install it first (winget install Gyan.FFmpeg)")
    r = subprocess.run([ff, "-v", "error", "-y", *args], capture_output=True, text=True)
    if r.returncode:
        sys.exit(f"ffmpeg failed:\n{r.stderr[-1500:]}")


def make_clip(out: Path, fmt: str, seconds: float, w: int, h: int,
              bats: list[tuple[str, str, float, float]], start_utc: datetime) -> None:
    roof_y = int(h * 0.72)
    bw, bh = max(4, w // 160), max(3, w // 260)
    graph = [f"color=c=0x1c201e:s={w}x{h}:r=25,noise=alls=16:allf=t,"
             f"drawbox=x=0:y={roof_y}:w={w}:h={h - roof_y}:color=0x070808:t=fill[v0]"]
    if bats:
        labels = "".join(f"[s{i}]" for i in range(len(bats)))
        graph.append(f"color=c=0xe8e8e8:s={bw}x{bh}:r=25,split={len(bats)}{labels}"
                     if len(bats) > 1 else f"color=c=0xe8e8e8:s={bw}x{bh}:r=25[s0]")
        for i, (xe, ye, t0, t1) in enumerate(bats):
            graph.append(f"[v{i}][s{i}]overlay=x='{xe}':y='{ye}'"
                         f":enable='between(t,{t0:.2f},{t1:.2f})'[v{i + 1}]")
    final = f"[v{len(bats)}]"
    video = ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",     # inputs first
             "-filter_complex", ";".join(graph), "-map", final, "-map", "0:a",
             "-t", f"{seconds:.3f}",
             "-c:v", "libx264", "-preset", "ultrafast", "-crf", "30", "-pix_fmt", "yuv420p"]
    if fmt == "avchd":
        args = [*video, "-flags", "+ildct+ilme", "-x264opts", "tff=1",
                "-c:a", "ac3", "-b:a", "192k",
                "-f", "mpegts", "-mpegts_m2ts_mode", "1", str(out)]
    else:
        args = [*video, "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart"]
        if fmt == "dcim":
            args += ["-metadata", f"creation_time={start_utc.strftime('%Y-%m-%dT%H:%M:%S.000000Z')}"]
        args.append(str(out))
    run_ffmpeg(args)


def sony_xml(start_local: datetime, frames: int, serial: str) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<NonRealTimeMeta xmlns="urn:schemas-professionalDisc:nonRealTimeMeta:ver.2.00" lastUpdate="{start_local.isoformat()}">
\t<Duration value="{frames}"/>
\t<CreationDate value="{start_local.isoformat()}"/>
\t<VideoFormat>
\t\t<VideoFrame videoCodec="AVC_1920_1080_HP@L41" captureFps="25p" formatFps="25p"/>
\t\t<VideoLayout pixel="1920" numOfVerticalLine="1080" aspectRatio="16:9"/>
\t</VideoFormat>
\t<Device manufacturer="Sony" modelName="SIMULATED-CAM" serialNo="{serial}"/>
</NonRealTimeMeta>
"""


def build_card(root: Path, idx: int, fmt: str, start: datetime, clips: int,
               clip_s: float, w: int, h: int, restart: bool, rng: random.Random,
               truth: list[dict]) -> None:
    card = root / f"CARD{idx}_{fmt.upper()}"
    if card.exists():
        shutil.rmtree(card)
    tz = timezone(timedelta(hours=1))                 # BST, like a summer survey

    # sessions: one continuous watch, or two with a gap if this card is "restarted"
    sessions = [(start, clips)] if not restart or clips < 2 else [
        (start, clips // 2 + clips % 2),
        (start + timedelta(seconds=(clips // 2 + clips % 2) * clip_s + 120), clips // 2)]

    n = 0
    for s_start, s_clips in sessions:
        total = s_clips * clip_s
        bats = plan_bats(total, rng)
        for b in bats:
            truth.append({"card": card.name,
                          # the moment the bat is mid-frame, rounded (not truncated) to
                          # the second a reviewer would read off the burned-in clock
                          "clock_time": (s_start + timedelta(seconds=round(b.t))).strftime("%H:%M:%S"),
                          "event": b.event, "species_group": b.species, "count": b.count,
                          "direction": {"emergence": "out", "re-entry": "in"}.get(b.event, ""),
                          "observer": "SIM", "notes": "synthetic"})
        for c in range(s_clips):
            clip_start = s_start + timedelta(seconds=c * clip_s)
            boxes = sprites(bats, c * clip_s, clip_s, w, h)
            start_utc = clip_start.replace(tzinfo=tz).astimezone(timezone.utc)
            if fmt == "avchd":
                folder = card / "PRIVATE" / "AVCHD" / "BDMV" / "STREAM"
                name = f"{n:05d}.MTS"
            elif fmt == "xavc":
                folder = card / "PRIVATE" / "M4ROOT" / "CLIP"
                name = f"C{n + 1:04d}.MP4"
            else:
                folder = card / "DCIM" / "100MEDIA"
                name = f"VID_{n + 1:04d}.MP4"
            folder.mkdir(parents=True, exist_ok=True)
            out = folder / name
            make_clip(out, fmt, clip_s, w, h, boxes, start_utc)
            if fmt == "xavc":
                (folder / f"C{n + 1:04d}M01.XML").write_text(
                    sony_xml(clip_start.replace(tzinfo=tz), int(clip_s * 25),
                             f"SIM{idx:04d}"), encoding="utf-8")
            end = (clip_start + timedelta(seconds=clip_s)).timestamp()   # local wall clock
            os.utime(out, (end, end))                 # camera closes the file at clip end
            n += 1
            print(f"  {out.relative_to(root)}")

    # the non-video files real cards carry, so scan has to ignore them
    if fmt == "avchd":
        bdmv = card / "PRIVATE" / "AVCHD" / "BDMV"
        for sub in ("CLIPINF", "PLAYLIST", "BACKUP"):
            (bdmv / sub).mkdir(exist_ok=True)
        for i in range(n):
            (bdmv / "CLIPINF" / f"{i:05d}.CPI").write_bytes(b"HDMV0200" + b"\0" * 56)
        (bdmv / "INDEX.BDM").write_bytes(b"INDX0100" + b"\0" * 56)
        (bdmv / "MOVIEOBJ.BDM").write_bytes(b"MOBJ0100" + b"\0" * 56)
    elif fmt == "xavc":
        (card / "PRIVATE" / "M4ROOT" / "MEDIAPRO.XML").write_text(
            '<?xml version="1.0"?><MediaProfile/>', encoding="utf-8")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("out", help="folder to create the fake cards in")
    ap.add_argument("--format", choices=[*FORMATS, "all"], default="all")
    ap.add_argument("--cards", type=int, default=2, help="cards per format (default 2)")
    ap.add_argument("--clips", type=int, default=3, help="clips per card (default 3)")
    ap.add_argument("--clip-seconds", type=float, default=60)
    ap.add_argument("--start", default="2026-06-14 21:05:00",
                    help="first recording start, local time (default %(default)s)")
    ap.add_argument("--small", action="store_true", help="640x360 instead of 1920x1080 (fast)")
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args(argv)

    root = Path(args.out)
    root.mkdir(parents=True, exist_ok=True)
    w, h = (640, 360) if args.small else (1920, 1080)
    rng = random.Random(args.seed)
    start = datetime.fromisoformat(args.start)
    truth: list[dict] = []

    formats = FORMATS if args.format == "all" else (args.format,)
    for fmt in formats:
        for i in range(1, args.cards + 1):
            print(f"card {i} ({fmt})")
            build_card(root, i, fmt, start + timedelta(minutes=(i - 1) * 2), args.clips,
                       args.clip_seconds, w, h, restart=i >= 2, rng=rng, truth=truth)

    with (root / "ground_truth.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=["card", *REVIEW_COLUMNS])
        writer.writeheader()
        writer.writerows(truth)
    (root / "SYNTHETIC_DATA_README.txt").write_text(
        "Everything in this folder was generated by make_fake_card.py for software\n"
        "testing. It is not survey data and must never be used or reported as such.\n",
        encoding="utf-8")
    print(f"\n{len(truth)} simulated crossings -> {root / 'ground_truth.csv'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
