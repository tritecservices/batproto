# Cyber Essentials readiness

Cyber Essentials (CE) is the UK government-backed baseline that public-sector buyers,
and increasingly PE-backed groups, ask suppliers for. CE is a self-assessment verified
by a certification body. **CE Plus** adds hands-on technical testing by an assessor.
Certificates last 12 months. Check the current requirements document from NCSC/IASME
before you apply: the questions are revised periodically.

Scope: the organisation that sells and operates the product. Include every device and
cloud service that handles the product or customer data, **unless it is segregated out
of scope**.

## The five controls, against this estate

| Control | Requirement (summary) | Current estate | Action |
| --- | --- | --- | --- |
| **Firewalls** | Boundary firewall on every internet-connected device; default admin passwords changed; no unnecessary inbound services | Debian server behind a router; API reached through a Cloudflare tunnel (no open inbound ports) | Enable `ufw` on Debian (deny incoming; allow SSH from the LAN only); check both routers' admin passwords and disable UPnP |
| **Secure configuration** | Remove unused software and accounts; no default passwords; auto-run off; unlock with PIN/password | Debian and laptop are new builds | Run the preflight checks; remove unused packages; laptop screen lock |
| **User access control** | Unique accounts; admin rights only for admin tasks, on separate accounts; **MFA on all cloud services** | Azure, GitHub, Netlify, Cloudflare, Microsoft 365 | Turn on MFA on every one; separate admin account on the laptop; the `brain` service user on Debian has no login shell |
| **Malware protection** | Anti-malware on in-scope Windows/macOS, or application allow-listing | Windows laptop (Defender) | Defender on and updating; Linux server: allow-listing via the package manager and no untrusted binaries |
| **Security update management** | Supported, licensed software only; **high and critical updates within 14 days**; unsupported software removed or out of scope | Debian 13 (supported); Windows 11; **Mac mini on macOS Catalina (unsupported)** | See the Mac mini finding below; `unattended-upgrades` on Debian; winget/Intune updates on laptops |

## Finding: Mac mini on macOS Catalina

Apple no longer ships security updates for Catalina, so an in-scope device running it
**fails** the update management control. Options, best first:

1. **Replace** the NAS with a supported device, or move its storage onto the Debian
   server.
2. **Segregate it out of scope:** its own network segment with no internet access,
   reachable only from the Debian server over SMB. The earlier two-router design
   already points this way. Document the segregation for the assessor.
3. Unofficial macOS patchers don't help: the OS is still unsupported by its vendor.

## Evidence to keep for the assessor

- A device and cloud-service inventory (a CMDB extract, for ITIL service configuration
  management)
- Firewall rule exports; screenshots showing MFA enabled on each cloud service
- Patch reports (Debian `unattended-upgrades` logs; Windows update history)
- The admin account list

## Cyber Essentials Plus

Adds an external vulnerability scan, internal device checks and malware-delivery tests.
Do it after CE, once the Debian server is in production shape (roadmap phase 6).
