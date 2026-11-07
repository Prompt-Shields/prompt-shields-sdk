"""Inference credential vault (#342): envelope-encrypted inference API keys.

A tenant stores inference keys (several per provider, told apart by a
label) for virtual keys (#343) to use. Each key is stored as:

  - ``sealed_key``: ``"v1." + base64url(nonce || AES-256-GCM(dek, key))``
    under a fresh random 256-bit data key (DEK) of its own, with the tenant
    id, row id and provider as associated data, so a ciphertext copied to
    another row, tenant or provider does not decrypt;
  - ``wrapped_dek``: the DEK wrapped by the key-encryption key (KEK) of a
    ``collector.kms`` provider (Azure Key Vault in production, a local
    keyring for development), and ``kek_ref`` naming that KEK.

Only the DEK goes to the KMS, never the key. KEK rotation
(``rewrap_tenant``, CLI ``rewrap``) unwraps each DEK with the KEK it names
and wraps it with the current one; ``sealed_key`` is not touched, and rows
keep decrypting throughout, as long as the old KEK stays available
(an older ``PS_CREDENTIAL_KEK_KEYS`` entry, or an enabled older Key Vault key
version) until the rewrap has finished.

Write-only: nothing returns a key. ``serialize`` shows ``key_hint`` (``****``
+ last four characters), ``key_fingerprint`` (``sha256(key)[:16]``, the
SDK's ``api_key_fingerprint``) and metadata. Validation errors never echo a
key.

Internal API for the gateway-facing side (virtual keys, #343):
``get_credential(db, tenant_id, credential_id, actor=..., purpose=...)``
decrypts one key and appends an ``inference_credential.use`` audit row
(success or refusal) to the tenant's audit chain; the caller commits.

CLI (operator; per tenant under its RLS context, tenants listed through a
logged bypass)::

    python -m collector.credential_vault status  [--tenant ID | --all-tenants]
    python -m collector.credential_vault rewrap  [--tenant ID | --all-tenants] [--dry-run]
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import logging
import re
import secrets
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from db.models import InferenceCredential
from pydantic import SecretStr
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from collector import audit_trail, kms, rls

logger = logging.getLogger(__name__)

PROVIDER_RE = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}")
MAX_LABEL_LENGTH = 128
MIN_KEY_LENGTH = 8
MAX_KEY_LENGTH = 4096
MAX_CREDENTIALS_PER_TENANT = 500

SEALED_PREFIX = "v1."
_CONTEXT_LABEL = b"prompt-shields inference credential v1\x00"

ACTIVE, DISABLED, EXPIRED = "active", "disabled", "expired"

TARGET_TYPE = "inference_credential"
REWRAP_BYPASS_REASON = "credential-vault-rewrap"


def _utcnow() -> datetime:
    return datetime.now(UTC)


class CredentialUnavailable(Exception):
    """``get_credential`` could not return a key. ``reason`` is one of
    not_found, disabled, expired, key_provider, corrupt."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"inference credential unavailable: {reason}")


# ──────────────────────────────────────────────────────────────────────
# Validation (messages never echo the key)
# ──────────────────────────────────────────────────────────────────────


def validate_api_key(value: str) -> str:
    key = value.strip()
    if not MIN_KEY_LENGTH <= len(key) <= MAX_KEY_LENGTH:
        raise ValueError(f"api_key must be {MIN_KEY_LENGTH}-{MAX_KEY_LENGTH} characters")
    if not key.isascii() or not key.isprintable() or any(c.isspace() for c in key):
        raise ValueError("api_key must be printable ASCII without whitespace")
    return key


def validate_provider(value: str) -> str:
    provider = value.strip().lower()
    if not PROVIDER_RE.fullmatch(provider):
        raise ValueError(
            "provider must be 1-64 characters from a-z 0-9 _ . -, starting with a "
            "letter or digit"
        )
    return provider


def validate_label(value: str) -> str:
    label = value.strip()
    if not 1 <= len(label) <= MAX_LABEL_LENGTH or not label.isprintable():
        raise ValueError(f"label must be 1-{MAX_LABEL_LENGTH} printable characters")
    return label


