"""MCP server so Claude (and any MCP client) can query the brain directly.

Add to Claude Desktop / Claude Code:

    claude mcp add ecomsp-brain -s user -- python -m brain.mcp_server

or in claude_desktop_config.json:

    {"mcpServers": {"ecomsp-brain": {
        "command": "python", "args": ["-m", "brain.mcp_server"],
        "env": {"BRAIN_DB": "C:/brain/data/brain.db"}}}}

Read-only by design: nothing here can post to Discord or write to SQL Server.
"""
from __future__ import annotations

import json

try:                                    # mcp >= 2.0
    from mcp.server.mcpserver import MCPServer as _Server
except ModuleNotFoundError:             # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server

from .search import search as hybrid_search
from .store import Store

mcp = _Server("ecomsp-brain")
_store: Store | None = None


def store() -> Store:
    global _store
    if _store is None:
        _store = Store()
    return _store


@mcp.tool()
def search_brain(query: str, limit: int = 8, source: str | None = None) -> str:
    """Search archived Discord threads, GIS Stack Exchange, the QGIS forums, GitHub
    issues and the NightArc database schema.

    Use this before answering any QGIS, ArcGIS Pro, Anabat, Kaleidoscope or NightArc
    question. Filter with source = discord | stackexchange | discourse | github | nightarc.
    Always quote the returned url when you use a result.
    """
    hits = hybrid_search(store(), query, limit=limit, source=source)
    return json.dumps(hits, indent=2)


@mcp.tool()
def get_document(doc_id: str) -> str:
    """Fetch the full text of one document returned by search_brain."""
    d = store().get(doc_id)
    return json.dumps(d or {"error": "not found"}, indent=2)


@mcp.tool()
def list_sources() -> str:
    """Inventory of what is in the knowledge base, with document counts and date ranges."""
    return json.dumps(store().sources(), indent=2)


@mcp.tool()
def nightarc_schema(table: str | None = None) -> str:
    """Return NightArc SQL Server schema cards. Call this before writing any SQL
    against NightArc so column names are real rather than guessed."""
    st = store()
    if table:
        rows = st.db.execute(
            "SELECT id,title,text FROM documents WHERE source='nightarc' "
            "AND id LIKE 'nightarc:schema:%' AND lower(title) LIKE ?",
            (f"%{table.lower()}%",)).fetchall()
    else:
        rows = st.db.execute(
            "SELECT id,title,text FROM documents WHERE id LIKE 'nightarc:schema:%' "
            "ORDER BY title").fetchall()
    return json.dumps([dict(r) for r in rows], indent=2)


def main(transport: str = "stdio") -> None:
    """stdio for Claude Desktop / Claude Code; streamable-http to expose it to Foundry."""
    mcp.run(transport=transport) if transport != "stdio" else mcp.run()


if __name__ == "__main__":
    import sys
    main(sys.argv[1] if len(sys.argv) > 1 else "stdio")
