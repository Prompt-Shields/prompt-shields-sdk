"""How a usage event's ``user_id`` is stored (#334).

The gateway (OIDC subject) and the SDK (``ps_metadata.user_id``) send an
opaque per-user identifier with each event. The collector never stores it as
sent unless the tenant asked for that:

* ``user_id_hash`` is always stored: HMAC-SHA256 of the value, keyed with a
  per-tenant salt kept in ``tenant_secrets.user_id_salt``. It is the join key
  for per-user usage and erasure-by-user. The salt is generated on the first
  event that carries a ``user_id`` and never leaves the database: no API,
  export or log reads ``tenant_secrets``. Without the salt, a hash cannot be
  matched to a guessed user id, and hashes of one user differ across tenants.
* A tenant whose ``settings["store_raw_user_ids"]`` is ``true`` also gets the
  value as sent in ``user_id_raw``.

The salt lives in its own table, not in ``tenants.settings``, so the code
that rewrites the settings document (operator API, retention, connectors)
can never overwrite it: it is inserted once with ``ON CONFLICT DO NOTHING``
and never updated.

Changing the setting applies to events ingested afterwards; stored rows are
not rewritten.
"""

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from db.models import Tenant, TenantSecret
from sqlalchemy import select, text

SALT_KEY = "user_id_salt"
STORE_RAW_KEY = "store_raw_user_ids"
# Settings keys the tenants API refuses to set: the salt is not a setting.
PROTECTED_SETTINGS = frozenset({SALT_KEY})

# Concurrent first ingests agree: the second INSERT waits on the first's
# uncommitted key, then does nothing; the SELECT that follows (a new
# statement under READ COMMITTED) sees the committed salt.
_INSERT_SALT = text(
    "INSERT INTO tenant_secrets (tenant_id, user_id_salt) VALUES (:tenant_id, :salt) "
    "ON CONFLICT (tenant_id) DO NOTHING"
)


@dataclass(frozen=True)
class UserIdPolicy:
    salt: str
    store_raw: bool = False

    def columns(self, user_id: str | None) -> dict[str, str | None]:
        """The ``user_id_hash`` / ``user_id_raw`` values for one event."""
        if user_id is None:
            return {"user_id_hash": None, "user_id_raw": None}
        return {
            "user_id_hash": hash_user_id(self.salt, user_id),
            "user_id_raw": user_id if self.store_raw else None,
        }


def hash_user_id(salt: str, user_id: str) -> str:
    # surrogatepass: a value the database could not store never reaches
    # here (ingest drops it), but hashing must not raise regardless.
    return hmac.new(
        salt.encode(), user_id.encode("utf-8", "surrogatepass"), hashlib.sha256
    ).hexdigest()


def store_raw(settings: Any) -> bool:
    return isinstance(settings, dict) and settings.get(STORE_RAW_KEY) is True


async def _salt(db, tenant_id: UUID) -> str | None:
    return (
        await db.execute(
            select(TenantSecret.user_id_salt).where(TenantSecret.tenant_id == tenant_id)
        )
    ).scalar_one_or_none()


async def tenant_salt(db, tenant_id: UUID) -> str:
    """The tenant's salt, created on first use. Runs in the caller's
    transaction (in tenant context under RLS)."""
    salt = await _salt(db, tenant_id)
    if salt is None:
        await db.execute(
            _INSERT_SALT, {"tenant_id": tenant_id, "salt": secrets.token_hex(32)}
        )
        salt = await _salt(db, tenant_id)
        if not salt:
            raise RuntimeError("tenant user_id salt could not be stored")
    return salt


async def policy_for(db, tenant_id: UUID) -> UserIdPolicy:
    """The tenant's user_id policy (salt created if it has none yet)."""
    settings = (
        await db.execute(select(Tenant.settings).where(Tenant.id == tenant_id))
    ).scalar_one_or_none()
    return UserIdPolicy(salt=await tenant_salt(db, tenant_id), store_raw=store_raw(settings))
