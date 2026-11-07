"""SCIM 2.0 provisioning: /scim/v2/Users and /scim/v2/Groups (#339).

RFC 7643 (schema) and RFC 7644 (protocol), the subset identity providers
(Okta, Microsoft Entra ID, OneLogin, JumpCloud) use:

  GET    /scim/v2/ServiceProviderConfig
  POST   /scim/v2/Users                   create a user principal
  GET    /scim/v2/Users                   list (?filter=, ?startIndex=, ?count=)
  GET    /scim/v2/Users/{id}
  PUT    /scim/v2/Users/{id}              replace
  PATCH  /scim/v2/Users/{id}              PatchOp (active=false deactivates)
  DELETE /scim/v2/Users/{id}              delete
  POST   /scim/v2/Groups                  create a team (with members)
  GET    /scim/v2/Groups                  list (?filter=, ?excludedAttributes=members)
  GET    /scim/v2/Groups/{id}
  PUT    /scim/v2/Groups/{id}             replace (members too)
  PATCH  /scim/v2/Groups/{id}             PatchOp (add/remove/replace members)
  DELETE /scim/v2/Groups/{id}             delete (409 while it has projects)

Filters: one ``<attribute> eq "<value>"`` (Users: userName, externalId, id,
emails.value; Groups: displayName, externalId, id). No sorting, ETags or
bulk.

Mapping: a User is a principal of kind ``user`` (``id`` = principal id,
``userName`` = principals.user_name, ``externalId`` = external_id, which
admin API SSO matches against the ID token subject, ``displayName``,
primary email, ``active`` = not disabled). A Group is a team (``id`` = team
id, ``displayName`` = name, ``externalId`` = teams.external_id); its members
are the team memberships of user principals. Group roles and tenant-wide
roles from groups: collector/idp_sync.py.

Authentication: a per-tenant SCIM token (``ps-scim-...``, table
``scim_tokens``; issued by an owner through
``POST /api/v1/admin/scim-tokens``, collector/sso_admin.py), stored like
admin tokens (SHA-256 lookup prefix + bcrypt), with scopes ``scim:users``
and/or ``scim:groups``. It reaches /scim/v2 and nothing else, and an admin
token or ingest key does not reach /scim/v2.

What a SCIM token can never do: grant ``owner`` (SCIM carries no roles; a
group's roles are set by an admin and never owner); re-identify
(``externalId``), re-enable or delete a principal that holds the owner role
(an owner does that through the admin API); disable the tenant's last
enabled owner (409); touch service accounts (they are invisible here).

Deactivation (``active=false`` or ``PATCH`` / ``PUT``) disables the
principal, which makes every admin token and partner credential issued for
it fail on the next request, and revokes its SSO sessions and every token
they created (collector/idp_sync.py). Effect is immediate (well within the
60 s the issue asks for).

Audit: every change is a hash-chained audit trail row in the same
transaction, with the same actions as the admin API (``principal.*``,
``team.*``, ``membership.*``, ``role.change``), ``source: scim`` in the
details, and the SCIM token's lookup prefix as the actor
(``scim_token``). Never an email, user name or display name. Refused
tokens are ``auth.failure`` rows (realm ``scim``).
"""

from __future__ import annotations

import asyncio
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from db.api_keys import api_key_lookup_prefix, hash_api_key, verify_api_key
from db.models import (
    AdminToken,
    Membership,
    PartnerCredential,
    Principal,
    Project,
    ScimToken,
    Team,
    Tenant,
)
from fastapi import APIRouter, Body, Depends, Query, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from collector import audit_trail, idp_sync, rbac, rls
from collector.auth import (
    _DUMMY_HASH,
    check_failed_auth_limit,
    failed_auth_client,
    record_failed_auth,
)
from collector.rbac_admin import lock_owners
from collector.sanitize import BoundedJsonRoute, StorableJson, is_storable
from collector.tenant_keys import DuplicateName, key_status

TOKEN_PREFIX = "ps-scim-"
SCIM_SCOPES: dict[str, str] = {
    "scim:users": "Create, change, deactivate and delete user principals (/scim/v2/Users)",
    "scim:groups": "Create, change and delete teams and their members (/scim/v2/Groups)",
}

TOKEN_WARNING_SCIM = "Save the token now. It will not be shown again."

USER_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:User"
GROUP_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:Group"
LIST_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:ListResponse"
PATCH_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:PatchOp"
ERROR_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:Error"
SPC_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:ServiceProviderConfig"
MEDIA_TYPE = "application/scim+json"

DEFAULT_COUNT = 100
MAX_COUNT = 200
MAX_MEMBERS_PER_REQUEST = 1000

_BODY = Body(...)

router = APIRouter(prefix="/scim/v2", tags=["SCIM"], route_class=BoundedJsonRoute)


# ──────────────────────────────────────────────────────────────────────
# Errors (RFC 7644 §3.12)
# ──────────────────────────────────────────────────────────────────────


class ScimError(Exception):
    def __init__(self, status: int, detail: str, scim_type: str | None = None):
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.scim_type = scim_type


def scim_error_response(exc: ScimError) -> JSONResponse:
    body: dict[str, Any] = {
        "schemas": [ERROR_SCHEMA],
        "status": str(exc.status),
        "detail": exc.detail,
    }
    if exc.scim_type:
        body["scimType"] = exc.scim_type
    headers = {"WWW-Authenticate": 'Bearer realm="scim"'} if exc.status == 401 else None
    return JSONResponse(body, status_code=exc.status, media_type=MEDIA_TYPE, headers=headers)


