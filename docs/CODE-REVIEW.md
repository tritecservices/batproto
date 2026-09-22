# Code review — September 2026

Overall: a well-built base. Storage, hybrid search, the Foundry factory and the
compliance guards are thought through, and the docs are unusually honest. The problems
below are mostly edge cases that would lose data or leak it quietly.

## Fixed in this pass

| # | Severity | Where | Problem | Fix |
|---|---|---|---|---|
| 1 | High | `api.py` `auth()` | API was **open to anyone** when `BRAIN_API_KEY` was blank, and `.env.example` shipped `change-me` as a working key. One forgotten `.env` on a tunnelled box = every Discord thread and NAS file public. | Fails closed (503) on blank/placeholder keys; `hmac.compare_digest`; explicit `BRAIN_ALLOW_NO_KEY=1` for local dev. `/health` no longer lists sources without auth. |
| 2 | High | `discord_source.py` | Channel archives are one document per day. On a resumed run the bucket for "today" only holds messages *after* the cursor, and the upsert **replaced the earlier messages of that day**. | Appends to the stored day document when resuming. |
| 3 | High | `discord_source.py`, `nightarc_sql.py` | Sensitivity tagging (protected species, grid refs) only ran on NAS files. Discord threads and NightArc sample rows carry the same data. | Classifier moved to `brain/sensitivity.py` and applied to all three. |
| 4 | High | `search.py` / `api.py` | Search hits didn't include tags, so no agent or UI could tell a result was sensitive. | Hits now carry `tags`; `search()` accepts `exclude_tags`. |
| 5 | Med | `discourse.py` | Newer Discourse returns tags as objects. They were stored as dicts, which crashes anything that reads tags. **Your current `brain.db` already contains these.** | Ingester stores names; search normalises old rows. |
| 6 | Med | `discord_source.py` | Archived threads under normal text channels were skipped — most solved threads are archived. | Walks `archived_threads()` too. |
| 7 | Med | `refresh.sh`, `refresh.ps1` | Nightly refresh never ingested `files` (the NAS), the source the README calls the most valuable. | Added. |
| 8 | Med | `files.py` | `BRAIN_FILE_ROOTS` was split on `:`, which cuts `C:\Survey` in half on Windows. | Splits on comma or `os.pathsep`. |
| 9 | Low | `store.py` | Docstring said "safe to put on a NAS"; `.env.example` correctly says never. | Docstring corrected. |

New: `/public/search` for the website (off by default), CORS allowlist, rate limit.
Regression tests added for 1, 3, 4, 5, 8 and the public endpoint.

## Still open — worth doing next

1. **Embeddings are keyed by chunk only, not (chunk, model).** `embeddings.chunk_id` is
   the primary key, so switching `BRAIN_EMBEDDER` *overwrites* vectors rather than
   "backfilling alongside" as the README claims. Needs a schema migration to
   `PRIMARY KEY (chunk_id, model)`.
2. **Your DB is embedded with `hash-1024`** — the weak test embedder (default in
   `embed.py` is `hash` even though `.env.example` says `local`). Run
   `python -m brain.cli embed --embedder local` on Debian. Search relevance will jump.
3. **`chunk_text` doesn't do what its docstring says.** It doesn't protect fenced code
   blocks, and a single huge paragraph (logs, tracebacks) becomes one oversized chunk.
4. **Deleted sources never leave the store.** A file removed from the NAS, or a Discord
   message deleted by its author, stays searchable forever. Discord's policy expects
   deletions to be honoured — add a tombstone pass.
5. **Forum threads are re-downloaded in full on every run** (no cursor). Fine now, slow
   and rate-limit-prone as the server grows.
6. **Agent specs point `spec_url` at `localhost:8077`**, and the served spec's
   `servers` URL comes from whichever machine serves it. Run `foundry apply` with
   `BRAIN_PUBLIC_URL` set to the tunnel hostname or Foundry will call localhost.
7. **The API unit binds `0.0.0.0`.** With cloudflared on the same host, bind
   `127.0.0.1` so the only way in is the tunnel.
8. **No QGIS manual ingester.** You mention "QGIS manuals": clone
   `qgis/QGIS-Documentation` and run `ingest files --root QGIS-Documentation/docs`
   (it reads `.rst`), but the URLs will be `file://`. A small ingester mapping paths to
   `docs.qgis.org` URLs would be better. The docs are CC BY-SA — keep attribution.
9. **Scanned ecology reports are skipped** (`pypdf` finds no text, and `pypdf` isn't in
   `requirements.txt`). Add `pypdf`, and OCR scanned reports (Azure Document
   Intelligence fits your Azure setup).
10. `discourse.py` / `github_issues.py` convert UTC timestamps with `time.mktime`
    (local time) — dates are off by your UTC offset. Use `calendar.timegm`.

## Discord: what the bot can and can't ingest

The bot only sees servers whose admins invite it. For community servers you just read
(e.g. public GIS servers), you can't add it yourself — ask the admins, and don't use a
user-token exporter (a ToS breach that can get your account banned). Discord's
Developer Policy also restricts using message content for AI training; retrieval with
attribution, as here, is the defensible use, and you should still check with each
server's admins before archiving it.
