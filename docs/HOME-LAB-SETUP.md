# Home lab setup — Debian 13, 8 GB RAM

Ordered so each phase is independently testable and nothing later can silently
depend on something earlier that didn't actually work. Run
`python scripts/preflight.py` after every phase; it tells you the next fix.

Target box: Debian 13 (trixie), which ships Python 3.13 — new enough, nothing to
backport ([Debian 13 package versions](https://computingforgeeks.com/things-to-do-after-installing-debian-13-trixie/)).

## Phase 0 — before you touch anything

| Prerequisite | Why | Check |
|---|---|---|
| Debian 13, 2+ vCPU, 8 GB RAM | 8 GB is comfortable for retrieval. It is *not* comfortable if you also run SQL Server in Docker — that wants 2 GB minimum ([mssql image requirements](https://hub.docker.com/r/microsoft/mssql-server)) and will take more if you let it | `free -h` |
| 25 GB free disk | 15 GB of that is CPU torch plus the model cache if you want good embeddings. The corpus itself is small — 793 documents is ~30 MB | `df -h` |
| Outbound HTTPS to api.stackexchange.com, discourse.osgeo.org, api.github.com, discord.com | all four ingesters. If the lab is on a restricted VLAN this is the thing that breaks first | `preflight.py --group sources` |
| `sudo` | ODBC driver and Azure CLI install system-wide | |
| A workstation with a browser | device-code sign-in and the Discord/Foundry portals. The server never needs a GUI | |

Nothing here needs Azure or Discord yet. Phases 1–3 are fully self-contained.

## Phase 1 — base install

```bash
sudo apt install -y git
git clone <your-repo> /opt/ecomsp-brain && cd /opt/ecomsp-brain
bash scripts/bootstrap-debian.sh
```

This creates `.venv`, installs the core packages, copies `.env.example` to `.env`,
runs the test suite and then preflight.

One Debian 13 gotcha the script handles for you: trixie enforces PEP 668, so
`pip install` into the system Python is refused outright. Everything must live in the
virtualenv. If you see `error: externally-managed-environment`, you forgot
`. .venv/bin/activate`.

**Success looks like:** 14/14 tests pass, and preflight shows PASS for python,
virtualenv, sqlite fts5 and all four source APIs.

## Phase 2 — prove ingestion with no accounts at all

```bash
. .venv/bin/activate
python -m brain.cli ingest stackexchange --pages 2 --min-score 1
python -m brain.cli ingest discourse
python -m brain.cli ingest github
python -m brain.cli sources
python -m brain.cli search "geopackage locked by another process"
```

Anonymous quotas are enough for a test: 300 Stack Exchange requests/day, 60 GitHub
requests/hour. Register a key at [Stack Apps](https://stackapps.com) to get 10,000/day
and a [GitHub PAT](https://github.com/settings/tokens) (no scopes needed for public
repos) for 5,000/hour, then put both in `.env` before you do a full crawl.

**Success looks like:** several hundred documents, and search results whose URLs open
on real pages.

## Phase 3 — real embeddings

The default `hash` embedder exists so the pipeline runs with zero setup; retrieval with
it is noticeably poor and you should not judge the system on it.

```bash
bash scripts/bootstrap-debian.sh --embeddings   # CPU torch + bge-small, ~2.5 GB
sed -i 's/^BRAIN_EMBEDDER=.*/BRAIN_EMBEDDER=local/' .env
python -m brain.cli embed --embedder local      # backfills alongside existing vectors
```

On 8 GB CPU-only, `bge-small-en-v1.5` embeds roughly 40–80 chunks/second, so a few
thousand chunks is minutes, not hours. Vectors are keyed by model name, so switching is
non-destructive and keyword search keeps working throughout.

Keep it on `local` unless you have checked your client contracts — `openai`/`azure` send
client text off-site.

## Phase 4 — Discord

On [the Developer Portal](https://discord.com/developers/applications):

1. **New Application**, then **Bot** → **Reset Token**, copy it into `DISCORD_BOT_TOKEN`.
2. **Bot → Privileged Gateway Intents → MESSAGE CONTENT: on.** Without this every
   message body arrives empty and you will think the ingester is broken. Preflight cannot
   verify this over the API, so it always warns — check it by eye.
3. **OAuth2 → URL Generator**: scope `bot`, permissions **View Channels** and
   **Read Message History**. That is permissions integer `66560` if you build the URL
   by hand.
4. Get a server admin to authorise it. For a client server, ask in writing and keep the
   reply.
5. Put the server's id in `ALLOWED_GUILDS` (comma-separated). Leaving it empty means
   every server the bot joins gets indexed, which is not what you want.

```bash
python scripts/preflight.py --group discord   # confirms identity + which guilds it sees
python -m brain.cli ingest discord --days 30  # start small
```

A bot token, an admin invite and a guild whitelist are the whole compliance story here.
A user token would be self-botting, which breaches the
[Discord Terms of Service](https://discord.com/terms), and the Developer Policy
separately forbids using API data to train models — so this archive is for retrieval,
not fine-tuning.

## Phase 5 — NightArc / SQL Server Express

Two decisions: where SQL Server lives, and how you authenticate.

### 5a. ODBC driver on Debian

```bash
bash scripts/bootstrap-debian.sh --odbc
```

Driver 18.6 added official Debian 13 packages, so no repo-pinning tricks are needed
([ODBC 18.6 release notes](https://techcommunity.microsoft.com/blog/sqlserver/odbc-driver-18-6-for-sql-server-released/4478880),
[download page](https://learn.microsoft.com/en-us/sql/connect/odbc/download-odbc-driver-for-sql-server?view=sql-server-ver17)).
Two things bite people: apt 3.0 on trixie won't take the old `apt-key` path so the
keyring is dearmored to `/usr/share/keyrings`, and the install needs the EULA
pre-accepted — the script does both.

### 5b. Option A, a throwaway instance on the Debian box

SQL Server has no native Debian build, but the container runs fine on Docker Engine on
Debian ([supported platforms](https://learn.microsoft.com/en-us/sql/linux/sql-server-linux-setup?view=sql-server-ver17)).
This is the right choice for a first test: no client data anywhere near it.

```bash
sudo apt install -y docker.io && sudo usermod -aG docker "$USER"   # then re-login
export SA_PASSWORD='Str0ng-Lab-Pass!'
docker run -d --name mssql --restart unless-stopped \
  -e ACCEPT_EULA=Y -e MSSQL_SA_PASSWORD="$SA_PASSWORD" \
  -e MSSQL_PID=Express -e MSSQL_MEMORY_LIMIT_MB=1536 \
  -p 1433:1433 mcr.microsoft.com/mssql/server:2022-latest

docker exec -i mssql /opt/mssql-tools18/bin/sqlcmd -S localhost -U sa \
  -P "$SA_PASSWORD" -C < scripts/lab-nightarc-fixture.sql
```

`MSSQL_MEMORY_LIMIT_MB=1536` matters on an 8 GB box — SQL Server otherwise helps itself
to 80% of RAM ([memory best practices](https://learn.microsoft.com/en-us/sql/linux/configure/performance-best-practices-sql-server-memory?view=sql-server-ver17))
and your embedder will start swapping. `MSSQL_PID=Express` keeps it to Express limits so
the lab behaves like the real thing.

The fixture builds a plausible bat-acoustics schema — sites, detectors, deployments,
recordings, auto vs manual classified calls — so you can exercise the schema ingester
before going anywhere near a client database. Replace it with your real shape once
you've read it.

```env
NIGHTARC_SERVER=127.0.0.1,1433
NIGHTARC_DB=NightArc
NIGHTARC_USER=sa
NIGHTARC_PASSWORD=Str0ng-Lab-Pass!
ODBC_DRIVER=ODBC Driver 18 for SQL Server
```

### 5c. Option B, the real SQL Express on a Windows box

On the Windows host: SQL Server Configuration Manager → enable **TCP/IP** for the
instance, restart it, and either start the **SQL Server Browser** service (needed for
`HOST\SQLEXPRESS` name resolution over udp/1434) or set a fixed TCP port and use
`HOST,1433`. Open the port in Windows Firewall.

Use a dedicated read-only login rather than `sa` — the ingester only ever selects, and
the code blocks write statements anyway, but defence in depth is free:

```sql
CREATE LOGIN brain_ro WITH PASSWORD = 'change-me';
USE NightArc;
CREATE USER brain_ro FOR LOGIN brain_ro;
ALTER ROLE db_datareader ADD MEMBER brain_ro;   -- SELECT on all tables, nothing else
GRANT VIEW DEFINITION TO brain_ro;              -- needed for the schema cards
```

Avoid Windows integrated auth from Debian unless you want to configure Kerberos — a SQL
login is far less work for a lab. Driver 18 defaults to encryption on, so for a
self-signed certificate you'll need `TrustServerCertificate=yes`, which
`connection_string()` already sets.

```bash
python scripts/preflight.py --group nightarc   # tcp reach, login, table count
python -m brain.cli ingest nightarc
python -m brain.cli search "call sequence species" --source nightarc
```

## Phase 6 — the API and Claude

```bash
sed -i "s/^BRAIN_API_KEY=.*/BRAIN_API_KEY=$(openssl rand -hex 24)/" .env
python -m brain.cli serve --host 0.0.0.0 --port 8077
curl -s -H "x-api-key: $KEY" "http://localhost:8077/search?q=geopackage+lock" | head
```

Claude runs on your workstation, and the MCP server here speaks stdio, so it has to be
launched locally. Two working shapes:

**Claude Code on the Debian box over SSH** — simplest, everything stays server-side:

```bash
claude mcp add ecomsp-brain -s user -- /opt/ecomsp-brain/.venv/bin/python -m brain.mcp_server
```

**Claude Desktop on your workstation, server on Debian** — wrap the launch in SSH so
stdio is tunnelled:

```json
{
  "mcpServers": {
    "ecomsp-brain": {
      "command": "ssh",
      "args": ["-T", "brain@lab.internal",
               "cd /opt/ecomsp-brain && .venv/bin/python -m brain.mcp_server"]
    }
  }
}
```

Use key-based SSH auth; a password prompt will just look like a hung MCP server.

For ChatGPT or Copilot Studio, point a custom action/connector at
`/openapi-for-foundry` instead — same archive, no stdio involved.

## Phase 7 — Foundry

You have a tenant already, so what's left is project-level.

**In the portal**

1. A **Foundry resource + project**, with a custom subdomain — token auth requires it
   ([authentication](https://learn.microsoft.com/en-us/azure/foundry/concepts/authentication-authorization-foundry)).
2. **Deploy a model** in that project. The shipped specs ask for `gpt-5-mini`; if that
   isn't available in your region, change `model:` in `agents/*.yaml` to whatever you
   deployed. Also confirm your region and model support OpenAPI tools.
3. **Roles on the Foundry resource**: **Foundry User** to create and run agents, and
   **Foundry Project Manager** as well, because creating the API-key project connection
   needs it ([OpenAPI tool docs](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/tools/openapi),
   [RBAC reference](https://learn.microsoft.com/en-us/azure/foundry/concepts/rbac-foundry)).
   Assigning them needs Owner or User Access Administrator at the scope.
4. **Manage → Project details → Connected resources → new connection, type Custom
   keys.** Key = `x-api-key` (exactly — it must match the security scheme `name` in the
   spec, which is why `/openapi-for-foundry` emits it lowercase), Value = your
   `BRAIN_API_KEY`. Name it `brain-api-key` to match the agent YAML, or update
   `connection_id` in the specs. The SDK accepts the connection name or the full
   `/subscriptions/.../connections/<name>` path.

**Make the API reachable from Azure.** Foundry calls your OpenAPI endpoint from its own
network, so `localhost` cannot work. For a lab, a quick Cloudflare tunnel needs no
account or domain ([Cloudflare Tunnel docs](https://developers.cloudflare.com/pages/how-to/preview-with-cloudflare-tunnel/)):

```bash
curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 \
  -o /usr/local/bin/cloudflared && chmod +x /usr/local/bin/cloudflared
cloudflared tunnel --url http://localhost:8077        # prints an https://....trycloudflare.com
```

Put that hostname in `BRAIN_PUBLIC_URL`. Quick-tunnel URLs change on every restart, and
the agent specs bake the spec URL in, so use a named tunnel with your own hostname once
you're past experimenting.

**Then apply the agents**

```bash
bash scripts/bootstrap-debian.sh --azure
az login --use-device-code          # headless-friendly: code + URL, sign in on your laptop
export FOUNDRY_PROJECT_ENDPOINT="https://<resource>.services.ai.azure.com/api/projects/<project>"
python -m brain.cli foundry validate agents/     # offline sanity check first
python scripts/preflight.py --group foundry      # confirms login, project, public URL
python -m brain.cli foundry apply agents/
python -m brain.cli foundry ask --agent qgis-troubleshooter "GeoPackage locks on a network share"
```

If `ask` returns an answer with citations from your own archive, the whole chain works.
If the agent answers but never cites the brain, the tool call is failing — almost always
the tunnel being down or the connection Key not being exactly `x-api-key`.

## Phase 8 — leave it running

```bash
sudo useradd -r -s /usr/sbin/nologin brain && sudo chown -R brain:brain /opt/ecomsp-brain
sudo cp scripts/systemd/* /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ecomsp-brain-api.service ecomsp-brain-refresh.timer
systemctl list-timers ecomsp-brain-refresh
journalctl -u ecomsp-brain-refresh -f
```

The refresh runs at 02:30 with a randomised delay, is `Persistent=true` so a powered-off
lab catches up, and takes a dated SQLite backup keeping the last fortnight. Re-ingestion
is incremental — unchanged documents are skipped by content hash and Discord resumes from
stored cursors — so a nightly run is cheap.

Bind the API to `0.0.0.0` only on a trusted VLAN. It is a read-only interface protected
by one shared key; treat it as internal.

## Rough order of effort

| Phase | Time | Blocked on |
|---|---|---|
| 1–2 base + public sources | 20 min | nothing |
| 3 embeddings | 30 min, mostly download | nothing |
| 4 Discord | 20 min | server admin consent |
| 5 SQL, container | 20 min | nothing |
| 5 SQL, real instance | 30 min | Windows host access |
| 6 API + Claude | 15 min | nothing |
| 7 Foundry | 1–2 hrs | role assignments, model quota |
| 8 systemd | 15 min | nothing |

Phases 1–6 give you a working private research archive across all your AI tools. Phase 7
is only needed for the Foundry agents, so don't let it block the rest.
