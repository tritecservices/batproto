<#
  Build the Windows installer for Emergence Review Kit.

    powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1

  Run from tools\emergence-kit on a Windows machine (CI does exactly this).
  Produces dist\EmergenceKit-<version>-x64.msi plus a SHA-256 checksum file.

  Requires: Python 3.12 (x64), .NET SDK 8+ (for the WiX tool).
  The version comes from emergence_kit/__init__.py - one source of truth.
#>
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

$version = (python -c "import emergence_kit; print(emergence_kit.__version__)").Trim()
Write-Host "Building Emergence Review Kit $version"

# 1. Frozen app: no Python needed on the target machine.
python -m pip install --upgrade pip
python -m pip install "pyinstaller>=6.10" numpy
if (Test-Path build) { Remove-Item -Recurse -Force build }
if (Test-Path dist) { Remove-Item -Recurse -Force dist }
python -m PyInstaller --noconfirm --clean --onedir --console `
    --name emergence-kit `
    --paths . `
    --collect-submodules emergence_kit `
    --hidden-import numpy `
    packaging\launcher.py

# Smoke test the frozen build before packaging it.
& dist\emergence-kit\emergence-kit.exe --version
if ($LASTEXITCODE -ne 0) { throw "frozen build failed its smoke test" }

# 2. MSI with WiX v5 (pinned: v6+ requires accepting a maintenance-fee EULA).
dotnet tool update --global wix --version 5.0.2      # installs, or pins if present
if ($LASTEXITCODE -ne 0) { throw "could not install the WiX tool" }
$env:PATH += ";$env:USERPROFILE\.dotnet\tools"
$msi = "dist\EmergenceKit-$version-x64.msi"
wix build packaging\emergence-kit.wxs -arch x64 `
    -d Version=$version -d SourceDir=dist\emergence-kit `
    -o $msi
if ($LASTEXITCODE -ne 0) { throw "wix build failed" }

# 3. Checksum for the release notes and change record.
$hash = (Get-FileHash $msi -Algorithm SHA256).Hash.ToLower()
"$hash *$(Split-Path $msi -Leaf)" | Out-File -Encoding ascii "$msi.sha256"
Write-Host "Built $msi"
Write-Host "SHA-256 $hash"
