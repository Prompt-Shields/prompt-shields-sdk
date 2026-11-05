import asyncio
from uuid import UUID

from db.api_keys import hash_api_key, verify_api_key
from fastapi import HTTPException, Request

from collector import audit_trail, auth_cache, rls, tenant_keys

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


def too_many_failures(retry_after: int) -> HTTPException:
    return HTTPException(
        status_code=429,
        detail={
            "type": "about:blank",
            "title": "Too Many Requests",
            "status": 429,
            "detail": "Too many failed authentications from this address. "
            f"Retry after {retry_after} seconds.",
        },
        headers={"Retry-After": str(retry_after)},
    )


def failed_auth_client(request: Request) -> str:
    """Failed-auth limiter key: the client address as uvicorn reports it
    (X-Forwarded-For only from FORWARDED_ALLOW_IPS proxies)."""
    client = getattr(request, "client", None)
    return auth_cache.client_key(client.host if client else None)


def check_failed_auth_limit(request: Request) -> str:
    """429 before a bcrypt check when this client has failed too often.
    Returns the limiter key for ``record_failed_auth``."""
    client = failed_auth_client(request)
    retry_after = auth_cache.get_failed_auth_limiter().retry_after(client)
    if retry_after:
        raise too_many_failures(retry_after)
    return client


def record_failed_auth(client: str) -> None:
    auth_cache.get_failed_auth_limiter().record_failure(client)


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


# Tenant admin surface: a refused key here is an audit trail auth.failure.
ADMIN_PATH_PREFIX = "/api/v1/admin/"


async def _audit_admin_failure(request: Request, reason: str) -> None:
    path = getattr(getattr(request, "url", None), "path", "")
    if path.startswith(ADMIN_PATH_PREFIX):
        await audit_trail.record_auth_failure(
            request, realm="tenant_admin", reason=reason, status_code=401
        )


async def bearer_token(request: Request) -> str:
    """The Bearer token, or 401 (an audit trail auth.failure on admin routes)."""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        await _audit_admin_failure(request, "missing_credentials")
        raise _unauthorized("Missing or invalid Authorization header")
    return auth.removeprefix("Bearer ").strip()


async def resolve_tenant(request: Request) -> UUID:
    """Extract Bearer token, resolve to tenant_id.

    The key is found by its SHA-256 lookup prefix (in tenant_api_keys, then
    the legacy key on the tenant row; see collector/tenant_keys.py) and
    checked against the stored bcrypt hash. Unknown and wrong keys get the
    same 401 after the same bcrypt work. A revoked or expired key, or a
    rotated-out key past its grace window, is refused after the check.

    A key that passed recently skips bcrypt while its stored hash is
    unchanged, and a client with too many failed checks gets 429 instead of
    another bcrypt check for a key that is unknown or not live
    (collector/auth_cache.py).

    On success the request's session is scoped to the tenant for Postgres
    row-level security (collector/rls.py).
    """
    api_key = await bearer_token(request)

    db = request.state.db
    async with rls.auth_lookup(db):
        candidate = await tenant_keys.find_candidate(db, api_key)

    cache = auth_cache.get_key_cache()
    key_id = (
        auth_cache.cache_key_id(candidate.key_id, candidate.tenant_id)
        if candidate is not None
        else ""
    )
    if candidate is not None and cache.hit(
        api_key, candidate.tenant_id, key_id, candidate.stored_hash
    ):
        # Verified against this same stored hash within the TTL
        # (collector/auth_cache.py); the row read above is current.
        valid, client = True, None
    else:
        if candidate is None or not candidate.live:
            # Only a key that cannot get in is held back. The lookup prefix
            # is 64 bits of SHA-256 over the whole key, so a live candidate
            # means the caller holds the real key: a client sharing its
            # address (a proxy, NAT) cannot lock valid keys out.
            client = check_failed_auth_limit(request)
        else:
            client = failed_auth_client(request)
        stored_hash = candidate.stored_hash if candidate is not None else _DUMMY_HASH
        # bcrypt takes tens of ms; keep it off the event loop.
        valid = await asyncio.to_thread(verify_api_key, api_key, stored_hash)

    if candidate is None or not valid:
        record_failed_auth(client)
        await _audit_admin_failure(request, "invalid_credentials")
        raise _unauthorized("Invalid API key")
    if not candidate.live:
        if client is not None:
            record_failed_auth(client)
        await _audit_admin_failure(request, "invalid_credentials")
        raise _unauthorized("API key revoked or expired")
    if candidate.tenant_disabled:
        raise tenant_disabled()
    if client is not None:
        cache.put(api_key, candidate.tenant_id, key_id, candidate.stored_hash)
    await rls.set_tenant(db, candidate.tenant_id)
    return candidate.tenant_id
