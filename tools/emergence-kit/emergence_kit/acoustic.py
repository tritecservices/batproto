"""Bat detector recordings: read WAV + GUANO metadata, spectrograms, call measurements,
time-expanded playback. Needs numpy; everything else is the standard library.

Reads full-spectrum WAV as written by bat detectors (Wildlife Acoustics, Titley/Anabat,
Pettersson, Elekon, AudioMoth...): PCM 16/24/32-bit or 32-bit float, including the
WAVE_FORMAT_EXTENSIBLE header several detectors use, at any sample rate. GUANO metadata
(https://guano-md.org) is read from its ``guan`` chunk. Original files are only read.

Survey locations stored in GUANO (``Loc Position``) are reported as present/absent only;
coordinates never leave this module, the same rule as the video reports.
"""
from __future__ import annotations

import io
import re
import struct
import wave
import zlib
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import numpy as np

FORMAT_PCM, FORMAT_FLOAT, FORMAT_EXTENSIBLE = 1, 3, 0xFFFE
FILENAME_TIME = re.compile(r"(20\d{2})(\d{2})(\d{2})[_-]?(\d{2})(\d{2})(\d{2})")


class WavError(ValueError):
    pass


@dataclass
class Recording:
    path: Path
    sample_rate: int
    channels: int
    bits: int
    frames: int
    guano: dict = field(default_factory=dict)

    @property
    def duration_s(self) -> float:
        return self.frames / self.sample_rate if self.sample_rate else 0.0

    @property
    def timestamp(self) -> str | None:
        """Recording start, as the detector wrote it (GUANO), else from the file name."""
        ts = self.guano.get("Timestamp")
        if ts:
            return ts
        m = FILENAME_TIME.search(self.path.name)
        if m:
            y, mo, d, h, mi, s = map(int, m.groups())
            try:
                return datetime(y, mo, d, h, mi, s).isoformat()
            except ValueError:
                return None
        return None

    def summary(self) -> dict:
        g = self.guano
        return {
            "name": self.path.name, "duration_s": round(self.duration_s, 3),
            "sample_rate": self.sample_rate, "bits": self.bits, "channels": self.channels,
            "timestamp": self.timestamp,
            "auto_id": g.get("Species Auto ID"),
            "manual_id_in_file": g.get("Species Manual ID"),
            "detector": " ".join(x for x in (g.get("Make"), g.get("Model")) if x) or None,
            "has_location": "Loc Position" in g,
        }


# ------------------------------------------------------------------ reading
def _chunks(fh):
    riff = fh.read(12)
    if len(riff) < 12 or riff[:4] not in (b"RIFF", b"RF64") or riff[8:12] != b"WAVE":
        raise WavError("not a WAV file")
    while True:
        head = fh.read(8)
        if len(head) < 8:
            return
        cid, size = head[:4], struct.unpack("<I", head[4:])[0]
        start = fh.tell()
        yield cid, size, start
        fh.seek(start + size + (size & 1))


def parse_guano(raw: bytes) -> dict:
    text = raw.rstrip(b"\x00").decode("utf-8", errors="replace").replace("\r\n", "\n")
    out = {}
    for line in text.split("\n"):
        if ":" in line:
            k, v = line.split(":", 1)
            out[k.strip()] = v.strip().strip("\x00")
    return out


