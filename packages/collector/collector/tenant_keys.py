"""Tenant ingest keys: issue, rotate with a grace window, revoke.

Keys live in ``tenant_api_keys`` (one row per key, stable ``id``), hashed
like the legacy key (db/api_keys.py): a SHA-256 lookup prefix and a bcrypt
hash. The plaintext is returned once, by issue and rotate.

Legacy keys
-----------
Before this table a tenant had one key, on ``tenants.api_key_prefix`` /
``api_key_hash`` (still written by the demo seed and older fixtures). They
keep working: auth checks this table first and then the tenant row. A
legacy key is moved into this table, under the name ``legacy-ingest-key``
and ``origin = 'legacy'``, by migration 022, or for a key written to the
tenant row later by the first key *write* for that tenant (issuing a key,
``adopt_legacy_key``), so it can be listed, rotated and revoked like any
other. Listing keys never writes: until then the list does not show it. Moving clears the tenant-row columns, so a revoked
key cannot come back through the fallback. The key itself does not change.

Validity
--------
A key authenticates when it is not revoked, not past ``expires_at``, and
either is the current key or is the previous key of a rotation whose grace
window (``previous_expires_at``) has not ended.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from db.api_keys import api_key_lookup_prefix, hash_api_key
from db.models import Tenant, TenantApiKey
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

KEY_PREFIX = "ps-ik-"
LEGACY_KEY_NAME = "legacy-ingest-key"
# Longest rotation grace window, as for webhook secrets.
MAX_ROTATION_GRACE = timedelta(days=7)
KEY_WARNING = "Save the key now. It will not be shown again."

STATUSES = ("active", "expired", "revoked")


def generate_key() -> str:
    return KEY_PREFIX + secrets.token_urlsafe(32)


def hint(plaintext: str) -> str:
    return plaintext[-4:]


def key_status(row, now: datetime | None = None) -> str:
    """'revoked', 'expired' or 'active' for a key or admin token row."""
    now = now or datetime.now(UTC)
    if row.revoked_at is not None:
        return "revoked"
    if row.expires_at is not None and row.expires_at <= now:
        return "expired"
    return "active"


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def grace_ends_at(row: TenantApiKey, now: datetime | None = None) -> datetime | None:
    now = now or datetime.now(UTC)
    if (
        row.previous_key_hash
        and row.previous_expires_at
        and row.previous_expires_at > now
        and key_status(row, now) == "active"
    ):
        return row.previous_expires_at
    return None


def serialize_key(row: TenantApiKey) -> dict:
    """Metadata only: never the key or its hashes."""
    now = datetime.now(UTC)
    return {
        "id": str(row.id),
        "name": row.name,
        "key_hint": row.key_hint,
        "origin": row.origin or "api",
        "status": key_status(row, now),
        # Set while the key replaced by the last rotation still authenticates.
        "rotation_grace_ends_at": _iso(grace_ends_at(row, now)),
        "created_at": _iso(row.created_at),
        "rotated_at": _iso(row.rotated_at),
        "expires_at": _iso(row.expires_at),
        "revoked_at": _iso(row.revoked_at),
    }


class DuplicateName(Exception):
    """A live key (or token) already has this name."""


class NotRotatable(Exception):
    """The key is revoked or expired."""


async def _name_taken(db: AsyncSession, tenant_id: UUID, name: str) -> bool:
    return (
        await db.execute(
            select(TenantApiKey.id).where(
                TenantApiKey.tenant_id == tenant_id,
                TenantApiKey.name == name,
                TenantApiKey.revoked_at.is_(None),
            )
        )
    ).first() is not None


async def adopt_legacy_key(db: AsyncSession, tenant_id: UUID) -> TenantApiKey | None:
    """Move the tenant row's legacy key into tenant_api_keys (see module doc).

    Returns the new row, or None when the tenant has no legacy key. Flushes;
    the caller commits.
    """
    tenant = (
        await db.execute(select(Tenant).where(Tenant.id == tenant_id))
    ).scalar_one_or_none()
    if tenant is None or not tenant.api_key_hash or not tenant.api_key_prefix:
        return None
    name, n = LEGACY_KEY_NAME, 1
    while await _name_taken(db, tenant_id, name):
        n += 1
        name = f"{LEGACY_KEY_NAME}-{n}"
    row = TenantApiKey(
        tenant_id=tenant_id,
        name=name,
        key_prefix=tenant.api_key_prefix,
        key_hash=tenant.api_key_hash,
        key_hint=None,
        origin="legacy",
        created_at=tenant.created_at or datetime.now(UTC),
    )
    tenant.api_key_prefix = None
    tenant.api_key_hash = None
    # Clear the tenant row in the same transaction, so auth never finds the
    # key in both places.
    await db.flush()
    db.add(row)
    await db.flush()
    return row


async def issue_key(
    db: AsyncSession,
    tenant_id: UUID,
    name: str,
    *,
    expires_at: datetime | None = None,
) -> tuple[TenantApiKey, str]:
    """Create a key. Returns (row, plaintext). Flushes; the caller commits."""
    if await _name_taken(db, tenant_id, name):
        raise DuplicateName(name)
    plaintext = generate_key()
    row = TenantApiKey(
        tenant_id=tenant_id,
        name=name,
        key_prefix=api_key_lookup_prefix(plaintext),
        key_hash=hash_api_key(plaintext),
        key_hint=hint(plaintext),
        origin="api",
        created_at=datetime.now(UTC),
        expires_at=expires_at,
    )
    db.add(row)
    await db.flush()
    return row, plaintext


def rotate_key(row: TenantApiKey, grace: timedelta) -> str:
    """Give the key a new secret, keeping the same id. The old secret keeps
    authenticating for ``grace`` (zero ends it now). Returns the plaintext."""
    now = datetime.now(UTC)
    if key_status(row, now) != "active":
        raise NotRotatable(key_status(row, now))
    plaintext = generate_key()
    if grace > timedelta(0):
        # Only the secret being replaced is kept; one still in an earlier
        # grace window ends now.
        row.previous_key_prefix = row.key_prefix
        row.previous_key_hash = row.key_hash
        row.previous_expires_at = now + grace
    else:
        row.previous_key_prefix = None
        row.previous_key_hash = None
        row.previous_expires_at = None
    row.key_prefix = api_key_lookup_prefix(plaintext)
    row.key_hash = hash_api_key(plaintext)
    row.key_hint = hint(plaintext)
    row.rotated_at = now
    return plaintext


def revoke_key(row: TenantApiKey) -> bool:
    """Revoke (idempotent). Returns True if this call revoked it."""
    if row.revoked_at is not None:
        return False
    row.revoked_at = datetime.now(UTC)
    row.previous_key_prefix = None
    row.previous_key_hash = None
    row.previous_expires_at = None
    return True


# ──────────────────────────────────────────────────────────────────────
# Auth lookup (collector/auth.py)
# ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class KeyCandidate:
    """The one stored hash a presented key could match, and whether a match
    would be allowed in."""

    tenant_id: UUID
    stored_hash: str
    live: bool
    tenant_disabled: bool


async def find_candidate(db: AsyncSession, plaintext: str) -> KeyCandidate | None:
    """Find the hash to check ``plaintext`` against (current or previous
    secret in tenant_api_keys, then the legacy tenant row). Run inside
    ``rls.auth_lookup``."""
    prefix = api_key_lookup_prefix(plaintext)
    now = datetime.now(UTC)
    row = (
        await db.execute(
            select(TenantApiKey, Tenant.disabled_at)
            .join(Tenant, Tenant.id == TenantApiKey.tenant_id)
            .where(
                or_(
                    TenantApiKey.key_prefix == prefix,
                    TenantApiKey.previous_key_prefix == prefix,
                )
            )
        )
    ).first()
    if row is not None:
        key, disabled_at = row
        active = key_status(key, now) == "active"
        if key.key_prefix == prefix:
            return KeyCandidate(
                key.tenant_id, key.key_hash, active, disabled_at is not None
            )
        in_grace = bool(key.previous_expires_at and key.previous_expires_at > now)
        return KeyCandidate(
            key.tenant_id,
            key.previous_key_hash or "",
            active and in_grace,
            disabled_at is not None,
        )

    legacy = (
        await db.execute(
            select(Tenant.id, Tenant.api_key_hash, Tenant.disabled_at).where(
                Tenant.api_key_prefix == prefix,
                Tenant.api_key_hash.is_not(None),
            )
        )
    ).one_or_none()
    if legacy is None:
        return None
    return KeyCandidate(
        legacy.id, legacy.api_key_hash, True, legacy.disabled_at is not None
    )
