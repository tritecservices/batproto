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

function Step($name) { Write-Host "`n==== $name ====" }

Step "1/5 frozen app (PyInstaller)"
# 1. Frozen app: no Python needed on the target machine.
python -m pip install --upgrade pip
python -m pip install "pyinstaller>=6.10" numpy
if (Test-Path build) { Remove-Item -Recurse -Force build }
if (Test-Path dist) { Remove-Item -Recurse -Force dist }
python -m PyInstaller --noconfirm --clean packaging\emergence-kit.spec
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

Step "2/5 smoke test"
# Smoke test the frozen build before packaging it.
& dist\emergence-kit\emergence-kit.exe --version
if ($LASTEXITCODE -ne 0) { throw "frozen build failed its smoke test" }
if (-not (Test-Path dist\emergence-kit\survey-studio.exe)) { throw "survey-studio.exe missing from the build" }
if (-not (Test-Path dist\emergence-kit\_internal\emergence_kit\studio\static\app.js)) {
    throw "Survey Studio's app files were not packaged" }
# the app's server must start in the frozen build: start it, read its address, stop it
$p = Start-Process dist\emergence-kit\emergence-kit.exe -ArgumentList "studio","--no-window" `
      -RedirectStandardOutput studio-smoke.txt -PassThru -WindowStyle Hidden
Start-Sleep -Seconds 5
$url = (Get-Content studio-smoke.txt -ErrorAction SilentlyContinue | Select-Object -First 1)
Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue
if (-not ($url -match '^http://127\.0\.0\.1:\d+/\?t=')) { throw "Survey Studio did not start in the frozen build" }
Write-Host "Survey Studio started OK in the frozen build"

Step "3/5 MSI (WiX)"
# 2. MSI with WiX v5 (pinned: v6+ requires accepting a maintenance-fee EULA).
dotnet tool update --global wix --version 5.0.2      # installs, or pins if present
if ($LASTEXITCODE -ne 0) { throw "could not install the WiX tool" }
$env:PATH += ";$env:USERPROFILE\.dotnet\tools"
wix --version
$msi = Join-Path (Get-Location) "dist\EmergenceKit-$version-x64.msi"
# absolute path: WiX resolves relative paths against the .wxs file's folder, not ours
$src = (Resolve-Path "dist\emergence-kit").Path
Write-Host "packaging $src"
$ico = (Resolve-Path "packaging\survey-studio.ico").Path
wix build packaging\emergence-kit.wxs -arch x64 `
    -d Version=$version -d "SourceDir=$src" -d "IconFile=$ico" `
    -o $msi
if ($LASTEXITCODE -ne 0) { throw "wix build failed" }
# Guard: an MSI that harvested no files still builds "successfully" - but it's tiny.
$size = (Get-Item $msi).Length
Write-Host ("MSI size: {0:N1} MB" -f ($size / 1MB))
if ($size -lt 5MB) { throw "MSI is only $size bytes - the app files were not packaged" }

Step "4/5 software bill of materials"
# Exactly what went into this installer: every Python package in the build
# environment, with versions and licences (CycloneDX JSON). Published beside the MSI.
python -m pip install "cyclonedx-bom>=4"
$sbom = Join-Path (Get-Location) "dist\EmergenceKit-$version-sbom.cdx.json"
cyclonedx-py environment -o $sbom
if ($LASTEXITCODE -ne 0 -or -not (Test-Path $sbom)) {
    Write-Warning "SBOM generation failed - release notes must say so"
} else {
    Write-Host "SBOM: $sbom"
}

Step "5/5 checksum"
# 3. Checksum for the release notes and change record.
$hash = (Get-FileHash $msi -Algorithm SHA256).Hash.ToLower()
"$hash *$(Split-Path $msi -Leaf)" | Out-File -Encoding ascii "$msi.sha256"
Write-Host "Built $msi"
Write-Host "SHA-256 $hash"
