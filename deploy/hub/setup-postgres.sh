#!/usr/bin/env bash
# One-off: PostgreSQL database and roles for the hub on Debian 13.
#   sudo bash deploy/hub/setup-postgres.sh
# Uses peer authentication over the local socket: the service account `ecomsp-hub`
# connects as the database role of the same name, and no password exists to leak.
set -euo pipefail

apt-get install -y --no-install-recommends postgresql postgresql-client

id ecomsp-hub >/dev/null 2>&1 || useradd --system --home /var/lib/ecomsp --shell /usr/sbin/nologin ecomsp-hub
install -d -o ecomsp-hub -g ecomsp-hub -m 0750 /var/lib/ecomsp /var/lib/ecomsp/hub-audit /srv/ecomsp/surveys /srv/backups/hub

sudo -u postgres psql -v ON_ERROR_STOP=1 <<'SQL'
DO $$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'ecomsp-hub') THEN
    CREATE ROLE "ecomsp-hub" LOGIN;
  END IF;
END $$;
SQL
if ! sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='hub'" | grep -q 1; then
  sudo -u postgres createdb --owner=ecomsp-hub --encoding=UTF8 --template=template0 hub
fi
# a scratch database the weekly restore test overwrites
if ! sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='hub_restore_test'" | grep -q 1; then
  sudo -u postgres createdb --owner=ecomsp-hub --encoding=UTF8 --template=template0 hub_restore_test
fi
echo "PostgreSQL ready: database 'hub' owned by role 'ecomsp-hub' (peer auth, no password)"
