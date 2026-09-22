"""Single entry point: python -m brain.cli <command>"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

from .store import Store


def _p(obj) -> None:
    print(json.dumps(obj, indent=2, default=str))


def cmd_ingest(args) -> None:
    store = Store(args.db)
    what = args.what
    if what in ("discord", "all"):
        from .ingest import discord_source
        try:
            _p({"discord": discord_source.run(store, days=args.days, full=args.full)})
        except SystemExit as e:
            print(f"discord skipped: {e}", file=sys.stderr)
    if what in ("stackexchange", "se", "all"):
        from .ingest import stackexchange
        _p({"stackexchange": stackexchange.run(store, min_score=args.min_score,
                                               max_pages=args.pages)})
    if what in ("discourse", "forum", "all"):
        from .ingest import discourse
        _p({"discourse": discourse.run(store, max_pages=args.pages)})
    if what in ("github", "gh", "all"):
        from .ingest import github_issues
        _p({"github": github_issues.run(store, max_pages=args.pages)})
    if what in ("files", "nas", "all"):
        from .ingest import files
        try:
            _p({"files": files.run(store, roots=args.root or None,
                                   max_mb=args.max_mb, full=args.full)})
        except SystemExit as e:
            print(f"files skipped: {e}", file=sys.stderr)
    if what in ("manuals", "docs") or (
            what == "all" and (args.config or __import__("os").path.exists("manuals.yaml"))):
        from .ingest import manuals
        cfg = args.config or "manuals.yaml"
        try:
            _p({"manuals": manuals.run(store, cfg, only=args.only, fetch=args.download)})
        except FileNotFoundError:
            print(f"manuals skipped: no config at {cfg} (copy manuals.example.yaml)",
                  file=sys.stderr)
    if what in ("nightarc", "sql", "all"):
        from .ingest import nightarc_sql
        try:
            _p({"nightarc": nightarc_sql.run(store)})
        except Exception as e:
            print(f"nightarc skipped: {e.__class__.__name__}: {e}", file=sys.stderr)
    if not args.no_embed:
        from .embed import embed_pending, get_embedder
        emb = get_embedder(args.embedder)
        _p({"embedded_chunks": embed_pending(store, emb), "model": emb.name})


def cmd_embed(args) -> None:
    from .embed import embed_pending, get_embedder
    store = Store(args.db)
    emb = get_embedder(args.embedder)
    _p({"embedded_chunks": embed_pending(store, emb), "model": emb.name})


def cmd_search(args) -> None:
    from .search import search
    store = Store(args.db)
    hits = search(store, args.query, limit=args.limit, source=args.source)
    for h in hits:
        when = (datetime.fromtimestamp(h["created_at"], timezone.utc).strftime("%Y-%m-%d")
                if h["created_at"] else "?")
        print(f"\n[{h['source']}] {h['title']}  ({when})  score={h['score']}")
        print(f"  {h['url'] or h['doc_id']}")
        print("  " + h["excerpt"][:400].replace("\n", "\n  "))
    if not hits:
        print("no matches")


def cmd_sources(args) -> None:
    _p(Store(args.db).sources())


def cmd_serve(args) -> None:
    import uvicorn
    uvicorn.run("brain.api:app", host=args.host, port=args.port, reload=False)


def cmd_mcp(args) -> None:
    from .mcp_server import main
    main()


def cmd_foundry(args) -> None:
    from .foundry import AgentSpec, FoundryFactory
    if args.action == "validate":            # no Azure calls, safe offline check
        for path in sorted(__import__("pathlib").Path(args.path).glob("*.y*ml")):
            spec = AgentSpec.from_file(path)
            _p({"file": path.name, "name": spec.name, "model": spec.model,
                "tools": [t.get("type") for t in spec.tools],
                "instruction_chars": len(spec.instructions)})
        return
    factory = FoundryFactory(endpoint=args.endpoint)
    if args.action == "apply":
        _p(factory.apply_dir(args.path))
    elif args.action == "list":
        _p(factory.list_agents())
    elif args.action == "delete":
        factory.delete(args.path)
        _p({"deleted": args.path})
    elif args.action == "ask":
        print(factory.ask(args.agent, args.path))


def main(argv=None) -> None:
    from . import secrets
    secrets.ensure_loaded()                 # Azure Key Vault, if AZURE_KEYVAULT_URL is set
    ap = argparse.ArgumentParser(prog="brain", description="Ecology MSP knowledge base")
    ap.add_argument("--db", default=None, help="sqlite path (default data/brain.db)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    i = sub.add_parser("ingest", help="pull a source into the store")
    i.add_argument("what", choices=["discord", "stackexchange", "se", "discourse", "forum",
                                    "github", "gh", "nightarc", "sql", "files", "nas",
                                    "manuals", "docs", "all"])
    i.add_argument("--root", action="append",
                   help="file share path to ingest; repeatable. "
                        "Defaults to BRAIN_FILE_ROOTS")
    i.add_argument("--max-mb", type=float, default=25.0,
                   help="per-file size cap for the files source")
    i.add_argument("--config", default=None,
                   help="manuals: YAML list of manuals (default manuals.yaml)")
    i.add_argument("--only", action="append",
                   help="manuals: ingest just this manual id; repeatable")
    i.add_argument("--download", action="store_true",
                   help="manuals: fetch listed PDF urls that aren't on disk yet")
    i.add_argument("--days", type=int, default=365, help="discord backfill window")
    i.add_argument("--full", action="store_true", help="ignore cursors, re-read everything")
    i.add_argument("--pages", type=int, default=5)
    i.add_argument("--min-score", type=int, default=1)
    i.add_argument("--embedder", default=None)
    i.add_argument("--no-embed", action="store_true")
    i.set_defaults(func=cmd_ingest)

    e = sub.add_parser("embed", help="backfill embeddings")
    e.add_argument("--embedder", default=None)
    e.set_defaults(func=cmd_embed)

    s = sub.add_parser("search", help="query the store")
    s.add_argument("query")
    s.add_argument("--limit", type=int, default=8)
    s.add_argument("--source", default=None)
    s.set_defaults(func=cmd_search)

    sub.add_parser("sources", help="corpus inventory").set_defaults(func=cmd_sources)

    sv = sub.add_parser("serve", help="run the HTTP/OpenAPI server")
    sv.add_argument("--host", default="0.0.0.0")
    sv.add_argument("--port", type=int, default=8077)
    sv.set_defaults(func=cmd_serve)

    sub.add_parser("mcp", help="run the MCP stdio server").set_defaults(func=cmd_mcp)

    f = sub.add_parser("foundry", help="manage Microsoft Foundry agents")
    f.add_argument("action", choices=["validate", "apply", "list", "delete", "ask"])
    f.add_argument("path", nargs="?", default="agents/",
                   help="spec dir, agent name, or question for `ask`")
    f.add_argument("--agent", default=None, help="agent name for `ask`")
    f.add_argument("--endpoint", default=None)
    f.set_defaults(func=cmd_foundry)

    args = ap.parse_args(argv)
    if args.db is None:
        from .store import DEFAULT_DB
        args.db = DEFAULT_DB
    args.func(args)


if __name__ == "__main__":
    main()
