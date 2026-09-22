# Emergence Review Kit

Bat emergence survey video, from camera card to report tables.

**Problem:** Sony AVCHD cameras record a night as several `.MTS` files, usually
interlaced, with keyframes far apart. Reviewing that in VLC or Wisard straight off the
network share is painfully slow, and the counts end up retyped into every report.

**What the tool does:**

```
scan   ->  prep                         ->  (ecologist reviews)  ->  report
cards      local copy + join + review       fills in review log      emergence tables,
           copy with burned-in clock        CSV per recording        minutes after sunset
```

Originals are **never modified**. They are survey evidence.

## Status

| Command | State |
|---|---|
| `scan` | **Working.** Finds recordings, joins split clips, reads times and format, writes `manifest.json` |
| `prep` | Hour 2: spec below |
| `report` | Hour 3: spec below |

## Setup (Windows)

```powershell
winget install Gyan.FFmpeg          # ffmpeg + ffprobe; reopen the terminal afterwards
# optional but strongly recommended: exiftool (https://exiftool.org) for the camera's
# own recording time. Without it, start times come from file dates and are flagged.
cd tools\emergence-kit
python -m emergence_kit scan "D:\" --out manifest.json          # a card in a reader
python -m emergence_kit scan "\\nas\surveys\2026-06-14" --out manifest.json
python -m unittest discover -s tests -v
```

If ffmpeg is installed but not on PATH, set `EMERGENCE_FFPROBE` (and later
`EMERGENCE_FFMPEG`, `EMERGENCE_EXIFTOOL`) to the full path of the `.exe`.

No Python packages needed: standard library only, Python 3.10+.

## Code map

```
emergence_kit/
  probe.py   ffprobe/exiftool wrappers -> ClipInfo (start time + where it came from)
  scan.py    find videos, card detection, group split clips -> Recording, manifest
  cli.py     argparse entry point; prep/report are stubs that say which hour they are
tests/       unittest; generates real interlaced .MTS clips with ffmpeg
```

---

## Hour 2: `prep`

```
python -m emergence_kit prep manifest.json --to C:\Review\2026-06-14 [--dry-run] [--hw qsv]
```

1. **Copy locally**, resumable: skip when the destination has the same size and SHA-256.
   Write `checksums.sha256` next to the copies (chain of custody). Copy to `*.part`, then
   rename, so an interrupted copy never looks finished.
2. **Join** each recording's clips without re-encoding, using ffmpeg's concat demuxer:
   write `list.txt` with `file 'path'` lines (escape `'` as `'\''`), then
   `ffmpeg -f concat -safe 0 -i list.txt -c copy -map 0 <id>.mts`.
   Check that the output duration matches the sum of the clips, within 1 s.
3. **Review copy** `<id>_review.mp4`. Put the filter in a text file and pass it with
   `-filter_script:v`. That avoids Windows quoting, and the drawtext escaping is
   exactly as below (tested with ffmpeg 6.1):
   ```
   # <id>.vf.txt   (one line)
   yadif=0:-1:0,scale=-2:720,drawtext=text='%{pts\:gmtime\:<EPOCH>\:%H\\\:%M\\\:%S}':x=20:y=20:fontsize=36:fontcolor=white:box=1:boxcolor=black@0.6
   ```
   ```
   ffmpeg -i <id>.mts -filter_script:v <id>.vf.txt -c:v libx264 -preset veryfast -crf 23
          -g 25 -keyint_min 25 -sc_threshold 0 -movflags +faststart -an <id>_review.mp4
   ```
   - `yadif=0:-1:0,` only when `interlaced` is true in the manifest.
   - `-g` and `-keyint_min` = the frame rate rounded, so there's a keyframe every second
     and scrubbing is instant.
   - `<EPOCH>` = the recording's start as *wall-clock seconds*:
     `int(start_naive_local.replace(tzinfo=timezone.utc).timestamp())`, used with
     `gmtime`, so the clock on screen shows the camera's local time whatever timezone
     the laptop is in.
   - `--hw qsv`: swap `libx264 -preset veryfast -crf 23` for
     `h264_qsv -global_quality 23` on Intel laptops.
   - If drawtext can't find a font on Windows, add
     `fontfile='C\:/Windows/Fonts/arial.ttf':` inside drawtext.
4. Write `review_log_<id>.csv` (see hour 3) with the header only, if it doesn't exist.
5. `prep_report.json`: what was copied, joined and encoded, and how long each step took.

Tests: two generated clips, then `prep`, then assert the joined duration is 4 s ± 0.5,
the review copy's keyframe interval is 1 s (`ffprobe -skip_frame nokey`), and a second
run copies nothing.

## Hour 3: `report`

```
python -m emergence_kit report C:\Review\2026-06-14 --lat 51.5 --lon -0.12 --out report.html
```

Review log CSV, one per recording, filled in by the ecologist while watching:

| column | example | notes |
|---|---|---|
| `clock_time` | `21:14:32` | read off the burned-in clock |
| `event` | `emergence` | `emergence`, `re-entry`, `pass`, `foraging`, `other` |
| `species_group` | `Pipistrellus sp.` | free text; the ecologist's call, never the tool's |
| `count` | `1` | integer ≥ 1 |
| `direction` | `N` | optional |
| `observer` | `LB` | initials |
| `notes` | | |

Output:

- Per recording: sunset time, first and last emergence (clock time and **minutes after
  sunset**), totals by event and species group, and counts per 15-minute bin.
- Per survey night: all recordings together, with camera/position labels.
- `report.html` (paste into Word) plus `summary.csv`.
- Sunset: implement the NOAA solar position formula in pure Python (about 40 lines,
  no dependency). Test it against published sunset times for a few UK dates.
- Validate the log: unknown events, counts < 1 and times outside the recording window
  are reported by row number, not silently dropped.
- Location is needed for sunset but the output must not print coordinates or grid
  references. Label sites by name only.

## Hour 4: packaging

- `pyproject.toml` already defines an `emergence-kit` command: `pip install -e .`
- A small `Run review prep.bat` for people who don't use a terminal.
- Product page status on the website stays "In development" until a real card has been
  through scan → prep → report.

## Licensing notes

- ffmpeg is **called, not bundled**. ffmpeg builds that include x264 are GPL, and
  distributing one inside a paid product brings GPL obligations. Tell customers to
  install ffmpeg, or ship an LGPL build and use the hardware encoder (`h264_qsv`).
- exiftool is called as an external program too.
- This tool reads video files and writes new ones. It doesn't integrate with or modify
  NightArc, Wisard or VLC.
