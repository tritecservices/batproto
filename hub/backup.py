"""Backup, verify and restore for the hub (ITIL service continuity management).

    python -m hub.backup create --out /srv/backups [--keep 14]
    python -m hub.backup verify /srv/backups/hub-20260923T020000Z
    python -m hub.backup restore /srv/backups/hub-... --to postgresql://.../hub_restore
    python -m hub.backup restore-test /srv/backups/hub-... --to postgresql://.../hub_scratch

A backup is one folder:

    db.dump | db.sqlite       the hub database (pg_dump custom format, or SQLite copy)
    audit/                    the hub's audit logs (hash-chained)
    knowledge/                per-tenant knowledge base databases, if configured
    review/<tenant>/<survey>/ review logs, QA logs, reports and audit trails from server
                              storage - the human work. Video is NOT included: it's large
                              and already exists on the cards and the file share; protect
                              it with storage snapshots or replication instead.
    manifest.json             SHA-256 of every file, row counts, schema version

A backup that has never been restored is a hope, not a backup: run ``restore-test``
on a schedule (see docs/BACKUP.md). It restores into a scratch database and checks the
row counts and audit chains match what the manifest recorded.

Backups contain personal data (names, review decisions). Keep them on encrypted
storage in the UK, e.g. an Azure Storage account in UK South with immutable
(WORM) retention, and restrict who can read them.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

from . import __version__
from .db import Database, ts, utcnow
from .schema import current

TABLES = ("surveys", "recordings", "locks", "jobs", "audit_anchors", "schema_migrations")
REVIEW_PATTERNS = ("*.csv", "audit.jsonl", "audit.head", "*.json", "report.html")


class BackupError(Exception):
    pass


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _sqlite_copy(src: Path, dst: Path) -> None:
    """Consistent copy of a live SQLite database (the backup API, not a file copy)."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(src) as s, sqlite3.connect(dst) as d:
        s.backup(d)


def _pg_env(url: str) -> dict:
    return {**os.environ, "PGCONNECT_TIMEOUT": "15"}


def _tool(name: str) -> str:
    path = shutil.which(name) or os.environ.get(f"HUB_{name.upper()}")
    if not path:
        raise BackupError(f"{name} not found; install postgresql-client matching the server")
    return path


def row_counts(db: Database) -> dict:
    out = {}
    with db.tx() as c:
        for t in TABLES:
            out[t] = c.execute(f"SELECT COUNT(*) AS n FROM {t}").one()["n"]  # noqa: S608 - fixed names
    return out