def open_recording(path: Path) -> Recording:
    path = Path(path)
    fmt = data = guano = None
    with path.open("rb") as fh:
        for cid, size, start in _chunks(fh):
            if cid == b"fmt ":
                fmt = fh.read(min(size, 64))
            elif cid == b"data":
                data = (start, size)
            elif cid == b"guan":
                guano = parse_guano(fh.read(min(size, 1 << 16)))
    if not fmt or not data:
        raise WavError("WAV has no fmt or data chunk")
    tag, ch, sr, _, align, bits = struct.unpack("<HHIIHH", fmt[:16])
    if tag == FORMAT_EXTENSIBLE and len(fmt) >= 26:
        tag = struct.unpack("<H", fmt[24:26])[0]
    if tag not in (FORMAT_PCM, FORMAT_FLOAT) or ch < 1 or not sr or not align:
        raise WavError(f"unsupported WAV encoding (format {tag}, {bits}-bit)")
    rec = Recording(path, sr, ch, bits, data[1] // align, guano or {})
    rec._data, rec._tag, rec._align = data, tag, align          # type: ignore[attr-defined]
    return rec


def read_samples(rec: Recording, start_s: float = 0.0, dur_s: float | None = None) -> np.ndarray:
    """Mono float32 samples in [-1, 1] (first channel)."""
    first = max(0, int(start_s * rec.sample_rate))
    n = rec.frames - first if dur_s is None else min(rec.frames - first, int(dur_s * rec.sample_rate))
    if n <= 0:
        return np.zeros(0, dtype=np.float32)
    off, _ = rec._data                                           # type: ignore[attr-defined]
    align, width = rec._align, rec.bits // 8                     # type: ignore[attr-defined]
    with rec.path.open("rb") as fh:
        fh.seek(off + first * align)
        raw = fh.read(n * align)
    n = len(raw) // align
    if rec._tag == FORMAT_FLOAT and width == 4:                  # type: ignore[attr-defined]
        x = np.frombuffer(raw[: n * align], dtype="<f4").reshape(n, -1)[:, 0]
        return x.astype(np.float32)
    if width == 2:
        x = np.frombuffer(raw[: n * align], dtype="<i2").reshape(n, -1)[:, 0] / 32768.0
    elif width == 3:
        b = np.frombuffer(raw[: n * align], dtype=np.uint8).reshape(n, align)[:, :3].astype(np.int32)
        v = b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)
        x = np.where(v & 0x800000, v - (1 << 24), v) / 8388608.0
    elif width == 4:
        x = np.frombuffer(raw[: n * align], dtype="<i4").reshape(n, -1)[:, 0] / 2147483648.0
    elif width == 1:
        x = (np.frombuffer(raw[: n * align], dtype=np.uint8).reshape(n, -1)[:, 0] - 128) / 128.0
    else:
        raise WavError(f"unsupported sample width {rec.bits}")
    return x.astype(np.float32)


