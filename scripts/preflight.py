#!/usr/bin/env python3
"""Preflight check. Run this before anything else, and again after each setup step.

    python scripts/preflight.py            # everything
    python scripts/preflight.py --group sources
    python scripts/preflight.py --json     # for monitoring

Exit code 0 = no FAILs. WARNs are things you can live without for a first test.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RESULTS: list[dict] = []
GREEN, YELLOW, RED, DIM, RESET = "\033[32m", "\033[33m", "\033[31m", "\033[2m", "\033[0m"


def record(group: str, name: str, status: str, detail: str = "", fix: str = "") -> None:
    RESULTS.append({"group": group, "check": name, "status": status,
                    "detail": detail, "fix": fix})


def ok(g, n, d="", fix=""): record(g, n, "PASS", d)   # fix accepted so callers can be ternaries
def warn(g, n, d="", fix=""): record(g, n, "WARN", d, fix)
def fail(g, n, d="", fix=""): record(g, n, "FAIL", d, fix)


def env(key: str) -> str | None:
    v = os.environ.get(key)
    return v.strip() if v and v.strip() else None


def load_dotenv() -> None:
    """Read .env so the checks see the same config the app will."""
    path = ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


# ------------------------------------------------------------------ platform
def check_platform() -> None:
    g = "platform"
    v = sys.version_info
    if v >= (3, 11):
        ok(g, "python", f"{v.major}.{v.minor}.{v.micro}")
    else:
        fail(g, "python", f"{v.major}.{v.minor}", "need 3.11+; Debian 13 ships 3.13")

    if sys.prefix != sys.base_prefix or env("VIRTUAL_ENV"):
        ok(g, "virtualenv", sys.prefix)
    else:
        fail(g, "virtualenv", "running against system python",
             "Debian 13 blocks pip into system python (PEP 668). "
             "python3 -m venv .venv && . .venv/bin/activate")

    ok(g, "sqlite", sqlite3.sqlite_version)
    try:
        sqlite3.connect(":memory:").execute("CREATE VIRTUAL TABLE t USING fts5(x)")
        ok(g, "sqlite fts5", "available")
    except sqlite3.OperationalError:
        fail(g, "sqlite fts5", "missing", "apt install libsqlite3-0 (or rebuild python)")

    try:
        total = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 1e9
        if total >= 7:
            ok(g, "ram", f"{total:.1f} GB")
        else:
            warn(g, "ram", f"{total:.1f} GB", "fine for retrieval; tight if you also "
                                              "run SQL Server in docker")
    except (ValueError, OSError):
        pass

    # CPU age matters for embedding throughput: Ivy Bridge (2012 Mac mini) has AVX but
    # not AVX2, which costs roughly 2-3x on transformer inference.
    try:
        flags = ""
        cpu = Path("/proc/cpuinfo")
        if cpu.exists():
            for line in cpu.read_text().splitlines():
                if line.startswith("flags"):
                    flags = line
                    break
        if flags:
            if "avx512" in flags:
                ok(g, "cpu simd", "avx512 - fast embedding")
            elif " avx2 " in flags:
                ok(g, "cpu simd", "avx2 - fine for embedding")
            elif " avx " in flags:
                warn(g, "cpu simd", "avx only, no avx2",
                     "pre-2013 CPU. Embedding runs but 2-3x slower; prefer doing "
                     "bulk embedding on a newer node. Run scripts/bench.py to measure")
            else:
                warn(g, "cpu simd", "no avx", "sentence-transformers will be very slow")
    except OSError:
        pass

    free = shutil.disk_usage(ROOT).free / 1e9
    (ok if free >= 15 else warn)(g, "free disk", f"{free:.1f} GB",
                                "" if free >= 15 else "torch + a full corpus wants ~15 GB")


# --------------------------------------------------------------- python deps
REQUIRED = {"requests": "requests", "fastapi": "fastapi", "uvicorn": "uvicorn",
            "pydantic": "pydantic", "yaml": "pyyaml", "mcp": "mcp"}
OPTIONAL = {"sqlite_vec": "sqlite-vec (faster vector search)",
            "discord": "discord.py (Discord ingest)",
            "pyodbc": "pyodbc (NightArc / SQL Server)",
            "sentence_transformers": "sentence-transformers (offline embeddings)",
            "openai": "openai (hosted embeddings)",
            "azure.ai.projects": "azure-ai-projects (Foundry agents)",
            "azure.identity": "azure-identity (Foundry auth)"}


def check_deps() -> None:
    g = "packages"
    import importlib
    for mod, label in REQUIRED.items():
        try:
            importlib.import_module(mod)
            ok(g, label)
        except ImportError:
            fail(g, label, "not installed", "pip install -r requirements.txt")
    for mod, label in OPTIONAL.items():
        try:
            importlib.import_module(mod)
            ok(g, label)
        except ImportError:
            warn(g, label, "not installed", f"pip install {label.split(' ')[0]}")


# ------------------------------------------------------------------- sources
def http_probe(url: str, timeout: int = 15) -> tuple[bool, str]:
    try:
        import requests
        r = requests.get(url, timeout=timeout,
                         headers={"User-Agent": "ecomsp-brain-preflight/1.0"})
        return r.status_code < 400, f"HTTP {r.status_code}"
    except Exception as exc:
        return False, exc.__class__.__name__


def check_sources() -> None:
    g = "sources"
    probes = [
        ("stack exchange api", "https://api.stackexchange.com/2.3/info?site=gis.stackexchange"),
        ("osgeo discourse", "https://discourse.osgeo.org/categories.json"),
        ("github api", "https://api.github.com/rate_limit"),
        ("discord api", "https://discord.com/api/v10/gateway"),
    ]
    for name, url in probes:
        good, detail = http_probe(url)
        (ok if good else fail)(g, name, detail,
                               "" if good else "check egress / proxy / DNS on the lab VLAN")

    if env("STACKAPPS_KEY"):
        ok(g, "stackapps key", "set (10,000 req/day)")
    else:
        warn(g, "stackapps key", "absent", "anonymous quota is 300/day; "
                                          "register at stackapps.com")
    if env("GITHUB_TOKEN"):
        ok(g, "github token", "set (5,000 req/hr)")
    else:
        warn(g, "github token", "absent", "anonymous is 60 req/hr")


# ------------------------------------------------------------------- discord
def check_discord() -> None:
    g = "discord"
    token = env("DISCORD_BOT_TOKEN")
    if not token:
        warn(g, "bot token", "absent", "create a bot in the Developer Portal; "
                                       "a USER token is a ToS breach, never use one")
        return
    if token.lower().startswith(("mfa.", "user ")) or "." not in token:
        fail(g, "bot token", "does not look like a bot token",
             "bot tokens come from Developer Portal > Bot > Reset Token")
        return
    try:
        import requests
        r = requests.get("https://discord.com/api/v10/users/@me",
                         headers={"Authorization": f"Bot {token}"}, timeout=20)
        if r.status_code == 200:
            me = r.json()
            ok(g, "bot identity", f"{me.get('username')} ({me.get('id')})")
        elif r.status_code == 401:
            fail(g, "bot identity", "401 unauthorized", "token wrong or was reset")
            return
        else:
            fail(g, "bot identity", f"HTTP {r.status_code}")
            return

        gr = requests.get("https://discord.com/api/v10/users/@me/guilds",
                          headers={"Authorization": f"Bot {token}"}, timeout=20)
        guilds = gr.json() if gr.status_code == 200 else []
        if guilds:
            ok(g, "guild membership", ", ".join(f"{x['name']} ({x['id']})" for x in guilds[:5]))
        else:
            warn(g, "guild membership", "bot is in no servers",
                 "invite it with an OAuth2 URL: scopes bot, permissions "
                 "View Channels + Read Message History")

        allow = env("ALLOWED_GUILDS")
        if allow:
            ids = {x.strip() for x in allow.split(",") if x.strip()}
            known = {str(x["id"]) for x in guilds}
            unknown = ids - known
            if unknown:
                warn(g, "guild whitelist", f"listed but not joined: {', '.join(unknown)}")
            else:
                ok(g, "guild whitelist", f"{len(ids)} guild(s)")
        else:
            warn(g, "guild whitelist", "ALLOWED_GUILDS empty",
                 "empty means every joined server is indexed; set it deliberately")
    except Exception as exc:
        fail(g, "bot identity", f"{exc.__class__.__name__}: {exc}")

    # message_content is privileged and cannot be read over REST; state it plainly
    warn(g, "message content intent", "cannot be verified via API",
         "Developer Portal > Bot > Privileged Gateway Intents > MESSAGE CONTENT. "
         "Without it every message body comes back empty")


# ------------------------------------------------------------------ nightarc
def check_nightarc() -> None:
    g = "nightarc"
    server = env("NIGHTARC_SERVER")
    if not server:
        warn(g, "config", "NIGHTARC_SERVER unset", "skip if not testing NightArc yet")
        return
    try:
        import pyodbc
    except ImportError:
        warn(g, "pyodbc", "not installed", "apt install unixodbc-dev && pip install pyodbc")
        return

    drivers = pyodbc.drivers()
    wanted = env("ODBC_DRIVER") or "ODBC Driver 18 for SQL Server"
    if wanted in drivers:
        ok(g, "odbc driver", wanted)
    elif any("SQL Server" in d for d in drivers):
        warn(g, "odbc driver", f"have {[d for d in drivers if 'SQL Server' in d]}",
             f"set ODBC_DRIVER to one of those, or install {wanted}")
    else:
        fail(g, "odbc driver", f"none found (drivers: {drivers})",
             "ODBC Driver 18.6+ supports Debian 13; see docs/HOME-LAB-SETUP.md")
        return

    host = server.split("\\")[0].split(",")[0]
    port = int(server.split(",")[1]) if "," in server else 1433
    if host.lower() in ("localhost", "(local)", ".", "127.0.0.1"):
        host = "127.0.0.1"
    try:
        with socket.create_connection((host, port), timeout=5):
            ok(g, "tcp reachable", f"{host}:{port}")
    except OSError as exc:
        fail(g, "tcp reachable", f"{host}:{port} {exc.__class__.__name__}",
             "named instances need the SQL Browser on udp/1434, or use host,1433. "
             "Check the Windows firewall and that TCP/IP is enabled in SQL Configuration Manager")
        return

    try:
        sys.path.insert(0, str(ROOT))
        from brain.ingest.nightarc_sql import connect, safe_query
        cn = connect()
        ver = safe_query(cn, "SELECT @@VERSION")[0][0].splitlines()[0]
        tables = safe_query(cn, "SELECT COUNT(*) FROM sys.tables")[0][0]
        ok(g, "sql login", ver[:70])
        (ok if tables else warn)(g, "tables visible", f"{tables} table(s)",
                                 "" if tables else "wrong database, or no SELECT grant")
        cn.close()
    except Exception as exc:
        fail(g, "sql login", f"{exc.__class__.__name__}: {str(exc)[:160]}",
             "for a lab container use NIGHTARC_USER=sa; for Windows auth you need "
             "Kerberos configured on Debian")


# ----------------------------------------------------------------- the brain
def check_files() -> None:
    """The file share. Most of the risk here is operational rather than technical:
    a share that vanishes on reboot, or one mounted read-write next to a walker."""
    g = "files"
    sys.path.insert(0, str(ROOT))
    raw = env("BRAIN_FILE_ROOTS")
    if not raw:
        warn(g, "roots", "BRAIN_FILE_ROOTS not set",
             "set it to your mounted share, e.g. /mnt/nas/surveys")
        return

    roots = [r.strip() for r in re.split(r"[,:]", raw) if r.strip()]
    ok(g, "roots", f"{len(roots)} configured")

    # /proc/mounts tells us the filesystem type and options for real
    mounts = []
    try:
        for line in Path("/proc/mounts").read_text().splitlines():
            parts = line.split()
            if len(parts) >= 4:
                mounts.append((parts[1], parts[2], parts[3]))
    except OSError:
        pass
    mounts.sort(key=lambda m: -len(m[0]))

    for r in roots:
        root = Path(r).expanduser()
        label = str(root)
        if not root.exists():
            fail(g, f"mount {label}", "path does not exist",
                 "mount the share; add it to /etc/fstab with nofail,_netdev "
                 "so a missing NAS cannot block boot")
            continue
        if not root.is_dir():
            fail(g, f"mount {label}", "not a directory")
            continue
        if not os.access(root, os.R_OK | os.X_OK):
            fail(g, f"mount {label}", "not readable by this user",
                 "check uid=/gid= in the mount options")
            continue

        def covers(mountpoint: str) -> bool:
            if label == mountpoint:
                return True
            prefix = mountpoint if mountpoint.endswith("/") else mountpoint + "/"
            return label.startswith(prefix)

        mp = next((m for m in mounts if covers(m[0])), None)
        fstype = mp[1] if mp else "unknown"
        opts = mp[2].split(",") if mp else []
        network = fstype in {"cifs", "smb3", "nfs", "nfs4", "fuse.sshfs", "afpfs"}

        if mp and mp[0] == label and network:
            ok(g, f"mount {label}", f"{fstype} mounted here")
        elif network:
            ok(g, f"mount {label}", f"inside {fstype} mount {mp[0]}")
        elif mp is None:
            warn(g, f"mount {label}", "could not determine the filesystem",
                 "readable, so ingest will work; only the ro/network advice is skipped")
        else:
            warn(g, f"mount {label}", f"local {fstype}, not a network mount",
                 "fine for a local folder; if you meant the NAS it is not mounted "
                 "and you are indexing an empty mountpoint")

        if network:
            if "ro" in opts:
                ok(g, f"read-only {label}", "mounted ro - the ingester cannot damage it")
            else:
                warn(g, f"read-only {label}", "mounted read-write",
                     "the ingester only reads, but mount it ro in fstab so nothing "
                     "on this box can ever write to the live share")
            if not any(o.startswith("vers=") for o in opts) and fstype.startswith(("cifs", "smb")):
                warn(g, f"smb version {label}", "no vers= pinned",
                     "pin vers=3.1.1 (or 3.0 for older NAS firmware) so a "
                     "protocol downgrade cannot surprise you")

        # is there anything worth reading, and can we actually open a file
        try:
            from brain.ingest.files import iter_files
            found, stats = iter_files(root, 25_000_000)
            if found:
                kinds: dict[str, int] = {}
                for f in found:
                    kinds[f.suffix.lower()] = kinds.get(f.suffix.lower(), 0) + 1
                top = ", ".join(f"{k or 'none'}={v}" for k, v in
                               sorted(kinds.items(), key=lambda kv: -kv[1])[:8])
                ok(g, f"content {label}", f"{len(found)} ingestible files ({top})")
                try:
                    with open(found[0], "rb") as fh:
                        fh.read(16)
                    ok(g, f"read test {label}", found[0].name)
                except OSError as exc:
                    fail(g, f"read test {label}", f"{exc.__class__.__name__}: {exc}",
                         "permissions on the share, or the session dropped")
            else:
                warn(g, f"content {label}", "no supported file types found",
                     f"skipped {stats['skipped_junk']} junk, "
                     f"{stats['too_big']} over the size cap")
        except Exception as exc:
            fail(g, f"walk {label}", f"{exc.__class__.__name__}: {exc}")

    # the mistake that actually destroys data
    db = Path(env("BRAIN_DB") or "data/brain.db").resolve()
    on_net = next((m for m in mounts
                   if str(db).startswith(m[0].rstrip("/") + "/")
                   and m[1] in {"cifs", "smb3", "nfs", "nfs4", "fuse.sshfs"}), None)
    if on_net:
        fail(g, "database location", f"brain.db sits on a {on_net[1]} mount",
             "move it to local disk now. SQLite WAL locking over SMB/NFS corrupts "
             "databases. Use scripts/promote-db.sh to copy snapshots to the NAS instead")
    else:
        ok(g, "database location", "brain.db is on local disk")


def check_network() -> None:
    """Multi-machine topology: two routers, a NAS on one of them, and a laptop that
    has to reach both. This group exists because a double-NAT mistake presents as
    ten unrelated failures elsewhere."""
    g = "network"

    # --- where am I: report it so you can compare across the three machines
    gateway = subnet = ""
    try:
        out = subprocess.run(["ip", "route"], capture_output=True, text=True, timeout=5).stdout
        for line in out.splitlines():
            if line.startswith("default via "):
                gateway = line.split()[2]
            elif " src " in line and "/" in line.split()[0]:
                subnet = subnet or line.split()[0]
    except (OSError, subprocess.SubprocessError):
        pass
    if not gateway:                       # macOS / Windows fallback
        try:
            gateway = subprocess.run(["sh", "-c", "netstat -rn 2>/dev/null | awk '/^default|^0.0.0.0/{print $2; exit}'"],
                                     capture_output=True, text=True, timeout=5).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
    my_ip = ""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sk:
            sk.connect(("192.0.2.1", 9))   # no packets sent; just resolves the chosen route
            my_ip = sk.getsockname()[0]
    except OSError:
        pass
    if my_ip or gateway:
        ok(g, "this host", f"ip {my_ip or '?'}  gateway {gateway or '?'}"
                           + (f"  subnet {subnet}" if subnet else ""))
    else:
        warn(g, "this host", "could not determine addressing")

    # --- the NAS, by TCP rather than by ping: SMB is what actually matters
    nas = env("NAS_HOST")
    if nas and my_ip:
        # A real comparison, not a reminder: same /24 means same router.
        try:
            nas_ip = socket.gethostbyname(nas)
            if nas_ip.rsplit(".", 1)[0] == my_ip.rsplit(".", 1)[0]:
                ok(g, "same subnet as nas", f"{my_ip} and {nas_ip} share a /24")
            else:
                warn(g, "same subnet as nas", f"{my_ip} vs {nas_ip} - different subnets",
                     "these two are behind different routers. SMB will work in one "
                     "direction only and break unpredictably on reboot. Put the brain "
                     "host and the NAS on the same router, or bridge the second one")
        except OSError:
            warn(g, "same subnet as nas", f"cannot resolve {nas}")
    if nas:
        for port, label in ((445, "smb"), (548, "afp")):
            try:
                with socket.create_connection((nas, port), timeout=4):
                    ok(g, f"nas {label}", f"{nas}:{port} open")
                    break
            except OSError as exc:
                if port == 548:
                    fail(g, "nas smb", f"{nas}:445 {exc.__class__.__name__}",
                         "is the mini awake? Energy Saver must not sleep it. Check "
                         "System Preferences > Sharing > File Sharing, and that your "
                         "user is listed under Options > Windows File Sharing")
    else:
        warn(g, "nas", "NAS_HOST not set", "set NAS_HOST to the mini's IP to test it")

    # --- tailscale, which is how the laptop crosses to the other network
    ts = shutil.which("tailscale") or "/Applications/Tailscale.app/Contents/MacOS/Tailscale"
    if shutil.which("tailscale") or Path(ts).exists():
        try:
            raw = subprocess.run([ts, "status", "--json"], capture_output=True,
                                 text=True, timeout=15).stdout
            st = json.loads(raw)
            state = st.get("BackendState", "?")
            if state == "Running":
                peers = st.get("Peer") or {}
                online = sum(1 for p in peers.values() if p.get("Online"))
                ok(g, "tailscale", f"running, {online}/{len(peers)} peers online")
                # advertised routes are useless until approved in the admin console,
                # and that is the step people forget
                self_node = st.get("Self") or {}
                adv = self_node.get("AllowedIPs") or []
                routes = [r for r in adv if not r.endswith(("/32", "/128"))]
                if routes:
                    ok(g, "subnet routes", f"advertised and approved: {', '.join(routes)}")
                else:
                    warn(g, "subnet routes", "none approved for this node",
                         "if this box is the subnet router, run tailscale up "
                         "--advertise-routes=<lan>/24 AND approve the route in the "
                         "Tailscale admin console - it does nothing until approved")
            else:
                warn(g, "tailscale", f"backend state {state}", "sudo tailscale up")
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            warn(g, "tailscale", f"status unreadable: {exc.__class__.__name__}")
    else:
        warn(g, "tailscale", "not installed",
             "only needed on the laptop and the Debian subnet router - the 2012 mini "
             "cannot run it (needs macOS 12+)")

    # --- the tunnel binary, needed before Foundry can reach anything
    if shutil.which("cloudflared"):
        ok(g, "cloudflared", "installed")
    else:
        warn(g, "cloudflared", "not installed",
             "needed on the brain host so Foundry can reach the API over HTTPS")

    # --- can we reach the brain on another node
    peer = env("BRAIN_PEER_URL")
    if peer:
        good, detail = http_probe(f"{peer.rstrip('/')}/health", timeout=10)
        (ok if good else fail)(g, "peer brain api", f"{peer} {detail}", "" if good else
                               "check the API is bound to 0.0.0.0 not 127.0.0.1, and "
                               "that the host firewall allows the port")


def check_brain() -> None:
    g = "brain"
    sys.path.insert(0, str(ROOT))
    try:
        from brain.store import Store
    except ImportError as exc:
        fail(g, "import", str(exc), "run from the repo root")
        return
    db = env("BRAIN_DB") or "data/brain.db"
    try:
        st = Store(db)
        srcs = st.sources()
        total = sum(s["n"] for s in srcs)
        if total:
            ok(g, "store", f"{total} docs: " + ", ".join(f"{s['source']}={s['n']}" for s in srcs))
        else:
            warn(g, "store", "empty", "python -m brain.cli ingest stackexchange --pages 1")
        ok(g, "vector extension", "sqlite-vec loaded" if getattr(st, "vec_ok", False)
           else "fallback cosine (fine under ~200k chunks)")
        st.close()
    except Exception as exc:
        fail(g, "store", f"{exc.__class__.__name__}: {exc}")

    kind = env("BRAIN_EMBEDDER") or "hash"
    if kind == "hash":
        warn(g, "embedder", "hash", "offline and keyless but weak retrieval; "
                                    "use BRAIN_EMBEDDER=local for real results")
    else:
        try:
            from brain.embed import get_embedder
            emb = get_embedder(kind)
            emb.encode(["preflight"])
            ok(g, "embedder", f"{kind} -> {emb.name} ({emb.dim}d)")
        except Exception as exc:
            fail(g, "embedder", f"{kind}: {exc.__class__.__name__}",
                 "pip install sentence-transformers, or check the provider key")

    port = int(env("BRAIN_PORT") or 8077)
    good, detail = http_probe(f"http://127.0.0.1:{port}/health", timeout=5)
    if good:
        ok(g, "api", f"listening on {port}")
    else:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=2):
                warn(g, "api", f"port {port} is in use by something else")
        except OSError:
            warn(g, "api", "not running", f"python -m brain.cli serve --port {port}")

    if (env("BRAIN_API_KEY") or "change-me") in ("", "change-me"):
        warn(g, "api key", "default or unset",
             "set BRAIN_API_KEY; Foundry needs a real one in a project connection")
    else:
        ok(g, "api key", "set")


# ------------------------------------------------------------------- foundry
def check_foundry() -> None:
    g = "foundry"
    endpoint = env("FOUNDRY_PROJECT_ENDPOINT")
    if not endpoint:
        warn(g, "endpoint", "unset", "skip if you are only testing Claude/MCP first")
        return
    if not endpoint.startswith("https://") or "/api/projects/" not in endpoint:
        fail(g, "endpoint", endpoint,
             "expected https://<resource>.services.ai.azure.com/api/projects/<project>")
    else:
        ok(g, "endpoint", endpoint)

    az = shutil.which("az")
    if not az:
        warn(g, "azure cli", "not installed",
             "curl -sL https://aka.ms/InstallAzureCLIDeb | sudo bash")
    else:
        try:
            out = subprocess.run([az, "account", "show", "-o", "json"],
                                 capture_output=True, text=True, timeout=45)
            if out.returncode == 0:
                acct = json.loads(out.stdout)
                ok(g, "azure login", f"{acct.get('name')} / {acct.get('tenantId','')[:8]}...")
            else:
                fail(g, "azure login", "not signed in",
                     "az login --use-device-code   (headless server)")
        except Exception as exc:
            warn(g, "azure login", exc.__class__.__name__)

    try:
        from azure.ai.projects import AIProjectClient
        from azure.identity import DefaultAzureCredential
        if endpoint:
            client = AIProjectClient(endpoint=endpoint, credential=DefaultAzureCredential())
            agents = list(client.agents.list())
            ok(g, "project reachable", f"{len(agents)} existing agent(s)")
    except ImportError:
        warn(g, "project reachable", "azure-ai-projects not installed")
    except Exception as exc:
        fail(g, "project reachable", f"{exc.__class__.__name__}: {str(exc)[:140]}",
             "needs the Foundry User role on the Foundry resource, and a custom "
             "subdomain for token auth")

    public = env("BRAIN_PUBLIC_URL") or ""
    if public.startswith("https://") and "localhost" not in public:
        good, detail = http_probe(f"{public}/health", timeout=20)
        (ok if good else fail)(g, "api publicly reachable", f"{public} {detail}",
                               "" if good else "Foundry calls your API from Azure, so the "
                                               "tunnel must be up before applying agents")
    else:
        warn(g, "api publicly reachable", public or "unset",
             "Foundry cannot reach localhost. Use a cloudflared tunnel and set "
             "BRAIN_PUBLIC_URL to the https address")


GROUPS = {"platform": check_platform, "packages": check_deps, "sources": check_sources,
          "discord": check_discord, "nightarc": check_nightarc, "files": check_files,
          "network": check_network, "brain": check_brain, "foundry": check_foundry}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--group", action="append", choices=list(GROUPS),
                    help="limit to one or more groups")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    load_dotenv()
    for name in (args.group or GROUPS):
        GROUPS[name]()

    if args.json:
        print(json.dumps(RESULTS, indent=2))
    else:
        colour = {"PASS": GREEN, "WARN": YELLOW, "FAIL": RED}
        current = None
        for r in RESULTS:
            if r["group"] != current:
                current = r["group"]
                print(f"\n{current.upper()}")
            mark = f"{colour[r['status']]}{r['status']:<4}{RESET}"
            print(f"  {mark} {r['check']:<26} {r['detail']}")
            if r["fix"] and r["status"] != "PASS":
                print(f"       {DIM}-> {r['fix']}{RESET}")
        counts = {s: sum(1 for r in RESULTS if r["status"] == s)
                  for s in ("PASS", "WARN", "FAIL")}
        print(f"\n{counts['PASS']} pass, {counts['WARN']} warn, {counts['FAIL']} fail")
    return 1 if any(r["status"] == "FAIL" for r in RESULTS) else 0


if __name__ == "__main__":
    sys.exit(main())