async def scim_error_handler(request: Request, exc: ScimError) -> JSONResponse:
    return scim_error_response(exc)


def _scim_json(body: dict, status: int = 200, headers: dict | None = None) -> JSONResponse:
    return JSONResponse(body, status_code=status, media_type=MEDIA_TYPE, headers=headers)


# ──────────────────────────────────────────────────────────────────────
# Tokens
# ──────────────────────────────────────────────────────────────────────


def generate_token() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def serialize_token(row: ScimToken) -> dict:
    """Metadata only: never the token or its hash."""

    def iso(v):
        return v.isoformat() if v else None

    return {
        "id": str(row.id),
        "name": row.name,
        "scopes": list(row.scopes or ()),
        "token_hint": row.token_hint,
        "status": key_status(row),
        "created_by": row.created_by,
        "created_at": iso(row.created_at),
        "expires_at": iso(row.expires_at),
        "revoked_at": iso(row.revoked_at),
    }


async def issue_token(
    db: AsyncSession,
    tenant_id: UUID,
    *,
    name: str,
    scopes: list[str],
    expires_at: datetime | None,
    created_by: str,
) -> tuple[ScimToken, str]:
    """Create a SCIM token. Returns (row, plaintext). Flushes."""
    taken = (
        await db.execute(
            select(ScimToken.id).where(
                ScimToken.tenant_id == tenant_id,
                ScimToken.name == name,
                ScimToken.revoked_at.is_(None),
            )
        )
    ).first()
    if taken is not None:
        raise DuplicateName(name)
    plaintext = generate_token()
    row = ScimToken(
        tenant_id=tenant_id,
        name=name,
        scopes=list(dict.fromkeys(scopes)),
        token_prefix=api_key_lookup_prefix(plaintext),
        token_hash=hash_api_key(plaintext),
        token_hint=plaintext[-4:],
        created_by=created_by,
        created_at=datetime.now(UTC),
        expires_at=expires_at,
    )
    db.add(row)
    await db.flush()
    return row, plaintext


@dataclass(frozen=True)
class ScimCaller:
    tenant_id: UUID
    token_id: UUID
    token_prefix: str
    scopes: frozenset[str]

    @property
    def actor(self) -> audit_trail.Actor:
        return audit_trail.Actor("scim_token", self.token_prefix)


async def _auth_failure(request: Request, reason: str, status: int, tenant_id=None) -> None:
    await audit_trail.record_auth_failure(
        request,
        realm="scim",
        reason=reason,
        status_code=status,
        tenant_id=tenant_id,
        db=request.state.db if tenant_id else None,
    )


async def resolve_scim(request: Request) -> ScimCaller:
    auth = request.headers.get("Authorization", "")
    token = auth[7:].strip() if auth[:7].lower() == "bearer " else ""
    if not token:
        await _auth_failure(request, "missing_credentials", 401)
        raise ScimError(401, "Missing or invalid Authorization header")
    if not token.startswith(TOKEN_PREFIX):
        await _auth_failure(request, "invalid_credentials", 401)
        raise ScimError(401, "A SCIM token (ps-scim-...) is required")
    db = request.state.db
    prefix = api_key_lookup_prefix(token)
    async with rls.auth_lookup(db):
        row = (
            await db.execute(
                select(
                    ScimToken.id,
                    ScimToken.tenant_id,
                    ScimToken.scopes,
                    ScimToken.token_hash,
                    ScimToken.expires_at,
                    ScimToken.revoked_at,
                    Tenant.disabled_at,
                )
                .join(Tenant, Tenant.id == ScimToken.tenant_id)
                .where(ScimToken.token_prefix == prefix)
            )
        ).one_or_none()
    if row is None or key_status(row) != "active":
        client = check_failed_auth_limit(request)
    else:
        client = failed_auth_client(request)
    stored = row.token_hash if row is not None else _DUMMY_HASH
    valid = await asyncio.to_thread(verify_api_key, token, stored)
    if row is None or not valid or key_status(row) != "active":
        record_failed_auth(client)
        await _auth_failure(request, "invalid_credentials", 401)
        raise ScimError(401, "Invalid, revoked or expired SCIM token")
    if row.disabled_at is not None:
        raise ScimError(403, "Tenant is disabled")
    await rls.set_tenant(db, row.tenant_id)
    return ScimCaller(
        tenant_id=row.tenant_id,
        token_id=row.id,
        token_prefix=prefix,
        scopes=frozenset(row.scopes or ()),
    )


def require_scim(scope: str):
    if scope not in SCIM_SCOPES:
        raise ValueError(f"Unknown SCIM scope: {scope!r}")

    async def _dep(request: Request) -> ScimCaller:
        caller = await resolve_scim(request)
        if scope not in caller.scopes:
            await _auth_failure(request, "insufficient_scope", 403, caller.tenant_id)
            raise ScimError(403, f"SCIM token lacks required scope: {scope}")
        return caller

    # Read by tests/test_route_scopes.py.
    _dep.scim_scope = scope
    return _dep


async def _any_scim(request: Request) -> ScimCaller:
    return await resolve_scim(request)


_any_scim.scim_scope = "any"

_ANY = Depends(_any_scim)
_USERS = Depends(require_scim("scim:users"))
_GROUPS = Depends(require_scim("scim:groups"))


