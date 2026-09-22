# Ecology MSP Brain

One archive of everything your team reads, queryable by every AI you pay for, plus a
factory that turns it into Microsoft Foundry agents.

Sources in: Discord threads (authorised bot), GIS Stack Exchange, the OSGeo QGIS
forums, QGIS GitHub issues, and the NightArc SQL Server Express schema.
Consumers out: Claude and any MCP client, Microsoft Foundry agents, ChatGPT or Copilot
via the HTTP API, and your own products.

```
 Discord ──┐
 GIS SE  ──┤                  ┌── MCP stdio ────────── Claude Desktop / Claude Code
 OSGeo   ──┼─► SQLite store ──┼── HTTP + OpenAPI ────── Foundry agents, Copilot Studio,
 GitHub  ──┤   FTS5 + vectors │                         ChatGPT custom GPT actions
 NightArc─┘                   └── CLI ───────────────── you, grepping your own history
```

## Start here

Setting this up across more than one machine? Read **`docs/START-HERE.md`** first — it
has the ordered prerequisites for a laptop + Debian server + NAS across two networks,
and the three assumptions that don't survive contact with reality (Netlify can't run
Python, a 2012 Mac mini can't run Tailscale, and two routers break SMB in one direction).

## Install

On Debian, follow **[docs/HOME-LAB-SETUP.md](docs/HOME-LAB-SETUP.md)** — it is the full
phase-by-phase runbook with every prerequisite, including SQL Server and Foundry.
Running it across two machines: **[docs/TWO-NODE-LAB.md](docs/TWO-NODE-LAB.md)**.

```bash
bash scripts/bootstrap-debian.sh        # --odbc --embeddings --azure --all
python scripts/preflight.py             # 30+ checks, tells you the next fix
```

Manually, anywhere else:

```bash
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                              # then fill it in
```

## Ingest

```bash
python -m brain.cli ingest stackexchange --pages 5 --min-score 1
python -m brain.cli ingest discourse
python -m brain.cli ingest github
python -m brain.cli ingest discord --days 365     # needs DISCORD_BOT_TOKEN + ALLOWED_GUILDS
python -m brain.cli ingest nightarc               # needs pyodbc + the SQL Express instance
python -m brain.cli ingest files                  # your NAS / file share - see docs/NAS-INGEST.md
python -m brain.cli ingest all                    # everything, then embed
```

Re-running is cheap: documents are skipped unless their text hash changed, Discord
channels resume from a stored cursor, and the file share skips anything whose mtime and
size are unchanged — so a bulk copy or restore that moves every timestamp re-reads but
rewrites nothing, and costs no re-embedding. Nightly refresh with rotating backups is wired up
for both platforms: `scripts/systemd/` on Debian, `scripts/refresh.ps1` for Windows Task
Scheduler.

## Query

```bash
python -m brain.cli search "qgis crashes opening large geopackage"
python -m brain.cli search "call sequence species column" --source nightarc
python -m brain.cli search "layer datasource OLDSERVER" --source files
python -m brain.cli sources
```

## Connect the AIs

**Claude** — MCP, read-only, four tools (`search_brain`, `get_document`, `list_sources`,
`nightarc_schema`):

```bash
claude mcp add ecomsp-brain -s user -e BRAIN_DB=C:/brain/data/brain.db -- python -m brain.mcp_server
```

**Everything else** — the HTTP API:

```bash
python -m brain.cli serve            # http://localhost:8077, X-API-Key header
```

`/search`, `/document/{id}`, `/sources`, and `/openapi-for-foundry` which serves an
OpenAPI 3.0.3 document already shaped the way Foundry wants it (explicit `servers`,
an `apiKeyHeader` security scheme, no duplicate key parameter). Point a ChatGPT custom
GPT action or a Copilot Studio connector at the same spec.

To reach Foundry the API must be publicly resolvable — put it behind Azure Container
Apps, App Service, or a tunnel, and set `BRAIN_PUBLIC_URL`.

## Foundry agents

Agents are YAML in `agents/`. Applying a changed spec creates a new agent *version*,
so prompt changes are rollback-able.

```bash
python -m brain.cli foundry validate agents/     # offline, no Azure calls
az login
export FOUNDRY_PROJECT_ENDPOINT="https://<resource>.services.ai.azure.com/api/projects/<project>"
python -m brain.cli foundry apply agents/
python -m brain.cli foundry list
python -m brain.cli foundry ask --agent qgis-troubleshooter "GeoPackage locks on a network share"
```

Shipped agents:

