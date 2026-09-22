# Start here: three machines, two networks

This is the ordered prerequisites list for the whole thing. Read the corrections first —
three parts of the plan as described won't work, and two of them would cost you an
evening before you found out why.

---

## Three corrections before you buy into the plan

### 1. Netlify cannot host the brain

Netlify Functions run TypeScript, JavaScript and Go. Not Python. Netlify's own support
staff put it plainly: "Currently, you can deploy functions built with TypeScript,
JavaScript, and Go" ([Netlify support](https://answers.netlify.com/t/python-lambda-functions/3423)),
and a related thread spells out the consequence — Python exists on your build machine
but "once the function is deployed, Python is no longer available"
([Netlify support](https://answers.netlify.com/t/spawn-python3-enoent-when-running-a-python-script-in-nextjs-api/91823)).

The brain is FastAPI plus a 29 MB SQLite file with a vector index. That is not a
serverless workload regardless of language — it wants a warm process and a local disk.

**So:** Netlify hosts the *front end* — a search UI, a client-facing demo, the marketing
page for whatever you productise. The brain stays on Debian and is reached over HTTPS
through a Cloudflare tunnel. That split is a feature, not a compromise: the UI is
public and cheap, the data never leaves your LAN.

Also worth knowing before you design around it: Netlify's free plan is now
credit-metered, not quota-based — 300 credits a month, with production deploys at 15
credits each, bandwidth at 20 credits per GB and web requests at 2 credits per 10,000
([Netlify pricing](https://www.netlify.com/pricing/)). Which leads to a specific
design decision: **do not** proxy your API traffic through Netlify rewrites. It works
([Netlify rewrites and proxies](https://docs.netlify.com/manage/routing/redirects/rewrites-proxies/)),
but every API call then costs you requests *and* bandwidth credits for data that could
have gone browser-to-tunnel directly.

### 2. "Tenant" doesn't run anywhere you control

A Microsoft 365 / Entra tenant is cloud-side. It isn't hosted on Netlify or on the
laptop — the laptop is just where you *administer* it. Same for Foundry: the agents run
in Azure, and your laptop holds the credentials and the agent definitions. Useful
because it means neither is affected by your home network topology, except that Foundry
must be able to *reach* your brain (phase 6).

### 3. The Mac mini cannot join the mesh network

This is the one that would have wasted your time. The clean way to link machines across
two routers is Tailscale — but the current client requires macOS Monterey 12.0 or later
([Tailscale macOS docs](https://tailscale.com/docs/install/mac)), and your Late-2012
mini stops at Catalina 10.15. Tailscale's changelog dates the cutoff precisely: v1.60.0
was the last build that ran on 10.15 ([Tailscale changelog](https://tailscale.com/changelog)).

That's now the third thing Catalina has blocked, after Homebrew 7.0.0 dropping 10.15 and
PyTorch dropping macOS x86_64. The mini is a NAS and nothing more.

**The fix is tidy though:** Debian becomes a Tailscale *subnet router* and advertises the
mini's LAN. A subnet router "lets devices connect to your tailnet without installing
the Tailscale client" ([Tailscale subnet routers](https://tailscale.com/docs/features/subnet-routers)).
So the mini gets reachable from your laptop across both networks without ever running
Tailscale itself.

---

## The resulting topology

```
        NETWORK A (router 1)                    NETWORK B (router 2)
   ┌───────────────────────────────┐      ┌──────────────────────────┐
   │  Debian 13 server   8 GB      │      │   Laptop      24 GB      │
   │  ├ brain API :8077            │      │   ├ code + git           │
   │  ├ data/brain.db  (LOCAL)     │      │   ├ Netlify CLI deploy   │
   │  ├ cloudflared tunnel ────────┼──────┼─▶ ├ az CLI / Foundry     │
   │  ├ tailscale SUBNET ROUTER    │◀─────┼── ├ SQL Express or Docker│
   │  └ nightly refresh timer      │ tail │   └ tailscale client     │
   │            │ SMB (ro)         │ scale└──────────────────────────┘
   │  ┌─────────▼─────────┐        │
   │  │ Mac mini  8 GB    │        │         ── keep these two ──
   │  │ NAS, SMB 3.0 only │        │         together on ONE subnet:
   │  │ no tailscale       │       │         they talk constantly
   │  └───────────────────┘        │
   └───────────────────────────────┘
                 │
                 ▼  HTTPS via tunnel
        Foundry agents  +  Netlify front end (public)
```

**The one rule that drives this layout:** Debian and the mini must sit on the *same*
subnet, behind the *same* router. They talk over SMB constantly during ingest. The
laptop can be anywhere, because Tailscale makes location irrelevant for it.

### Why two routers is the risky part

Two routers in series creates double NAT, and the failure is directional: devices behind
the second router can reach the first router's network, but devices on the first network
cannot initiate connections back
([Speedefy](https://www.speedefy.com/article/fix-double-nat/)). So if the mini is on
network A and Debian on network B, ingest works — until you reboot and the mount comes
up in the wrong direction, or you try to reach the API from network A and can't.

Pick one of these, in order of preference:

1. **Put router 2 in access-point / bridge mode.** One subnet, no NAT layer, everything
   sees everything. Disables the second router's NAT and DHCP
   ([Edovia](https://help.edovia.com/en/screens-connect-5/troubleshooting/double-nat)).
   Do this if the second router exists only for wifi coverage.
2. **Keep both routing, put Debian + mini on the same one, use Tailscale for the laptop.**
   This is what the diagram shows, and it's correct if the two networks are deliberately
   separate — e.g. you're keeping an out-of-support Catalina machine off your main LAN,
   which is a legitimate reason.

Deliberately segmenting that mini is defensible. Just be intentional about it rather
than discovering the split by accident.

---

## Prerequisites by machine

### Laptop — 24 GB — the control plane

Gets the heavy work because it has the RAM and doesn't need to be always-on.

| Need | Why | Check |
|---|---|---|
| Python 3.11–3.13 | project code; 3.13 matches Debian 13 | `python --version` |
| Git | source control | `git --version` |
| **Node.js 18.14+** | Netlify CLI floor ([Netlify CLI docs](https://docs.netlify.com/api-and-cli-guides/cli-guides/get-started-with-cli/)); use **20.12.2+** if you want `netlify database` ([CLI reference](https://docs.netlify.com/build/data-and-storage/netlify-database/cli/)) | `node --version` |
| Netlify CLI | deploys | `npm i -g netlify-cli` |
| Azure CLI | Foundry auth | `az version` |
| Docker Desktop **or** SQL Server Express | a NightArc instance to test against | `docker --version` |
| ODBC Driver 18 for SQL Server | `pyodbc` needs it | driver list in preflight |
| Tailscale client | reach the other network | `tailscale status` |
| 25 GB free disk | embeddings model + Docker image + DB | — |

Do the embedding runs here. `BRAIN_EMBEDDER=local` loads bge-small-en-v1.5, and 24 GB
handles that comfortably where 8 GB is tight alongside an API process.

### Debian 13 server — 8 GB — the always-on brain

| Need | Why |
|---|---|
| `python3-venv`, `python3-pip` | Debian 13 enforces PEP 668, so a venv is mandatory, not optional |
| `cifs-utils` | mount the NAS |
| `sqlite3` CLI | backup verification in `promote-db.sh` |
| `cloudflared` | public HTTPS for Foundry — install from Cloudflare's own apt repo so `apt upgrade` keeps it current ([Cloudflare packages](https://pkg.cloudflare.com/index.html)) |
| Tailscale | subnet router; the installer detects trixie correctly ([install script](https://tailscale.com/docs/install/mac)) |
| Static IP or DHCP reservation | the tunnel and the mount both hard-code it |

`scripts/bootstrap-debian.sh` does the apt and venv work in one pass.

**8 GB is the real constraint.** Do not also run SQL Server here — on 8 GB it takes
about 80% of RAM unless capped, which is why `MSSQL_MEMORY_LIMIT_MB=1536` came up
earlier. Keep SQL Server on the laptop.

### Mac mini — 8 GB — NAS only

Already done: SMB file sharing enabled. Remaining prerequisites are small but matter.

- A dedicated share for survey material, and a **separate** one for backups, so Debian
  can mount the first read-only and only the second read-write.
- A dedicated local account for Debian to authenticate as, listed under File Sharing →
  Options → "Windows File Sharing" ([Apple: share Mac files with Windows users](https://support.apple.com/en-gb/guide/mac-help/mchlp1657/mac)).
- Energy Saver: disable sleep. A sleeping NAS mid-ingest looks exactly like a
  permissions failure.
- A DHCP reservation for its IP.
- SMB 3.0 is the ceiling on Catalina — pin `vers=3.0` on the Debian side.
- Nothing else. No Python, no Docker, no Tailscale. It is a disk with an ethernet port.

---

## The order to do it in

Each phase ends with a command that proves it worked. Don't move on from a red one —
every phase depends on the one before, and a failure diagnosed three phases later
costs far more than the two minutes of checking.

### Phase 0 — decide the network layout (15 min)

Before installing anything, settle the double-NAT question above. Note each machine's
IP and which router it's on. Everything downstream hard-codes these.

```bash
ip route | grep default        # Debian: which gateway am I behind
```

### Phase 1 — code on the laptop (30 min)

```bash
git clone <your repo> && cd ecomsp-brain
python -m venv .venv && . .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
python tests/test_pipeline.py                   # expect: all passed (20 tests)
python scripts/preflight.py --group platform --group packages
```

Everything from here runs offline and keyless, so you can validate the whole pipeline
before touching a cloud account.

### Phase 2 — first real corpus, still laptop-only (30 min)

```bash
python -m brain.cli ingest se --pages 3 --min-score 5
python -m brain.cli ingest discourse
python -m brain.cli search "qgis crashes opening large geopackage"
```

If search returns sensible hits, the store, the chunker, the embedder and the fusion
ranker are all working. That's the core proven before any infrastructure exists.

### Phase 3 — Debian as the brain host (1 hr)

```bash
./scripts/bootstrap-debian.sh --embeddings
python3 scripts/preflight.py --group platform --group packages --group brain
./scripts/promote-db.sh --pull you@laptop:/path/to/ecomsp-brain/data/brain.db
sudo systemctl enable --now ecomsp-brain-api
curl -s localhost:8077/health
```

Move the database rather than re-ingesting — `promote-db.sh` snapshots with `.backup`,
verifies with `integrity_check`, then copies, so you can do this against a running API.

### Phase 4 — the NAS (45 min)

Full detail in `NAS-INGEST.md`. Short version:

```bash
sudo apt install cifs-utils
# /etc/fstab line with: ro,vers=3.0,nofail,_netdev,credentials=/etc/cifs-nas.cred
sudo mount -a && findmnt /mnt/nas/surveys
echo 'BRAIN_FILE_ROOTS=/mnt/nas/surveys' >> .env
python3 scripts/preflight.py --group files
python3 -m brain.cli ingest files
```

`nofail,_netdev` is not optional. Without them a mini that's asleep or off stops Debian
from booting, and you're plugging in a monitor to fix it.

**Never point `BRAIN_DB` at the share.** Preflight fails hard if you do.

### Phase 5 — link the two networks (30 min)

On Debian, as subnet router — substitute the real subnet:

```bash
curl -fsSL https://tailscale.com/install.sh | sh
echo 'net.ipv4.ip_forward = 1' | sudo tee -a /etc/sysctl.d/99-tailscale.conf
sudo sysctl -p /etc/sysctl.d/99-tailscale.conf
sudo tailscale up --advertise-routes=192.168.1.0/24
```

Then approve the advertised route in the Tailscale admin console — it does not take
effect until you do, and this is the step people miss. Install the client on the laptop,
and confirm from the laptop:

```bash
tailscale status
curl -s http://<debian-tailscale-ip>:8077/health
smbclient -L //<mini-lan-ip> -U youruser     # the mini, via the subnet route
```

If the second command works from the other network, the topology is solved.

### Phase 6 — tenant and Foundry (1–2 hrs, laptop)

Roles are the usual sticking point, and there are two distinct layers:

- To **create** the Foundry resource: a role that permits it, such as **Foundry Account
  Owner** at subscription scope ([Foundry quickstart](https://learn.microsoft.com/en-us/azure/foundry/tutorials/quickstart-create-foundry-resources)).
- To **assign** roles to anyone: **Owner** or **User Access Administrator**
  ([Foundry auth](https://learn.microsoft.com/en-us/azure/foundry/concepts/authentication-authorization-foundry)).
- To then use it day to day: **Foundry User**, plus **Foundry Project Manager** to create
  the API-key connection the agents need.

```bash
az login --use-device-code
# .env: FOUNDRY_PROJECT_ENDPOINT=https://<resource>.services.ai.azure.com/api/projects/<project>
python -m brain.cli foundry validate agents/
python scripts/preflight.py --group foundry
```

Remember the bug fixed earlier: the connection Key must be lowercase `x-api-key`, exactly
matching the OpenAPI security scheme name. A mismatch gives you a silent 401 inside the
agent with no useful error.

### Phase 7 — expose the brain, then deploy the front end (1 hr)

On Debian:

```bash
cloudflared tunnel --url http://localhost:8077
```

Take the generated HTTPS URL into `.env` as `BRAIN_PUBLIC_URL`, restart the API so the
OpenAPI document advertises the right server, then point the Foundry tool at
`<public-url>/openapi-for-foundry`. Quick tunnels get a new hostname each restart, so
once it works, switch to a named tunnel with your own domain.

Then the Netlify piece — a static front end that calls `BRAIN_PUBLIC_URL` directly:

```bash
netlify login && netlify init && netlify deploy --prod
```

Set the API base URL as a Netlify environment variable, not in committed source. And
put the browser-facing key behind a small Netlify Function if the UI is ever public —
a key in front-end JavaScript is a key you've published.

---

## Time and sequencing

| Phase | Where | Time | Blocks |
|---|---|---|---|
| 0 network plan | — | 15 min | everything |
| 1 code | laptop | 30 min | all |
| 2 corpus | laptop | 30 min | 3 |
| 3 brain host | Debian | 1 hr | 4, 7 |
| 4 NAS | Debian + mini | 45 min | — |
| 5 tailscale | Debian + laptop | 30 min | — |
| 6 tenant/Foundry | laptop + Azure | 1–2 hrs | 7 |
| 7 tunnel + Netlify | both | 1 hr | — |

Roughly a full day, and phases 1–2 alone give you a working searchable archive. If you
only get that far, you already have the thing with commercial value — the rest is
distribution.

## One end-to-end check

```bash
python scripts/preflight.py            # 39 checks, all groups
python scripts/preflight.py --json | jq '[.[] | select(.status=="FAIL")]'
```

WARNs are survivable for a first test. FAILs are not, and each one carries the fix with
it.
