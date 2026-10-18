"""Per-tenant provider admin credentials and key fingerprint links (#93).

Credentials
-----------
A tenant's OpenAI / Anthropic admin key for the org usage pull
(collector.provider_usage) is stored in ``provider_credentials``, sealed
with the PS_WEBHOOK_SECRET_KEYS keyring (collector.secret_keyring) under
its own purpose label and bound to the row id. The key is write-only:
nothing returns it. Reads show a masked hint (last four characters), the
key id it is encrypted under and timestamps. ``PS_PROVIDER_ADMIN_KEYS``
(env) still works as a fallback for tenants with no stored credential.

A stored credential under an older keyring key is re-encrypted with the
active key the next time a pull uses it.

Key fingerprint links (corroboration)
-------------------------------------
The SDK sends ``api_key_fingerprint`` = ``sha256(api_key)[:16]`` with each
event. Provider usage APIs report only an opaque key id (OpenAI
``key_...``, Anthropic ``apikey_...``); their key-listing APIs add a
redacted preview (OpenAI ``redacted_value``, Anthropic
``partial_key_hint``) but never the key or a hash of it. Neither is enough
to compute or safely match the fingerprint, so the collector does not try.
Instead the tenant registers ``(provider, api_key_id) -> fingerprint``,
computing the fingerprint locally where the key already lives.

`corroborate_by_fingerprint` then joins the two sightings: a
platform_signal asset for key K and model M, and SDK usage events carrying
K's fingerprint for vendor = provider and model M (asset model, requested
or served model). Each asset gets the other's source, so both become
``verified`` (two independent sources). It runs after every usage pull and
after a link is registered. Gateway events carry no fingerprint, so
gateway sightings are not matched by key.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from uuid import UUID, uuid4

from db.models import AIAsset, AIUsageEvent, ProviderCredential, ProviderKeyFingerprint
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from collector import secret_keyring
from collector.dedup import compute_confidence
from collector.webhooks import AssetChanges

PROVIDERS: tuple[str, ...] = ("openai", "anthropic")
PLATFORM_SOURCE = "platform_signal"

# Purpose label for HKDF: a webhook token never opens as a credential.
_INFO = b"prompt-shields provider admin credentials v1"

MIN_ADMIN_KEY_LENGTH = 20
MAX_ADMIN_KEY_LENGTH = 512
FINGERPRINT_RE = re.compile(r"[0-9a-f]{16}")
API_KEY_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,128}")

CredentialsNotConfigured = secret_keyring.KeyringNotConfigured


def _utcnow() -> datetime:
    return datetime.now(UTC)


# ──────────────────────────────────────────────────────────────────────
# Credentials
# ──────────────────────────────────────────────────────────────────────


def validate_admin_key(value: str) -> str:
    """Return the trimmed key or raise ValueError. Messages never echo it."""
    key = value.strip()
    if not MIN_ADMIN_KEY_LENGTH <= len(key) <= MAX_ADMIN_KEY_LENGTH:
        raise ValueError(
            f"admin_key must be {MIN_ADMIN_KEY_LENGTH}-{MAX_ADMIN_KEY_LENGTH} characters"
        )
    if not key.isascii() or not key.isprintable() or any(c.isspace() for c in key):
        raise ValueError("admin_key must be printable ASCII without whitespace")
    return key


def key_hint(key: str) -> str:
    return "****" + key[-4:]


def seal_credential(key: str, credential_id: UUID) -> str:
    """Raises CredentialsNotConfigured without a keyring."""
    return secret_keyring.seal(key, credential_id.bytes, _INFO)


def open_credential(row: ProviderCredential) -> str:
    """Raises ValueError (SecretKeyMissing for an unknown key id)."""
    return secret_keyring.unseal(row.secret_ciphertext, row.id.bytes, _INFO)


def serialize_credential(row: ProviderCredential) -> dict:
    """Public view. Never includes the key or its ciphertext."""
    try:
        kid = secret_keyring.token_key_id(row.secret_ciphertext)
    except ValueError:
        kid = None
    return {
        "provider": row.provider,
        "key_hint": row.key_hint,
        "key_id": kid,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "rotated_at": row.rotated_at.isoformat() if row.rotated_at else None,
    }


async def get_credential(
    db: AsyncSession, tenant_id: UUID, provider: str
) -> ProviderCredential | None:
    return (
        await db.execute(
            select(ProviderCredential).where(
                ProviderCredential.tenant_id == tenant_id,
                ProviderCredential.provider == provider,
            )
        )
    ).scalar_one_or_none()


async def list_credentials(
    db: AsyncSession, tenant_id: UUID
) -> list[ProviderCredential]:
    return list(
        (
            await db.execute(
                select(ProviderCredential)
                .where(ProviderCredential.tenant_id == tenant_id)
                .order_by(ProviderCredential.provider)
            )
        ).scalars()
    )


async def create_credential(
    db: AsyncSession, tenant_id: UUID, provider: str, admin_key: str
) -> ProviderCredential:
    """Store a new credential. Does not commit. The caller checks there is
    none yet for (tenant, provider)."""
    key = validate_admin_key(admin_key)
    cred_id = uuid4()
    row = ProviderCredential(
        id=cred_id,
        tenant_id=tenant_id,
        provider=provider,
        secret_ciphertext=seal_credential(key, cred_id),
        key_hint=key_hint(key),
        created_at=_utcnow(),
    )
    db.add(row)
    return row


def rotate_credential(row: ProviderCredential, admin_key: str) -> None:
    """Replace the stored key (sealed under the active keyring key)."""
    key = validate_admin_key(admin_key)
    row.secret_ciphertext = seal_credential(key, row.id)
    row.key_hint = key_hint(key)
    row.rotated_at = _utcnow()


def rekey_if_needed(row: ProviderCredential, key: str) -> bool:
    """Re-seal under the active keyring key if `row` is under another.
    Does not commit. Returns True if it changed the row."""
    try:
        if secret_keyring.token_key_id(row.secret_ciphertext) == (
            secret_keyring.active_keyring_id()
        ):
            return False
    except ValueError:
        return False
    row.secret_ciphertext = seal_credential(key, row.id)
    return True


# ──────────────────────────────────────────────────────────────────────
# Key fingerprint links
# ──────────────────────────────────────────────────────────────────────


def validate_fingerprint(value: str) -> str:
    fp = value.strip().lower()
    if not FINGERPRINT_RE.fullmatch(fp):
        raise ValueError(
            "api_key_fingerprint must be 16 hex characters: the first 16 of "
            "sha256(api key), as the SDK sends it"
        )
    return fp


def validate_api_key_id(value: str) -> str:
    if not API_KEY_ID_RE.fullmatch(value):
        raise ValueError("api_key_id must be 1-128 characters from A-Z a-z 0-9 _ -")
    return value


def serialize_link(row: ProviderKeyFingerprint) -> dict:
    return {
        "provider": row.provider,
        "api_key_id": row.api_key_id,
        "api_key_fingerprint": row.api_key_fingerprint,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


async def list_links(db: AsyncSession, tenant_id: UUID) -> list[ProviderKeyFingerprint]:
    return list(
        (
            await db.execute(
                select(ProviderKeyFingerprint)
                .where(ProviderKeyFingerprint.tenant_id == tenant_id)
                .order_by(
                    ProviderKeyFingerprint.provider, ProviderKeyFingerprint.api_key_id
                )
            )
        ).scalars()
    )


async def get_link(
    db: AsyncSession, tenant_id: UUID, provider: str, api_key_id: str
) -> ProviderKeyFingerprint | None:
    return (
        await db.execute(
            select(ProviderKeyFingerprint).where(
                ProviderKeyFingerprint.tenant_id == tenant_id,
                ProviderKeyFingerprint.provider == provider,
                ProviderKeyFingerprint.api_key_id == api_key_id,
            )
        )
    ).scalar_one_or_none()


async def put_link(
    db: AsyncSession, tenant_id: UUID, provider: str, api_key_id: str, fingerprint: str
) -> tuple[ProviderKeyFingerprint, bool]:
    """Create or replace a link. Does not commit. Returns (row, created)."""
    api_key_id = validate_api_key_id(api_key_id)
    fingerprint = validate_fingerprint(fingerprint)
    row = await get_link(db, tenant_id, provider, api_key_id)
    if row is not None:
        row.api_key_fingerprint = fingerprint
        return row, False
    row = ProviderKeyFingerprint(
        tenant_id=tenant_id,
        provider=provider,
        api_key_id=api_key_id,
        api_key_fingerprint=fingerprint,
        created_at=_utcnow(),
    )
    db.add(row)
    return row, True


def _platform_key_id(asset: AIAsset) -> str | None:
    """Key id from a platform_signal asset's calling_service
    ("<provider>:<project|workspace>=<id>:key=<key id>")."""
    _, sep, key = (asset.calling_service or "").rpartition(":key=")
    return key if sep and key and key != "none" else None


def _add_source(asset: AIAsset, source: str) -> bool:
    sources = list(asset.discovery_source or [])
    if source in sources:
        return False
    asset.discovery_source = sources + [source]
    asset.confidence = compute_confidence(asset.discovery_source)
    return True


async def corroborate_by_fingerprint(db: AsyncSession, tenant_id: UUID) -> int:
    """Cross-mark platform_signal assets and SDK assets that saw the same
    key (by registered fingerprint link) and model. Does not commit.

    Returns the number of platform_signal assets that gained a source.
    """
    links = await list_links(db, tenant_id)
    if not links:
        return 0
    by_provider_key = {
        (link.provider, link.api_key_id): link.api_key_fingerprint for link in links
    }
    providers = sorted({link.provider for link in links})

    platform_assets = [
        a
        for a in (
            await db.execute(
                select(AIAsset).where(
                    AIAsset.tenant_id == tenant_id, AIAsset.vendor.in_(providers)
                )
            )
        ).scalars()
        if PLATFORM_SOURCE in (a.discovery_source or [])
    ]
    if not platform_assets:
        return 0

    fingerprints = sorted(set(by_provider_key.values()))
    observed = (
        await db.execute(
            select(
                AIUsageEvent.api_key_fingerprint,
                AIUsageEvent.source,
                AIUsageEvent.requested_model,
                AIUsageEvent.served_model,
                AIAsset,
            )
            .join(AIAsset, AIAsset.id == AIUsageEvent.asset_id)
            .where(
                AIUsageEvent.tenant_id == tenant_id,
                AIUsageEvent.api_key_fingerprint.in_(fingerprints),
                AIUsageEvent.source != PLATFORM_SOURCE,
                AIAsset.vendor.in_(providers),
            )
        )
    ).all()

    # (vendor, fingerprint, model) -> [(source, observed asset)]
    sightings: dict[tuple[str, str, str], list[tuple[str, AIAsset]]] = {}
    for fp, source, requested, served, asset in observed:
        for model in {asset.model, requested, served} - {None}:
            seen = sightings.setdefault((asset.vendor, fp, model), [])
            if (source, asset) not in seen:
                seen.append((source, asset))

    changes = AssetChanges()
    corroborated = 0
    for p_asset in platform_assets:
        key_id = _platform_key_id(p_asset)
        fp = by_provider_key.get((p_asset.vendor, key_id)) if key_id else None
        if fp is None or not p_asset.model:
            continue
        gained = False
        for source, o_asset in sightings.get((p_asset.vendor, fp, p_asset.model), []):
            gained |= _add_source(p_asset, source)
            if o_asset.id != p_asset.id and _add_source(o_asset, PLATFORM_SOURCE):
                changes.record(o_asset, False)
        if gained:
            corroborated += 1
            changes.record(p_asset, False)
    await changes.enqueue(db, tenant_id)
    return corroborated
