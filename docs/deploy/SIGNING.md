# Code signing

Unsigned installers work, but Windows SmartScreen warns about them, and many enterprise
policies (WDAC, AppLocker, Intune security baselines) block them outright. Signing is
required before the first customer deployment.

**Recommended: Azure Trusted Signing.** It's Microsoft's managed signing service: the
certificate lives in Azure, with no hardware token or key file to lose, and it fits the
Azure tenant you already have. It needs identity validation of the publishing
organisation, so it depends on phase 1: sign as the legal entity that owns the IP.

Steps, once the entity is settled:

1. In Azure, create a Trusted Signing account and a certificate profile (public trust),
   in the UK South region.
2. Complete organisation identity validation.
3. Create an app registration for GitHub Actions, and give it the "Trusted Signing
   Certificate Profile Signer" role on the profile.
4. Add its tenant ID, client ID and secret as GitHub Actions secrets. Better still, use
   OIDC federated credentials, so there's no secret at all.
5. Uncomment the signing step in `.github/workflows/release.yml`. Sign the executable
   before `wix build`, and the MSI after it.

Check the current pricing and eligibility on Microsoft's Trusted Signing page before
buying.