def validate_expires_at(value: datetime | None, now: datetime | None = None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        raise ValueError("expires_at must include a time zone")
    if value <= (now or _utcnow()):
        raise ValueError("expires_at must be in the future")
    return value


def key_hint(key: str) -> str:
    return "****" + key[-4:]


def key_fingerprint(key: str) -> str:
    """sha256(key)[:16], the same value the SDK sends as api_key_fingerprint."""
    return hashlib.sha256(key.encode()).hexdigest()[:16]


# ──────────────────────────────────────────────────────────────────────
# Envelope encryption
# ──────────────────────────────────────────────────────────────────────


def _context(tenant_id: UUID, credential_id: UUID, provider: str) -> bytes:
    # UUIDs are fixed-length, so the concatenation is unambiguous.
    return _CONTEXT_LABEL + tenant_id.bytes + credential_id.bytes + provider.encode()


def _row_context(row: InferenceCredential) -> bytes:
    return _context(row.tenant_id, row.id, row.provider)


@dataclass(frozen=True)
class Sealed:
    sealed_key: str
    wrapped_dek: str
    kek_ref: str


async def seal(
    key: str,
    tenant_id: UUID,
    provider: str,
    credential_id: UUID,
    key_provider: kms.KeyProvider | None = None,
) -> Sealed:
    """Encrypt ``key`` under a fresh DEK and wrap the DEK with the active
    KEK. Raises kms.KeyProviderNotConfigured / kms.KeyProviderError."""
    key_provider = key_provider or kms.active_provider()
    context = _context(tenant_id, credential_id, provider)
    dek = secrets.token_bytes(kms.DEK_BYTES)
    nonce = secrets.token_bytes(12)
    ct = AESGCM(dek).encrypt(nonce, key.encode(), context)
    wrapped = await key_provider.wrap(dek, context)
    return Sealed(
        sealed_key=SEALED_PREFIX + base64.urlsafe_b64encode(nonce + ct).decode(),
        wrapped_dek=base64.urlsafe_b64encode(wrapped.wrapped).decode(),
        kek_ref=wrapped.kek_ref,
    )


async def _unwrap_dek(row: InferenceCredential) -> bytes:
    try:
        wrapped = base64.urlsafe_b64decode(row.wrapped_dek.encode())
    except ValueError:
        raise kms.KeyProviderError("stored data key is malformed") from None
    return await kms.provider_for_ref(row.kek_ref).unwrap(
        row.kek_ref, wrapped, _row_context(row)
    )


async def open_row(row: InferenceCredential) -> str:
    """Decrypt a row's key. Raises kms.KeyProviderError (unwrap failed),
    kms.KeyProviderNotConfigured, or ValueError (ciphertext does not open
    for this row). Not audited: use ``get_credential``."""
    dek = await _unwrap_dek(row)
    if not row.sealed_key.startswith(SEALED_PREFIX):
        raise ValueError("unknown sealed key format")
    try:
        raw = base64.urlsafe_b64decode(row.sealed_key[len(SEALED_PREFIX) :].encode())
        return AESGCM(dek).decrypt(raw[:12], raw[12:], _row_context(row)).decode()
    except (InvalidTag, ValueError):
        raise ValueError("inference credential could not be decrypted") from None


# ──────────────────────────────────────────────────────────────────────
# Records
# ──────────────────────────────────────────────────────────────────────


def status(row: InferenceCredential, now: datetime | None = None) -> str:
    if row.disabled_at is not None:
        return DISABLED
    if row.expires_at is not None and row.expires_at <= (now or _utcnow()):
        return EXPIRED
    return ACTIVE


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def serialize(row: InferenceCredential, now: datetime | None = None) -> dict:
    """Public view: never the key, its ciphertext or the wrapped data key."""
    return {
        "id": str(row.id),
        "provider": row.provider,
        "label": row.label,
        "status": status(row, now),
        "key_hint": row.key_hint,
        "key_fingerprint": row.key_fingerprint,
        "kek_provider": kms.ref_provider(row.kek_ref),
        "kek_ref": row.kek_ref,
        "expires_at": _iso(row.expires_at),
        "disabled_at": _iso(row.disabled_at),
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
        "rotated_at": _iso(row.rotated_at),
        "rewrapped_at": _iso(row.rewrapped_at),
        "last_used_at": _iso(row.last_used_at),
    }


async def list_credentials(
    db: AsyncSession, tenant_id: UUID, provider: str | None = None
) -> list[InferenceCredential]:
    q = select(InferenceCredential).where(InferenceCredential.tenant_id == tenant_id)
    if provider is not None:
        q = q.where(InferenceCredential.provider == provider)
    q = q.order_by(InferenceCredential.provider, InferenceCredential.label)
    return list((await db.execute(q)).scalars())


async def find(
    db: AsyncSession, tenant_id: UUID, credential_id: UUID
) -> InferenceCredential | None:
    """The row, checked against ``tenant_id`` explicitly (not only by RLS,
    which is opt-in)."""
    return (
        await db.execute(
            select(InferenceCredential).where(
                InferenceCredential.tenant_id == tenant_id,
                InferenceCredential.id == credential_id,
            )
        )
    ).scalar_one_or_none()


async def label_taken(
    db: AsyncSession, tenant_id: UUID, provider: str, label: str, exclude: UUID | None = None
) -> bool:
    q = select(InferenceCredential.id).where(
        InferenceCredential.tenant_id == tenant_id,
        InferenceCredential.provider == provider,
        InferenceCredential.label == label,
    )
    if exclude is not None:
        q = q.where(InferenceCredential.id != exclude)
    return (await db.execute(q)).first() is not None


async def count(db: AsyncSession, tenant_id: UUID) -> int:
    return (
        await db.execute(
            select(func.count())
            .select_from(InferenceCredential)
            .where(InferenceCredential.tenant_id == tenant_id)
        )
    ).scalar_one()


async def create(
    db: AsyncSession,
    tenant_id: UUID,
    *,
    provider: str,
    label: str,
    api_key: str,
    expires_at: datetime | None = None,
) -> InferenceCredential:
    """Validate, seal and add a row. Does not commit. The caller checks the
    label is free and the tenant is under its limit. Raises ValueError or a
    kms error."""
    provider = validate_provider(provider)
    label = validate_label(label)
    key = validate_api_key(api_key)
    expires_at = validate_expires_at(expires_at)
    cred_id = uuid4()
    sealed = await seal(key, tenant_id, provider, cred_id)
    row = InferenceCredential(
        id=cred_id,
        tenant_id=tenant_id,
        provider=provider,
        label=label,
        sealed_key=sealed.sealed_key,
        wrapped_dek=sealed.wrapped_dek,
        kek_ref=sealed.kek_ref,
        key_hint=key_hint(key),
        key_fingerprint=key_fingerprint(key),
        expires_at=expires_at,
        created_at=_utcnow(),
    )
    db.add(row)
    return row


async def rotate(
    row: InferenceCredential,
    api_key: str,
    *,
    expires_at: datetime | None = None,
    keep_expiry: bool = True,
) -> None:
    """Replace the stored key: new DEK, new ciphertext, wrapped with the
    active KEK. ``expires_at`` replaces the expiry when given; otherwise it
    is kept (``keep_expiry``) or cleared."""
    key = validate_api_key(api_key)
    new_expiry = validate_expires_at(expires_at)
    sealed = await seal(key, row.tenant_id, row.provider, row.id)
    now = _utcnow()
    row.sealed_key = sealed.sealed_key
    row.wrapped_dek = sealed.wrapped_dek
    row.kek_ref = sealed.kek_ref
    row.key_hint = key_hint(key)
    row.key_fingerprint = key_fingerprint(key)
    if new_expiry is not None or not keep_expiry:
        row.expires_at = new_expiry
    row.rotated_at = now
    row.updated_at = now


# ──────────────────────────────────────────────────────────────────────
# Internal API: decrypt for use (virtual keys, #343)
# ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Credential:
    id: UUID
    tenant_id: UUID
    provider: str
    label: str
    secret: SecretStr

    def __repr__(self) -> str:  # never print the key
        return (
            f"Credential(id={self.id}, provider={self.provider!r}, "
            f"label={self.label!r}, secret=***)"
        )

    __str__ = __repr__


async def _audit_use(
    db: AsyncSession,
    tenant_id: UUID,
    credential_id: UUID,
    actor: audit_trail.Actor,
    purpose: str,
    reason: str | None = None,
) -> None:
    details: dict = {"purpose": purpose}
    if reason:
        details["reason"] = reason
    await audit_trail.record(
        db,
        tenant_id=tenant_id,
        action="inference_credential.use",
        outcome=audit_trail.FAILURE if reason else audit_trail.SUCCESS,
        actor=actor,
        target_type=TARGET_TYPE,
        target_id=credential_id,
        details=details,
    )


async def get_credential(
    db: AsyncSession,
    tenant_id: UUID,
    credential_id: UUID,
    *,
    actor: audit_trail.Actor,
    purpose: str,
    now: datetime | None = None,
) -> Credential:
    """Decrypt one of the tenant's inference keys for use.

    Every call, served or refused, appends an ``inference_credential.use``
    row (actor, purpose, reason on refusal; never the key) to the tenant's
    audit chain in ``db``'s transaction and, on success, sets
    ``last_used_at``. Does not commit: commit soon after, also after a
    ``CredentialUnavailable``, to keep the audit row. The KMS call runs
    before the audit row is written, so the chain lock is not held across
    it. Raises CredentialUnavailable.
    """
    now = now or _utcnow()
    row = await find(db, tenant_id, credential_id)
    reason: str | None = None
    secret: str | None = None
    if row is None:
        reason = "not_found"
    elif status(row, now) != ACTIVE:
        reason = status(row, now)
    else:
        try:
            secret = await open_row(row)
        except (kms.KeyProviderError, kms.KeyProviderNotConfigured) as exc:
            logger.warning(
                "inference credential %s: key provider failed (%s)",
                credential_id,
                type(exc).__name__,
            )
            reason = "key_provider"
        except ValueError:
            reason = "corrupt"
    await _audit_use(db, tenant_id, credential_id, actor, purpose, reason)
    if reason is not None or row is None or secret is None:
        raise CredentialUnavailable(reason or "corrupt")
    row.last_used_at = now
    return Credential(
        id=row.id,
        tenant_id=row.tenant_id,
        provider=row.provider,
        label=row.label,
        secret=SecretStr(secret),
    )


# ──────────────────────────────────────────────────────────────────────
# KEK rotation: rewrap data keys
# ──────────────────────────────────────────────────────────────────────


async def kek_summary(db: AsyncSession, tenant_id: UUID) -> dict[str, int]:
    """Number of the tenant's credentials per kek_ref."""
    rows = (
        await db.execute(
            select(InferenceCredential.kek_ref, func.count())
            .where(InferenceCredential.tenant_id == tenant_id)
            .group_by(InferenceCredential.kek_ref)
        )
    ).all()
    return {ref: n for ref, n in rows}


async def rewrap_tenant(
    db: AsyncSession,
    tenant_id: UUID,
    *,
    actor: audit_trail.Actor,
    dry_run: bool = False,
    key_provider: kms.KeyProvider | None = None,
) -> dict:
    """Rewrap every data key of ``tenant_id`` not under the current KEK.

    Unwraps each DEK with the KEK its ``kek_ref`` names, wraps it with the
    active provider's current KEK and updates ``wrapped_dek`` / ``kek_ref``
    only if the row's wrapped key is unchanged meanwhile (a concurrent
    rotate wins; the row is reported as skipped). ``sealed_key`` is never
    touched. Commits, with one ``inference_credential.rewrap`` audit row
    (counts and KEK provider names, no key material) unless nothing needed
    doing or ``dry_run``.
    """
    key_provider = key_provider or kms.active_provider()
    target = await key_provider.current_ref()
    rows = list(
        (
            await db.execute(
                select(InferenceCredential)
                .where(
                    InferenceCredential.tenant_id == tenant_id,
                    InferenceCredential.kek_ref != target,
                )
                .order_by(InferenceCredential.id)
            )
        ).scalars()
    )
    result = {
        "target_kek_ref": target,
        "pending": len(rows),
        "rewrapped": 0,
        "skipped": 0,
        "failed": 0,
        "errors": [],
    }
    if dry_run or not rows:
        await db.rollback()
        return result
    from_providers: set[str] = set()
    for row in rows:
        old_wrapped, old_ref = row.wrapped_dek, row.kek_ref
        try:
            dek = await _unwrap_dek(row)
            new = await key_provider.wrap(dek, _row_context(row))
        except (kms.KeyProviderError, kms.KeyProviderNotConfigured) as exc:
            result["failed"] += 1
            result["errors"].append({"id": str(row.id), "error": type(exc).__name__})
            continue
        updated = await db.execute(
            update(InferenceCredential)
            .where(
                InferenceCredential.id == row.id,
                InferenceCredential.tenant_id == tenant_id,
                InferenceCredential.wrapped_dek == old_wrapped,
                InferenceCredential.kek_ref == old_ref,
            )
            .values(
                wrapped_dek=base64.urlsafe_b64encode(new.wrapped).decode(),
                kek_ref=new.kek_ref,
                rewrapped_at=_utcnow(),
            )
            .execution_options(synchronize_session=False)
        )
        if updated.rowcount == 1:
            result["rewrapped"] += 1
            from_providers.add(kms.ref_provider(old_ref))
        else:
            result["skipped"] += 1
    await audit_trail.record(
        db,
        tenant_id=tenant_id,
        action="inference_credential.rewrap",
        outcome=audit_trail.FAILURE if result["failed"] else audit_trail.SUCCESS,
        actor=actor,
        target_type=TARGET_TYPE,
        details={
            "rewrapped": result["rewrapped"],
            "skipped": result["skipped"],
            "failed": result["failed"],
            "from_kek_providers": sorted(from_providers),
            "to_kek_provider": kms.ref_provider(target),
        },
    )
    await db.commit()
    return result


# ──────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────


async def _tenants(session_factory) -> list[UUID]:
    info = rls.bypass_info(REWRAP_BYPASS_REASON)
    async with session_factory(info=info) as db:
        return list(
            (
                await db.execute(
                    select(InferenceCredential.tenant_id)
                    .distinct()
                    .order_by(InferenceCredential.tenant_id)
                )
            ).scalars()
        )


async def run(
    session_factory, command: str, tenant: UUID | None, dry_run: bool = False
) -> dict:
    tenants = [tenant] if tenant else await _tenants(session_factory)
    out: dict = {"command": command, "tenants": {}}
    actor = audit_trail.system_actor("credential-vault-cli")
    for tenant_id in tenants:
        async with session_factory() as db:
            await rls.set_tenant(db, tenant_id)
            try:
                if command == "status":
                    out["tenants"][str(tenant_id)] = {"kek_refs": await kek_summary(db, tenant_id)}
                else:
                    out["tenants"][str(tenant_id)] = await rewrap_tenant(
                        db, tenant_id, actor=actor, dry_run=dry_run
                    )
            except (kms.KeyProviderError, kms.KeyProviderNotConfigured) as exc:
                await db.rollback()
                out["tenants"][str(tenant_id)] = {"error": str(exc)}
    if command == "rewrap":
        out["failed"] = sum(
            t.get("failed", 0) + (1 if "error" in t else 0) for t in out["tenants"].values()
        )
    return out


async def _main(command: str, tenant: UUID | None, dry_run: bool) -> dict:
    from collector.app import SessionLocal, engine

    try:
        return await run(SessionLocal, command, tenant, dry_run)
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Inference credential vault")
    parser.add_argument("command", choices=("status", "rewrap"))
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--tenant", type=UUID)
    target.add_argument("--all-tenants", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="rewrap: only count")
    args = parser.parse_args(argv)
    out = asyncio.run(_main(args.command, args.tenant, args.dry_run))
    print(json.dumps(out, indent=2, sort_keys=True))
    return 1 if out.get("failed") else 0


if __name__ == "__main__":
    sys.exit(main())
