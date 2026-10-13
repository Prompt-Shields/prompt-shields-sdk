import asyncio
from uuid import UUID

from db.api_keys import api_key_lookup_prefix, hash_api_key, verify_api_key
from db.models import Tenant
from fastapi import HTTPException, Request
from sqlalchemy import select

# Checked when no tenant matches the key's lookup prefix, so an unknown key
# costs the same bcrypt work as a known key with a wrong secret.
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


async def resolve_tenant(request: Request) -> UUID:
    """Extract Bearer token, resolve to tenant_id.

    The key is found by its SHA-256 lookup prefix and checked against the
    stored bcrypt hash. Unknown and wrong keys get the same 401 after the
    same bcrypt work.
    """
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise _unauthorized("Missing or invalid Authorization header")
    api_key = auth.removeprefix("Bearer ").strip()

    db = request.state.db
    stmt = select(Tenant.id, Tenant.api_key_hash).where(
        Tenant.api_key_prefix == api_key_lookup_prefix(api_key),
        Tenant.api_key_hash.is_not(None),
    )
    row = (await db.execute(stmt)).one_or_none()

    stored_hash = row.api_key_hash if row is not None else _DUMMY_HASH
    # bcrypt takes tens of ms; keep it off the event loop.
    valid = await asyncio.to_thread(verify_api_key, api_key, stored_hash)

    if row is None or not valid:
        raise _unauthorized("Invalid API key")
    return row.id
