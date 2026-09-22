#!/usr/bin/env bash
# Bootstrap the brain on Debian 13 (trixie). Idempotent - safe to re-run.
#
#   bash scripts/bootstrap-debian.sh              # base install
#   bash scripts/bootstrap-debian.sh --odbc       # + Microsoft ODBC 18 (needs sudo)
#   bash scripts/bootstrap-debian.sh --embeddings # + torch/sentence-transformers (~2.5 GB)
#   bash scripts/bootstrap-debian.sh --azure      # + Azure CLI
#   bash scripts/bootstrap-debian.sh --all
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

WITH_ODBC=0; WITH_EMB=0; WITH_AZ=0
for arg in "$@"; do
  case "$arg" in
    --odbc) WITH_ODBC=1 ;;
    --embeddings) WITH_EMB=1 ;;
    --azure) WITH_AZ=1 ;;
    --all) WITH_ODBC=1; WITH_EMB=1; WITH_AZ=1 ;;
    *) echo "unknown flag: $arg" >&2; exit 2 ;;
  esac
done

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

say "checking the distro"
. /etc/os-release
echo "$PRETTY_NAME"
[ "${ID:-}" = "debian" ] || echo "WARNING: written for Debian; adapt package names"

say "apt packages"
sudo apt-get update -qq
sudo apt-get install -y --no-install-recommends \
  python3 python3-venv python3-dev build-essential \
  git curl ca-certificates gnupg sqlite3 unixodbc unixodbc-dev

say "virtualenv (Debian 13 refuses pip into system python - PEP 668)"
[ -d .venv ] || python3 -m venv .venv
# shellcheck disable=SC1091
. .venv/bin/activate
python -m pip install --quiet --upgrade pip wheel

say "core python packages"
pip install --quiet sqlite-vec requests "fastapi>=0.115" "uvicorn[standard]" \
  pydantic "mcp>=1.2" pyyaml

if [ "$WITH_ODBC" = 1 ]; then
  say "Microsoft ODBC Driver 18 for SQL Server"
  # 18.6+ added official Debian 13 packages. The keyring must be dearmored;
  # apt 3.0 on trixie rejects the old apt-key path.
  sudo install -d -m 0755 /usr/share/keyrings
  curl -fsSL https://packages.microsoft.com/keys/microsoft.asc \
    | sudo gpg --dearmor -o /usr/share/keyrings/microsoft-prod.gpg
  echo "deb [arch=amd64,arm64 signed-by=/usr/share/keyrings/microsoft-prod.gpg] \
https://packages.microsoft.com/debian/13/prod trixie main" \
    | sudo tee /etc/apt/sources.list.d/mssql-release.list >/dev/null
  sudo apt-get update -qq
  # pre-accepting the EULA keeps the install non-interactive
  sudo install -d -m 0755 /opt/microsoft/msodbcsql18
  sudo touch /opt/microsoft/msodbcsql18/ACCEPT_EULA
  sudo ACCEPT_EULA=Y apt-get install -y msodbcsql18 mssql-tools18
  pip install --quiet pyodbc
  echo "drivers: $(python -c 'import pyodbc;print(pyodbc.drivers())')"
fi

if [ "$WITH_EMB" = 1 ]; then
  say "offline embeddings (CPU torch, ~2.5 GB - be patient)"
  pip install --quiet torch --index-url https://download.pytorch.org/whl/cpu
  pip install --quiet sentence-transformers
  python - <<'PY'
from sentence_transformers import SentenceTransformer
m = SentenceTransformer("BAAI/bge-small-en-v1.5")   # ~130 MB, cached in ~/.cache
print("embedding dim:", m.get_sentence_embedding_dimension())
PY
fi

if [ "$WITH_AZ" = 1 ]; then
  say "Azure CLI + Foundry SDK"
  if ! command -v az >/dev/null; then
    curl -sL https://aka.ms/InstallAzureCLIDeb | sudo bash
  fi
  pip install --quiet "azure-ai-projects>=2.5.0" azure-identity
  echo "next: az login --use-device-code"
fi

say "discord ingest"
pip install --quiet "discord.py>=2.4"

say "config"
[ -f .env ] || { cp .env.example .env; echo "created .env - fill it in"; }
mkdir -p data data/backups

say "tests"
python tests/test_pipeline.py | tail -3

say "preflight"
python scripts/preflight.py || true

cat <<'EOF'

Done. Activate with:  . .venv/bin/activate
Then:                 python -m brain.cli ingest stackexchange --pages 2
EOF
