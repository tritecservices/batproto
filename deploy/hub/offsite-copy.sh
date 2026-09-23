#!/usr/bin/env bash
# Copy new hub backups to Azure Blob Storage (UK South), signed in as the server's
# managed identity (Azure Arc) - no keys or SAS tokens on the server.
#   BACKUP_STORAGE_ACCOUNT=<name> in /etc/ecomsp/hub.env
set -euo pipefail
: "${BACKUP_STORAGE_ACCOUNT:?set BACKUP_STORAGE_ACCOUNT in /etc/ecomsp/hub.env}"
az login --identity --allow-no-subscriptions --output none
az storage blob sync --account-name "$BACKUP_STORAGE_ACCOUNT" --auth-mode login \
  --container hub-backups --source /srv/backups/hub --output none
echo "off-site copy done: $BACKUP_STORAGE_ACCOUNT/hub-backups"
