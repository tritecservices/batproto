# Hosting and data residency

Buyer question: "Where is our data, and does any of it leave the UK?" This page answers
it per component. Commitment: **customer survey data and knowledge bases stay in the
UK.** Exceptions are listed, not hidden.

| Component | Where it runs | Customer data in it | Residency |
| --- | --- | --- | --- |
| Emergence Review Kit | The customer's own Windows laptops | Survey video, logs, audit | Wherever the customer's devices are |
| Knowledge base API and database | Customer or our server in the UK (Debian now; Azure **UK South** in phase 6) | Support history, manuals, per-tenant databases, access audit | UK |
| Secrets | Azure Key Vault, **UK South** | Keys and tokens only | UK |
| Foundry agents | Azure AI Foundry project in **UK South** | Questions, retrieved excerpts, drafts | UK **if** the model uses a regional (Standard) deployment in UK South. "Global" deployment types may process prompts in any Azure region. Check the model's availability in UK South before committing. |
| Public website | Netlify (global CDN) | Contact form entries only | **Outside the UK** (Netlify is US-based). Check Netlify's DPA and transfer mechanism; keep survey data off the site (the form says so) |
| Public demo search | Knowledge base API via Cloudflare | Public forum content only (enforced in code) | Traffic passes through Cloudflare's global network; no customer data |
| Cloudflare tunnel and Access | Cloudflare edge | Encrypted traffic to the API | TLS ends at Cloudflare's edge, which may be outside the UK. For strict residency, use Azure Front Door or App Gateway in UK South instead (phase 6) |
| Source code and CI | GitHub (US) | **None:** enforced by the provenance check | N/A |
| Discord | Discord (US) | Messages already live there; only authorised servers are archived | The archive copy is stored in the UK |

## Penetration test scope (for phase 5 sign-off)

| In scope | Notes |
| --- | --- |
| Knowledge base API, as exposed through the tunnel | Authentication bypass, tenant isolation, injection, the public demo endpoint's limits |
| Entra ID integration | Token validation, role enforcement, tenant allow-list |
| Emergence Review Kit installer and binaries | Install permissions, DLL search-order issues, update path |
| Debian server (authenticated scan) | Supporting evidence for Cyber Essentials Plus |

Use a CREST- or CHECK-accredited tester. Retest after the fixes, and keep the summary
letter for due diligence.
