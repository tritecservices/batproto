# Changelog

All notable changes to the platform. Versions follow semantic versioning
(MAJOR.MINOR.PATCH). Each release links to its change record.

## Survey hub 0.1.0 and Emergence Review Kit 0.3.0 (unreleased)

### Added: Survey Studio, the desktop app
- **Survey Studio** (`survey-studio.exe`, Start menu and desktop shortcuts): the whole
  workflow for ecologists without a command line. Import camera cards with progress,
  a review player (frame step, speed, 1-second keyframes, a timeline showing movement
  and logged events), one-key event logging (E/R/P/F/O, number keys for counts),
  species/direction/notes, undo, autosave, a movement checklist (N/B), sign-off,
  independent QA with the comparison shown, and reports. Location entered for sun
  times is never saved.
- An "Acoustic analysis" workspace placeholder (the next workspace to be built).
- Runs offline: a local server on 127.0.0.1 with a per-launch secret, strict content
  security policy and DNS-rebinding and forged-request protection; opens as its own
  window through Microsoft Edge. One instance per user. Logs to
  `%APPDATA%\Ecomsp\SurveyStudio\studio.log`.
- Team hub integration: opening a recording reserves it at the hub; others see it
  read-only.
- Quick guide for ecologists: `docs/SURVEY-STUDIO.md`.

### Fixed
- `prep`: the burned-in clock could read one second behind the frame's position in the
  review copy (timestamps from joined camera clips jitter by a few milliseconds). Frames
  are now timed by frame number, so the clock on screen and the logged time always agree.
  Re-run `prep --force` to remake older review copies.

### Added (phase 6: multi-user)
- **Survey hub** (`hub/`): central service on PostgreSQL (SQLite for trials), Entra ID
  sign-in only, per-tenant isolation in every query.
  - Review locks: one reviewer per recording, expiring and renewable; admin lock break
    with a mandatory reason.
  - Separation of duties enforced centrally: QA needs Survey.QA, a different person from
    the reviewer, and the same log that was signed off.
  - Job queue for `scan`, `prep` and `detect` on the server: leases, retries with
    back-off, dead jobs kept for the service desk, `FOR UPDATE SKIP LOCKED` for many
    workers; paths decided by the worker, parameters allow-listed.
  - Audit anchoring: detects folder audit trails that were cut short or rebuilt, which a
    folder can't detect on its own.
  - Backup (`hub.backup`): database, audit logs, knowledge bases and review logs, with a
    SHA-256 manifest; verify, restore, and an automated weekly restore test.
  - Numbered schema migrations; `/health` for monitoring; systemd units with hardening;
    Debian set-up script; off-site copy to Azure Storage (UK South) by managed identity.
- Emergence Kit: `hub link | status | lock | unlock | submit | jobs | anchor | verify`;
  `signoff` and `qa` go through the hub for linked folders; anchoring after each command
  (best effort, so offline field work continues).
- Tests: 58 hub tests, run on SQLite and PostgreSQL 16, including concurrency (6
  workers, 40 jobs, no duplicates; 8 simultaneous lock attempts, one winner), the kit's
  CLI over HTTP against a running hub, and a worker running real jobs on a fake card.

### Fixed
- `prep`: ffmpeg's stderr went to a pipe nobody read, which could fill on long encodes
  and hang the encode; it now goes to a temporary file.
- `detect`: closes the decoder pipe, and reports a video ffmpeg can't decode.
- Repository: `brain/api.py`, `cli.py`, `mcp_server.py`, `store.py`, `sensitivity.py`,
  the QGIS agent and the website's product catalogue had not all reached the repository
  in phases 2-3. They are restored, and the knowledge base tests pass with FastAPI
  installed.

## Emergence Review Kit 0.2.0

### Added
- `prep`: local copies with SHA-256 checksums (`checksums.sha256`), review copies with
  1-second keyframes and the camera clock burned in, empty review logs; resumable.
- `verify`: re-hashes local copies against the checksum record.
- `report`: validates review logs; offline sunrise/sunset (NOAA); first/last emergence
  or re-entry, minutes from sun, 15-minute bins; HTML, CSV and JSON outputs. The survey
  location is never written out.
- `detect`: motion-based first pass producing a jump list per recording.
- MP4 support: Sony XAVC S (with XML sidecar times) and generic DCIM cameras.
- Windows installer (MSI) built in CI, with silent install and an Intune guide.
- Developer tools: synthetic camera cards with ground truth, a simulated reviewer,
  and a detection scorer.

- Audit trail (phase 4): hash-chained `audit.jsonl` per folder recording who ran every
  command and the SHA-256 of inputs and outputs; `audit verify` and `audit export`.
- Review QA: `signoff` fingerprints the review log; `qa` compares an independent second
  log (a different person is required) and records the decision and agreement figures;
  the report shows sign-off/QA status and flags logs edited after sign-off.
- Knowledge base: Microsoft Entra ID sign-in, app roles, per-tenant databases
  (phase 3); per-tenant hash-chained access audit that stores query fingerprints, not
  query text.

- Security (phase 5): secrets from Azure Key Vault (fail closed); weekly and per-change
  dependency vulnerability scans; SBOM for every build and release; Dependabot;
  SECURITY.md with response targets; DPIA draft, Cyber Essentials readiness, hosting and
  residency statement, pen-test scope.

### Fixed
- Installer build: WiX packaged an empty MSI when given a relative source path; now
  absolute, with a size guard.
- ffmpeg 7/8 compatibility (`-/filter:v`), with automatic fallback.
- MP4 header times are now treated as UTC (they were an hour out in summer).

### Governance
- Clean-room policy, data provenance register and automated provenance check.

## Emergence Review Kit 0.1.0
- `scan`: finds Sony AVCHD recordings and joins split clips; manifest with time sources.