| Agent | Job |
|---|---|
| `qgis-troubleshooter` | First-line GIS support grounded in your own threads first, public answers second |
| `bat-acoustics-pipeline` | Anabat / Kaleidoscope → NightArc → GIS, reads the real SQL schema before writing SQL |
| `product-scout` | Mines the archive for recurring pain and writes build specs for sellable tools |

Supported tool types in a spec: `openapi`, `mcp`, `web_search`, `code_interpreter`,
`file_search`. For API-key auth, store the key in a Foundry project connection of type Custom keys
whose Key is exactly `x-api-key` — it must match the security scheme `name` in the spec
— and reference the connection as `connection_id`.

## Embeddings

`BRAIN_EMBEDDER` picks the model. `hash` needs nothing and exists so the pipeline runs
offline; retrieval is noticeably weaker. `local` (sentence-transformers, `bge-small`)
keeps client text on your hardware and is the sensible default for an MSP. `openai` /
`azure` are strongest but send text to the provider — check your client contracts first.

Switching embedder is safe: embeddings are keyed by model name, so run
`python -m brain.cli embed --embedder local` and the new vectors backfill alongside.
Keyword search (FTS5) works with no embeddings at all.

## Tests

```bash
python tests/test_pipeline.py
```

Covers chunking, idempotent upserts, orphan-free reindexing, hybrid ranking, the FTS
query sanitiser, source filtering, API auth, the Foundry OpenAPI shape, real Foundry
payload construction, agent spec validity, and the SQL write guard.

## Compliance, deliberately built in

These are not footnotes — they are enforced in code, and they are what keeps a
resale business out of trouble.

- **Discord**: bot tokens only, admin-invited, guild whitelist. Self-bots and message
  extraction outside the API breach the
  [Discord ToS](https://discord.com/terms), and the
  [Developer Policy](https://discord-media.com/en/news/discord-developer-policy-2.html)
  specifically forbids using API data to train models. This store is for retrieval;
  do not fine-tune on it.
- **Stack Exchange**: official [API v2.3](https://api.stackexchange.com/docs), backoff
  honoured, every document keeps its URL and author because the content is CC BY-SA —
  attribution and share-alike follow it into anything you republish.
- **OSGeo Discourse**: documented `.json` endpoints, rate limited politely.
- **NightArc**: read-only connection plus a statement guard that blocks writes. Survey
  data can end up in planning and licensing decisions; corrupting it has consequences
  beyond IT.
- **Your own files**: the share is mounted read-only and only ever read. Material
  mentioning protected species, roosts, licences or British grid references is tagged
  `sensitive` / `has-grid-ref` so agents can be instructed to cite the source without
  reproducing a location. Publishing a roost location is a real-world harm, not a
  policy technicality.
- **Productising**: use the archive to learn *what* hurts, then build originals against
  published APIs. Lifting third-party workings or client-specific material into
  something you sell is the risk, not the retrieval. `product-scout` is instructed to
  flag exactly that, including the GPL implications of QGIS plugin distribution.

## Layout

```
brain/
  store.py              SQLite + FTS5 + vectors, thread-safe, content-hash dedupe
  embed.py              hash / local / openai / azure embedders
  search.py             BM25 + cosine fused with Reciprocal Rank Fusion
  api.py                FastAPI + Foundry-shaped OpenAPI
  mcp_server.py         MCP server for Claude (mcp 1.x and 2.x)
  foundry.py            declarative agent factory
  cli.py                one entry point
  ingest/
    discord_source.py   authorised bot, forum threads as whole documents
    stackexchange.py    GIS SE with answers, runtime-built API filter
    discourse.py        OSGeo QGIS categories, auto-discovered
    github_issues.py    closed issues, where the fixes live
    nightarc_sql.py     schema cards + optional free-text rows
    files.py            NAS/file share: QGIS projects, GUANO WAVs, CSV exports, Office
agents/                 YAML agent specs
scripts/
  preflight.py          44 environment checks, grouped, --json for monitoring
  bench.py              embed/search/write throughput, for assigning node roles
  bootstrap-debian.sh   one-shot Debian 13 install
  lab-nightarc-fixture.sql  fake bat-acoustics database for safe testing
  promote-db.sh         verified snapshot copy between nodes, or rotated --to a NAS
  refresh.sh / .ps1     nightly refresh, Debian and Windows
  systemd/              api service + refresh timer
docs/
  HOME-LAB-SETUP.md     full prerequisites runbook
  TWO-NODE-LAB.md       splitting across two machines
  NAS-INGEST.md         mounting the share safely + what each file type contributes
  START-HERE.md         multi-machine prerequisites, ordered, with checkpoints
tests/                  offline test suite
```
