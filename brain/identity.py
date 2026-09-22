"""Identity for the brain API: Microsoft Entra ID sign-in, app roles, tenant isolation.

Modes (BRAIN_AUTH):
  key          shared X-API-Key only (the original behaviour; development, single site)
  entra        Microsoft Entra ID bearer tokens only (enterprise)
  key+entra    either; for migrating callers one at a time

Entra settings:
  ENTRA_AUDIENCE          the API's Application ID URI or client id, e.g. api://ecomsp-brain
  ENTRA_ALLOWED_TENANTS   comma-separated tenant ids allowed in (one per subsidiary)
  BRAIN_TENANT_DB_DIR     if set, each tenant gets its own database: <dir>/<tenant-id>.db
                          (tenant isolation: one subsidiary can never read another's data)

Roles are Entra *app roles* on the API's app registration, assigned to users, groups or
managed identities (e.g. a Foundry project) in each tenant's admin centre:

  Brain.Read       search and read the knowledge base
  Survey.Review    review footage and write review logs (used by the survey service)
  Survey.QA        second-reviewer QA sign-off
  Platform.Admin   administration; implies every other role

Tokens are verified against Microsoft's published signing keys (JWKS, cached), and
checked for signature, expiry, audience, issuer and an allowed tenant. Nothing about
a caller is trusted until that has passed.
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field

ROLES = ("Brain.Read", "Survey.Review", "Survey.QA", "Platform.Admin")
JWKS_URL = "https://login.microsoftonline.com/{tenant}/discovery/v2.0/keys"
JWKS_TTL_S = 24 * 3600


class AuthError(Exception):
    """status 401 = who are you? (missing/invalid credentials); 403 = not allowed."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass(frozen=True)
class Principal:
    kind: str                      # key | user | app
    tenant_id: str                 # "default" for key auth
    subject: str                   # Entra object id, or "api-key"
    name: str | None = None        # UPN / preferred_username / app name
    roles: frozenset = field(default_factory=frozenset)

    def has(self, role: str) -> bool:
        return "Platform.Admin" in self.roles or role in self.roles

    def audit(self) -> dict:
        """What gets written to audit logs: who, in which tenant, as what."""
        return {"kind": self.kind, "tenant": self.tenant_id, "subject": self.subject,
                "name": self.name, "roles": sorted(self.roles)}


@dataclass
class EntraConfig:
    audience: str
    allowed_tenants: frozenset

    @classmethod
    def from_env(cls) -> "EntraConfig":
        aud = os.environ.get("ENTRA_AUDIENCE", "").strip()
        tenants = frozenset(t.strip().lower() for t in
                            os.environ.get("ENTRA_ALLOWED_TENANTS", "").split(",") if t.strip())
        if not aud or not tenants:
            raise AuthError(503, "Entra sign-in is enabled but ENTRA_AUDIENCE / "
                                 "ENTRA_ALLOWED_TENANTS are not configured")
        return cls(aud, tenants)


def auth_mode() -> str:
    mode = os.environ.get("BRAIN_AUTH", "key").strip().lower()
    return mode if mode in ("key", "entra", "key+entra") else "key"


# ------------------------------------------------------------------ JWKS
class JwksCache:
    """Signing keys per tenant, refreshed daily or when an unknown key id appears
    (Microsoft rotates keys; a stale cache must not lock everyone out)."""

    def __init__(self, fetch=None):
        self._fetch = fetch or self._http_fetch
        self._keys: dict[str, tuple[float, dict]] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _http_fetch(tenant: str) -> dict:
        import requests
        r = requests.get(JWKS_URL.format(tenant=tenant), timeout=10)
        r.raise_for_status()
        return r.json()

    def key(self, tenant: str, kid: str):
        from jwt.algorithms import RSAAlgorithm

        with self._lock:
            cached = self._keys.get(tenant)
            fresh = cached and time.time() - cached[0] < JWKS_TTL_S
            keys = cached[1] if fresh else None
            if keys is None or kid not in keys:
                data = self._fetch(tenant)
                keys = {k["kid"]: k for k in data.get("keys", []) if "kid" in k}
                self._keys[tenant] = (time.time(), keys)
        jwk = keys.get(kid)
        if not jwk:
            raise AuthError(401, "token signed with an unknown key")
        return RSAAlgorithm.from_jwk(json.dumps(jwk))


