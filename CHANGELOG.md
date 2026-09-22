# Changelog

All notable changes to the platform. Versions follow semantic versioning
(MAJOR.MINOR.PATCH). Each release links to its change record.

## Emergence Review Kit 0.2.0 (unreleased)

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
