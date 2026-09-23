# Backup and restore (ITIL service continuity)

## Targets

| | Target | How |
| --- | --- | --- |
| **RPO** (data you could lose) | 24 hours | Nightly backup at 02:00 |
| **RTO** (time to restore service) | 4 hours | Restore procedure below; practised weekly by the automated restore test |
| Retention on the server | 14 nightly backups | `--keep 14` |
| Retention off-site | 30 days of deleted and overwritten versions | Azure Storage (UK South), with blob versioning and soft delete |

These are proposals: agree them with each client, put them in the service description,
and review them yearly.

## What is backed up

| Included | Not included (and why) |
| --- | --- |
| The hub database (surveys, recordings, locks, jobs, anchors) | **Video.** It's large, and the originals exist on the cards and the file share. Protect it with storage snapshots or replication |
| The hub's audit logs (hash chains) | Secrets: they live in Azure Key Vault, which has its own soft delete and purge protection |
| Knowledge base databases, per tenant | |
| Review logs, QA logs, reports and audit trails from server survey storage: the human work | |

Each backup is a folder `hub-<UTC time>` with a `manifest.json` that lists every file's
SHA-256, the row count of every table and the schema version. The backup is written to
a `.partial` folder and renamed only when complete, so a half-written backup never looks
finished.

## Commands

```bash
python -m hub.backup create --out /srv/backups/hub --keep 14
python -m hub.backup verify /srv/backups/hub/hub-20260923T020000Z
python -m hub.backup restore-test latest --out /srv/backups/hub \
       --to "postgresql:///hub_restore_test?host=/var/run/postgresql"
```

`verify` catches missing, corrupted, altered and added files. `restore` refuses to run
from a backup that fails verification, and refuses to overwrite a database that already
has a hub schema unless you pass `--force`.

## Automated restore test (weekly)

`ecomsp-hub-restore-test.timer` runs every Sunday at 04:00. It restores the newest backup
into the scratch database `hub_restore_test`, then checks:

1. the backup's checksums;
2. the schema version after restore;
3. the row count of every table against the manifest;
4. every audit log in the backup still verifies as a hash chain.

**A failed restore test is an incident.** The backups can't be relied on until it's
fixed. Check with `journalctl -u ecomsp-hub-restore-test`.

## Restoring the service (disaster recovery)

1. Stop the hub: `systemctl stop ecomsp-hub-api 'ecomsp-hub-worker@*'`.
2. Pick the backup. Take the newest one that passes `verify`, or get one from Azure:
   `az storage blob download-batch --account-name <acct> --auth-mode login -s hub-backups
   --pattern 'hub-<time>/*' -d /srv/restore`.
3. Restore into a new, empty database: `createdb -O ecomsp-hub -E UTF8 -T template0 hub_new`,
   then `python -m hub.backup restore <backup> --to "postgresql:///hub_new?host=/var/run/postgresql"`.
4. Point `HUB_DATABASE_URL` at `hub_new`, or rename the databases. Copy `audit/` back
   to `BRAIN_AUDIT_DIR`, and each folder under `review/` back into survey storage.
5. Start the hub, check `/health`, and run `emergence-kit hub verify` on an affected
   folder.
6. Tell users. **Anything done since the backup was taken is lost from the hub.** Laptop
   folders still hold their own audit trails: run `hub anchor` from each one to re-anchor
   what was lost.
7. Record the incident, the backup used, the data-loss window and the time taken
   (actual RTO).

## Off-site copy

`deploy/hub/offsite-copy.sh` runs after each nightly backup. It syncs
`/srv/backups/hub` to the `hub-backups` container, signed in as the server's Azure Arc
managed identity. No keys or SAS tokens are stored on the server. Set
`BACKUP_STORAGE_ACCOUNT` in `/etc/ecomsp/hub.env`, and give the server's identity
**Storage Blob Data Contributor** on that account.

For ransomware resilience, add an immutability policy (WORM) to the container once the
retention period is agreed. Once locked, it can't be shortened, so decide the period
first.

Backups contain personal data (names and review decisions), so they're covered by the
DPIA and by the retention schedule.
