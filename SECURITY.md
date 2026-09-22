# Security policy

## Reporting a vulnerability

Please report security problems **privately**. Don't open a public issue.

- Use GitHub's **"Report a vulnerability"** button on this repository's Security tab
  (private vulnerability reporting), or
- email **security@[company domain]** [EDIT].

Include what you found, how to reproduce it, and the version (`emergence-kit --version`,
or the commit). We'll credit you in the release notes unless you'd rather we didn't.

## Response targets

Set on ITIL incident priorities. Severity uses CVSS v3.1 where a score exists.

| Severity | Acknowledge | Triage and plan | Fix released |
| --- | --- | --- | --- |
| Critical (9.0–10) | 1 business day | 2 business days | 7 days |
| High (7.0–8.9) | 2 business days | 5 business days | 14 days |
| Medium (4.0–6.9) | 5 business days | 10 business days | Next scheduled release |
| Low (< 4.0) | 10 business days | As capacity allows | As capacity allows |

The 14-day target for high and critical matches the Cyber Essentials patching rule.
Customers are told about fixes through release notes, and directly for critical issues.

## Supported versions

Only the latest minor release gets security fixes. Deploy new releases through the
pilot, then broad, rings described in `docs/deploy/INTUNE.md`.

## How the code is protected

| Control | Where |
| --- | --- |
| Clean-room check: no client data, secrets or Discord links in the repository | `scripts/check_provenance.py`, run in CI |
| Known-vulnerability scans of Python and website dependencies, weekly and on every change | `.github/workflows/security.yml` |
| Software bill of materials for every build and release | Security workflow; `EmergenceKit-<ver>-sbom.cdx.json` beside each MSI |
| Automated dependency updates as reviewable pull requests | `.github/dependabot.yml` |
| Secrets in Azure Key Vault, loaded with managed identity; the API fails closed | `brain/secrets.py` |
| Microsoft Entra ID sign-in, app roles, per-tenant data separation | `brain/identity.py`, `docs/IDENTITY.md` |
| Tamper-evident audit trails for survey evidence and data access | `docs/AUDIT.md` |
| Originals never modified; SHA-256 chain of custody for survey files | `emergence-kit prep` / `verify` |
| Protected-species locations never written to outputs or served publicly | `brain/sensitivity.py`, `emergence_kit/report.py` |

## Repository settings (owner: check these are on)

In GitHub, go to **Settings → Code security**: turn on **Secret scanning** and **Push
protection**, **Dependabot alerts**, and **Private vulnerability reporting**.
