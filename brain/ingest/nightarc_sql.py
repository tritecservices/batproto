"""NightArc / SQL Server Express ingestion.

Two jobs:

1. `schema_documents()` -- read-only introspection of the NightArc database into
   plain-English schema cards (tables, columns, keys, row counts, sample values).
   This is the highest-value thing you can give an agent: it stops the model
   inventing column names when it writes SQL against NightArc.

2. `table_documents()` -- optional ingestion of free-text columns (notes, comments,
   species IDs, QA flags) so past survey decisions become searchable.

Connections are forced read-only at the application level (ApplicationIntent plus a
statement guard). Point it at a restored backup, not production, for first runs.
"""
from __future__ import annotations

import os
import re
from typing import Iterator

from ..sensitivity import classify_sensitivity
from ..store import Document, Store

WRITE_GUARD = re.compile(
    r"\b(insert|update|delete|drop|alter|truncate|merge|exec|execute|create)\b", re.I)


def connection_string(server: str | None = None, database: str | None = None,
                      driver: str | None = None) -> str:
    """Typical Express targets: r'.\\SQLEXPRESS', 'localhost\\SQLEXPRESS', '(local)\\SQLEXPRESS'."""
    server = server or os.environ.get("NIGHTARC_SERVER", r"localhost\SQLEXPRESS")
    database = database or os.environ.get("NIGHTARC_DB", "NightArc")
    driver = driver or os.environ.get("ODBC_DRIVER", "ODBC Driver 18 for SQL Server")
    user = os.environ.get("NIGHTARC_USER")
    pwd = os.environ.get("NIGHTARC_PASSWORD")
    auth = (f"UID={user};PWD={pwd};" if user else "Trusted_Connection=yes;")
    return (f"DRIVER={{{driver}}};SERVER={server};DATABASE={database};{auth}"
            "Encrypt=yes;TrustServerCertificate=yes;ApplicationIntent=ReadOnly;"
            "Connection Timeout=30;")


def connect(conn_str: str | None = None):
    import pyodbc
    cn = pyodbc.connect(conn_str or connection_string(), readonly=True, autocommit=True)
    return cn


def safe_query(cn, sql: str, params: tuple = ()) -> list[tuple]:
    if WRITE_GUARD.search(sql):
        raise PermissionError("write statements are blocked by the read-only guard")
    cur = cn.cursor()
    cur.execute(sql, params)
    return cur.fetchall()


SCHEMA_SQL = """
SELECT s.name AS schema_name, t.name AS table_name, c.name AS column_name,
       ty.name AS data_type, c.max_length, c.is_nullable, c.column_id
FROM sys.tables t
JOIN sys.schemas s  ON s.schema_id = t.schema_id
JOIN sys.columns c  ON c.object_id = t.object_id
JOIN sys.types ty   ON ty.user_type_id = c.user_type_id
ORDER BY s.name, t.name, c.column_id
"""

KEYS_SQL = """
SELECT s.name AS schema_name, t.name AS table_name, i.name AS index_name,
       c.name AS column_name, i.is_primary_key, i.is_unique
FROM sys.indexes i
JOIN sys.tables t ON t.object_id = i.object_id
JOIN sys.schemas s ON s.schema_id = t.schema_id
JOIN sys.index_columns ic ON ic.object_id = i.object_id AND ic.index_id = i.index_id
JOIN sys.columns c ON c.object_id = i.object_id AND c.column_id = ic.column_id
WHERE i.is_primary_key = 1 OR i.is_unique = 1
"""

FK_SQL = """
SELECT fk.name, ps.name AS parent_schema, pt.name AS parent_table, pc.name AS parent_col,
       rs.name AS ref_schema, rt.name AS ref_table, rc.name AS ref_col
FROM sys.foreign_keys fk
JOIN sys.foreign_key_columns fkc ON fkc.constraint_object_id = fk.object_id
JOIN sys.tables pt ON pt.object_id = fkc.parent_object_id
JOIN sys.schemas ps ON ps.schema_id = pt.schema_id
JOIN sys.columns pc ON pc.object_id = fkc.parent_object_id AND pc.column_id = fkc.parent_column_id
JOIN sys.tables rt ON rt.object_id = fkc.referenced_object_id
JOIN sys.schemas rs ON rs.schema_id = rt.schema_id
JOIN sys.columns rc ON rc.object_id = fkc.referenced_object_id
                   AND rc.column_id = fkc.referenced_column_id
"""

