"""HTTP routes for the inference credential vault (#342).
Logic: collector.credential_vault; envelope encryption: collector.kms.

Tenant admin, under /api/v1/admin, scope ``admin:inference-credentials``
(GET also with ``admin:read``). Writes need an admin token: the scope is
not among the scopes a tenant ingest key reaches while
PS_ADMIN_REQUIRE_SCOPED_TOKENS is off. Through RBAC (#338) the owner and
admin roles hold the scope.

  GET    /inference-credentials                 list (masked; ?provider=)
  POST   /inference-credentials                 store {provider, label, api_key, expires_at?}
  GET    /inference-credentials/{id}            read (masked)
  PATCH  /inference-credentials/{id}            {label?, expires_at?, disabled?}
  POST   /inference-credentials/{id}/rotate     replace the key {api_key, expires_at?}
  DELETE /inference-credentials/{id}            delete

The key is write-only: no response, log line or audit row carries it, and
a 400 or 422 never echoes it (422 bodies drop the submitted values).
Responses show ``key_hint``, ``key_fingerprint``, the KEK the data key is
wrapped under and timestamps. Every call writes a ``partner_audit_log``
row (path and status, never the body); every change, and every refused
change, writes an ``audit_events`` row (inference_credential.create |
update | rotate | delete) in the same transaction as the change.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict

from collector import audit_trail, kms
from collector import credential_vault as cv
from collector.admin_auth import AdminPrincipal, require_admin
from collector.audit import record_read
from collector.provider_credentials_api import _SAFE_LOC, _RedactedValidationRoute

SCOPE = "admin:inference-credentials"
_ADMIN = Depends(require_admin(SCOPE))


class _VaultRoute(_RedactedValidationRoute):
    safe_loc = _SAFE_LOC | {
        "label",
        "api_key",
        "expires_at",
        "disabled",
        "credential_id",
    }


router = APIRouter(prefix="/api/v1/admin", tags=["Admin"], route_class=_VaultRoute)


class _WriteOnly(BaseModel):
    # No pydantic constraints on the key: a failed constraint echoes the
    # input. Length and charset are checked in credential_vault (400).
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    def __repr__(self) -> str:  # never print the key
        return f"{type(self).__name__}(api_key=***)"

    __str__ = __repr__


class CreateInferenceCredentialBody(_WriteOnly):
    provider: str
    label: str
    api_key: str
    expires_at: datetime | None = None


class RotateInferenceCredentialBody(_WriteOnly):
    api_key: str
    expires_at: datetime | None = None


class UpdateInferenceCredentialBody(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    label: str | None = None
    expires_at: datetime | None = None
    disabled: bool | None = None


def _problem(status: int, title: str, detail: str) -> HTTPException:
    return HTTPException(
        status_code=status,
        detail={"type": "about:blank", "title": title, "status": status, "detail": detail},
    )


def _not_found() -> HTTPException:
    return _problem(404, "Not Found", "No inference credential with this id")


def _kms_problem(exc: Exception) -> HTTPException:
    # kms messages name settings, hosts and status codes, never key material.
    if isinstance(exc, kms.KeyProviderNotConfigured):
        return _problem(
            503, "Service Unavailable", f"Credential vault key provider not configured: {exc}"
        )
    return _problem(503, "Service Unavailable", f"Credential vault key provider failed: {exc}")


_KMS_ERRORS = (kms.KeyProviderNotConfigured, kms.KeyProviderError)


# ──────────────────────────────────────────────────────────────────────
# Handlers: (db, tenant_id, ...) -> (status, body, audit target id)
# ──────────────────────────────────────────────────────────────────────


async def _list(db, tenant_id: UUID, provider: str | None):
    rows = await cv.list_credentials(db, tenant_id, provider)
    return 200, {"data": [cv.serialize(r) for r in rows]}, None


async def _get(db, tenant_id: UUID, credential_id: UUID):
    row = await cv.find(db, tenant_id, credential_id)
    if row is None:
        raise _not_found()
    return 200, cv.serialize(row), row.id


async def _create(db, tenant_id: UUID, body: CreateInferenceCredentialBody):
    try:
        provider = cv.validate_provider(body.provider)
        label = cv.validate_label(body.label)
    except ValueError as exc:
        raise _problem(400, "Bad Request", str(exc)) from exc
    if await cv.label_taken(db, tenant_id, provider, label):
        raise _problem(409, "Conflict", "A credential with this provider and label exists")
    if await cv.count(db, tenant_id) >= cv.MAX_CREDENTIALS_PER_TENANT:
        raise _problem(
            409, "Conflict", f"At most {cv.MAX_CREDENTIALS_PER_TENANT} inference credentials"
        )
    try:
        row = await cv.create(
            db,
            tenant_id,
            provider=provider,
            label=label,
            api_key=body.api_key,
            expires_at=body.expires_at,
        )
    except _KMS_ERRORS as exc:
        raise _kms_problem(exc) from exc
    except ValueError as exc:
        raise _problem(400, "Bad Request", str(exc)) from exc
    await db.flush()
    return 201, cv.serialize(row), row.id


async def _update(db, tenant_id: UUID, credential_id: UUID, body: UpdateInferenceCredentialBody):
    row = await cv.find(db, tenant_id, credential_id)
    if row is None:
        raise _not_found()
    fields = body.model_fields_set
    try:
        if "label" in fields:
            if body.label is None:
                raise ValueError("label cannot be null")
            label = cv.validate_label(body.label)
            if await cv.label_taken(db, tenant_id, row.provider, label, exclude=row.id):
                raise _problem(
                    409, "Conflict", "A credential with this provider and label exists"
                )
            row.label = label
        if "expires_at" in fields:
            row.expires_at = cv.validate_expires_at(body.expires_at)
    except ValueError as exc:
        raise _problem(400, "Bad Request", str(exc)) from exc
    if "disabled" in fields and body.disabled is not None:
        if body.disabled and row.disabled_at is None:
            row.disabled_at = cv._utcnow()
        elif not body.disabled:
            row.disabled_at = None
    row.updated_at = cv._utcnow()
    await db.flush()
    return 200, cv.serialize(row), row.id


async def _rotate(db, tenant_id: UUID, credential_id: UUID, body: RotateInferenceCredentialBody):
    row = await cv.find(db, tenant_id, credential_id)
    if row is None:
        raise _not_found()
    try:
        await cv.rotate(row, body.api_key, expires_at=body.expires_at)
    except _KMS_ERRORS as exc:
        raise _kms_problem(exc) from exc
    except ValueError as exc:
        raise _problem(400, "Bad Request", str(exc)) from exc
    await db.flush()
    return 200, cv.serialize(row), row.id


async def _delete(db, tenant_id: UUID, credential_id: UUID):
    row = await cv.find(db, tenant_id, credential_id)
    if row is None:
        raise _not_found()
    await db.delete(row)
    return 204, None, credential_id


Handler = Callable[..., Awaitable[tuple[int, dict | None, UUID | None]]]

_AUDITED: dict[Handler, str] = {
    _create: "inference_credential.create",
    _update: "inference_credential.update",
    _rotate: "inference_credential.rotate",
    _delete: "inference_credential.delete",
}


def _details(handler: Handler, args: tuple) -> dict:
    """Ids, names and field names only: never the key or label free text
    beyond what the row shows anyway."""
    if handler is _create:
        try:  # an invalid provider value is not recorded (it could be a pasted key)
            return {"provider": cv.validate_provider(args[0].provider)}
        except ValueError:
            return {"provider": None}
    if handler is _update:
        return {"fields": sorted(args[1].model_fields_set)}
    if handler is _rotate:
        return {"expires_at_set": args[1].expires_at is not None}
    return {}


async def _run(request: Request, response: Response, principal: AdminPrincipal, handler, *args):
    tenant_id = principal.tenant_id
    db = request.state.db
    actor = audit_trail.tenant_key_actor(request)
    action = _AUDITED.get(handler)
    target_id = args[0] if args and isinstance(args[0], UUID) else None
    try:
        status, body, target_id = await handler(db, tenant_id, *args)
        if action is not None:
            await audit_trail.record(
                db,
                tenant_id=tenant_id,
                action=action,
                actor=actor,
                target_type=cv.TARGET_TYPE,
                target_id=target_id,
                status_code=status,
                details=_details(handler, args),
            )
        await db.commit()
    except HTTPException as exc:
        await db.rollback()
        if action is not None:
            await audit_trail.record_committed(
                db,
                tenant_id=tenant_id,
                action=action,
                actor=actor,
                outcome=audit_trail.FAILURE,
                target_type=cv.TARGET_TYPE,
                target_id=target_id,
                status_code=exc.status_code,
                details=_details(handler, args),
            )
        await record_read(request, tenant_id=tenant_id, status_code=exc.status_code)
        raise
    await record_read(request, tenant_id=tenant_id, status_code=status)
    response.status_code = status
    return body


_BASE = "/inference-credentials"
_ONE = _BASE + "/{credential_id}"


@router.get(_BASE, summary="List stored inference credentials (masked)")
async def list_inference_credentials(
    request: Request,
    response: Response,
    provider: str | None = Query(default=None, max_length=64),
    principal: AdminPrincipal = _ADMIN,
):
    if provider is not None:
        try:
            provider = cv.validate_provider(provider)
        except ValueError as exc:
            raise _problem(400, "Bad Request", str(exc)) from exc
    return await _run(request, response, principal, _list, provider)


@router.post(_BASE, status_code=201, summary="Store an inference API key (write-only)")
async def create_inference_credential(
    body: CreateInferenceCredentialBody,
    request: Request,
    response: Response,
    principal: AdminPrincipal = _ADMIN,
):
    return await _run(request, response, principal, _create, body)


@router.get(_ONE, summary="Get a stored inference credential (masked)")
async def get_inference_credential(
    credential_id: UUID,
    request: Request,
    response: Response,
    principal: AdminPrincipal = _ADMIN,
):
    return await _run(request, response, principal, _get, credential_id)


@router.patch(_ONE, summary="Relabel, set the expiry of, disable or enable a credential")
async def update_inference_credential(
    credential_id: UUID,
    body: UpdateInferenceCredentialBody,
    request: Request,
    response: Response,
    principal: AdminPrincipal = _ADMIN,
):
    return await _run(request, response, principal, _update, credential_id, body)


@router.post(_ONE + "/rotate", summary="Replace a stored inference API key")
async def rotate_inference_credential(
    credential_id: UUID,
    body: RotateInferenceCredentialBody,
    request: Request,
    response: Response,
    principal: AdminPrincipal = _ADMIN,
):
    return await _run(request, response, principal, _rotate, credential_id, body)


@router.delete(_ONE, status_code=204, summary="Delete a stored inference credential")
async def delete_inference_credential(
    credential_id: UUID,
    request: Request,
    response: Response,
    principal: AdminPrincipal = _ADMIN,
):
    await _run(request, response, principal, _delete, credential_id)