# ──────────────────────────────────────────────────────────────────────
# Shared helpers
# ──────────────────────────────────────────────────────────────────────


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(v: datetime | None) -> str | None:
    return v.isoformat() if v else None


def _base(request: Request) -> str:
    return str(request.base_url).rstrip("/") + "/scim/v2"


def _uuid(value: str, what: str) -> UUID:
    try:
        return UUID(value)
    except (ValueError, TypeError, AttributeError):
        raise ScimError(404, f"{what} not found") from None


def _auditor(request: Request, db: AsyncSession, caller: ScimCaller, status: int):
    async def audit(action: str, target_type: str, target_id, **details) -> None:
        await audit_trail.record(
            db,
            tenant_id=caller.tenant_id,
            action=action,
            actor=caller.actor,
            target_type=target_type,
            target_id=target_id,
            status_code=status,
            details={"source": idp_sync.SCIM_SOURCE, **details},
        )

    return audit


def _text(value: Any, what: str, max_len: int, *, required: bool = False) -> str | None:
    if value is None or value == "":
        if required:
            raise ScimError(400, f"{what} is required", "invalidValue")
        return None
    if not isinstance(value, str) or len(value) > max_len or not is_storable(value):
        raise ScimError(400, f"{what} must be a string of at most {max_len} characters", "invalidValue")
    value = value.strip()
    if required and not value:
        raise ScimError(400, f"{what} is required", "invalidValue")
    return value or None


def _bool(value: Any, what: str) -> bool:
    # Microsoft Entra ID sends "True" / "False" strings in PATCH values.
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.lower() in ("true", "false"):
        return value.lower() == "true"
    raise ScimError(400, f"{what} must be a boolean", "invalidValue")


_FILTER_RE = re.compile(r'^\s*([A-Za-z][A-Za-z0-9.]*)\s+eq\s+"((?:[^"\\]|\\.)*)"\s*$', re.IGNORECASE)


def parse_filter(raw: str | None, allowed: dict[str, str]) -> tuple[str, str] | None:
    """(field, value) for one ``attr eq "value"``; attribute names are
    case-insensitive (RFC 7644 §3.4.2.2). ``allowed`` maps lowercase SCIM
    attribute -> field name."""
    if raw is None or not raw.strip():
        return None
    m = _FILTER_RE.match(raw)
    if not m or m.group(1).lower() not in allowed:
        raise ScimError(
            400,
            "Only one 'attribute eq \"value\"' filter is supported, on: "
            + ", ".join(sorted(allowed)),
            "invalidFilter",
        )
    value = re.sub(r"\\(.)", r"\1", m.group(2))
    if not is_storable(value):
        raise ScimError(400, "Filter value cannot be stored", "invalidFilter")
    return allowed[m.group(1).lower()], value


def _page(start_index: int, count: int) -> tuple[int, int]:
    return max(start_index, 1), max(0, min(count, MAX_COUNT))


def _list_response(resources: list[dict], total: int, start_index: int) -> dict:
    return {
        "schemas": [LIST_SCHEMA],
        "totalResults": total,
        "startIndex": start_index,
        "itemsPerPage": len(resources),
        "Resources": resources,
    }


def _patch_ops(payload: dict) -> list[dict]:
    if PATCH_SCHEMA not in (payload.get("schemas") or []):
        raise ScimError(400, f"PATCH body must name the {PATCH_SCHEMA} schema", "invalidSyntax")
    ops = payload.get("Operations")
    if not isinstance(ops, list) or not ops or not all(isinstance(o, dict) for o in ops):
        raise ScimError(400, "Operations must be a non-empty list", "invalidSyntax")
    for op in ops:
        if not isinstance(op.get("op"), str) or op["op"].lower() not in ("add", "remove", "replace"):
            raise ScimError(400, "op must be add, remove or replace", "invalidSyntax")
        if op.get("path") is not None and not isinstance(op["path"], str):
            raise ScimError(400, "path must be a string", "invalidPath")
    return ops


async def _is_owner(db: AsyncSession, principal_id: UUID) -> bool:
    return (
        await db.execute(
            select(Membership.id).where(
                Membership.principal_id == principal_id,
                Membership.team_id.is_(None),
                Membership.role == rbac.OWNER,
            )
        )
    ).first() is not None


async def _other_enabled_owners(db: AsyncSession, tenant_id: UUID, principal_id: UUID) -> int:
    return (
        await db.execute(
            select(func.count())
            .select_from(Membership)
            .join(Principal, Principal.id == Membership.principal_id)
            .where(
                Membership.tenant_id == tenant_id,
                Membership.team_id.is_(None),
                Membership.role == rbac.OWNER,
                Membership.principal_id != principal_id,
                Principal.disabled_at.is_(None),
            )
        )
    ).scalar_one()


# ──────────────────────────────────────────────────────────────────────
# ServiceProviderConfig
# ──────────────────────────────────────────────────────────────────────


@router.get("/ServiceProviderConfig", summary="SCIM service provider configuration")
async def service_provider_config(caller: ScimCaller = _ANY) -> JSONResponse:
    return _scim_json(
        {
            "schemas": [SPC_SCHEMA],
            "patch": {"supported": True},
            "bulk": {"supported": False, "maxOperations": 0, "maxPayloadSize": 0},
            "filter": {"supported": True, "maxResults": MAX_COUNT},
            "changePassword": {"supported": False},
            "sort": {"supported": False},
            "etag": {"supported": False},
            "authenticationSchemes": [
                {
                    "type": "oauthbearertoken",
                    "name": "SCIM bearer token",
                    "description": "A per-tenant SCIM token (ps-scim-...)",
                    "primary": True,
                }
            ],
        }
    )