def create(db: Database, out_root: Path, keep: int | None = None,
           audit_dir: Path | None = None, knowledge_dir: Path | None = None,
           storage_root: Path | None = None) -> Path:
    stamp = utcnow().strftime("%Y%m%dT%H%M%SZ")
    target = Path(out_root) / f"hub-{stamp}"
    work = Path(out_root) / f".hub-{stamp}.partial"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    counts = row_counts(db)
    schema = current(db)

    if db.dialect == "postgresql":
        r = subprocess.run([_tool("pg_dump"), "--format=custom", "--no-owner", "--no-acl",
                            "--file", str(work / "db.dump"), db.url],
                           capture_output=True, text=True, env=_pg_env(db.url))
        if r.returncode:
            raise BackupError(f"pg_dump failed: {r.stderr.strip()[:500]}")
    else:
        _sqlite_copy(Path(db.path), work / "db.sqlite")

    audit_dir = audit_dir or Path(os.environ.get("BRAIN_AUDIT_DIR", "data/audit"))
    if audit_dir.is_dir():
        for f in sorted(audit_dir.glob("*.jsonl")) + sorted(audit_dir.glob("*.head")):
            (work / "audit").mkdir(exist_ok=True)
            shutil.copy2(f, work / "audit" / f.name)

    knowledge_dir = knowledge_dir or (Path(os.environ["BRAIN_TENANT_DB_DIR"])
                                      if os.environ.get("BRAIN_TENANT_DB_DIR") else None)
    if knowledge_dir and knowledge_dir.is_dir():
        for f in sorted(knowledge_dir.glob("*.db")):
            _sqlite_copy(f, work / "knowledge" / f.name)

    storage_root = storage_root or (Path(os.environ["HUB_STORAGE_ROOT"])
                                    if os.environ.get("HUB_STORAGE_ROOT") else None)
    if storage_root and storage_root.is_dir():
        for review in sorted(storage_root.glob("*/*/review")):
            rel = review.parent.relative_to(storage_root)
            for pattern in REVIEW_PATTERNS:
                for f in review.rglob(pattern):
                    if f.is_file():
                        dst = work / "review" / rel / f.relative_to(review)
                        dst.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(f, dst)

    files = {p.relative_to(work).as_posix(): {"sha256": _sha256(p), "bytes": p.stat().st_size}
             for p in sorted(work.rglob("*")) if p.is_file()}
    manifest = {"created": ts(utcnow()), "hub_version": __version__, "dialect": db.dialect,
                "schema": schema, "row_counts": counts, "files": files}
    (work / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    work.rename(target)                    # appears complete or not at all
    if keep:
        prune(Path(out_root), keep)
    return target


def prune(out_root: Path, keep: int) -> list[Path]:
    """Delete all but the newest `keep` backups. Only touches folders named hub-*."""
    backups = sorted(p for p in out_root.glob("hub-*") if p.is_dir()
                     and (p / "manifest.json").exists())
    old = backups[:-keep] if keep > 0 else []
    for p in old:
        shutil.rmtree(p)
    return old


def verify(backup: Path) -> list[str]:
    backup = Path(backup)
    mf = backup / "manifest.json"
    if not mf.exists():
        return ["manifest.json missing - incomplete backup"]
    manifest = json.loads(mf.read_text(encoding="utf-8"))
    problems = []
    for rel, info in manifest["files"].items():
        p = backup / rel
        if not p.exists():
            problems.append(f"{rel}: missing")
        elif _sha256(p) != info["sha256"]:
            problems.append(f"{rel}: checksum mismatch (corrupted or altered)")
    listed = set(manifest["files"]) | {"manifest.json"}
    extra = [p.relative_to(backup).as_posix() for p in backup.rglob("*")
             if p.is_file() and p.relative_to(backup).as_posix() not in listed]
    problems += [f"{e}: not in the manifest (added after the backup)" for e in extra]
    return problems


def restore(backup: Path, to_url: str, force: bool = False) -> dict:
    backup = Path(backup)
    problems = verify(backup)
    if problems:
        raise BackupError("backup failed verification: " + "; ".join(problems[:5]))
    manifest = json.loads((backup / "manifest.json").read_text(encoding="utf-8"))
    target = Database.connect(to_url)
    if target.dialect != manifest["dialect"]:
        raise BackupError(f"backup is {manifest['dialect']}, target is {target.dialect}")
    try:
        existing = current(target)
    except Exception:                                 # noqa: BLE001
        existing = 0
    if existing and not force:
        raise BackupError("target database already has a hub schema; restore into an "
                          "empty database, or pass --force to replace it")
    if target.dialect == "postgresql":
        target.close()
        r = subprocess.run([_tool("pg_restore"), "--clean", "--if-exists", "--no-owner",
                            "--no-acl", "--exit-on-error", "--dbname", to_url,
                            str(backup / "db.dump")],
                           capture_output=True, text=True, env=_pg_env(to_url))
        if r.returncode:
            raise BackupError(f"pg_restore failed: {r.stderr.strip()[:500]}")
    else:
        target.close()
        dst = Path(target.path)
        for suffix in ("", "-wal", "-shm"):
            Path(str(dst) + suffix).unlink(missing_ok=True)
        _sqlite_copy(backup / "db.sqlite", dst)
    return manifest


def restore_test(backup: Path, scratch_url: str) -> list[str]:
    """Restore into a scratch database and prove it's usable. Returns problems."""
    manifest = restore(backup, scratch_url, force=True)
    db = Database.connect(scratch_url)
    problems = []
    try:
        if current(db) != manifest["schema"]:
            problems.append(f"schema {current(db)} after restore, backup says {manifest['schema']}")
        counts = row_counts(db)
        for t, n in manifest["row_counts"].items():
            if counts.get(t) != n:
                problems.append(f"{t}: {counts.get(t)} rows after restore, backup had {n}")
    finally:
        db.close()
    problems += _check_audit_copies(Path(backup))
    return problems


def _check_audit_copies(backup: Path) -> list[str]:
    """The audit logs inside the backup must still verify as hash chains."""
    problems = []
    audit = backup / "audit"
    if audit.is_dir():
        from brain import audit as brain_audit
        old = os.environ.get("BRAIN_AUDIT_DIR")
        os.environ["BRAIN_AUDIT_DIR"] = str(audit)
        try:
            for log in audit.glob("*.jsonl"):
                problems += [f"audit/{log.name}: {p}" for p in brain_audit.verify(log.stem)]
        finally:
            if old is None:
                os.environ.pop("BRAIN_AUDIT_DIR", None)
            else:
                os.environ["BRAIN_AUDIT_DIR"] = old
    for folder in sorted({p.parent for p in (backup / "review").rglob("audit.jsonl")}):
        try:
            from emergence_kit import audit as kit_audit
        except ImportError:
            break
        bad, _ = kit_audit.verify(folder)
        problems += [f"{folder.relative_to(backup).as_posix()}: {p}" for p in bad]
    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="hub.backup")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create")
    c.add_argument("--out", required=True)
    c.add_argument("--keep", type=int, help="keep only the newest N backups")
    v = sub.add_parser("verify")
    v.add_argument("backup")
    r = sub.add_parser("restore")
    r.add_argument("backup")
    r.add_argument("--to", required=True, help="target database URL (empty database)")
    r.add_argument("--force", action="store_true")
    t = sub.add_parser("restore-test")
    t.add_argument("backup", help="a backup folder, or 'latest' in --out")
    t.add_argument("--to", required=True, help="scratch database URL; it will be overwritten")
    t.add_argument("--out", help="backup root, when backup is 'latest'")
    args = ap.parse_args(argv)
    try:
        if args.cmd == "create":
            from brain import secrets as _secrets
            _secrets.ensure_loaded()
            path = create(Database.connect(), Path(args.out), args.keep)
            print(f"backup written: {path}")
            return 0
        if args.cmd == "verify":
            problems = verify(Path(args.backup))
            for p in problems:
                print(f"PROBLEM: {p}")
            print("backup OK" if not problems else f"{len(problems)} problem(s)")
            return 1 if problems else 0
        if args.cmd == "restore":
            m = restore(Path(args.backup), args.to, args.force)
            print(f"restored backup from {m['created']} (schema {m['schema']}) into the target")
            return 0
        backup = Path(args.backup)
        if args.backup == "latest":
            found = sorted(Path(args.out or ".").glob("hub-*"))
            if not found:
                print("no backups found", file=sys.stderr)
                return 2
            backup = found[-1]
        problems = restore_test(backup, args.to)
        for p in problems:
            print(f"PROBLEM: {p}")
        print(f"restore test of {backup.name}: " + ("PASSED" if not problems else "FAILED"))
        return 1 if problems else 0
    except BackupError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
