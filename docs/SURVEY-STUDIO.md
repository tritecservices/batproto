# Survey Studio: quick guide for ecologists

Survey Studio is the app for reviewing bat emergence and re-entry survey video. Open it
from the **Start menu** or the **desktop icon**. You never need the command line.

## 1. First time

Enter your initials. They go into every event you log, so the report shows who
recorded what. Your Windows sign-in is recorded against sign-offs and QA automatically.

## 2. Import the night's camera cards

**Import camera cards** → name the survey (e.g. *Barn A dusk 14 June 2026*) → **Add a
card** for each camera → **Start import**.

- Add the card itself (its drive letter, in a card reader), or a folder already copied
  from one. Sony AVCHD (.MTS), Sony XAVC S (.MP4) and most MP4 cameras work.
- Survey Studio copies the footage to this computer with checksums, joins clips the
  camera split, and makes review copies that scrub instantly with the camera's clock on
  screen. **The cards are never changed.**
- Keep surveys on the laptop's own drive. Review copies play badly over the network.
- Intel laptop? Tick **Quick Sync**, which is much faster.

## 3. Find movement (optional, recommended)

**Find movement** lists moments with small moving objects in every recording. It's a
checklist to speed you up (press **N** to jump to the next one), not a count: you
still watch and decide.

## 4. Review

Click a recording. Play it, and when you see a bat, press a key. The event is logged
at the time on the clock:

| Key | Logs |   | Key | Does |
| --- | --- | --- | --- | --- |
| **E** | Emergence | | **Space** | Play / pause |
| **R** | Re-entry | | **← →** | Back / forward 1 second (Shift: 5 s) |
| **P** | Pass | | **, .** | One frame back / forward |
| **F** | Foraging | | **[ ]** | Slower / faster |
| **O** | Other | | **N / B** | Next / previous movement to check |
| **3 then E** | 3 bats emerging at once | | **Ctrl+Z** | Undo |

Pick the species or group before logging, or type it on the row afterwards. Click an
event's name to add a direction or note, and click a time to jump back to that moment.
Everything saves as you go.

When you've watched the whole recording: **I've finished: sign off this recording**.
If nothing flew, sign off with an empty log. That's a result too.

## 5. QA (a second person)

A colleague opens the same survey, picks the recording and switches to **QA check**.
They log independently (the first reviewer's log is hidden), then choose **Compare
with the review and decide**. Survey Studio shows how the two logs agree and records
the decision. The app won't let you QA a recording you reviewed yourself.

## 6. Report

**Make report** → site name, latitude and longitude → **Make report**. You get
first/last emergence and minutes after sunset, 15-minute counts, species totals and
a "Check before use" list: `report.html` (paste into Word) plus CSV tables for Excel,
all in the survey folder.

The location is used only to calculate sunset and sunrise. **It's never saved in the
report or any file**, so reports can be shared without revealing roost locations.

## Bat detector recordings (Acoustic analysis)

Click **Acoustic analysis** at the top → **Open a folder of recordings**. Any detector's
full-spectrum WAV files work (16/24/32-bit, any sample rate, GUANO metadata read).

- Each recording shows as a **spectrogram** with a frequency scale. The strip below it
  marks every call found; click one (or a row in the call table) to zoom in.
- **Play ×10** plays it slowed down ten times, so the calls are audible.
- The call table gives start, duration, start→end frequency and peak frequency. The
  summary chips show typical end and peak frequency, as a guide to the species.
- Choose **Your identification** and press **Enter** to save and move to the next file.
  **↑ ↓** move between files, **Z** zooms back out.
- Identifications are saved with your initials in `survey_studio_labels.csv` in the
  recordings folder (alongside the detector's auto ID), and each one is recorded in the
  folder's audit trail. The WAV files are never changed. Locations in the files are
  never shown or exported.

## Working as a team

If IT has connected your surveys to the team hub, Survey Studio shows **Team hub** at
the bottom left, and:

- when you open a recording, it's reserved for you. Colleagues see it's being reviewed,
  and their view is read-only until you move on;
- sign-off and QA are checked centrally, across everyone's laptops;
- heavy processing can run on the server instead of your laptop.

## If something goes wrong

- **"ffmpeg isn't installed"**: IT needs to deploy ffmpeg (see the IT guide,
  `docs/deploy/INTUNE.md`).
- **A time shows "check time"**: that recording's start time came from the file date,
  not the camera. Check the camera clock against the burned-in time.
- **Audit problem** (bottom left, red): a survey file was changed outside Survey Studio.
  Don't deliver the survey; contact the service desk.
- The app's own log is at `%APPDATA%\Ecomsp\SurveyStudio\studio.log`. Send it with any
  support request.