# ------------------------------------------------------------------ analysis
def stft_db(x: np.ndarray, sr: int, nfft: int = 512, hop: int | None = None,
            max_cols: int = 2400) -> tuple[np.ndarray, float, float]:
    """Power spectrogram in dB, shape (freq bins, time columns). Returns (S, seconds per
    column, Hz per bin). The hop grows for long files so the width stays <= max_cols."""
    if len(x) < nfft:
        x = np.pad(x, (0, nfft - len(x)))
    hop = hop or max(nfft // 4, int(np.ceil((len(x) - nfft) / max_cols)) or 1)
    n_cols = 1 + (len(x) - nfft) // hop
    idx = np.arange(nfft)[None, :] + hop * np.arange(n_cols)[:, None]
    frames = x[idx] * np.hanning(nfft).astype(np.float32)
    spec = np.abs(np.fft.rfft(frames, axis=1)) ** 2
    s = 10 * np.log10(spec.T + 1e-12)
    return s.astype(np.float32), hop / sr, sr / nfft


def _colormap(v: np.ndarray) -> np.ndarray:
    """0..1 -> RGB, a dark-blue-to-yellow ramp readable on screen and in print."""
    stops = np.array([[10, 12, 30], [35, 45, 110], [30, 120, 140], [70, 180, 110],
                      [200, 220, 60], [255, 250, 210]], dtype=np.float32)
    pos = np.clip(v, 0, 1) * (len(stops) - 1)
    i = np.minimum(pos.astype(int), len(stops) - 2)
    t = (pos - i)[..., None]
    return (stops[i] * (1 - t) + stops[i + 1] * t).astype(np.uint8)


def png_bytes(rgb: np.ndarray) -> bytes:
    """Encode an (h, w, 3) uint8 image as PNG with the standard library only."""
    h, w, _ = rgb.shape
    raw = b"".join(b"\x00" + rgb[r].tobytes() for r in range(h))

    def chunk(tag, body):
        return (struct.pack(">I", len(body)) + tag + body
                + struct.pack(">I", zlib.crc32(tag + body) & 0xFFFFFFFF))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def spectrogram_png(rec: Recording, fmax_hz: float | None = None, height: int = 360,
                    dyn_db: float = 70.0, start_s: float = 0.0,
                    dur_s: float | None = None) -> tuple[bytes, dict]:
    x = read_samples(rec, start_s, dur_s)
    zoomed = dur_s is not None and dur_s <= 1.0
    nfft = (512 if zoomed else 1024) if rec.sample_rate >= 200_000 else 256
    if zoomed:
        s, sec_per_col, hz_per_bin = stft_db(x, rec.sample_rate, nfft, hop=nfft // 8)
    else:
        # full resolution, then keep the loudest column in each group, so short calls
        # stay visible when a long recording is squeezed into the width
        s, sec_per_col, hz_per_bin = stft_db(x, rec.sample_rate, nfft, hop=nfft // 4,
                                              max_cols=10**9)
        group = max(1, int(np.ceil(s.shape[1] / 2400)))
        if group > 1:
            cols = s.shape[1] // group * group
            s = s[:, :cols].reshape(s.shape[0], -1, group).max(axis=2)
            sec_per_col *= group
    nyq = rec.sample_rate / 2
    fmax = min(fmax_hz or nyq, nyq)
    top = max(2, int(fmax / hz_per_bin))
    s = s[:top]
    # floor just above the noise, so background is dark and calls stand out
    floor = float(np.median(s)) + 6.0
    ref = max(float(np.percentile(s, 99.9)), floor + 20.0)
    img = (s - floor) / min(dyn_db, ref - floor)
    img = img[::-1]                                   # high frequencies at the top
    # resample rows to the requested height (nearest)
    rows = np.linspace(0, img.shape[0] - 1, height).astype(int)
    rgb = _colormap(img[rows])
    meta = {"seconds": len(x) / rec.sample_rate, "start_s": start_s, "fmax_hz": fmax,
            "width": rgb.shape[1], "height": height, "sec_per_col": sec_per_col}
    return png_bytes(rgb), meta


def find_calls(rec: Recording, fmin_hz: float = 15_000, fmax_hz: float = 130_000,
               snr_db: float = 14.0, min_ms: float = 1.0, max_ms: float = 80.0) -> list[dict]:
    """Echolocation pulses and simple measurements (start, duration, start/end/peak
    frequency, bandwidth). A first pass for the ecologist, not an identification."""
    x = read_samples(rec)
    sr = rec.sample_rate
    nfft = 512 if sr >= 200_000 else 256
    hop = nfft // 4
    s, dt, df = stft_db(x, sr, nfft, hop=hop, max_cols=10**9)
    lo, hi = int(fmin_hz / df), min(s.shape[0], int(fmax_hz / df))
    if hi <= lo + 2:
        return []
    band = s[lo:hi]
    noise = np.median(band, axis=1, keepdims=True)
    excess = band - noise
    peak_bin = excess.argmax(axis=0)
    peak_db = excess.max(axis=0)
    active = peak_db > snr_db
    calls, i, n = [], 0, active.size
    while i < n:
        if not active[i]:
            i += 1
            continue
        j = i
        while j + 1 < n and (active[j + 1] or (j + 2 < n and active[j + 2])):
            j += 1
        dur_ms = (j - i + 1) * dt * 1000
        if min_ms <= dur_ms <= max_ms:
            seg = slice(i, j + 1)
            freqs = (peak_bin[seg] + lo) * df
            strong = peak_db[seg] > peak_db[seg].max() - 20       # ignore faint edges
            f = freqs[strong] if strong.any() else freqs
            k = int(np.argmax(peak_db[seg]))
            calls.append({
                "start_s": round(i * dt, 4), "duration_ms": round(dur_ms, 1),
                "f_start_khz": round(float(f[0]) / 1000, 1),
                "f_end_khz": round(float(f[-1]) / 1000, 1),
                "f_peak_khz": round(float(freqs[k]) / 1000, 1),
                "bandwidth_khz": round(float(f.max() - f.min()) / 1000, 1),
                "snr_db": round(float(peak_db[seg].max()), 1)})
        i = j + 1
    return calls


def time_expanded_wav(rec: Recording, factor: int = 10, max_s: float = 20.0) -> bytes:
    """The recording slowed down `factor` times (pitch drops by the same factor), so
    ultrasound becomes audible. No resampling: same samples, lower playback rate."""
    x = read_samples(rec, 0, max_s)
    peak = float(np.abs(x).max()) or 1.0
    pcm = (np.clip(x / peak * 0.9, -1, 1) * 32767).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(max(8000, int(rec.sample_rate / factor)))
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


def list_folder(folder: Path, limit: int = 5000) -> list[Path]:
    files = sorted(p for p in Path(folder).rglob("*") if p.suffix.lower() == ".wav" and p.is_file())
    return files[:limit]


# ------------------------------------------------------------------ labels
LABEL_FILE = "survey_studio_labels.csv"
LABEL_COLUMNS = ["file", "timestamp", "auto_id", "manual_id", "calls", "observer",
                 "labelled_at", "notes"]


def read_labels(folder: Path) -> dict[str, dict]:
    import csv
    f = Path(folder) / LABEL_FILE
    if not f.exists():
        return {}
    with f.open(encoding="utf-8-sig", newline="") as fh:
        return {r["file"]: r for r in csv.DictReader(fh)}


def write_labels(folder: Path, labels: dict[str, dict]) -> None:
    import csv
    import os
    import tempfile
    folder = Path(folder)
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".labels.", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=LABEL_COLUMNS, extrasaction="ignore")
        w.writeheader()
        for k in sorted(labels):
            w.writerow(labels[k])
    os.replace(tmp, folder / LABEL_FILE)
