import asyncio
from uuid import UUID

from db.api_keys import hash_api_key, verify_api_key
from fastapi import HTTPException, Request

from collector import rls, tenant_keys

# Checked when no key matches the lookup prefix, so an unknown key costs the
# same bcrypt work as a known key with a wrong secret.
_DUMMY_HASH = hash_api_key("ps-dummy-key-for-constant-time-miss")


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status_code=401,
        detail={
            "type": "about:blank",
            "title": "Unauthorized",
            "status": 401,
            "detail": detail,
        },
    )


def tenant_disabled() -> HTTPException:
    return HTTPException(
        status_code=403,
        detail={
            "type": "about:blank",
            "title": "Forbidden",
            "status": 403,
            "detail": "Tenant is disabled",
        },
    )


def bearer_token(request: Request) -> str:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise _unauthorized("Missing or invalid Authorization header")
    return auth.removeprefix("Bearer ").strip()


async def resolve_tenant(request: Request) -> UUID:
    """Extract Bearer token, resolve to tenant_id.

    The key is found by its SHA-256 lookup prefix (in tenant_api_keys, then
    the legacy key on the tenant row; see collector/tenant_keys.py) and
    checked against the stored bcrypt hash. Unknown and wrong keys get the
    same 401 after the same bcrypt work. A revoked or expired key, or a
    rotated-out key past its grace window, is refused after the check.

    On success the request's session is scoped to the tenant for Postgres
    row-level security (collector/rls.py).
    """
    api_key = bearer_token(request)

    db = request.state.db
    async with rls.auth_lookup(db):
        candidate = await tenant_keys.find_candidate(db, api_key)

    stored_hash = candidate.stored_hash if candidate is not None else _DUMMY_HASH
    # bcrypt takes tens of ms; keep it off the event loop.
    valid = await asyncio.to_thread(verify_api_key, api_key, stored_hash)

    if candidate is None or not valid:
        raise _unauthorized("Invalid API key")
    if not candidate.live:
        raise _unauthorized("API key revoked or expired")
    if candidate.tenant_disabled:
        raise tenant_disabled()
    await rls.set_tenant(db, candidate.tenant_id)
    return candidate.tenant_id
