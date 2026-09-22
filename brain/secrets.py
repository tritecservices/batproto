"""Secrets from Azure Key Vault instead of .env files.

If AZURE_KEYVAULT_URL is set (https://<vault>.vault.azure.net/), every secret listed
in SECRET_MAP is read from the vault at start-up and placed in the process
environment, where the rest of the code already looks. Nothing is written to disk.

Authentication is DefaultAzureCredential, so no credential needs storing either:
  * on Azure (App Service, Container Apps, VMs): the resource's managed identity
  * on the Debian server: an Azure Arc-connected machine identity, or a service
    principal via AZURE_CLIENT_ID / AZURE_TENANT_ID / AZURE_CLIENT_CERTIFICATE_PATH
  * on a developer laptop: `az login`

Fails closed: if a vault is configured but can't be reached, start-up stops rather
than carrying on with whatever stale values happen to be in the environment.

Grant the identity the "Key Vault Secrets User" role on the vault (read-only). Vault
secret names use dashes; they map to the environment variables below.
"""
from __future__ import annotations

import logging
import os

log = logging.getLogger("brain.secrets")

# vault secret name -> environment variable
SECRET_MAP = {
    "brain-api-key": "BRAIN_API_KEY",
    "discord-bot-token": "DISCORD_BOT_TOKEN",
    "nightarc-password": "NIGHTARC_PASSWORD",
    "openai-api-key": "OPENAI_API_KEY",
    "azure-openai-api-key": "AZURE_OPENAI_API_KEY",
    "github-token": "GITHUB_TOKEN",
    "stackapps-key": "STACKAPPS_KEY",
}

_loaded = False


class SecretsError(RuntimeError):
    pass


def _client(url: str):
    try:
        from azure.identity import DefaultAzureCredential
        from azure.keyvault.secrets import SecretClient
    except ImportError as exc:
        raise SecretsError("AZURE_KEYVAULT_URL is set but azure-keyvault-secrets / "
                           "azure-identity are not installed") from exc
    return SecretClient(vault_url=url, credential=DefaultAzureCredential())


def load(client=None, override: bool = False) -> dict[str, str]:
    """Pull mapped secrets into os.environ. Returns {env var: 'vault' | 'absent'}.

    `override=False` keeps values already set in the environment (so an operator can
    deliberately pin one), but the result says which came from the vault."""
    global _loaded
    url = os.environ.get("AZURE_KEYVAULT_URL", "").strip()
    if not url and client is None:
        return {}
    if not url.startswith("https://") and client is None:
        raise SecretsError("AZURE_KEYVAULT_URL must be an https:// vault URL")
    client = client or _client(url)
    result: dict[str, str] = {}
    for name, env in SECRET_MAP.items():
        if os.environ.get(env) and not override:
            result[env] = "environment"
            continue
        try:
            value = client.get_secret(name).value
        except Exception as exc:                       # noqa: BLE001 - SDK raises many types
            if exc.__class__.__name__ == "ResourceNotFoundError":
                result[env] = "absent"                 # optional secret, not in this vault
                continue
            raise SecretsError(f"could not read '{name}' from Key Vault: "
                               f"{exc.__class__.__name__}") from None
        if value:
            os.environ[env] = value
            result[env] = "vault"
    _loaded = True
    log.info("secrets loaded: %s", {k: v for k, v in result.items()})  # names only, never values
    return result


def ensure_loaded() -> None:
    """Idempotent start-up hook for the API, CLI and MCP server."""
    if not _loaded:
        load()
