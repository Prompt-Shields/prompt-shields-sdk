"""Gateway virtual keys and credential pools (#343).

A virtual key (``ps-vk-...``) lets a caller make model calls through the
gateway without holding a provider key. It is

  - bound to a team, a project (whose team it then also carries) and/or a
    principal (a person or service account, collector/rbac.py): the gateway
    stamps these on telemetry, rate limits, budgets and guardrails instead
    of the caller-declared X-PS-Team / X-PS-Project. The binding is fixed at
    issue: issue a new key to change it;
  - optionally limited to models matching ``allowed_models`` (shell-style
    globs, ``fnmatch`` semantics, matched by the gateway on the model each
    target is sent);
  - backed by a pool of inference credentials from the vault (#342), several
    per provider, used ``round_robin`` or ``least_recently_limited`` by the
    gateway, which fails over to the next credential of the same provider
    on 429 (and marks a credential that gets 401/403 unhealthy).

Storage: ``key_hash`` is SHA-256 of the key (256 random bits after the
prefix, so a fast hash is safe and resolution needs no bcrypt) and
``key_hint`` its last four characters. The key is returned once, at issue.

Resolution (``resolve``, POST /api/v1/virtual-keys/resolve, ingest key with
``virtual-keys:resolve``): the gateway sends a key and gets its binding and
its pool's active credentials, decrypted. Each decrypt goes through
``credential_vault.get_credential`` (one ``inference_credential.use`` audit
row, purpose ``virtual_key:<id>``); decrypted keys are then kept in this
process for PS_VIRTUAL_KEY_CREDENTIAL_CACHE_SECONDS (default 300), keyed to
the row's ciphertext and data key so a rotation or rewrap is picked up at
once. A credential's status (disabled, expired) and the virtual key's
(revoked, expired, its principal disabled) are read from the database on
every resolve, so revocation reaches each gateway within its own cache TTL
(30 s).
"""

from __future__ import annotations

import fnmatch
import hashlib
import re
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from db.models import (
    InferenceCredential,
    Principal,
    Project,
    Team,
    VirtualKey,
    VirtualKeyCredential,
)
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from collector import audit_trail
from collector import credential_vault as cv
from collector.config import settings

KEY_PREFIX = "ps-vk-"
KEY_WARNING = "Save the key now. It will not be shown again."
TARGET_TYPE = "virtual_key"

ROUND_ROBIN = "round_robin"
LEAST_RECENTLY_LIMITED = "least_recently_limited"
STRATEGIES = (ROUND_ROBIN, LEAST_RECENTLY_LIMITED)

ACTIVE, EXPIRED, REVOKED = "active", "expired", "revoked"

NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
MODEL_PATTERN_RE = re.compile(r"[A-Za-z0-9._:/@*?\[\]-]{1,200}")
MAX_VIRTUAL_KEYS_PER_TENANT = 1000
MAX_POOL_SIZE = 20
MAX_ALLOWED_MODELS = 100
# Gateways cache a resolution (active or not) this long.
GATEWAY_CACHE_TTL_SECONDS = 30


def _utcnow() -> datetime:
    return datetime.now(UTC)


def generate_key() -> str:
    return KEY_PREFIX + secrets.token_urlsafe(32)


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def key_hint(key: str) -> str:
    return key[-4:]


# ──────────────────────────────────────────────────────────────────────
# Validation
# ──────────────────────────────────────────────────────────────────────


def validate_name(value: str) -> str:
    name = value.strip()
    if not NAME_RE.fullmatch(name):
        raise ValueError(
            "name must be 1-128 characters from A-Z a-z 0-9 . _ -, starting with a "
            "letter or digit"
        )
    return name


def validate_strategy(value: str) -> str:
    if value not in STRATEGIES:
        raise ValueError(f"strategy must be one of {', '.join(STRATEGIES)}")
    return value


def validate_allowed_models(value: list[str] | None) -> list[str] | None:
    """None: any model. A list: at least one pattern."""
    if value is None:
        return None
    if not value:
        raise ValueError("allowed_models must not be empty (omit it or use null for any model)")
    if len(value) > MAX_ALLOWED_MODELS:
        raise ValueError(f"allowed_models takes at most {MAX_ALLOWED_MODELS} patterns")
    out: list[str] = []
    for item in value:
        pattern = item.strip() if isinstance(item, str) else ""
        if not MODEL_PATTERN_RE.fullmatch(pattern):
            raise ValueError(
                "each allowed_models entry is a model name or glob of 1-200 characters "
                "from A-Z a-z 0-9 . _ : / @ - * ? [ ]"
            )
        if pattern not in out:
            out.append(pattern)
    return out


