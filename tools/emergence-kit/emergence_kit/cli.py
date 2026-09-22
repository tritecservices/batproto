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
    print(format_table(manifest))
    print(f"\nmanifest written to {args.out}")
    return 1 if manifest["failed"] else 0


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

    p = sub.add_parser("prep", help="copy locally, join clips, make review copies (hour 2)")
    p.set_defaults(func=cmd_todo("prep", 2))
    r = sub.add_parser("report", help="turn review logs into report tables (hour 3)")
    r.set_defaults(func=cmd_todo("report", 3))
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)
