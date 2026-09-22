# Two-node lab: Debian server + 2012 Mac mini

Short version: **wipe macOS and put Debian 13 on the mini**, then use it as the SQL
Server host and staging node. Keeping Catalina costs you more than it gives.

## Why not keep macOS

A Late-2012 Mac mini tops out at macOS Catalina 10.15.7
([Apple compatibility list](https://discussions.apple.com/thread/254871719)), and that
ceiling now breaks the toolchain in two independent places:

- **Homebrew 7.0.0, released September 2026, no longer runs on macOS 10.15 or earlier,
  and Intel Macs dropped to Tier 3** ([Homebrew 7.0.0 release notes](https://brew.sh/2026/09/13/homebrew-7.0.0/)).
  So the normal way to get a current Python, `unixodbc` and `sqlite` on that box is gone.
- **PyTorch dropped macOS x86_64 binaries after 2.2.2**, and 2.2.2 only supports Python
  3.10–3.12 ([deprecation summary](https://docs.langflow.org/macos-support),
  [the version timeline](https://fossies.org/linux/langflow/docs/TORCH_MACOS_AMD64_PYTHON313_INVESTIGATION.md)).
  Local embeddings on Intel macOS means an old Python plus an old torch, or building from
  source. On Debian it is one `pip install`.

Catalina also stopped getting security patches in 2022, which matters for a box holding
client support history.

[OpenCore Legacy Patcher](https://dortania.github.io/OpenCore-Legacy-Patcher/MODELS.html)
would get it to a newer macOS, and it works well, but then you are maintaining a patched
OS purely to run an ML stack that upstream has stopped shipping for your CPU. For a
headless lab server that is effort spent in the wrong direction.

The hardware itself is genuinely useful: Ivy Bridge i5/i7, gigabit ethernet, about
11–15 W at idle, happy to run for years. It just wants Linux.

## Installing Debian 13 on it

Mostly unremarkable — it is a normal 64-bit EFI x86 machine
([Debian wiki on Intel Macs](https://wiki.debian.org/MacMiniIntel)). Four things to know:

1. **Update the Mac firmware from macOS first**, while you still have it. Old firmware
   has flaky EFI boot behaviour.
2. **Use the ethernet port.** The 2012 mini's Broadcom wifi needs non-free firmware that
   the installer may not have offline, which turns a 20-minute install into an evening
   ([Broadcom on Apple hardware](https://wiki.debian.org/InstallingDebianOn/Apple/MacBookPro/10-1)).
   It is a server on a lab VLAN — wire it.
3. Hold Option at boot to pick the USB installer. Install in EFI mode, wipe the whole
   disk, skip the desktop task and select SSH server.
4. Fans run dumb under Linux on Apple hardware. Install `macfanctld` or `mbpfan` if it
   sits at an audible idle, and keep an eye on `sensors`.

Then the same bootstrap as the other box:

```bash
git clone <your-repo> /opt/ecomsp-brain && cd /opt/ecomsp-brain
bash scripts/bootstrap-debian.sh --odbc
python scripts/preflight.py
```

Preflight now reports CPU SIMD level. Expect `avx only, no avx2` on Ivy Bridge — AVX2
arrived with Haswell in 2013, and transformer inference is roughly 2–3x slower without
it. That single fact drives the role split below.

## Decide roles by measuring, not guessing

```bash
python scripts/bench.py        # run on both boxes, compare
```

It reports embedding throughput, search latency and ingest write rate. On this sandbox
(AVX-512 Xeon, 2 vCPU) the current numbers are 793 documents searching at 28 ms p50 and
about 4,300 documents/sec ingested. Your two boxes will differ mainly on the embedding
line.

**The rule that falls out of it:** put bulk embedding wherever chunks/sec is highest.
Everything else — the store, the API, the MCP server — is I/O bound and can live
anywhere.

## Recommended split

```
Mac mini (Debian)                    Debian server
─────────────────────                ─────────────────────
SQL Server Express container   <---- NightArc ingest reads over tcp/1433
staging brain (safe to break)        prod brain: store + API + MCP
cloudflared named tunnel             nightly refresh timer
                                     bulk embedding (if it benches faster)
```

The mini earns its place by hosting SQL Server. That is a clean network boundary — the
ingester talks TCP, so nothing needs shared storage — and it keeps a 1.5 GB memory hog
off the box serving queries. It solves the RAM contention flagged in the single-node
runbook.

```bash
# on the mini
sudo apt install -y docker.io && sudo usermod -aG docker "$USER"
export SA_PASSWORD='Str0ng-Lab-Pass!'
docker run -d --name mssql --restart unless-stopped \
  -e ACCEPT_EULA=Y -e MSSQL_SA_PASSWORD="$SA_PASSWORD" \
  -e MSSQL_PID=Express -e MSSQL_MEMORY_LIMIT_MB=1536 \
  -p 1433:1433 mcr.microsoft.com/mssql/server:2022-latest
docker exec -i mssql /opt/mssql-tools18/bin/sqlcmd -S localhost -U sa \
  -P "$SA_PASSWORD" -C < scripts/lab-nightarc-fixture.sql
```

Then on the Debian server, point at it across the network:

```env
NIGHTARC_SERVER=macmini.lab.internal,1433
NIGHTARC_DB=NightArc
NIGHTARC_USER=sa
NIGHTARC_PASSWORD=Str0ng-Lab-Pass!
```

`python scripts/preflight.py --group nightarc` will confirm TCP reach, login and table
count from the other side.

The mini is also the right host for the `cloudflared` named tunnel — it is doing nothing
CPU-heavy, and it keeps the public ingress off the box holding the archive.

## The one thing not to do

**Do not put `data/brain.db` on an NFS or SMB share for both machines to open.** SQLite's
WAL locking is not dependable over network filesystems and you will corrupt the store.
One writer per file.

Move whole snapshots instead:

```bash
# on staging, push a verified snapshot to prod
scripts/promote-db.sh brain@prod.internal:/opt/ecomsp-brain/data/brain.db

# or pull prod's archive down to staging to experiment against real data
scripts/promote-db.sh --pull brain@prod.internal:/opt/ecomsp-brain/data/brain.db
```

That uses SQLite's `.backup`, which is safe against a running writer, runs
`integrity_check` and prints the per-source document counts before swapping, keeps the
previous file as `.prev`, clears stale `-wal`/`-shm` files, and restarts the API service
if one is running there.

## A genuinely good use for the staging node

Ingesting a client's Discord server or a live NightArc database for the first time is
where things go wrong — wrong channels, wrong tables, an embedder you then want to
change. Doing that on the mini first, with `--pull` to get a copy of real content to
test against, means the archive your agents query never sees a half-finished experiment.

It is also where to test embedder changes. Switching `BRAIN_EMBEDDER` is non-destructive
because vectors are keyed by model name, but a full re-embed of a large corpus is hours
of CPU. Do it on staging, measure whether retrieval actually improved, then promote the
database rather than repeating the compute.

## Alternative if the mini benches faster

Entirely possible, depending on what the other box is. If so, flip it: mini becomes the
always-on prod node, since 11 W of idle draw for a 24/7 service is hard to argue with,
and the Debian server becomes the batch node doing full crawls and re-embeds, promoting
results over. Same scripts, same rule — one writer per database file.