COUNT_SQL = """
SELECT s.name, t.name, SUM(p.rows) AS row_count
FROM sys.tables t
JOIN sys.schemas s ON s.schema_id = t.schema_id
JOIN sys.partitions p ON p.object_id = t.object_id AND p.index_id IN (0,1)
GROUP BY s.name, t.name
"""

TEXT_TYPES = {"varchar", "nvarchar", "text", "ntext", "char", "nchar"}


def schema_documents(cn, database: str | None = None, sample_rows: int = 3
                     ) -> Iterator[Document]:
    db = database or os.environ.get("NIGHTARC_DB", "NightArc")
    cols: dict[tuple[str, str], list] = {}
    for r in safe_query(cn, SCHEMA_SQL):
        cols.setdefault((r[0], r[1]), []).append(r)
    keys: dict[tuple[str, str], list[str]] = {}
    for r in safe_query(cn, KEYS_SQL):
        if r[4]:
            keys.setdefault((r[0], r[1]), []).append(r[3])
    fks: dict[tuple[str, str], list[str]] = {}
    for r in safe_query(cn, FK_SQL):
        fks.setdefault((r[1], r[2]), []).append(f"{r[3]} -> {r[4]}.{r[5]}.{r[6]}")
    counts = {(r[0], r[1]): r[2] for r in safe_query(cn, COUNT_SQL)}

    for (schema, table), columns in cols.items():
        lines = [f"# {db}.{schema}.{table}", "",
                 f"Rows (approx): {counts.get((schema, table), '?')}",
                 f"Primary key: {', '.join(keys.get((schema, table), [])) or 'none'}", "",
                 "## Columns", "", "| column | type | nullable |", "|---|---|---|"]
        for c in columns:
            width = "" if c[4] in (-1, None) else f"({c[4]})"
            lines.append(f"| {c[2]} | {c[3]}{width} | {'yes' if c[5] else 'no'} |")
        if rels := fks.get((schema, table)):
            lines += ["", "## Foreign keys", ""] + [f"- {x}" for x in rels]
        if sample_rows:
            try:
                names = [c[2] for c in columns][:12]
                sel = ", ".join(f"[{n}]" for n in names)
                rows = safe_query(cn, f"SELECT TOP {sample_rows} {sel} "
                                      f"FROM [{schema}].[{table}]")
                if rows:
                    lines += ["", "## Sample values", "", "| " + " | ".join(names) + " |",
                              "|" + "---|" * len(names)]
                    for row in rows:
                        cells = [str(v)[:40].replace("|", "/") if v is not None else ""
                                 for v in row]
                        lines.append("| " + " | ".join(cells) + " |")
            except Exception as exc:                      # permissions, weird types
                lines += ["", f"(sample unavailable: {exc.__class__.__name__})"]
        yield Document(
            id=f"nightarc:schema:{db}.{schema}.{table}",
            source="nightarc",
            origin=f"{db}.{schema}",
            title=f"NightArc schema: {schema}.{table}",
            # sample rows are real survey data and can carry grid refs / species codes
            tags=["schema", "sql-server", "nightarc", *classify_sensitivity("\n".join(lines))],
            meta={"database": db, "schema": schema, "table": table,
                  "rows": counts.get((schema, table))},
            text="\n".join(lines),
        )


def table_documents(cn, table: str, id_column: str, text_columns: list[str],
                    schema: str = "dbo", limit: int = 5000) -> Iterator[Document]:
    """Turn free-text survey columns into searchable documents."""
    sel = ", ".join(f"[{c}]" for c in [id_column, *text_columns])
    rows = safe_query(cn, f"SELECT TOP {limit} {sel} FROM [{schema}].[{table}]")
    for row in rows:
        rid, values = row[0], row[1:]
        body = "\n".join(f"{c}: {v}" for c, v in zip(text_columns, values) if v)
        if not body.strip():
            continue
        yield Document(
            id=f"nightarc:row:{schema}.{table}:{rid}",
            source="nightarc",
            origin=f"{schema}.{table}",
            title=f"{table} #{rid}",
            tags=["record", "nightarc", *classify_sensitivity(body)],
            meta={"table": table, "row_id": str(rid)},
            text=body,
        )


def run(store: Store, sample_rows: int = 3) -> dict:
    cn = connect()
    try:
        return store.bulk_upsert(schema_documents(cn, sample_rows=sample_rows))
    finally:
        cn.close()