# ──────────────────────────────────────────────────────────────────────
# Users
# ──────────────────────────────────────────────────────────────────────

USER_FILTERS = {
    "username": "user_name",
    "externalid": "external_id",
    "id": "id",
    "emails.value": "email",
    "emails": "email",
}


async def _user_groups(db: AsyncSession, principal_id: UUID) -> list[dict]:
    rows = (
        await db.execute(
            select(Team.id, Team.name)
            .join(Membership, Membership.team_id == Team.id)
            .where(Membership.principal_id == principal_id, Membership.project_id.is_(None))
            .order_by(Team.name)
        )
    ).all()
    return [{"value": str(r.id), "display": r.name, "type": "direct"} for r in rows]


def user_resource(request: Request, p: Principal, groups: list[dict]) -> dict:
    out: dict[str, Any] = {
        "schemas": [USER_SCHEMA],
        "id": str(p.id),
        "userName": p.user_name or p.email or str(p.id),
        "active": p.disabled_at is None,
        "groups": groups,
        "meta": {
            "resourceType": "User",
            "created": _iso(p.created_at),
            "lastModified": _iso(p.updated_at or p.created_at),
            "location": f"{_base(request)}/Users/{p.id}",
        },
    }
    if p.external_id:
        out["externalId"] = p.external_id
    if p.display_name:
        out["displayName"] = p.display_name
        out["name"] = {"formatted": p.display_name}
    if p.email:
        out["emails"] = [{"value": p.email, "primary": True, "type": "work"}]
    return out


def _primary_email(emails: Any) -> str | None:
    if emails is None:
        return None
    if not isinstance(emails, list) or not all(isinstance(e, dict) for e in emails):
        raise ScimError(400, "emails must be a list of objects", "invalidValue")
    if not emails:
        return None
    chosen = next((e for e in emails if e.get("primary") is True), emails[0])
    return _text(chosen.get("value"), "emails.value", 320)


def _display_name(payload: dict) -> str | None:
    name = payload.get("displayName")
    if name:
        return _text(name, "displayName", 200)
    parts = payload.get("name")
    if isinstance(parts, dict):
        if parts.get("formatted"):
            return _text(parts["formatted"], "name.formatted", 200)
        joined = " ".join(
            p for p in (parts.get("givenName"), parts.get("familyName")) if isinstance(p, str) and p
        )
        return _text(joined, "name", 200)
    return None


def _user_fields(payload: dict) -> dict:
    """The fields a full User representation sets (POST, PUT)."""
    fields = {
        "user_name": _text(payload.get("userName"), "userName", 255, required=True),
        "external_id": _text(payload.get("externalId"), "externalId", 255),
        "display_name": _display_name(payload),
        "email": _primary_email(payload.get("emails")),
    }
    if "active" in payload:
        fields["active"] = _bool(payload["active"], "active")
    return fields


