# Emergence Review Kit and Survey Studio

Bat emergence survey video, from camera card to report tables.

**Ecologists use Survey Studio**, the desktop app (Start menu → Survey Studio): see
`docs/SURVEY-STUDIO.md` in the repository root. Everything below is the engine and
command line underneath it, for IT, automation and the team hub.

```powershell
emergence-kit studio            # open the app from a terminal (same as the shortcut)
```

**Problem:** survey camcorders record a night as several split files (`.MTS` from
Sony AVCHD, `.MP4` from Sony XAVC S and most IR cameras), often interlaced, with
keyframes far apart. Reviewing that in VLC or Wisard straight off the
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
| `devtools/make_fake_card.py` | **Working.** Generates synthetic cards with simulated bats + a ground-truth log |
| `prep` / `verify` | **Working.** Local copies with SHA-256, review copies (1 s keyframes, clock burned in), empty review logs |
| `report` | **Working.** Validates review logs, offline sunset/sunrise, first/last emergence, 15-min bins -> HTML/CSV/JSON |
| `devtools/fill_logs_from_truth.py` | Plays the reviewer on fake cards so the whole pipeline runs end to end |
| `detect` | **Working (first pass).** Flags moments with small moving objects -> `detections_<id>.csv` jump list. Needs `numpy` |
| `devtools/score_detections.py` | Scores `detect` against fake-card ground truth (caught / false alarms) |
| `signoff` / `qa` | **Working.** Reviewer sign-off; independent second-reviewer QA (must be a different person) |
| `audit verify` / `audit export` | **Working.** Tamper-evident audit trail of every command, per folder |
| `hub ...` | **Working.** Team working through the survey hub: locks, central QA rules, server jobs, audit anchoring (`docs/HUB.md`) |

## Install (Windows, managed)

IT departments deploy the signed MSI built by the Release workflow. It needs no Python,
installs silently and puts `emergence-kit` on PATH. See `docs/deploy/INTUNE.md` in the
repository root. ffmpeg is deployed separately (licensing), and the kit finds it in the
standard winget, Program Files and Chocolatey locations.

## Setup (Windows, developers)

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

No Python packages needed: standard library only, Python 3.10+ (`detect` also needs
`numpy`; `hub` needs the Azure CLI for sign-in).

## Supported cards

| Card | Layout | Start time from | Trust |
|---|---|---|---|
| Sony AVCHD | `PRIVATE/AVCHD/BDMV/STREAM/00000.MTS` | exiftool (camera's MDPM block), else file date | good with exiftool |
| Sony XAVC S | `PRIVATE/M4ROOT/CLIP/C0001.MP4` + `C0001M01.XML` | the XML sidecar, with UTC offset, model and serial | best |
| Other cameras | `DCIM/100xxxxx/*.MP4`, or loose files | exiftool / MP4 header | **flagged**: many cameras write local time where the MP4 spec says UTC |

Every recording in the manifest says which source its time came from.

## No camera card? Make a fake one

```powershell
python devtools\make_fake_card.py C:\FakeCards --small --clip-seconds 20   # ~20 s to build
python -m emergence_kit scan C:\FakeCards --out C:\FakeCards\manifest.json
```

This builds two cards of each type: AVCHD, XAVC S and DCIM. Card 2 of each is stopped
and restarted mid-survey, so `scan` should report 9 recordings. The clips are dark
"night" video with small bright bats crossing at known times, and every crossing is in
`ground_truth.csv`, in the review-log columns. That file is the expected answer when
you build `report`. Drop `--small` for full 1920x1080 (slower, larger).

It is **synthetic data**: never use it as, or mix it with, survey data.

## Code map

```
emergence_kit/
  probe.py   ffprobe/exiftool wrappers -> ClipInfo (start time + where it came from)
  scan.py    find videos, card detection, group split clips -> Recording, manifest
  prep.py    local copies + checksums, review copies        report.py  results tables
  detect.py  motion first pass                             sun.py     NOAA sunrise/sunset
  audit.py   hash-chained audit trail                      qa.py      sign-off and QA
  hub_client.py  survey hub (locks, jobs, anchors)         cli.py     command line
devtools/
  make_fake_card.py   synthetic AVCHD / XAVC S / DCIM cards + ground_truth.csv
tests/       unittest; generates real clips and fake cards with ffmpeg
```

---

## `prep` (built; notes kept for reference)

```
python -m emergence_kit prep manifest.json --to C:\Review\2026-06-14 --dry-run   # space check
python -m emergence_kit prep manifest.json --to C:\Review\2026-06-14 [--hw qsv]
python -m emergence_kit verify C:\Review\2026-06-14        # re-hash copies any time
```

Output: `originals/<card>/...` (byte-identical copies), `checksums.sha256`, and per
recording `<id>/<id>_review.mp4` plus `<id>/review_log_<id>.csv`. Re-running skips
finished work. The review copy is encoded directly from the local clips via a concat
list, so no joined intermediate file doubles the disk use.

Original design notes:

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

## End to end on fake cards (5 minutes)

```powershell
python devtools\make_fake_card.py C:\FakeCards --small --clip-seconds 20
python -m emergence_kit scan C:\FakeCards --out C:\FakeCards\manifest.json
python -m emergence_kit prep C:\FakeCards\manifest.json --to C:\Review\test
python devtools\fill_logs_from_truth.py C:\FakeCards\ground_truth.csv C:\Review\test --with-mistakes
python -m emergence_kit report C:\Review\test --lat 51.5 --lon -0.12 --site "Test barn"
start C:\Review\test\report.html
```

Automatic first pass, scored against the simulation:

```powershell
python -m pip install numpy
python -m emergence_kit detect C:\Review\test
python devtools\score_detections.py C:\FakeCards\ground_truth.csv C:\Review\test
```

Defaults (sensitivity 5, min 4 px, peak 12) favour catching every bat over avoiding
false alarms. On the fake cards they catch 46/46 with 3 false alarms, but the fake bats
are bright and clean: **recalibrate on real footage** before quoting any accuracy.

`--lat/--lon` are used only to calculate sunset/sunrise and are never written to any
output. Recordings starting after midday are treated as dusk (emergence, vs sunset);
before midday as dawn (re-entry, vs sunrise).

## Working as a team: the survey hub

```powershell
emergence-kit hub link   C:\Review\test --survey barn-a-2026-06-14   # once per folder
emergence-kit hub lock   C:\Review\test <recording>                  # before reviewing
emergence-kit signoff    C:\Review\test <recording>                  # checked by the hub
emergence-kit hub verify C:\Review\test                              # audit vs the hub
```

Needs `EMERGENCE_HUB_URL`, `EMERGENCE_HUB_SCOPE` and `az login`. See `docs/HUB.md` in the
repository root.

## Sign-off, QA and the audit trail

```powershell
emergence-kit signoff C:\Review\test card1-xavc-20260614-210500            # reviewer
# a different person fills qa_log_<id>.csv independently, then:
emergence-kit qa C:\Review\test card1-xavc-20260614-210500 --decision approve --note "..."
emergence-kit audit verify C:\Review\test
emergence-kit audit export C:\Review\test --out audit.csv
```

Every command writes to `<folder>/audit.jsonl`, a hash chain in which each entry covers
the one before it, so edits, deletions and reordering are detected. The report shows who
reviewed and who QA'd each recording, and flags logs edited after sign-off. Full
details: `docs/AUDIT.md` in the repository root.

## `report` (built; original design notes)

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
