# Using the Mac mini NAS with the brain

You enabled file sharing on the 2012 mini. That makes it genuinely useful to this
project, but it also puts you one command away from the one mistake that corrupts
the database. Read the red box first.

---

## Read this before anything else

> **Never put `data/brain.db` on the NAS share.**
>
> SQLite's write-ahead log relies on file locking that SMB and NFS do not implement
> reliably. The failure is not a clean error - you get intermittent
> `database is locked`, then silent corruption that you discover weeks later when a
> query returns nothing. Paperless-ngx users hit exactly this when they moved the
> database to a network share ([paperless-ngx #3492](https://github.com/paperless-ngx/paperless-ngx/issues/3492)),
> and Litestream documents the same constraint: the database must live on a local
> volume, not a network volume ([Litestream docs](https://litestream.io/guides/docker/)).
>
> The NAS is for **source material in** and **backups out**. Never the live database.

So the split is:

| Lives on | What |
|---|---|
| Debian local disk | `data/brain.db`, the API, the embedder |
| NAS share (read-only mount) | survey folders, QGIS projects, WAVs, exports - the ingest source |
| NAS share (write) | rotated `.db` snapshots made by `scripts/promote-db.sh` |

---

## Phase 1 - mount the share read-only on Debian

```bash
sudo apt install cifs-utils
sudo mkdir -p /mnt/nas/surveys
```

Put the credentials in a root-only file rather than in `fstab`, where every user on
the box can read them:

```bash
sudo install -m 600 /dev/null /etc/cifs-nas.cred
sudo tee /etc/cifs-nas.cred >/dev/null <<'EOF'
username=yourmacuser
password=yourpassword
EOF
```

Then one line in `/etc/fstab`:

```
//192.168.1.50/Surveys  /mnt/nas/surveys  cifs  credentials=/etc/cifs-nas.cred,ro,vers=3.0,uid=1000,gid=1000,iocharset=utf8,nofail,_netdev,x-systemd.automount  0  0
```

Each option is doing a job:

- **`ro`** - the ingester only reads, but this makes it impossible for anything on the
  Debian box to damage the live survey data. This is the single most valuable option here.
- **`nofail,_netdev`** - without these, a mini that is off or slow to wake blocks
  Debian's boot and you end up on a recovery console. `_netdev` waits for the network
  first; `nofail` lets boot continue if the mount fails ([mount.cifs man page](https://manpages.debian.org/trixie/cifs-utils/mount.cifs.8.en.html)).
- **`vers=3.0`** - macOS 10.15 SMB tops out at 3.0. Pin it, or a negotiation failure
  looks like a permissions error and you will spend an hour on the wrong problem.
- **`uid=1000,gid=1000`** - files appear owned by your user, so you do not need sudo
  to read them.

Mount and confirm:

```bash
sudo systemctl daemon-reload && sudo mount -a
findmnt /mnt/nas/surveys          # should show cifs and ro
ls /mnt/nas/surveys
```

If macOS refuses the connection, check System Preferences → Sharing → File Sharing →
Options and make sure SMB is ticked and the user is listed under "Windows File Sharing".

---

## Phase 2 - point the brain at it

In `.env`:

```bash
BRAIN_FILE_ROOTS=/mnt/nas/surveys
BRAIN_FILE_MAX_MB=25
```

Multiple roots are comma or colon separated. Check before ingesting:

```bash
python scripts/preflight.py --group files
```

That verifies the path exists, is readable, is actually a network mount rather than an
empty mountpoint, is mounted `ro`, has a pinned SMB version, counts the ingestible
files by type, opens one to prove the session works - and fails loudly if
`BRAIN_DB` has ended up on a network mount.

Then:

```bash
python -m brain.cli ingest files            # or: --root /some/other/path
```

---

## What it actually does with your files

It is not a flat text dump. Each type is read for what makes it searchable:

| Type | What gets indexed |
|---|---|
| `.qgs` / `.qgz` | Project title, CRS, and every layer with its provider and **full data source path**. A `.qgz` is a zip holding the `.qgs` XML plus a `.qgd` sidecar ([QGIS file formats](https://docs.qgis.org/3.44/en/docs/user_manual/appendices/qgis_file_formats.html)), so it is unzipped first. |
| `.wav` | GUANO metadata from the `guan` RIFF sub-chunk only - make, model, timestamp, position, auto and manual species IDs. The audio is never read, so a 40 GB folder of recordings costs almost nothing. |
| `.csv` / `.tsv` | Header, row count, a sample, and the distinct values in species/site/ID columns. A 200k-row Kaleidoscope `id.csv` becomes a page describing the table, not 200k rows of noise. |
| `.docx` `.xlsx` `.pptx` | Text from the zipped XML directly - no extra dependency. |
| `.pdf` | Text via `pypdf` if installed. Scanned PDFs are skipped rather than stored empty; those need OCR. |
| text, code, config, `.sql` | Read as-is, size capped. |

The GUANO reader follows the spec properly: the block can sit anywhere in the RIFF
container (recorders commonly append it after `data`), so sub-chunks are walked rather
than assumed, and fields are UTF-8 `Namespace|Key: value` lines
([GUANO specification](https://github.com/riggsd/guano-spec/blob/master/guano_specification.md)).

Junk that shares accumulate is skipped: `._*` AppleDouble forks, `.DS_Store`,
`Thumbs.db`, Office `~$` lock files, `.Spotlight-V100`, `@eaDir`, `$RECYCLE.BIN`.

### Why this is the part worth having

The public sources in this system - Stack Exchange, the QGIS forums, GitHub - are
available to every competitor you have. This one is not. A question like
"which client projects still point at the old file server" is unanswerable from any
public corpus and takes minutes to answer badly by hand:

```bash
python -m brain.cli search "layer datasource OLDSERVER" --source files
```

That is also the first thing worth turning into a product, because it needs your data
model, not a clever model.

---

## Sensitivity tagging

Anything mentioning protected species, roosts, setts, licence numbers, Annex II /
Schedule 1 language, or the six-letter codes for sensitive species (`RHIHIP`, `BARBAR`,
`MYOBEC` and similar) is tagged **`sensitive`**. Anything containing something that
looks like a British grid reference is tagged **`has-grid-ref`**.

The tags do not block retrieval - they let you instruct an agent to use the material
without reproducing locations. Put that in the agent's instructions explicitly:

> Never output grid references, `Loc Position` values, or roost locations from
> documents tagged `sensitive`. Cite the file path instead and say the location is
> withheld.

This matters commercially as well as legally. Publishing a bat roost location in a
deliverable is the kind of mistake that ends a consultancy relationship, and the
tagging is deliberately over-inclusive for that reason.

---

## Phase 3 - backups onto the NAS, the safe way

This is the legitimate use of the share for the database, and the reason
`scripts/promote-db.sh` exists: it takes a proper `.backup` snapshot, runs
`integrity_check` on the copy, and only then moves it - so a mid-write moment never
produces a corrupt backup.

```bash
./scripts/promote-db.sh --to /mnt/nas/backups     # needs a rw mount for this path
```

If you want a second rw mount purely for backups while keeping the survey data
read-only, mount the two shares separately:

```
//192.168.1.50/Surveys  /mnt/nas/surveys  cifs  credentials=/etc/cifs-nas.cred,ro,vers=3.0,nofail,_netdev  0 0
//192.168.1.50/Backups  /mnt/nas/backups  cifs  credentials=/etc/cifs-nas.cred,vers=3.0,nofail,_netdev    0 0
```

Nightly, via the existing timer:

```bash
sudo systemctl enable --now ecomsp-brain-refresh.timer
```

---

## Honest limits of this mini as a NAS

- **USB 2.0 / gigabit ethernet on a 2012 machine.** Fine for ingesting documents.
  Not somewhere to put an active SQL Server data file.
- **macOS 10.15 is out of security support.** It is on your LAN with file sharing on.
  Do not port-forward to it, and keep it off the guest network.
- **SMB 3.0 maximum**, no SMB 3.1.1 encryption.
- If you later wipe it to Debian as discussed in `TWO-NODE-LAB.md`, Samba there gives
  you SMB 3.1.1, a supported OS, and the option of running SQL Server in Docker on the
  same box. The share config above keeps working with only the `vers=` line changed.

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| `mount error(13): Permission denied` | Wrong credentials, or the macOS user is not enabled for Windows File Sharing |
| `mount error(112): Host is down` | Almost always an SMB version mismatch - try `vers=2.1` |
| Boot hangs waiting on the mount | `nofail,_netdev` missing from the fstab line |
| Ingest finds 0 files | Share not mounted; you are reading an empty mountpoint. `findmnt` will show nothing |
| Thousands of `._` files ingested | Not possible - they are excluded - but their presence means macOS is writing resource forks; harmless |
| `database is locked` | `BRAIN_DB` is on the share. Move it to local disk immediately |
