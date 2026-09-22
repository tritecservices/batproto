# Nightly refresh for Windows. Register with Task Scheduler:
#
#   schtasks /create /tn "Brain refresh" /tr "powershell -ExecutionPolicy Bypass -File C:\brain\scripts\refresh.ps1" /sc daily /st 02:30 /ru SYSTEM
#
# Incremental by design: Discord channels resume from stored cursors, and unchanged
# documents are skipped by content hash, so a nightly run is cheap.

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

if (Test-Path ".venv\Scripts\Activate.ps1") { . .venv\Scripts\Activate.ps1 }

$Log = Join-Path $Root "data\refresh.log"
function Log($m) { "$(Get-Date -Format s)  $m" | Tee-Object -FilePath $Log -Append }

Log "refresh start"

foreach ($src in @("discord", "stackexchange", "discourse", "github", "files", "nightarc")) {
    try {
        Log "ingesting $src"
        python -m brain.cli ingest $src --no-embed 2>&1 | Tee-Object -FilePath $Log -Append
    } catch {
        Log "WARN $src failed: $($_.Exception.Message)"   # one bad source must not stop the rest
    }
}

Log "embedding new chunks"
python -m brain.cli embed 2>&1 | Tee-Object -FilePath $Log -Append

Log "inventory"
python -m brain.cli sources 2>&1 | Tee-Object -FilePath $Log -Append

# keep a dated copy; this database is the asset
$stamp = Get-Date -Format "yyyyMMdd"
Copy-Item "data\brain.db" "data\backups\brain-$stamp.db" -Force -ErrorAction SilentlyContinue
Get-ChildItem "data\backups\brain-*.db" -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTime -Descending | Select-Object -Skip 14 | Remove-Item -Force

Log "refresh done"
