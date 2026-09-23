"""Synthetic bat detector recordings for testing Survey Studio's acoustic workspace.

    python devtools/make_fake_bat_wavs.py C:\\FakeBats

Writes 384 kHz 16-bit WAV files with a GUANO metadata chunk, like a full-spectrum
detector: pipistrelle (FM sweep ending ~45 kHz), soprano pipistrelle (~55 kHz),
noctule (shallow ~20 kHz), Myotis (steep 90-30 kHz), plus noise-only files. Every call
is listed in calls_truth.csv. SYNTHETIC DATA: never mix with survey recordings.
"""
from __future__ import annotations

import argparse
import csv
import struct
import sys
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

SR = 384_000
SPECIES = {  # f_start, f_end (Hz), duration (s), interval (s), auto-id label
    "PIPPIP": (72_000, 45_000, 0.006, 0.090, "Pipistrellus pipistrellus"),
    "PIPPYG": (82_000, 55_000, 0.005, 0.080, "Pipistrellus pygmaeus"),
    "NYCNOC": (26_000, 19_000, 0.016, 0.300, "Nyctalus noctula"),
    "MYOSP": (90_000, 30_000, 0.003, 0.070, "Myotis sp."),
}


def call(f0, f1, dur):
    t = np.arange(int(dur * SR)) / SR
    # exponential sweep: steep start, flattening towards the end frequency (FM-QCF-like)
    k = np.log(f1 / f0) / dur
    phase = 2 * np.pi * f0 * (np.exp(k * t) - 1) / k
    env = np.sin(np.pi * np.linspace(0, 1, t.size)) ** 0.6
    return np.sin(phase) * env


def guano(ts: datetime, auto_id: str | None) -> bytes:
    lines = ["GUANO|Version:1.0", "Make:Simulated", "Model:FakeDetector 1",
             f"Timestamp:{ts.isoformat()}", "Loc Position:51.4545 -2.5879",
             f"Samplerate:{SR}", "Original Filename:synthetic"]
    if auto_id:
        lines.append(f"Species Auto ID:{auto_id}")
    body = "\n".join(lines).encode()
    return body + (b"\x00" if len(body) % 2 else b"")


def write_wav(path: Path, x: np.ndarray, meta: bytes) -> None:
    pcm = (np.clip(x, -1, 1) * 32767).astype("<i2").tobytes()
    fmt = struct.pack("<HHIIHH", 1, 1, SR, SR * 2, 2, 16)
    chunks = (b"fmt " + struct.pack("<I", len(fmt)) + fmt
              + b"guan" + struct.pack("<I", len(meta)) + meta
              + b"data" + struct.pack("<I", len(pcm)) + pcm)
    path.write_bytes(b"RIFF" + struct.pack("<I", 4 + len(chunks)) + b"WAVE" + chunks)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--seconds", type=float, default=3.0)
    ap.add_argument("--seed", type=int, default=3)
    args = ap.parse_args(argv)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(args.seed)
    start = datetime(2026, 6, 14, 21, 32, 0)
    plan = ["PIPPIP", "PIPPYG", None, "NYCNOC", "PIPPIP", "MYOSP", None, "PIPPYG"]
    truth = []
    for i, code in enumerate(plan):
        ts = start + timedelta(minutes=3 * i, seconds=int(rng.integers(0, 50)))
        n = int(args.seconds * SR)
        x = rng.normal(0, 0.004, n)
        if code:
            f0, f1, dur, gap, label = SPECIES[code]
            t = 0.25 + rng.uniform(0, 0.1)
            while t + dur < args.seconds - 0.2:
                c = call(f0, f1, dur) * rng.uniform(0.2, 0.6)
                s = int(t * SR)
                x[s:s + c.size] += c
                truth.append({"file": "", "species": code, "start_s": round(t, 4),
                              "f_end_khz": f1 / 1000})
                t += gap * rng.uniform(0.9, 1.1)
            auto = label
        else:
            auto = None
        name = f"SIM_{ts:%Y%m%d_%H%M%S}.wav"
        for row in truth:
            if not row["file"]:
                row["file"] = name
        write_wav(out / name, x, guano(ts, auto))
        print(f"{name}: {code or 'noise only'}")
    with (out / "calls_truth.csv").open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["file", "species", "start_s", "f_end_khz"])
        w.writeheader()
        w.writerows(truth)
    return 0


if __name__ == "__main__":
    sys.exit(main())
