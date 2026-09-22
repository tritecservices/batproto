"""python -m emergence_kit <command>"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .probe import ToolMissing


def cmd_scan(args) -> int:
    from .scan import format_table, scan, write_manifest

    roots = [Path(p) for p in args.paths]
    missing = [str(p) for p in roots if not p.exists()]
    if missing:
        print(f"not found: {', '.join(missing)}", file=sys.stderr)
        return 2
    progress = None if args.quiet else (lambda m: print(m, file=sys.stderr))
    try:
        manifest = scan(roots, gap_tolerance_s=args.gap, use_exiftool=not args.no_exiftool,
                        progress=progress)
    except ToolMissing as exc:
        print(exc, file=sys.stderr)
        return 3
    write_manifest(manifest, Path(args.out))
    from . import audit
    try:
        audit.record(Path(args.out).resolve().parent, "scan", {
            "roots": manifest["roots"], "files": manifest["summary"]["files"],
            "recordings": manifest["summary"]["recordings"],
            "manifest": Path(args.out).name, "manifest_sha256": audit.sha256_file(Path(args.out))})
    except RuntimeError as exc:
        print(f"WARNING: {exc}", file=sys.stderr)
    print(format_table(manifest))
    print(f"\nmanifest written to {args.out}")
    return 1 if manifest["failed"] else 0


def cmd_prep(args) -> int:
    import json
    from .prep import PrepOptions, prep

    mpath = Path(args.manifest)
    if not mpath.exists():
        print(f"manifest not found: {mpath} (run scan first)", file=sys.stderr)
        return 2
    manifest = json.loads(mpath.read_text(encoding="utf-8"))
    opts = PrepOptions(dest=Path(args.to), dry_run=args.dry_run, hw=args.hw, crf=args.crf,
                       only=args.only or [], skip_review=args.skip_review, force=args.force)
    try:
        report = prep(manifest, opts)
    except ToolMissing as exc:
        print(exc, file=sys.stderr)
        return 3
    except RuntimeError as exc:
        print(f"prep stopped: {exc}", file=sys.stderr)
        return 1
    if not args.dry_run:
        for r in report["recordings"]:
            rv = r["review"] if isinstance(r["review"], str) else r["review"]["status"]
            print(f"{r['id']}: copied {r['copied']}, already local {r['skipped_copy']}, "
                  f"review copy {rv}")
        print(f"\nreport: {Path(args.to) / 'prep_report.json'}")
        print("next: review each *_review.mp4 and fill in its review_log_*.csv")
    return 0


def cmd_verify(args) -> int:
    from .prep import verify
    problems = verify(Path(args.dest), say=print)
    for p in problems:
        print(p)
    return 1 if problems else 0


def cmd_report(args) -> int:
    from .report import run
    dest = Path(args.prep_dir)
    if not (dest / "prep_report.json").exists():
        print(f"no prep_report.json in {dest} (run prep first)", file=sys.stderr)
        return 2
    summary = run(dest, args.lat, args.lon, args.site, args.utc_offset,
                  Path(args.out_dir) if args.out_dir else None)
    for r in summary["recordings"]:
        key = "first_emergence" if r["survey"] == "dusk" else "first_re-entry"
        rel = r.get(key + "_vs_sun_min", "")
        print(f"{r['recording']}: {r['survey']}, {r['log_rows']} log rows, "
              f"first key event {r.get(key) or '-'}"
              + (f" ({rel:+d} min)" if isinstance(rel, int) else ""))
    out = Path(args.out_dir) if args.out_dir else dest
    if summary["issues"]:
        print(f"\n{len(summary['issues'])} thing(s) to check - see the top of report.html")
    print(f"written: {out / 'report.html'}, summary.csv, events.csv, summary.json")
    return 0


def cmd_detect(args) -> int:
    from .detect import DetectOptions, run
    dest = Path(args.prep_dir)
    if not (dest / "prep_report.json").exists():
        print(f"no prep_report.json in {dest} (run prep first)", file=sys.stderr)
        return 2
    opts = DetectOptions(sensitivity=args.sensitivity, min_pixels=args.min_pixels,
                         min_peak=args.min_peak,
                         fps=args.fps, width=args.width, only=args.only or [])
    try:
        out = run(dest, opts)
    except ToolMissing as exc:
        print(exc, file=sys.stderr)
        return 3
    total = sum(r["motion"] for r in out["recordings"])
    print(f"{total} motion event(s) across {len(out['recordings'])} recording(s); "
          f"see <recording>/detections_<id>.csv - jump to each video_offset in the review copy")
    return 0


def cmd_signoff(args) -> int:
    from .qa import QAError, signoff
    try:
        e = signoff(Path(args.prep_dir), args.recording, args.note or "")
    except (QAError, RuntimeError) as exc:
        print(f"not signed off: {exc}", file=sys.stderr)
        return 1
    print(f"{args.recording}: signed off by {e['actor']['user']} "
          f"({e['details']['rows']} log rows), audit entry {e['seq']}")
    return 0


def cmd_qa(args) -> int:
    from .qa import QAError, qa
    try:
        e = qa(Path(args.prep_dir), args.recording, args.decision, args.note or "",
               Path(args.qa_log) if args.qa_log else None)
    except (QAError, RuntimeError) as exc:
        print(f"QA not recorded: {exc}", file=sys.stderr)
        return 1
    c = e["details"]["comparison"]
    done = {"approve": "approved", "reject": "rejected"}[args.decision]
    print(f"{args.recording}: QA {done} by {e['actor']['user']} - "
          f"agreement {c['agreement']:.0%} ({c['matched']} matched, {c['only_primary']} only in "
          f"review, {c['only_qa']} only in QA), audit entry {e['seq']}")
    for ev, t in c["totals"].items():
        if t["difference"]:
            print(f"    {ev}: review {t['primary']}, QA {t['qa']} ({t['difference']:+d})")
    return 0


def cmd_audit(args) -> int:
    from . import audit
    folder = Path(args.folder)
    if args.action == "verify":
        problems, last = audit.verify(folder)
        for p in problems:
            print(f"PROBLEM: {p}")
        if not problems:
            n = last["seq"] if last else 0
            print(f"audit trail OK: {n} entries"
                  + (f", head {last['hash']} - record this in the change/ticket" if last else ""))
        return 1 if problems else 0
    n = audit.export_csv(folder, Path(args.out))
    print(f"exported {n} entries to {args.out}")
    return 0


def cmd_todo(name: str, hour: int):
    def run(_args) -> int:
        print(f"'{name}' is not built yet - it is hour {hour} of the plan in README.md",
              file=sys.stderr)
        return 4
    return run


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="emergence_kit",
        description="Prepare bat emergence survey video for fast review and reporting.")
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan", help="find recordings and write a manifest (read-only)")
    s.add_argument("paths", nargs="+", help="camera cards, copied card folders or files")
    s.add_argument("--out", default="manifest.json", help="manifest path (default: %(default)s)")
    s.add_argument("--gap", type=float, default=5.0,
                   help="seconds of slack when joining split clips (default: %(default)s)")
    s.add_argument("--no-exiftool", action="store_true",
                   help="skip exiftool even if installed (faster, weaker start times)")
    s.add_argument("--quiet", action="store_true")
    s.set_defaults(func=cmd_scan)

    p = sub.add_parser("prep", help="copy locally with checksums, make fast review copies")
    p.add_argument("manifest", help="manifest.json from scan")
    p.add_argument("--to", required=True, help="local folder for copies and review files")
    p.add_argument("--dry-run", action="store_true", help="show the plan and space needed")
    p.add_argument("--hw", choices=["none", "qsv"], default="none",
                   help="qsv = Intel Quick Sync hardware encoding (much faster on laptops)")
    p.add_argument("--crf", type=int, default=23, help="quality: lower = better/larger")
    p.add_argument("--only", action="append", help="just this recording id; repeatable")
    p.add_argument("--skip-review", action="store_true",
                   help="copy and checksum only, no review copies")
    p.add_argument("--force", action="store_true", help="remake review copies that exist")
    p.set_defaults(func=cmd_prep)

    v = sub.add_parser("verify", help="re-check local copies against checksums.sha256")
    v.add_argument("dest", help="the folder prep wrote to")
    v.set_defaults(func=cmd_verify)
    r = sub.add_parser("report", help="turn filled-in review logs into results tables")
    r.add_argument("prep_dir", help="the folder prep wrote to")
    r.add_argument("--lat", type=float, required=True,
                   help="survey latitude, for sunset/sunrise only (never written out)")
    r.add_argument("--lon", type=float, required=True,
                   help="survey longitude, + east / - west (never written out)")
    r.add_argument("--site", default="Survey site", help="site name to show in the report")
    r.add_argument("--utc-offset", type=float, default=None,
                   help="camera clock offset from UTC in hours, if not in the files (BST = 1)")
    r.add_argument("--out-dir", default=None, help="where to write (default: prep_dir)")
    r.set_defaults(func=cmd_report)

    d = sub.add_parser("detect", help="flag moments with small moving objects (first pass)")
    d.add_argument("prep_dir", help="the folder prep wrote to")
    d.add_argument("--sensitivity", type=float, default=5.0,
                   help="lower finds fainter movement but more false alarms (default 5)")
    d.add_argument("--min-pixels", type=int, default=4,
                   help="changed pixels needed in a frame (default 4)")
    d.add_argument("--min-peak", type=int, default=12,
                   help="an event must reach this many pixels at least once (default 12)")
    d.add_argument("--fps", type=float, default=12.5, help="analysis frame rate")
    d.add_argument("--width", type=int, default=640, help="analysis width in pixels")
    d.add_argument("--only", action="append", help="just this recording id; repeatable")
    d.set_defaults(func=cmd_detect)

    so = sub.add_parser("signoff", help="reviewer: declare a review log complete")
    so.add_argument("prep_dir")
    so.add_argument("recording", help="recording id")
    so.add_argument("--note", default="")
    so.set_defaults(func=cmd_signoff)

    q = sub.add_parser("qa", help="second reviewer: compare an independent log and decide")
    q.add_argument("prep_dir")
    q.add_argument("recording", help="recording id")
    q.add_argument("--decision", choices=["approve", "reject"], required=True)
    q.add_argument("--note", default="")
    q.add_argument("--qa-log", default=None, help="default: <id>/qa_log_<id>.csv")
    q.set_defaults(func=cmd_qa)

    au = sub.add_parser("audit", help="verify or export the audit trail")
    au.add_argument("action", choices=["verify", "export"])
    au.add_argument("folder", help="a prep folder (or the folder holding a manifest)")
    au.add_argument("--out", default="audit_export.csv", help="export: CSV path")
    au.set_defaults(func=cmd_audit)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)
