# Deploying Emergence Review Kit with Microsoft Intune

For customer IT teams. Two packages: **ffmpeg** (a dependency, deployed first) and the
**Emergence Review Kit MSI**. Both install per machine; users need no admin rights.

## 1. ffmpeg (dependency)

ffmpeg is not bundled with the kit (licensing: GPL builds that include x264 carry
obligations we don't pass on to you). Deploy it separately, with either:

- **winget, via an Intune Win32 app or remediation script** (recommended):
  `winget install --id Gyan.FFmpeg -e --scope machine --silent --accept-package-agreements --accept-source-agreements`
- **Your own packaged build** under `C:\Program Files\ffmpeg\bin`, which the kit also finds.

The kit looks for ffmpeg in this order: the `EMERGENCE_FFMPEG` / `EMERGENCE_FFPROBE`
environment variables, PATH, winget's links folders, `C:\Program Files\ffmpeg\bin`,
Chocolatey, then `C:\ffmpeg\bin`. To pin an exact binary, set the environment
variables machine-wide.

Optional: **ExifTool** (`winget install --id OliverBetz.ExifTool -e --scope machine`),
which gives more reliable recording times for Sony AVCHD and some MP4 cameras.

## 2. The kit

**App type:** Windows app (Win32), wrapping the MSI with the Microsoft Win32 Content Prep
Tool (`IntuneWinAppUtil.exe -c <folder> -s EmergenceKit-<ver>-x64.msi -o <out>`), or a
line-of-business MSI app.

| Setting | Value |
| --- | --- |
| Install command | `msiexec /i EmergenceKit-<ver>-x64.msi /qn /norestart /l*v %ProgramData%\Ecomsp\install.log` |
| Uninstall command | `msiexec /x {product code} /qn /norestart` (Intune fills in the product code for MSI apps) |
| Install behaviour | System |
| Restart | None required (a new PATH entry applies to newly opened terminals) |
| Requirements | Windows 10 21H2+ or Windows 11, 64-bit |
| Detection rule | MSI product code, **or** registry `HKLM\Software\Ecomsp\EmergenceKit` value `Version` equals `<ver>` |
| Dependency | The ffmpeg app from step 1 |

**Upgrades:** each MSI replaces the previous version in place (a fixed upgrade code, with
major upgrades). Assign the new version and supersede the old one.

## 3. Rollout rings (ITIL deployment management)

| Ring | Who | When | Exit criteria before the next ring |
| --- | --- | --- | --- |
| 0: IT test | 1–2 IT devices | Day 0 | Installs silently; `emergence-kit --version` runs; fake-card pipeline passes |
| 1: Pilot | 3–5 reviewers at one subsidiary | Day 1–7 | No P1/P2 incidents; reviewers complete a real survey |
| 2: Broad | Everyone in scope | After ring 1 sign-off | Change record closed |

Raise a change record for each ring, using the release notes and the MSI's SHA-256
(published beside it) as the configuration item reference.

## 4. Verify on a device

```powershell
emergence-kit --version
emergence-kit scan "D:\" --out $env:TEMP\manifest.json    # any camera card
```

## Support

Report problems through your normal service desk route. Include `emergence-kit --version`,
the install log and the command that failed.