async def _get_user(db: AsyncSession, tenant_id: UUID, user_id: str) -> Principal:
    pid = _uuid(user_id, "User")
    row = (
        await db.execute(
            select(Principal).where(
                Principal.id == pid, Principal.tenant_id == tenant_id, Principal.kind == "user"
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise ScimError(404, "User not found")
    return row


async def _check_unique_user(
    db: AsyncSession, tenant_id: UUID, user_name: str | None, external_id: str | None, exclude=None
) -> None:
    if user_name is not None:
        stmt = select(Principal.id).where(
            Principal.tenant_id == tenant_id,
            func.lower(Principal.user_name) == user_name.lower(),
        )
        if exclude is not None:
            stmt = stmt.where(Principal.id != exclude)
        if (await db.execute(stmt)).first() is not None:
            raise ScimError(409, "A user with this userName already exists", "uniqueness")
    if external_id is not None:
        stmt = select(Principal.id).where(
            Principal.tenant_id == tenant_id, Principal.external_id == external_id
        )
        if exclude is not None:
            stmt = stmt.where(Principal.id != exclude)
        if (await db.execute(stmt)).first() is not None:
            raise ScimError(409, "A principal with this externalId already exists", "uniqueness")


async def _deactivate(
    db: AsyncSession, caller: ScimCaller, row: Principal, now: datetime
) -> list[AdminToken]:
    if await _is_owner(db, row.id) and not await _other_enabled_owners(
        db, caller.tenant_id, row.id
    ):
        raise ScimError(
            409,
            "This is the tenant's last enabled owner; make another principal owner first",
            "mutability",
        )
    row.disabled_at = now
    return await idp_sync.revoke_principal_sessions(db, caller.tenant_id, row.id, now)


async def _apply_user(
    request: Request, caller: ScimCaller, row: Principal, fields: dict, status: int = 200
) -> JSONResponse:
    """Apply ``fields`` (any of user_name, external_id, display_name, email,
    active) to ``row``, audit and commit."""
    db = request.state.db
    now = _now()
    changed = [f for f in ("user_name", "external_id", "display_name", "email") if f in fields and fields[f] != getattr(row, f)]
    toggles = "active" in fields and fields["active"] != (row.disabled_at is None)
    owner = await _is_owner(db, row.id)
    if owner and ("external_id" in changed or (toggles and fields["active"])):
        raise ScimError(
            403,
            "A SCIM token cannot re-identify or re-enable an owner; an owner does "
            "that through the admin API",
            "mutability",
        )
    await _check_unique_user(
        db,
        caller.tenant_id,
        fields.get("user_name") if "user_name" in changed else None,
        fields.get("external_id") if "external_id" in changed else None,
        row.id,
    )
    for f in changed:
        setattr(row, f, fields[f])
    revoked: list[AdminToken] = []
    action = "principal.update"
    if toggles:
        if fields["active"]:
            row.disabled_at = None
            action = "principal.enable"
        else:
            revoked = await _deactivate(db, caller, row, now)
            action = "principal.disable"
    if changed or toggles:
        row.updated_at = now
        await db.flush()
        await _auditor(request, db, caller, status)(
            action,
            "principal",
            row.id,
            fields=changed,
            revoked_token_ids=[str(t.id) for t in revoked],
        )
    out = user_resource(request, row, await _user_groups(db, row.id))
    await db.commit()
    return _scim_json(out, status)


@router.post("/Users", status_code=201, summary="Create a user")
async def create_user(
    request: Request, payload: StorableJson = _BODY, caller: ScimCaller = _USERS
) -> JSONResponse:
    db = request.state.db
    fields = _user_fields(payload)
    await _check_unique_user(db, caller.tenant_id, fields["user_name"], fields["external_id"])
    now = _now()
    row = Principal(
        tenant_id=caller.tenant_id,
        kind="user",
        user_name=fields["user_name"],
        external_id=fields["external_id"],
        display_name=fields["display_name"],
        email=fields["email"],
        created_at=now,
        disabled_at=None if fields.get("active", True) else now,
    )
    db.add(row)
    await db.flush()
    await _auditor(request, db, caller, 201)(
        "principal.create", "principal", row.id, kind="user", active=row.disabled_at is None
    )
    out = user_resource(request, row, [])
    await db.commit()
    return _scim_json(out, 201, {"Location": out["meta"]["location"]})


@router.get("/Users", summary="List users")
async def list_users(
    request: Request,
    filter: str | None = Query(default=None, max_length=1000),
    start_index: int = Query(default=1, alias="startIndex"),
    count: int = Query(default=DEFAULT_COUNT),
    caller: ScimCaller = _USERS,
) -> JSONResponse:
    db = request.state.db
    start_index, count = _page(start_index, count)
    stmt = select(Principal).where(
        Principal.tenant_id == caller.tenant_id, Principal.kind == "user"
    )
    parsed = parse_filter(filter, USER_FILTERS)
    if parsed is not None:
        field, value = parsed
        if field == "id":
            try:
                stmt = stmt.where(Principal.id == UUID(value))
            except ValueError:
                stmt = stmt.where(False)
        elif field == "user_name":
            stmt = stmt.where(func.lower(Principal.user_name) == value.lower())
        elif field == "email":
            stmt = stmt.where(func.lower(Principal.email) == value.lower())
        else:
            stmt = stmt.where(Principal.external_id == value)
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (
        (
            await db.execute(
                stmt.order_by(Principal.created_at, Principal.id)
                .offset(start_index - 1)
                .limit(count)
            )
        )
        .scalars()
        .all()
    )
    resources = [user_resource(request, r, await _user_groups(db, r.id)) for r in rows]
    return _scim_json(_list_response(resources, total, start_index))


@router.get("/Users/{user_id}", summary="Get a user")
async def get_user(user_id: str, request: Request, caller: ScimCaller = _USERS) -> JSONResponse:
    db = request.state.db
    row = await _get_user(db, caller.tenant_id, user_id)
    return _scim_json(user_resource(request, row, await _user_groups(db, row.id)))


@router.put("/Users/{user_id}", summary="Replace a user")
async def replace_user(
    user_id: str,
    request: Request,
    payload: StorableJson = _BODY,
    caller: ScimCaller = _USERS,
) -> JSONResponse:
    # Serialise with every other change that can drop an owner (#372).
    await lock_owners(request.state.db, caller.tenant_id)
    row = await _get_user(request.state.db, caller.tenant_id, user_id)
    return await _apply_user(request, caller, row, _user_fields(payload))


_USER_PATHS = {
    "username": "user_name",
    "externalid": "external_id",
    "displayname": "display_name",
    "name.formatted": "display_name",
    "active": "active",
    "emails": "emails",
    "emails.value": "email",
    'emails[type eq "work"].value': "email",
    "emails[primary eq true].value": "email",
}


def _user_patch(ops: list[dict]) -> dict:
    """The fields a PatchOp sets."""
    fields: dict[str, Any] = {}
    for op in ops:
        kind = op["op"].lower()
        path = op.get("path")
        value = op.get("value")
        if path is None:
            if kind == "remove" or not isinstance(value, dict):
                raise ScimError(400, "An operation without a path needs an object value", "noTarget")
            items = list(value.items())
        else:
            items = [(path, value)]
        for raw_path, val in items:
            target = _USER_PATHS.get(raw_path.strip().lower())
            if raw_path.lower().startswith(USER_SCHEMA.lower() + ":"):
                target = _USER_PATHS.get(raw_path[len(USER_SCHEMA) + 1 :].strip().lower())
            if target is None:
                if raw_path.lower() in ("name.givenname", "name.familyname", "name"):
                    if isinstance(val, dict):
                        fields["display_name"] = _display_name({"name": val})
                    continue
                # Attributes this server does not store (title, locale,
                # enterprise extension, ...) are accepted and ignored.
                continue
            if kind == "remove":
                if target in ("user_name", "active"):
                    raise ScimError(400, f"{raw_path} cannot be removed", "mutability")
                fields["email" if target == "emails" else target] = None
            elif target == "active":
                fields["active"] = _bool(val, "active")
            elif target == "emails":
                fields["email"] = _primary_email(val if isinstance(val, list) else [val])
            elif target == "user_name":
                fields["user_name"] = _text(val, "userName", 255, required=True)
            elif target == "external_id":
                fields["external_id"] = _text(val, "externalId", 255)
            elif target == "display_name":
                fields["display_name"] = _text(val, "displayName", 200)
            else:
                fields["email"] = _text(val, "emails.value", 320)
    return fields


@router.patch("/Users/{user_id}", summary="Change or deactivate a user")
async def patch_user(
    user_id: str,
    request: Request,
    payload: StorableJson = _BODY,
    caller: ScimCaller = _USERS,
) -> JSONResponse:
    ops = _patch_ops(payload)
    # Serialise with every other change that can drop an owner (#372).
    await lock_owners(request.state.db, caller.tenant_id)
    row = await _get_user(request.state.db, caller.tenant_id, user_id)
    return await _apply_user(request, caller, row, _user_patch(ops))


@router.delete("/Users/{user_id}", status_code=204, summary="Delete a user")
async def delete_user(user_id: str, request: Request, caller: ScimCaller = _USERS) -> Response:
    """Revokes the admin tokens and partner credentials issued for it (and
    every token they created), removes its memberships, then deletes it."""
    db = request.state.db
    await lock_owners(db, caller.tenant_id)
    row = await _get_user(db, caller.tenant_id, user_id)
    if await _is_owner(db, row.id):
        raise ScimError(
            403,
            "A SCIM token cannot delete an owner; deactivate it (active=false) or "
            "have an owner delete it through the admin API",
            "mutability",
        )
    now = _now()
    revoked = await idp_sync.revoke_principal_sessions(db, caller.tenant_id, row.id, now)
    tokens = (
        (await db.execute(select(AdminToken).where(AdminToken.principal_id == row.id)))
        .scalars()
        .all()
    )
    for t in tokens:
        if t.revoked_at is None:
            t.revoked_at = now
        t.principal_id = None
    creds = (
        (await db.execute(select(PartnerCredential).where(PartnerCredential.principal_id == row.id)))
        .scalars()
        .all()
    )
    for c in creds:
        c.is_active = "false"
        c.principal_id = None
    memberships = (
        (await db.execute(select(Membership).where(Membership.principal_id == row.id)))
        .scalars()
        .all()
    )
    for m in memberships:
        await db.delete(m)
    await db.flush()
    await db.delete(row)
    await _auditor(request, db, caller, 204)(
        "principal.delete",
        "principal",
        row.id,
        memberships_removed=[str(m.id) for m in memberships],
        revoked_token_ids=sorted({str(t.id) for t in [*tokens, *revoked]}),
        partner_credentials_revoked=[str(c.id) for c in creds],
    )
    await db.commit()
    return Response(status_code=204)


# ──────────────────────────────────────────────────────────────────────
# Groups
# ──────────────────────────────────────────────────────────────────────

GROUP_FILTERS = {"displayname": "name", "externalid": "external_id", "id": "id"}


async def _group_members(db: AsyncSession, team_id: UUID) -> list[Principal]:
    return list(
        (
            await db.execute(
                select(Principal)
                .join(Membership, Membership.principal_id == Principal.id)
                .where(
                    Membership.team_id == team_id,
                    Membership.project_id.is_(None),
                    Principal.kind == "user",
                )
                .order_by(Principal.created_at, Principal.id)
            )
        )
        .scalars()
        .all()
    )


def group_resource(request: Request, team: Team, members: list[Principal] | None) -> dict:
    out: dict[str, Any] = {
        "schemas": [GROUP_SCHEMA],
        "id": str(team.id),
        "displayName": team.name,
        "meta": {
            "resourceType": "Group",
            "created": _iso(team.created_at),
            "lastModified": _iso(team.updated_at or team.created_at),
            "location": f"{_base(request)}/Groups/{team.id}",
        },
    }
    if team.external_id:
        out["externalId"] = team.external_id
    if members is not None:
        out["members"] = [
            {
                "value": str(p.id),
                "display": p.display_name or p.user_name or p.email or str(p.id),
                "$ref": f"{_base(request)}/Users/{p.id}",
                "type": "User",
            }
            for p in members
        ]
    return out


async def _get_group(db: AsyncSession, tenant_id: UUID, group_id: str) -> Team:
    tid = _uuid(group_id, "Group")
    row = (
        await db.execute(select(Team).where(Team.id == tid, Team.tenant_id == tenant_id))
    ).scalar_one_or_none()
    if row is None:
        raise ScimError(404, "Group not found")
    return row


def _group_name(value: Any) -> str:
    return _text(value, "displayName", 100, required=True)


async def _check_group_name(db: AsyncSession, tenant_id: UUID, name: str, exclude=None) -> None:
    stmt = select(Team.id).where(Team.tenant_id == tenant_id, Team.name == name)
    if exclude is not None:
        stmt = stmt.where(Team.id != exclude)
    if (await db.execute(stmt)).first() is not None:
        raise ScimError(409, "A group (team) with this displayName already exists", "uniqueness")


def _member_ids(value: Any) -> set[UUID]:
    if value is None:
        return set()
    items = value if isinstance(value, list) else [value]
    if len(items) > MAX_MEMBERS_PER_REQUEST:
        raise ScimError(400, f"At most {MAX_MEMBERS_PER_REQUEST} members per request", "tooMany")
    out: set[UUID] = set()
    for item in items:
        raw = item.get("value") if isinstance(item, dict) else None
        try:
            out.add(UUID(raw))
        except (ValueError, TypeError, AttributeError):
            raise ScimError(400, "members[].value must be a User id", "invalidValue") from None
    return out


_MEMBER_FILTER_RE = re.compile(r'^\s*members\s*\[\s*value\s+eq\s+"([^"]+)"\s*\]\s*$', re.IGNORECASE)


async def _sync_members(
    request: Request,
    caller: ScimCaller,
    team: Team,
    *,
    add: set[UUID] = frozenset(),
    remove: set[UUID] = frozenset(),
    status: int = 200,
) -> None:
    """Add and remove team members (user principals), then bring each
    affected principal's tenant-wide role in line (collector/idp_sync.py)."""
    db = request.state.db
    now = _now()
    audit = _auditor(request, db, caller, status)
    add = set(add) - set(remove)
    if add:
        found = set(
            (
                await db.execute(
                    select(Principal.id).where(
                        Principal.tenant_id == caller.tenant_id,
                        Principal.id.in_(add),
                        Principal.kind == "user",
                    )
                )
            ).scalars()
        )
        missing = add - found
        if missing:
            raise ScimError(400, f"Unknown User ids in members: {sorted(map(str, missing))}", "invalidValue")
    existing = {
        m.principal_id: m
        for m in (
            await db.execute(
                select(Membership).where(
                    Membership.team_id == team.id, Membership.project_id.is_(None)
                )
            )
        ).scalars()
    }
    role = await idp_sync.member_role(db, team.id)
    affected: set[UUID] = set()
    for pid in sorted(add):
        m = existing.get(pid)
        if m is None:
            m = Membership(
                tenant_id=caller.tenant_id,
                principal_id=pid,
                team_id=team.id,
                role=role,
                created_by=idp_sync.SCIM_SOURCE,
                created_at=now,
            )
            db.add(m)
            await db.flush()
            await audit(
                "membership.add",
                "membership",
                m.id,
                principal_id=str(pid),
                team_id=str(team.id),
                team=team.name,
                role=role,
            )
        elif m.created_by != idp_sync.SCIM_SOURCE or m.role != role:
            # Adopted by sync: the group now decides its role.
            before = m.role
            m.created_by = idp_sync.SCIM_SOURCE
            m.role = role
            m.updated_at = now
            await audit(
                "role.change",
                "membership",
                m.id,
                principal_id=str(pid),
                team_id=str(team.id),
                team=team.name,
                role=role,
                previous_role=before,
            )
        affected.add(pid)
    for pid in sorted(remove):
        m = existing.get(pid)
        if m is None:
            continue
        await db.delete(m)
        await db.flush()
        await audit(
            "membership.remove",
            "membership",
            m.id,
            principal_id=str(pid),
            team_id=str(team.id),
            team=team.name,
            role=m.role,
        )
        affected.add(pid)
    for pid in sorted(affected):
        await idp_sync.recompute_tenant_role(db, caller.tenant_id, pid, now, audit)


async def _current_member_ids(db: AsyncSession, team_id: UUID) -> set[UUID]:
    return {p.id for p in await _group_members(db, team_id)}


@router.post("/Groups", status_code=201, summary="Create a group (team)")
async def create_group(
    request: Request, payload: StorableJson = _BODY, caller: ScimCaller = _GROUPS
) -> JSONResponse:
    db = request.state.db
    name = _group_name(payload.get("displayName"))
    external_id = _text(payload.get("externalId"), "externalId", 255)
    members = _member_ids(payload.get("members"))
    await _check_group_name(db, caller.tenant_id, name)
    now = _now()
    team = Team(tenant_id=caller.tenant_id, name=name, external_id=external_id, created_at=now)
    db.add(team)
    await db.flush()
    await _auditor(request, db, caller, 201)("team.create", "team", team.id, name=name)
    await _sync_members(request, caller, team, add=members, status=201)
    out = group_resource(request, team, await _group_members(db, team.id))
    await db.commit()
    return _scim_json(out, 201, {"Location": out["meta"]["location"]})


@router.get("/Groups", summary="List groups (teams)")
async def list_groups(
    request: Request,
    filter: str | None = Query(default=None, max_length=1000),
    start_index: int = Query(default=1, alias="startIndex"),
    count: int = Query(default=DEFAULT_COUNT),
    excluded_attributes: str | None = Query(default=None, alias="excludedAttributes"),
    caller: ScimCaller = _GROUPS,
) -> JSONResponse:
    db = request.state.db
    start_index, count = _page(start_index, count)
    stmt = select(Team).where(Team.tenant_id == caller.tenant_id)
    parsed = parse_filter(filter, GROUP_FILTERS)
    if parsed is not None:
        field, value = parsed
        if field == "id":
            try:
                stmt = stmt.where(Team.id == UUID(value))
            except ValueError:
                stmt = stmt.where(False)
        else:
            stmt = stmt.where(getattr(Team, field) == value)
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (
        (await db.execute(stmt.order_by(Team.created_at, Team.id).offset(start_index - 1).limit(count)))
        .scalars()
        .all()
    )
    skip_members = "members" in {
        a.strip().lower() for a in (excluded_attributes or "").split(",")
    }
    resources = [
        group_resource(request, t, None if skip_members else await _group_members(db, t.id))
        for t in rows
    ]
    return _scim_json(_list_response(resources, total, start_index))


@router.get("/Groups/{group_id}", summary="Get a group (team)")
async def get_group(
    group_id: str,
    request: Request,
    excluded_attributes: str | None = Query(default=None, alias="excludedAttributes"),
    caller: ScimCaller = _GROUPS,
) -> JSONResponse:
    db = request.state.db
    team = await _get_group(db, caller.tenant_id, group_id)
    skip = "members" in {a.strip().lower() for a in (excluded_attributes or "").split(",")}
    return _scim_json(group_resource(request, team, None if skip else await _group_members(db, team.id)))


async def _rename_group(
    request: Request, caller: ScimCaller, team: Team, name: str | None, external_id, set_external: bool
) -> None:
    db = request.state.db
    changed = []
    if name is not None and name != team.name:
        await _check_group_name(db, caller.tenant_id, name, team.id)
        team.name = name
        changed.append("name")
    if set_external and external_id != team.external_id:
        team.external_id = external_id
        changed.append("external_id")
    if changed:
        team.updated_at = _now()
        await db.flush()
        await _auditor(request, db, caller, 200)(
            "team.update", "team", team.id, fields=changed, name=team.name
        )


@router.put("/Groups/{group_id}", summary="Replace a group (team) and its members")
async def replace_group(
    group_id: str,
    request: Request,
    payload: StorableJson = _BODY,
    caller: ScimCaller = _GROUPS,
) -> JSONResponse:
    db = request.state.db
    team = await _get_group(db, caller.tenant_id, group_id)
    name = _group_name(payload.get("displayName"))
    external_id = _text(payload.get("externalId"), "externalId", 255)
    wanted = _member_ids(payload.get("members"))
    await _rename_group(request, caller, team, name, external_id, True)
    current = await _current_member_ids(db, team.id)
    await _sync_members(request, caller, team, add=wanted - current, remove=current - wanted)
    out = group_resource(request, team, await _group_members(db, team.id))
    await db.commit()
    return _scim_json(out)


@router.patch("/Groups/{group_id}", summary="Change a group (team) and its members")
async def patch_group(
    group_id: str,
    request: Request,
    payload: StorableJson = _BODY,
    caller: ScimCaller = _GROUPS,
) -> Response:
    db = request.state.db
    ops = _patch_ops(payload)
    team = await _get_group(db, caller.tenant_id, group_id)
    current = await _current_member_ids(db, team.id)
    wanted = set(current)
    name: str | None = None
    external_id = team.external_id
    set_external = False
    for op in ops:
        kind = op["op"].lower()
        path = (op.get("path") or "").strip()
        value = op.get("value")
        if not path:
            if kind == "remove" or not isinstance(value, dict):
                raise ScimError(400, "An operation without a path needs an object value", "noTarget")
            items = list(value.items())
        else:
            items = [(path, value)]
        for raw_path, val in items:
            p = raw_path.strip()
            if p.lower().startswith(GROUP_SCHEMA.lower() + ":"):
                p = p[len(GROUP_SCHEMA) + 1 :]
            m = _MEMBER_FILTER_RE.match(p)
            lower = p.lower()
            if m is not None:
                if kind != "remove":
                    raise ScimError(400, "Only remove takes a members[value eq ...] path", "invalidPath")
                wanted.discard(_uuid(m.group(1), "User"))
            elif lower == "members":
                ids = _member_ids(val)
                if kind == "add":
                    wanted |= ids
                elif kind == "replace":
                    wanted = ids
                elif val is None:
                    wanted = set()
                else:
                    wanted -= ids
            elif lower == "displayname":
                if kind == "remove":
                    raise ScimError(400, "displayName cannot be removed", "mutability")
                name = _group_name(val)
            elif lower == "externalid":
                external_id = None if kind == "remove" else _text(val, "externalId", 255)
                set_external = True
            else:
                raise ScimError(400, f"Unsupported path: {raw_path}", "invalidPath")
    await _rename_group(request, caller, team, name, external_id, set_external)
    await _sync_members(request, caller, team, add=wanted - current, remove=current - wanted)
    out = group_resource(request, team, await _group_members(db, team.id))
    await db.commit()
    return _scim_json(out)


@router.delete("/Groups/{group_id}", status_code=204, summary="Delete a group (team)")
async def delete_group(group_id: str, request: Request, caller: ScimCaller = _GROUPS) -> Response:
    db = request.state.db
    team = await _get_group(db, caller.tenant_id, group_id)
    has_projects = (
        await db.execute(select(Project.id).where(Project.team_id == team.id))
    ).first() is not None
    if has_projects:
        raise ScimError(409, "The team has projects; move or delete them first", "mutability")
    await _sync_members(request, caller, team, remove=await _current_member_ids(db, team.id), status=204)
    # Any remaining memberships (service accounts) go with the team
    # (ON DELETE CASCADE), and so does its group role mapping.
    await db.delete(team)
    await db.flush()
    await _auditor(request, db, caller, 204)("team.delete", "team", team.id, name=team.name)
    await db.commit()
    return Response(status_code=204)
