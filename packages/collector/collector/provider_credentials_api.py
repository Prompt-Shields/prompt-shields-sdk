"""HTTP routes for provider admin credentials and key fingerprint links (#93).
Logic: collector.provider_credentials.

Tenant admin (tenant API key, or an admin token with scope
``admin:provider-credentials``; collector/admin_auth.py), under
/api/v1/admin; Partner API, under /partner/v1, scope
``provider-credentials:manage``. Same routes on both:

  GET    /provider-credentials                     list (masked)
  GET    /provider-credentials/{provider}          read (masked)
  POST   /provider-credentials/{provider}          store {"admin_key": ...}
  POST   /provider-credentials/{provider}/rotate   replace {"admin_key": ...}
  DELETE /provider-credentials/{provider}          delete

  GET    /provider-key-fingerprints                list links
  PUT    /provider-key-fingerprints/{provider}/{api_key_id}
                                                   {"api_key_fingerprint": ...}
  DELETE /provider-key-fingerprints/{provider}/{api_key_id}

The admin key is write-only: no response, log line or audit row carries
it. Responses show ``key_hint`` (last four characters), ``key_id`` (the
keyring key it is encrypted under) and timestamps. Invalid keys are
refused with a message that does not echo them. Every call writes a
``partner_audit_log`` row (path and status, never the body).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict

from collector import provider_credentials as pc
from collector.admin_auth import AdminPrincipal, require_admin
from collector.audit import record_read
from collector.partner_auth import PartnerPrincipal, require_scope
from collector.partner_registry import _audit, _enforce_rate_limit
from collector.provider_usage import ProviderUsageSettings, admin_keys_for_tenant

SCOPE = "provider-credentials:manage"
_SCOPE = Depends(require_scope(SCOPE))
_ADMIN = Depends(require_admin("admin:provider-credentials"))

Provider = Literal["openai", "anthropic"]

# Names a 422 may show in an error location; anything else (an unknown
# body field's name could itself be a pasted key) is shown as "*".
_SAFE_LOC = frozenset(
    {
        "body",
        "path",
        "query",
        "header",
        "admin_key",
        "api_key_fingerprint",
        "provider",
        "api_key_id",
    }
)


class _RedactedValidationRoute(APIRoute):
    """Request validation errors without the submitted values.

    FastAPI's 422 body echoes each failing ``input`` (pydantic's
    ``hide_input_in_errors`` does not reach it), so a key sent with the
    wrong type or under a misspelt field name would come back in the
    response. Keep type, location and message; drop ``input`` and ``ctx``.
    """

    def get_route_handler(self):
        handler = super().get_route_handler()

        async def redacting_handler(request: Request):
            try:
                return await handler(request)
            except RequestValidationError as exc:
                errors = [
                    {
                        "type": err.get("type"),
                        "loc": [
                            part if part in _SAFE_LOC or isinstance(part, int) else "*"
                            for part in err.get("loc", ())
                        ],
                        "msg": err.get("msg"),
                    }
                    for err in exc.errors()
                ]
                raise RequestValidationError(errors) from None

        return redacting_handler


tenant_router = APIRouter(
    prefix="/api/v1/admin", tags=["Admin"], route_class=_RedactedValidationRoute
)
partner_router = APIRouter(
    prefix="/partner/v1", tags=["Partner API"], route_class=_RedactedValidationRoute
)


class AdminKeyBody(BaseModel):
    # No pydantic constraints: a failed constraint echoes the input in the
    # 422 body. Length and charset are checked in validate_admin_key (400).
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    admin_key: str

    def __repr__(self) -> str:  # never print the key
        return "AdminKeyBody(admin_key=***)"

    __str__ = __repr__


class FingerprintBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    api_key_fingerprint: str


def _problem(status: int, title: str, detail: str) -> HTTPException:
    return HTTPException(
        status_code=status,
        detail={
            "type": "about:blank",
            "title": title,
            "status": status,
            "detail": detail,
        },
    )


def _not_found() -> HTTPException:
    return _problem(404, "Not Found", "No stored credential for this provider")


def _not_configured() -> HTTPException:
    return _problem(
        503,
        "Service Unavailable",
        "Credential storage needs PS_WEBHOOK_SECRET_KEYS on this collector",
    )


# ──────────────────────────────────────────────────────────────────────
# Shared handlers: (db, tenant_id, ...) -> (status, body). Commit on success.
# ──────────────────────────────────────────────────────────────────────


async def _list_credentials(db, tenant_id: UUID) -> tuple[int, dict]:
    rows = await pc.list_credentials(db, tenant_id)
    stored = {r.provider for r in rows}
    env = admin_keys_for_tenant(tenant_id, ProviderUsageSettings())
    return 200, {
        "data": [pc.serialize_credential(r) for r in rows],
        "meta": {
            "providers": list(pc.PROVIDERS),
            # Providers the pull would use PS_PROVIDER_ADMIN_KEYS for.
            "env_fallback": sorted(p for p in env if p not in stored),
        },
    }


async def _get_credential(db, tenant_id: UUID, provider: str) -> tuple[int, dict]:
    row = await pc.get_credential(db, tenant_id, provider)
    if row is None:
        raise _not_found()
    return 200, pc.serialize_credential(row)


async def _create_credential(
    db, tenant_id: UUID, provider: str, body: AdminKeyBody
) -> tuple[int, dict]:
    if await pc.get_credential(db, tenant_id, provider) is not None:
        raise _problem(
            409,
            "Conflict",
            "A credential is already stored for this provider; rotate it",
        )
    try:
        row = await pc.create_credential(db, tenant_id, provider, body.admin_key)
    except pc.CredentialsNotConfigured as exc:
        raise _not_configured() from exc
    except ValueError as exc:
        raise _problem(400, "Bad Request", str(exc)) from exc
    out = pc.serialize_credential(row)
    await db.commit()
    return 201, out


async def _rotate_credential(
    db, tenant_id: UUID, provider: str, body: AdminKeyBody
) -> tuple[int, dict]:
    row = await pc.get_credential(db, tenant_id, provider)
    if row is None:
        raise _not_found()
    try:
        pc.rotate_credential(row, body.admin_key)
    except pc.CredentialsNotConfigured as exc:
        raise _not_configured() from exc
    except ValueError as exc:
        raise _problem(400, "Bad Request", str(exc)) from exc
    out = pc.serialize_credential(row)
    await db.commit()
    return 200, out


async def _delete_credential(db, tenant_id: UUID, provider: str) -> tuple[int, None]:
    row = await pc.get_credential(db, tenant_id, provider)
    if row is None:
        raise _not_found()
    await db.delete(row)
    await db.commit()
    return 204, None


async def _list_links(db, tenant_id: UUID) -> tuple[int, dict]:
    return 200, {
        "data": [pc.serialize_link(r) for r in await pc.list_links(db, tenant_id)]
    }


async def _put_link(
    db, tenant_id: UUID, provider: str, api_key_id: str, body: FingerprintBody
) -> tuple[int, dict]:
    try:
        row, created = await pc.put_link(
            db, tenant_id, provider, api_key_id, body.api_key_fingerprint
        )
    except ValueError as exc:
        raise _problem(400, "Bad Request", str(exc)) from exc
    await db.flush()
    corroborated = await pc.corroborate_by_fingerprint(db, tenant_id)
    out = {
        "data": pc.serialize_link(row),
        "meta": {"corroborated_assets": corroborated},
    }
    await db.commit()
    return (201 if created else 200), out


async def _delete_link(
    db, tenant_id: UUID, provider: str, api_key_id: str
) -> tuple[int, None]:
    row = await pc.get_link(db, tenant_id, provider, api_key_id)
    if row is None:
        raise _problem(404, "Not Found", "No fingerprint link for this key id")
    await db.delete(row)
    await db.commit()
    return 204, None


Handler = Callable[..., Awaitable[tuple[int, dict | None]]]


async def _as_tenant(
    request: Request,
    response: Response,
    principal: AdminPrincipal,
    handler: Handler,
    *args,
):
    tenant_id = principal.tenant_id
    db = request.state.db
    try:
        status, body = await handler(db, tenant_id, *args)
    except HTTPException as exc:
        await db.rollback()
        await record_read(request, tenant_id=tenant_id, status_code=exc.status_code)
        raise
    await record_read(request, tenant_id=tenant_id, status_code=status)
    response.status_code = status
    return body


async def _as_partner(
    request: Request,
    response: Response,
    principal: PartnerPrincipal,
    handler: Handler,
    *args,
):
    await _enforce_rate_limit(request, response, principal)
    db = request.state.db
    endpoint, method = request.url.path, request.method
    try:
        status, body = await handler(db, principal.tenant_id, *args)
    except HTTPException as exc:
        await db.rollback()
        await _audit(request, principal, endpoint, method, exc.status_code)
        raise
    await _audit(request, principal, endpoint, method, status)
    response.status_code = status
    return body


# ──────────────────────────────────────────────────────────────────────
# Tenant admin
# ──────────────────────────────────────────────────────────────────────

_CRED = "/provider-credentials"
_LINK = "/provider-key-fingerprints"


@tenant_router.get(_CRED, summary="List stored provider admin credentials (masked)")
async def tenant_list_credentials(
    request: Request, response: Response, principal: AdminPrincipal = _ADMIN
):
    return await _as_tenant(request, response, principal, _list_credentials)


@tenant_router.get(
    _CRED + "/{provider}", summary="Get a stored provider credential (masked)"
)
async def tenant_get_credential(
    provider: Provider,
    request: Request,
    response: Response,
    principal: AdminPrincipal = _ADMIN,
):
    return await _as_tenant(request, response, principal, _get_credential, provider)


@tenant_router.post(
    _CRED + "/{provider}",
    status_code=201,
    summary="Store a provider admin key (write-only)",
)
async def tenant_create_credential(
    provider: Provider,
    body: AdminKeyBody,
    request: Request,
    response: Response,
    principal: AdminPrincipal = _ADMIN,
):
    return await _as_tenant(
        request, response, principal, _create_credential, provider, body
    )


@tenant_router.post(
    _CRED + "/{provider}/rotate", summary="Replace a stored provider admin key"
)
async def tenant_rotate_credential(
    provider: Provider,
    body: AdminKeyBody,
    request: Request,
    response: Response,
    principal: AdminPrincipal = _ADMIN,
):
    return await _as_tenant(
        request, response, principal, _rotate_credential, provider, body
    )


@tenant_router.delete(
    _CRED + "/{provider}", status_code=204, summary="Delete a stored provider admin key"
)
async def tenant_delete_credential(
    provider: Provider,
    request: Request,
    response: Response,
    principal: AdminPrincipal = _ADMIN,
):
    await _as_tenant(request, response, principal, _delete_credential, provider)


@tenant_router.get(_LINK, summary="List provider key id -> api_key_fingerprint links")
async def tenant_list_links(
    request: Request, response: Response, principal: AdminPrincipal = _ADMIN
):
    return await _as_tenant(request, response, principal, _list_links)


@tenant_router.put(
    _LINK + "/{provider}/{api_key_id}",
    summary="Link a provider key id to the SDK api_key_fingerprint",
)
async def tenant_put_link(
    provider: Provider,
    api_key_id: str,
    body: FingerprintBody,
    request: Request,
    response: Response,
    principal: AdminPrincipal = _ADMIN,
):
    return await _as_tenant(
        request, response, principal, _put_link, provider, api_key_id, body
    )


@tenant_router.delete(
    _LINK + "/{provider}/{api_key_id}",
    status_code=204,
    summary="Delete a fingerprint link",
)
async def tenant_delete_link(
    provider: Provider,
    api_key_id: str,
    request: Request,
    response: Response,
    principal: AdminPrincipal = _ADMIN,
):
    await _as_tenant(request, response, principal, _delete_link, provider, api_key_id)


# ──────────────────────────────────────────────────────────────────────
# Partner API
# ──────────────────────────────────────────────────────────────────────


@partner_router.get(_CRED, summary="List stored provider admin credentials (masked)")
async def partner_list_credentials(
    request: Request, response: Response, principal: PartnerPrincipal = _SCOPE
):
    return await _as_partner(request, response, principal, _list_credentials)


@partner_router.get(
    _CRED + "/{provider}", summary="Get a stored provider credential (masked)"
)
async def partner_get_credential(
    provider: Provider,
    request: Request,
    response: Response,
    principal: PartnerPrincipal = _SCOPE,
):
    return await _as_partner(request, response, principal, _get_credential, provider)


@partner_router.post(
    _CRED + "/{provider}",
    status_code=201,
    summary="Store a provider admin key (write-only)",
)
async def partner_create_credential(
    provider: Provider,
    body: AdminKeyBody,
    request: Request,
    response: Response,
    principal: PartnerPrincipal = _SCOPE,
):
    return await _as_partner(
        request, response, principal, _create_credential, provider, body
    )


@partner_router.post(
    _CRED + "/{provider}/rotate", summary="Replace a stored provider admin key"
)
async def partner_rotate_credential(
    provider: Provider,
    body: AdminKeyBody,
    request: Request,
    response: Response,
    principal: PartnerPrincipal = _SCOPE,
):
    return await _as_partner(
        request, response, principal, _rotate_credential, provider, body
    )


@partner_router.delete(
    _CRED + "/{provider}", status_code=204, summary="Delete a stored provider admin key"
)
async def partner_delete_credential(
    provider: Provider,
    request: Request,
    response: Response,
    principal: PartnerPrincipal = _SCOPE,
):
    await _as_partner(request, response, principal, _delete_credential, provider)


@partner_router.get(_LINK, summary="List provider key id -> api_key_fingerprint links")
async def partner_list_links(
    request: Request, response: Response, principal: PartnerPrincipal = _SCOPE
):
    return await _as_partner(request, response, principal, _list_links)


@partner_router.put(
    _LINK + "/{provider}/{api_key_id}",
    summary="Link a provider key id to the SDK api_key_fingerprint",
)
async def partner_put_link(
    provider: Provider,
    api_key_id: str,
    body: FingerprintBody,
    request: Request,
    response: Response,
    principal: PartnerPrincipal = _SCOPE,
):
    return await _as_partner(
        request, response, principal, _put_link, provider, api_key_id, body
    )


@partner_router.delete(
    _LINK + "/{provider}/{api_key_id}",
    status_code=204,
    summary="Delete a fingerprint link",
)
async def partner_delete_link(
    provider: Provider,
    api_key_id: str,
    request: Request,
    response: Response,
    principal: PartnerPrincipal = _SCOPE,
):
    await _as_partner(request, response, principal, _delete_link, provider, api_key_id)