def validate_expires_at(value: datetime | None, now: datetime | None = None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        raise ValueError("expires_at must include a time zone")
    if value <= (now or _utcnow()):
        raise ValueError("expires_at must be in the future")
    return value


def model_allowed(patterns: list[str] | None, model: str) -> bool:
    """Same rule as the gateway (psVirtualKeys/pool.ts modelAllowed)."""
    if patterns is None:
        return True
    return any(fnmatch.fnmatchcase(model, p) for p in patterns)


# ──────────────────────────────────────────────────────────────────────
# Records
# ──────────────────────────────────────────────────────────────────────


def status(row: VirtualKey, now: datetime | None = None) -> str:
    if row.revoked_at is not None:
        return REVOKED
    if row.expires_at is not None and row.expires_at <= (now or _utcnow()):
        return EXPIRED
    return ACTIVE


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _sid(value: UUID | None) -> str | None:
    return str(value) if value else None


def serialize(
    row: VirtualKey, pool: list[InferenceCredential], now: datetime | None = None
) -> dict:
    """Public view: never the key or its hash."""
    return {
        "id": str(row.id),
        "name": row.name,
        "key_hint": row.key_hint,
        "status": status(row, now),
        "team_id": _sid(row.team_id),
        "project_id": _sid(row.project_id),
        "principal_id": _sid(row.principal_id),
        "allowed_models": list(row.allowed_models) if row.allowed_models is not None else None,
        "strategy": row.strategy,
        "credentials": [
            {
                "id": str(c.id),
                "provider": c.provider,
                "label": c.label,
                "key_fingerprint": c.key_fingerprint,
                "status": cv.status(c, now),
            }
            for c in pool
        ],
        "created_by": row.created_by,
        "expires_at": _iso(row.expires_at),
        "revoked_at": _iso(row.revoked_at),
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
        "last_resolved_at": _iso(row.last_resolved_at),
    }


async def find(db: AsyncSession, tenant_id: UUID, key_id: UUID) -> VirtualKey | None:
    return (
        await db.execute(
            select(VirtualKey).where(VirtualKey.tenant_id == tenant_id, VirtualKey.id == key_id)
        )
    ).scalar_one_or_none()


async def list_keys(db: AsyncSession, tenant_id: UUID) -> list[VirtualKey]:
    return list(
        (
            await db.execute(
                select(VirtualKey)
                .where(VirtualKey.tenant_id == tenant_id)
                .order_by(VirtualKey.name)
            )
        ).scalars()
    )


async def pool(db: AsyncSession, tenant_id: UUID, key_id: UUID) -> list[InferenceCredential]:
    """The key's credentials, in pool order."""
    return list(
        (
            await db.execute(
                select(InferenceCredential)
                .join(
                    VirtualKeyCredential,
                    VirtualKeyCredential.credential_id == InferenceCredential.id,
                )
                .where(
                    VirtualKeyCredential.tenant_id == tenant_id,
                    VirtualKeyCredential.virtual_key_id == key_id,
                    InferenceCredential.tenant_id == tenant_id,
                )
                .order_by(VirtualKeyCredential.position, VirtualKeyCredential.id)
            )
        ).scalars()
    )


async def pools(
    db: AsyncSession, tenant_id: UUID, key_ids: list[UUID]
) -> dict[UUID, list[InferenceCredential]]:
    out: dict[UUID, list[InferenceCredential]] = {k: [] for k in key_ids}
    if not key_ids:
        return out
    rows = (
        await db.execute(
            select(VirtualKeyCredential.virtual_key_id, InferenceCredential)
            .join(
                InferenceCredential,
                VirtualKeyCredential.credential_id == InferenceCredential.id,
            )
            .where(
                VirtualKeyCredential.tenant_id == tenant_id,
                VirtualKeyCredential.virtual_key_id.in_(key_ids),
                InferenceCredential.tenant_id == tenant_id,
            )
            .order_by(VirtualKeyCredential.position, VirtualKeyCredential.id)
        )
    ).all()
    for key_id, cred in rows:
        out[key_id].append(cred)
    return out


async def name_taken(
    db: AsyncSession, tenant_id: UUID, name: str, exclude: UUID | None = None
) -> bool:
    q = select(VirtualKey.id).where(VirtualKey.tenant_id == tenant_id, VirtualKey.name == name)
    if exclude is not None:
        q = q.where(VirtualKey.id != exclude)
    return (await db.execute(q)).first() is not None


async def count(db: AsyncSession, tenant_id: UUID) -> int:
    return (
        await db.execute(
            select(func.count()).select_from(VirtualKey).where(VirtualKey.tenant_id == tenant_id)
        )
    ).scalar_one()


async def check_credentials(
    db: AsyncSession, tenant_id: UUID, credential_ids: list[UUID]
) -> list[UUID]:
    """Deduplicated, in the order given; every id must be one of the
    tenant's credentials. Raises ValueError."""
    ids = list(dict.fromkeys(credential_ids))
    if not ids:
        raise ValueError("credential_ids must name at least one inference credential")
    if len(ids) > MAX_POOL_SIZE:
        raise ValueError(f"a pool takes at most {MAX_POOL_SIZE} credentials")
    found = set(
        (
            await db.execute(
                select(InferenceCredential.id).where(
                    InferenceCredential.tenant_id == tenant_id,
                    InferenceCredential.id.in_(ids),
                )
            )
        ).scalars()
    )
    missing = [str(i) for i in ids if i not in found]
    if missing:
        raise ValueError(f"unknown inference credential(s): {', '.join(missing)}")
    return ids


async def set_pool(
    db: AsyncSession, tenant_id: UUID, key_id: UUID, credential_ids: list[UUID]
) -> None:
    """Replace the pool (ids already checked by ``check_credentials``)."""
    await db.execute(
        delete(VirtualKeyCredential).where(
            VirtualKeyCredential.tenant_id == tenant_id,
            VirtualKeyCredential.virtual_key_id == key_id,
        )
    )
    for position, cred_id in enumerate(credential_ids):
        db.add(
            VirtualKeyCredential(
                tenant_id=tenant_id,
                virtual_key_id=key_id,
                credential_id=cred_id,
                position=position,
            )
        )


@dataclass(frozen=True)
class Binding:
    team_id: UUID | None
    project_id: UUID | None
    principal_id: UUID | None


async def check_binding(
    db: AsyncSession,
    tenant_id: UUID,
    team_id: UUID | None,
    project_id: UUID | None,
    principal_id: UUID | None,
) -> Binding:
    """Every id must exist in the tenant; a project implies its team (a
    different team is refused); the principal must be enabled. Raises
    ValueError."""
    if project_id is not None:
        project = (
            await db.execute(
                select(Project).where(Project.tenant_id == tenant_id, Project.id == project_id)
            )
        ).scalar_one_or_none()
        if project is None:
            raise ValueError("project_id names no project of this tenant")
        if team_id is not None and team_id != project.team_id:
            raise ValueError("project_id belongs to another team than team_id")
        team_id = project.team_id
    elif team_id is not None:
        found = (
            await db.execute(select(Team.id).where(Team.tenant_id == tenant_id, Team.id == team_id))
        ).first()
        if found is None:
            raise ValueError("team_id names no team of this tenant")
    if principal_id is not None:
        principal = (
            await db.execute(
                select(Principal).where(
                    Principal.tenant_id == tenant_id, Principal.id == principal_id
                )
            )
        ).scalar_one_or_none()
        if principal is None:
            raise ValueError("principal_id names no principal of this tenant")
        if principal.disabled_at is not None:
            raise ValueError("principal_id names a disabled principal")
    return Binding(team_id, project_id, principal_id)


async def create(
    db: AsyncSession,
    tenant_id: UUID,
    *,
    name: str,
    binding: Binding,
    credential_ids: list[UUID],
    allowed_models: list[str] | None,
    strategy: str,
    expires_at: datetime | None,
    created_by: str | None,
) -> tuple[VirtualKey, str]:
    """Add a key and its pool (inputs already validated). Returns the row
    and the plaintext key. Does not commit."""
    plaintext = generate_key()
    row = VirtualKey(
        tenant_id=tenant_id,
        name=name,
        key_hash=hash_key(plaintext),
        key_hint=key_hint(plaintext),
        team_id=binding.team_id,
        project_id=binding.project_id,
        principal_id=binding.principal_id,
        allowed_models=allowed_models,
        strategy=strategy,
        created_by=created_by,
        expires_at=expires_at,
        created_at=_utcnow(),
    )
    db.add(row)
    await db.flush()
    await set_pool(db, tenant_id, row.id, credential_ids)
    return row, plaintext


# ──────────────────────────────────────────────────────────────────────
# Decrypted credential cache (resolution only)
# ──────────────────────────────────────────────────────────────────────


class CredentialCache:
    """Decrypted keys by (tenant, credential id), valid while the row's
    ciphertext and wrapped data key are unchanged and the entry is younger
    than the TTL. Process memory only; never logged or serialised."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: dict[tuple[UUID, UUID], tuple[str, str, float, str]] = {}

    @staticmethod
    def _version(row: InferenceCredential) -> str:
        return hashlib.sha256(
            f"{row.sealed_key}\x00{row.wrapped_dek}\x00{row.kek_ref}".encode()
        ).hexdigest()

    def get(self, row: InferenceCredential, ttl: float) -> str | None:
        if ttl <= 0:
            return None
        key = (row.tenant_id, row.id)
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            version, _, stored_at, secret = entry
            if version != self._version(row) or time.monotonic() - stored_at > ttl:
                del self._entries[key]
                return None
            return secret

    def put(self, row: InferenceCredential, secret: str, ttl: float) -> None:
        if ttl <= 0:
            return
        with self._lock:
            self._entries[(row.tenant_id, row.id)] = (
                self._version(row),
                row.provider,
                time.monotonic(),
                secret,
            )

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


_cache = CredentialCache()


def credential_cache() -> CredentialCache:
    return _cache


# ──────────────────────────────────────────────────────────────────────
# Resolution (gateway)
# ──────────────────────────────────────────────────────────────────────


def _inactive(reason: str) -> dict:
    return {"active": False, "reason": reason, "cache_ttl_seconds": GATEWAY_CACHE_TTL_SECONDS}


async def resolve(
    db: AsyncSession,
    tenant_id: UUID,
    key: str,
    *,
    actor: audit_trail.Actor,
    now: datetime | None = None,
) -> dict:
    """The gateway's view of ``key`` in ``tenant_id``: inactive (with a
    reason: not_found, revoked, expired, principal_disabled) or its binding
    and the pool's usable credentials, decrypted, in pool order. Writes an
    ``inference_credential.use`` audit row per decrypt (not per cache hit)
    and sets ``last_resolved_at``. Does not commit: commit soon after, also
    when the result is inactive or a credential was refused, to keep the
    audit rows."""
    now = now or _utcnow()
    if not key.startswith(KEY_PREFIX) or len(key) > 200:
        return _inactive("not_found")
    row = (
        await db.execute(
            select(VirtualKey).where(
                VirtualKey.tenant_id == tenant_id, VirtualKey.key_hash == hash_key(key)
            )
        )
    ).scalar_one_or_none()
    if row is None:
        return _inactive("not_found")
    state = status(row, now)
    if state != ACTIVE:
        return _inactive(state)
    if row.principal_id is not None:
        disabled = (
            await db.execute(
                select(Principal.disabled_at).where(
                    Principal.tenant_id == tenant_id, Principal.id == row.principal_id
                )
            )
        ).first()
        if disabled is None or disabled[0] is not None:
            return _inactive("principal_disabled")

    team_id = row.team_id
    if row.project_id is not None:
        # The project's current team: a project moved to another team
        # (collector/rbac_admin.py) takes its keys' attribution with it.
        current = (
            await db.execute(
                select(Project.team_id).where(
                    Project.tenant_id == tenant_id, Project.id == row.project_id
                )
            )
        ).first()
        if current is not None:
            team_id = current[0]

    ttl = float(settings.virtual_key_credential_cache_seconds)
    credentials: list[dict] = []
    unavailable: list[dict] = []
    for cred in await pool(db, tenant_id, row.id):
        cred_status = cv.status(cred, now)
        if cred_status != cv.ACTIVE:
            unavailable.append({"id": str(cred.id), "reason": cred_status})
            continue
        secret = _cache.get(cred, ttl)
        if secret is None:
            try:
                opened = await cv.get_credential(
                    db, tenant_id, cred.id, actor=actor, purpose=f"virtual_key:{row.id}", now=now
                )
            except cv.CredentialUnavailable as exc:
                unavailable.append({"id": str(cred.id), "reason": exc.reason})
                continue
            secret = opened.secret.get_secret_value()
            _cache.put(cred, secret, ttl)
        credentials.append(
            {
                "id": str(cred.id),
                "provider": cred.provider,
                "label": cred.label,
                "key_fingerprint": cred.key_fingerprint,
                "api_key": secret,
            }
        )
    row.last_resolved_at = now
    return {
        "active": True,
        "cache_ttl_seconds": GATEWAY_CACHE_TTL_SECONDS,
        "virtual_key": {
            "id": str(row.id),
            "name": row.name,
            "team_id": _sid(team_id),
            "project_id": _sid(row.project_id),
            "principal_id": _sid(row.principal_id),
            # The pseudonymous user id the gateway reports for calls made
            # with this key when it has no OIDC subject; the collector
            # hashes it like any user_id (collector/user_identity.py).
            "user_id": f"principal:{row.principal_id}" if row.principal_id else None,
            "allowed_models": (
                list(row.allowed_models) if row.allowed_models is not None else None
            ),
            "strategy": row.strategy,
            "expires_at": _iso(row.expires_at),
        },
        "credentials": credentials,
        "unavailable_credentials": unavailable,
    }
