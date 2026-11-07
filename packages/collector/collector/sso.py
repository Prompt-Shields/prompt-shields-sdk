"""OIDC single sign-on for the tenant admin API (#339).

  GET /api/v1/sso/{tenant_id}/login   start: 302 to the tenant's identity
                                      provider (authorization code flow
                                      with PKCE S256, state and nonce)
  GET /api/v1/sso/callback            finish: exchange the code, verify the
                                      ID token, return a short-lived admin
                                      session token for the principal

Both routes are unauthenticated by design (the IdP authenticates the user).
Configuration (issuer, client, redirect URI, algorithms, session lifetime)
is per tenant: ``PUT /api/v1/admin/sso/oidc`` (collector/sso_admin.py).

Flow and checks
---------------
``login`` creates a single-use ``oidc_login_states`` row (state and a
browser binding by SHA-256, nonce, PKCE verifier; ten minutes) and sets the
browser binding as an HttpOnly, Secure, SameSite=Lax cookie scoped to
/api/v1/sso, so a callback only completes in the browser that started the
login (no login CSRF). At most MAX_PENDING_LOGINS unexpired logins per
tenant (429 past that).

``callback`` deletes the state row first (a replayed callback finds
nothing), checks the cookie, exchanges the code at the IdP's token endpoint
with the PKCE verifier (and the client secret, HTTP Basic, for a
confidential client), then verifies the ID token (collector/oidc.py:
asymmetric signature against the issuer's JWKS with caching and key
rotation; issuer, audience / azp, exp, nbf, iat, nonce). Every IdP request
goes through the SSRF-safe egress client.

The ID token's subject (``sub``, or the tenant's ``subject_claim``, e.g.
``oid`` for Microsoft Entra ID) must equal the ``external_id`` of an enabled
user principal (provisioned by SCIM, collector/scim.py, or the admin API).
There is no just-in-time provisioning: an unknown subject is refused.

The session is an admin token (``ps-at-...``) issued for that principal
(``created_by: sso``) holding the admin scopes its tenant-wide roles grant,
for ``session_ttl_seconds`` (default one hour). Like every token issued for
a principal it reaches at most what the roles grant at request time, and
stops working the moment the principal is disabled; SCIM deactivation also
revokes it (collector/idp_sync.py). A principal with no admin scope gets no
session.

Audit: ``sso.login`` on the tenant's audit trail, success (actor: the
principal; target: the session token) and failure (reason), never an email
or token.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta
from uuid import UUID

import httpx
from db.models import OidcLoginState, Principal, Tenant, TenantOidcConfig
from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import delete, func, select

from collector import audit_trail, idp_sync, oidc, rbac, rls, secret_keyring
from collector.admin_auth import ADMIN_SCOPES, TOKEN_WARNING, issue_token

router = APIRouter(prefix="/api/v1/sso", tags=["SSO"])

LOGIN_TTL = timedelta(minutes=10)
MAX_PENDING_LOGINS = 500
COOKIE = "ps_sso_binding"
COOKIE_PATH = "/api/v1/sso"
CLIENT_SECRET_INFO = b"prompt-shields/oidc-client-secret/v1"


def _problem(status: int, title: str, detail: str) -> JSONResponse:
    return JSONResponse(
        {"type": "about:blank", "title": title, "status": status, "detail": detail},
        status_code=status,
        media_type="application/problem+json",
        headers={"Cache-Control": "no-store"},
    )


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _now() -> datetime:
    return datetime.now(UTC)


def secret_binding(tenant_id: UUID, config_id: UUID) -> bytes:
    return tenant_id.bytes + config_id.bytes


def open_client_secret(cfg: TenantOidcConfig) -> str | None:
    if cfg.client_secret_ciphertext is None:
        return None
    return secret_keyring.unseal(
        cfg.client_secret_ciphertext, secret_binding(cfg.tenant_id, cfg.id), CLIENT_SECRET_INFO
    )


def expected_audience(cfg: TenantOidcConfig) -> str:
    return cfg.audience or cfg.client_id


async def _config(db, tenant_id: UUID) -> TenantOidcConfig | None:
    """The tenant's enabled OIDC config, if the tenant exists and is enabled.
    Scopes the session to the tenant."""
    await rls.set_tenant(db, tenant_id)
    row = (
        await db.execute(
            select(TenantOidcConfig, Tenant.disabled_at)
            .join(Tenant, Tenant.id == TenantOidcConfig.tenant_id)
            .where(TenantOidcConfig.tenant_id == tenant_id)
        )
    ).one_or_none()
    if row is None or row[1] is not None or not row[0].enabled:
        return None
    return row[0]


async def _audit_failure(request: Request, tenant_id: UUID, reason: str, status: int, **details) -> None:
    db = request.state.db
    await db.rollback()
    await audit_trail.record_committed(
        db,
        tenant_id=tenant_id,
        action="sso.login",
        outcome=audit_trail.FAILURE,
        actor=audit_trail.Actor("anonymous", None),
        status_code=status,
        details={"reason": reason, **details},
    )


@router.get("/{tenant_id}/login", summary="Start an admin API SSO login")
async def login(tenant_id: UUID, request: Request):
    db = request.state.db
    cfg = await _config(db, tenant_id)
    if cfg is None:
        return _problem(404, "Not Found", "SSO is not configured for this tenant")
    now = _now()
    await db.execute(
        delete(OidcLoginState).where(
            OidcLoginState.tenant_id == tenant_id, OidcLoginState.expires_at <= now
        )
    )
    pending = (
        await db.execute(
            select(func.count())
            .select_from(OidcLoginState)
            .where(OidcLoginState.tenant_id == tenant_id)
        )
    ).scalar_one()
    if pending >= MAX_PENDING_LOGINS:
        await db.commit()
        return _problem(429, "Too Many Requests", "Too many logins in progress; retry shortly")
    try:
        disc = await oidc.cache.discovery(cfg.issuer)
    except oidc.IdpError:
        await db.commit()
        return _problem(502, "Bad Gateway", "The identity provider could not be reached")
    state = secrets.token_urlsafe(32)
    binding = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(48)
    db.add(
        OidcLoginState(
            tenant_id=tenant_id,
            state_hash=_sha256(state),
            browser_hash=_sha256(binding),
            nonce=nonce,
            code_verifier=verifier,
            created_at=now,
            expires_at=now + LOGIN_TTL,
        )
    )
    client_id, redirect_uri = cfg.client_id, cfg.redirect_uri
    await db.commit()
    url = httpx.URL(disc.authorization_endpoint).copy_merge_params(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": "openid email profile",
            "state": state,
            "nonce": nonce,
            "code_challenge": oidc.pkce_challenge(verifier),
            "code_challenge_method": "S256",
        }
    )
    resp = RedirectResponse(str(url), status_code=302)
    resp.headers["Cache-Control"] = "no-store"
    resp.set_cookie(
        COOKIE,
        binding,
        max_age=int(LOGIN_TTL.total_seconds()),
        path=COOKIE_PATH,
        secure=True,
        httponly=True,
        samesite="lax",
    )
    return resp


@router.get("/callback", summary="Finish an admin API SSO login")
async def callback(
    request: Request,
    state: str | None = Query(default=None, max_length=512),
    code: str | None = Query(default=None, max_length=4096),
    error: str | None = Query(default=None, max_length=256),
):
    db = request.state.db
    if not state:
        return _problem(400, "Bad Request", "Missing state")
    async with rls.auth_lookup(db):
        row = (
            await db.execute(
                select(
                    OidcLoginState.id,
                    OidcLoginState.tenant_id,
                    OidcLoginState.browser_hash,
                    OidcLoginState.nonce,
                    OidcLoginState.code_verifier,
                    OidcLoginState.expires_at,
                ).where(OidcLoginState.state_hash == _sha256(state))
            )
        ).one_or_none()
        if row is not None:
            # Single use, whatever happens next.
            await db.execute(delete(OidcLoginState).where(OidcLoginState.id == row.id))
            await db.commit()
    if row is None or row.expires_at <= _now():
        return _problem(400, "Bad Request", "Unknown or expired login; start again")
    tenant_id = row.tenant_id
    await rls.set_tenant(db, tenant_id)
    binding = request.cookies.get(COOKIE) or ""
    if not hmac.compare_digest(_sha256(binding), row.browser_hash):
        await _audit_failure(request, tenant_id, "browser_mismatch", 400)
        return _problem(400, "Bad Request", "This login was started in another browser")
    if error or not code:
        await _audit_failure(request, tenant_id, "idp_error", 400, idp_error=error or "no_code")
        return _problem(400, "Bad Request", "The identity provider did not return a code")
    cfg = await _config(db, tenant_id)
    if cfg is None:
        return _problem(404, "Not Found", "SSO is not configured for this tenant")
    try:
        disc = await oidc.cache.discovery(cfg.issuer)
        secret = open_client_secret(cfg)
        form = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": cfg.redirect_uri,
            "code_verifier": row.code_verifier,
        }
        if secret is None:
            form["client_id"] = cfg.client_id
        tokens = await oidc.fetcher.post_form(
            disc.token_endpoint, form, (cfg.client_id, secret) if secret is not None else None
        )
    except (oidc.IdpError, ValueError, secret_keyring.KeyringNotConfigured):
        await _audit_failure(request, tenant_id, "token_exchange_failed", 502)
        return _problem(502, "Bad Gateway", "The authorization code could not be exchanged")
    id_token = tokens.get("id_token")
    if not isinstance(id_token, str):
        await _audit_failure(request, tenant_id, "no_id_token", 502)
        return _problem(502, "Bad Gateway", "The identity provider returned no ID token")
    expected = oidc.Expected(
        issuer=cfg.issuer,
        audience=expected_audience(cfg),
        client_id=cfg.client_id,
        algorithms=tuple(cfg.algorithms or oidc.DEFAULT_ALGORITHMS),
        nonce=row.nonce,
    )
    try:
        claims = await oidc.verify_id_token(id_token, disc.jwks_uri, expected)
    except oidc.TokenError as exc:
        await _audit_failure(request, tenant_id, "invalid_id_token", 401, token_error=exc.reason)
        return _problem(401, "Unauthorized", f"ID token refused: {exc.reason}")
    subject = claims.get(cfg.subject_claim)
    if not isinstance(subject, str) or not subject or len(subject) > 255:
        await _audit_failure(request, tenant_id, "no_subject", 401)
        return _problem(401, "Unauthorized", f"ID token has no {cfg.subject_claim} claim")
    principal = (
        await db.execute(
            select(Principal).where(
                Principal.tenant_id == tenant_id,
                Principal.external_id == subject,
                Principal.kind == "user",
            )
        )
    ).scalar_one_or_none()
    if principal is None:
        await _audit_failure(request, tenant_id, "unknown_principal", 403)
        return _problem(
            403,
            "Forbidden",
            "No user principal has this identity; provision it through SCIM or the admin API",
        )
    try:
        roles = await rbac.tenant_roles(db, tenant_id, principal.id)
    except rbac.PrincipalDisabled:
        await _audit_failure(
            request, tenant_id, "principal_disabled", 403, principal_id=str(principal.id)
        )
        return _problem(403, "Forbidden", "This principal is disabled")
    scopes = rbac.ordered(rbac.role_scopes(roles) & set(ADMIN_SCOPES), ADMIN_SCOPES)
    if not scopes:
        await _audit_failure(
            request, tenant_id, "no_admin_roles", 403, principal_id=str(principal.id)
        )
        return _problem(403, "Forbidden", "This principal's roles grant no admin API scope")
    now = _now()
    ttl = cfg.session_ttl_seconds
    expires_at = now + timedelta(seconds=ttl)
    token_row, token = await issue_token(
        db,
        tenant_id,
        name=f"sso-{secrets.token_hex(8)}",
        scopes=scopes,
        expires_at=expires_at,
        created_by=idp_sync.SSO_CREATED_BY,
        principal_id=principal.id,
    )
    token_id, principal_id = str(token_row.id), str(principal.id)
    await audit_trail.record(
        db,
        tenant_id=tenant_id,
        action="sso.login",
        actor=audit_trail.Actor("principal", str(principal.id)),
        target_type="admin_token",
        target_id=token_row.id,
        status_code=200,
        details={
            "principal_id": str(principal.id),
            "roles": sorted(roles),
            "scopes": scopes,
            "expires_at": expires_at.isoformat(),
        },
    )
    await db.commit()
    resp = JSONResponse(
        {
            "token": token,
            "token_type": "Bearer",
            "token_id": token_id,
            "expires_at": expires_at.isoformat(),
            "expires_in": ttl,
            "principal_id": principal_id,
            "roles": sorted(roles),
            "scopes": scopes,
            "warning": TOKEN_WARNING,
        },
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
    )
    resp.delete_cookie(COOKIE, path=COOKIE_PATH, secure=True, httponly=True, samesite="lax")
    return resp