_jwks = JwksCache()


def verify_entra(token: str, cfg: EntraConfig, jwks: JwksCache | None = None) -> Principal:
    import jwt

    jwks = jwks or _jwks
    try:
        header = jwt.get_unverified_header(token)
        unverified = jwt.decode(token, options={"verify_signature": False})
    except jwt.PyJWTError:
        raise AuthError(401, "malformed token") from None
    tid = str(unverified.get("tid", "")).lower()
    if not tid:
        raise AuthError(401, "token has no tenant")
    if tid not in cfg.allowed_tenants:
        # checked before fetching keys, so unknown tenants can't make us fetch anything
        raise AuthError(403, "tenant not allowed")
    if header.get("alg") != "RS256":
        raise AuthError(401, "unexpected token algorithm")
    key = jwks.key(tid, header.get("kid", ""))
    issuers = [f"https://login.microsoftonline.com/{tid}/v2.0", f"https://sts.windows.net/{tid}/"]
    audiences = [cfg.audience]
    if cfg.audience.startswith("api://"):
        audiences.append(cfg.audience[len("api://"):])     # v2 tokens carry the bare id
    try:
        claims = jwt.decode(token, key, algorithms=["RS256"], audience=audiences,
                            options={"require": ["exp", "iat", "aud", "iss"]}, leeway=60)
        # issuer checked here rather than via PyJWT's `issuer=` list, which older
        # PyJWT releases (< 2.8) compare as a single string
        if claims.get("iss") not in issuers:
            raise jwt.InvalidIssuerError("issuer mismatch")
    except jwt.ExpiredSignatureError:
        raise AuthError(401, "token expired") from None
    except jwt.InvalidAudienceError:
        raise AuthError(401, "token is for a different API") from None
    except jwt.InvalidIssuerError:
        raise AuthError(401, "token issuer not recognised") from None
    except jwt.PyJWTError as exc:
        raise AuthError(401, f"invalid token: {exc.__class__.__name__}") from None

    roles = frozenset(r for r in claims.get("roles", []) if r in ROLES)
    is_app = claims.get("idtyp") == "app" or ("scp" not in claims and "upn" not in claims
                                              and "preferred_username" not in claims)
    return Principal(
        kind="app" if is_app else "user",
        tenant_id=tid,
        subject=str(claims.get("oid") or claims.get("sub")),
        name=claims.get("preferred_username") or claims.get("upn") or claims.get("app_displayname")
             or claims.get("azp"),
        roles=roles,
    )


def verify_key(presented: str | None) -> Principal:
    import hmac
    expected = os.environ.get("BRAIN_API_KEY", "")
    if expected.strip() in ("", "change-me", "changeme"):
        if os.environ.get("BRAIN_ALLOW_NO_KEY") == "1":
            return Principal("key", "default", "api-key", "unauthenticated-dev",
                             frozenset({"Brain.Read"}))
        raise AuthError(503, "server has no BRAIN_API_KEY configured")
    if not presented or not hmac.compare_digest(presented, expected):
        raise AuthError(401, "bad or missing X-API-Key")
    # a shared key can only ever read: it identifies no person, so it gets no
    # review, QA or admin rights
    return Principal("key", "default", "api-key", "shared-key", frozenset({"Brain.Read"}))


def authenticate(authorization: str | None, api_key: str | None) -> Principal:
    mode = auth_mode()
    bearer = None
    if authorization and authorization.lower().startswith("bearer "):
        bearer = authorization[7:].strip()
    if bearer and mode in ("entra", "key+entra"):
        return verify_entra(bearer, EntraConfig.from_env())
    if mode in ("key", "key+entra"):
        return verify_key(api_key)
    raise AuthError(401, "sign in with Microsoft Entra ID (Authorization: Bearer ...)")


def require(principal: Principal, role: str) -> Principal:
    if not principal.has(role):
        raise AuthError(403, f"requires the {role} role")
    return principal


def tenant_db_path(principal: Principal, default: str) -> str:
    """Tenant isolation: each Entra tenant reads and writes only its own database."""
    base = os.environ.get("BRAIN_TENANT_DB_DIR")
    if not base or principal.kind == "key":
        return default
    safe = "".join(c for c in principal.tenant_id if c.isalnum() or c == "-")
    if not safe:
        raise AuthError(401, "invalid tenant id")
    return os.path.join(base, f"{safe}.db")
