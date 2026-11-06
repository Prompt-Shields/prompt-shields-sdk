"""How a usage event's ``user_id`` is stored (#334).

The gateway (OIDC subject) and the SDK (``ps_metadata.user_id``) send an
opaque per-user identifier with each event. The collector never stores it as
sent unless the tenant asked for that:

* By default only ``user_id_hash`` is stored: HMAC-SHA256 of the value, keyed
  with a per-tenant salt kept in ``tenants.settings["user_id_salt"]``. The
  salt is generated on the first event that carries a ``user_id`` and never
  leaves the database: the operator tenants API leaves it out of every
  response and refuses to set or clear it. Without the salt, a hash cannot be
  matched to a guessed user id, and hashes of one user differ across tenants.
* A tenant whose ``settings["store_raw_user_ids"]`` is ``true`` gets the value
  as sent in ``user_id_raw`` instead (and no hash).

Changing the setting applies to events ingested afterwards; stored rows are
not rewritten.
"""

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from db.models import Tenant
from sqlalchemy import select, text

SALT_KEY = "user_id_salt"
STORE_RAW_KEY = "store_raw_user_ids"
# Settings no API returns, sets or clears.
PROTECTED_SETTINGS = frozenset({SALT_KEY})

# Sets the salt only if none is stored yet, so concurrent first ingests agree:
# the second UPDATE waits for the first's row lock, then finds a salt and
# changes nothing (READ COMMITTED re-checks the WHERE on the new row).
_SET_SALT = text(
    """
    UPDATE tenants
    SET settings = (
        CASE WHEN jsonb_typeof(settings) = 'object' THEN settings
             ELSE '{}'::jsonb END
    ) || jsonb_build_object(CAST(:key AS text), CAST(:salt AS text))
    WHERE id = :tenant_id AND (
        jsonb_typeof(settings) IS DISTINCT FROM 'object'
        OR jsonb_typeof(settings -> CAST(:key AS text)) IS DISTINCT FROM 'string'
        OR settings ->> CAST(:key AS text) = ''
    )
    """
)


@dataclass(frozen=True)
class UserIdPolicy:
    store_raw: bool
    salt: str | None = None

    def columns(self, user_id: str | None) -> dict[str, str | None]:
        """The ``user_id_hash`` / ``user_id_raw`` values for one event."""
        if user_id is None:
            return {"user_id_hash": None, "user_id_raw": None}
        if self.store_raw:
            return {"user_id_hash": None, "user_id_raw": user_id}
        return {"user_id_hash": hash_user_id(self.salt, user_id), "user_id_raw": None}


def hash_user_id(salt: str, user_id: str) -> str:
    # surrogatepass: a value the database could not store never reaches
    # here (ingest drops it), but hashing must not raise regardless.
    return hmac.new(
        salt.encode(), user_id.encode("utf-8", "surrogatepass"), hashlib.sha256
    ).hexdigest()


def store_raw(settings: Any) -> bool:
    return isinstance(settings, dict) and settings.get(STORE_RAW_KEY) is True


def public_settings(settings: Any) -> dict:
    """Tenant settings as an API may return them: without the salt."""
    return {k: v for k, v in (settings or {}).items() if k not in PROTECTED_SETTINGS}


async def _settings(db, tenant_id: UUID) -> Any:
    return (
        await db.execute(select(Tenant.settings).where(Tenant.id == tenant_id))
    ).scalar_one_or_none()


async def policy_for(db, tenant_id: UUID) -> UserIdPolicy:
    """The tenant's policy, generating its salt if it has none yet (and
    hashes are to be stored). Runs in the caller's transaction."""
    settings = await _settings(db, tenant_id)
    if store_raw(settings):
        return UserIdPolicy(store_raw=True)
    salt = settings.get(SALT_KEY) if isinstance(settings, dict) else None
    if not isinstance(salt, str) or not salt:
        await db.execute(
            _SET_SALT,
            {"key": SALT_KEY, "salt": secrets.token_hex(32), "tenant_id": tenant_id},
        )
        settings = await _settings(db, tenant_id)
        salt = settings.get(SALT_KEY) if isinstance(settings, dict) else None
        if not isinstance(salt, str) or not salt:
            raise RuntimeError("tenant user_id salt could not be stored")
    return UserIdPolicy(store_raw=False, salt=salt)
