# Identity: Microsoft Entra ID sign-in, roles and tenant isolation

Roadmap phase 3. The knowledge base API (`brain`) accepts Microsoft Entra ID tokens, so
every call is made by a named person or a named service, with roles each customer's IT
controls in their own admin centre. The shared API key remains for development only,
and it can only ever read.

## How it fits a PE-backed group

Acquired consultancies usually keep their own Microsoft 365 tenant. So the API is
registered as a **multi-tenant** app, and each subsidiary's tenant:

- is added to `ENTRA_ALLOWED_TENANTS`, the allow-list; any other tenant is refused
  before the API does anything else
- grants admin consent once
- assigns roles to its own staff, using its own groups
- gets **its own database** (`BRAIN_TENANT_DB_DIR/<tenant-id>.db`), so one subsidiary can
  never see another's support history or survey data

If the group runs a single tenant, list that one tenant; nothing else changes.

## Roles

Roles are Entra **app roles** on the API's app registration. Assign them to security
groups, not to individuals.

| Role (value) | Who | Can |
| --- | --- | --- |
| `Brain.Read` | Support staff, ecologists, Foundry agents | Search and read the knowledge base |
| `Survey.Review` | Reviewers | Review footage and write review logs (survey service, phase 6) |
| `Survey.QA` | Senior ecologists | Second-reviewer QA sign-off (phase 4) |
| `Platform.Admin` | Customer IT admins | Everything above, plus administration |

The shared key is treated as `Brain.Read` only: it identifies no person, so it gets no
review, QA or admin rights.

## Set-up (about 20 minutes, in the Microsoft Entra admin centre)

1. **App registrations → New registration:** name "Ecomsp Brain API". For supported
   account types, choose multi-tenant, or single tenant if the group has one tenant.
2. **Expose an API:** set the Application ID URI to `api://ecomsp-brain` (or accept the
   default `api://<client-id>`). That value is `ENTRA_AUDIENCE`.
3. **App roles → Create app role**, four times, using the values in the table above.
   Allowed member types: *Users/Groups* and *Applications* for `Brain.Read`; *Users/Groups*
   for the rest.
4. **Manifest:** set `"requestedAccessTokenVersion": 2`.
5. **In each subsidiary tenant:** grant admin consent, then go to **Enterprise
   applications → Ecomsp Brain API → Users and groups** and assign groups to roles.
6. **On the server:** set these in `.env`:
   ```
   BRAIN_AUTH=entra
   ENTRA_AUDIENCE=api://ecomsp-brain
   ENTRA_ALLOWED_TENANTS=<tenant-id-1>,<tenant-id-2>
   BRAIN_TENANT_DB_DIR=/opt/ecomsp-brain/data/tenants
   ```
   Use `BRAIN_AUTH=key+entra` while you move existing callers across.
7. **Check it:** `GET /whoami` with a token returns the tenant, the name and the roles the
   API sees.

## Foundry agents without a shared key

Give the Foundry project's managed identity the `Brain.Read` app role. Application
roles are assigned with Microsoft Graph, for example via PowerShell
`New-MgServicePrincipalAppRoleAssignment`. Then in each agent spec switch the tool to:

```yaml
    auth: managed_identity
    audience: api://ecomsp-brain
```

and re-run `python -m brain.cli foundry apply agents/`.

## What is checked on every call

The token's signature is checked against Microsoft's published keys (cached, and
refreshed when Microsoft rotates them). The API also checks the expiry (with 60 seconds'
leeway), the audience, the issuer, that the tenant is allowed and that the algorithm is
RS256. Tokens claiming no algorithm are refused. The tests in `tests/test_identity.py`
cover each of these with forged, expired and misdirected tokens.

## Desktop tools

The Emergence Review Kit runs on the user's own Entra-joined laptop and records the
signed-in Windows account (the Entra user) in its audit trail (phase 4). Roles are
enforced where the data is shared: on the **survey hub** (phase 6, `docs/HUB.md`),
because a local tool can't enforce rules against its own user. The hub accepts Entra
tokens only.

For the kit to get a token for the hub, the app registration needs an API scope, with
the Azure CLI pre-authorised as a client:

1. **Expose an API:** add the scope `access_as_user` (who can consent: admins and users).
2. **Authorized client applications:** add `04b07795-8ddb-461a-bbee-02f9e1bf7b46` (the
   Azure CLI), ticked for that scope.
3. **Manifest:** `requestedAccessTokenVersion: 2`.

Users then run `az login` once, and the kit calls `az account get-access-token --scope
api://<client id>/access_as_user`. The token carries their app roles. A packaged sign-in
(MSAL, no Azure CLI needed) is planned for the signed release.
