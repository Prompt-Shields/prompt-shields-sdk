"""Tamper-evident audit trail of admin and security-relevant actions.

``collector.audit`` logs registry *reads* (``partner_audit_log``). This
module records *changes* and security events in ``audit_events``:

  provider_credential.create|rotate|delete|use
  provider_key_fingerprint.put|delete
  partner_credential.create|update|revoke      (scopes, rate-limit tier)
  webhook_subscription.create|update|delete|rotate_secret
  webhook_delivery.redeliver
  tenant.retention.read|tenant.retention.set|tenant.erase   (operator)
  retention.purge                               (usage-event retention runs)
  registry.attest|registry.retire|registry.unretire|registry.lifecycle_sweep
  registry.merge_assets                         (duplicate asset merge)
  operator.access                               (operator-token routes)
  auth.failure                                  (admin routes, rate-limited)
  audit_log.purge                               (partner_audit_log purge)
  audit.purge                                   (checkpoint, see below)

Each row names the actor (tenant key prefix, partner credential id or an
operator/job name, never a secret), action, target, outcome, HTTP status,
request id (``X-Request-ID``) and time, plus a small ``details`` object of
ids, counts and names. Never secrets, URLs, emails, IP addresses or free
text.

Tamper evidence
    Rows form one hash chain per tenant (db/audit_chain.py): ``seq`` counts
    1, 2, ... and ``row_hash = sha256(prev_hash || canonical row)``. Writers
    take a per-tenant ``pg_advisory_xact_lock`` before reading the chain
    head, so concurrent requests append one after another, and a unique
    ``(tenant_id, seq)`` constraint backs that up. ``verify_chain`` walks a
    chain and reports the first break (a modified, missing or reordered
    row). ``python -m collector.audit_trail verify`` and
    ``GET /api/v1/operator/audit/verify`` run it.

    A hash chain proves nothing about rows removed from its *end*, or a
    chain rewritten from some row on by someone holding the database. Keep
    an outside copy: the ``audit.*`` webhook export (below) carries every
    row's hashes, and ``verify`` prints the head hash to record elsewhere.

Writes
    ``record`` adds a row in the caller's transaction (it does not commit),
    so a change and its audit row commit or roll back together. Failures
    and auth failures are written separately, after the rollback.
    Events that belong to no tenant (operator authentication, operator
    access, read-log purges) go to the system chain, tenant
    ``SYSTEM_TENANT_ID``.

Export
    Every tenant row is also queued in the webhook outbox
    (collector.webhooks) as event type ``audit.<action>`` for subscriptions
    to ``audit.*`` whose partner holds ``audit:read``: signed, retried and
    dead-lettered like asset events. The system chain is not exported to
    tenants.

Retention and erasure
    Tenant erasure keeps ``audit_events`` (retention.ERASE_EXEMPT): the
    rows hold ids only, and the erasure itself is the chain's last row.
    ``purge_expired`` (``PS_AUDIT_EVENT_RETENTION_DAYS``, off by default)
    removes a prefix of a chain and first appends an ``audit.purge``
    checkpoint row recording the seq and row_hash of the last row removed,
    so the first remaining row still links to something in the chain.
"""

from __future__ import annotations

import argparse
import asyncio
import contextvars
import json
import logging
import re
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import UUID

from db import audit_chain
from db.audit_chain import CHECKPOINT_ACTION, GENESIS_HASH, SYSTEM_TENANT_ID
from db.models import AuditEvent, uuid7
from fastapi import Request
from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from collector import rls, webhooks
from collector.audit import bearer_key_prefix

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "1.0"

SUCCESS = "success"
FAILURE = "failure"

# ──────────────────────────────────────────────────────────────────────
# Request id
# ──────────────────────────────────────────────────────────────────────

REQUEST_ID_HEADER = "X-Request-ID"
_REQUEST_ID_RE = re.compile(r"[A-Za-z0-9._:-]{1,128}")
REQUEST_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "ps_request_id", default=None
)


def clean_request_id(value: str | None) -> str | None:
    """A caller's X-Request-ID if it is a short token, else None."""
    if value and _REQUEST_ID_RE.fullmatch(value):
        return value
    return None


def current_request_id() -> str | None:
    return REQUEST_ID.get()


# ──────────────────────────────────────────────────────────────────────
# Actors
# ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Actor:
    """Who acted. ``id`` identifies a credential, never contains one."""

    type: str  # tenant_key | admin_token | partner | operator | system | anonymous
    id: str | None = None


OPERATOR = Actor("operator", "operator-api")

# Scoped admin tokens (collector.admin_auth) start with this.
ADMIN_TOKEN_PREFIX = "ps-at-"


def tenant_key_actor(request: Request) -> Actor:
    """The caller's tenant API key, or scoped admin token, by its SHA-256
    lookup prefix (db.api_keys; for a token, ``admin_tokens.token_prefix``)."""
    auth = request.headers.get("Authorization", "")
    token = auth.removeprefix("Bearer ").strip() if auth.startswith("Bearer ") else ""
    kind = "admin_token" if token.startswith(ADMIN_TOKEN_PREFIX) else "tenant_key"
    return Actor(kind, bearer_key_prefix(request))


def partner_actor(partner_id: UUID) -> Actor:
    return Actor("partner", str(partner_id))


def system_actor(name: str) -> Actor:
    return Actor("system", name)


def actor_from_name(name: str) -> Actor:
    """For callers that pass an actor string (collector.retention)."""
    return OPERATOR if name == OPERATOR.id else system_actor(name)


# ──────────────────────────────────────────────────────────────────────
# Details: ids, counts and names only
# ──────────────────────────────────────────────────────────────────────

# Canonicalisation lives in db/audit_chain.py, shared with the 021 backfill.
MAX_DETAIL_STRING = audit_chain.MAX_DETAIL_STRING
MAX_DETAIL_ITEMS = audit_chain.MAX_DETAIL_ITEMS
REDACTED = audit_chain.REDACTED
sanitize_details = audit_chain.sanitize_details


# ──────────────────────────────────────────────────────────────────────
# Writing
# ──────────────────────────────────────────────────────────────────────


def _is_postgres(db: AsyncSession) -> bool:
    return db.get_bind().dialect.name == "postgresql"


async def _lock_chain(db: AsyncSession, tenant_id: UUID) -> None:
    """Serialise writers of one tenant's chain until the transaction ends."""
    if _is_postgres(db):
        await db.execute(
            text(audit_chain.ADVISORY_LOCK_SQL),
            {"lock_class": audit_chain.ADVISORY_LOCK_CLASS, "tenant": str(tenant_id)},
        )


async def _chain_head(db: AsyncSession, tenant_id: UUID) -> tuple[int, str]:
    row = (
        await db.execute(
            select(AuditEvent.seq, AuditEvent.row_hash)
            .where(AuditEvent.tenant_id == tenant_id)
            .order_by(AuditEvent.seq.desc())
            .limit(1)
        )
    ).one_or_none()
    return (row.seq, row.row_hash) if row is not None else (0, GENESIS_HASH)


def chained_fields(row: AuditEvent) -> dict:
    return {name: getattr(row, name) for name in audit_chain.CHAINED_FIELDS}


def audit_payload(row: AuditEvent) -> dict:
    """The exported form of a row (webhook `data.audit`)."""
    return {
        "schema_version": SCHEMA_VERSION,
        "id": str(row.id),
        "tenant_id": str(row.tenant_id),
        "seq": row.seq,
        "created_at": audit_chain.format_ts(row.created_at),
        "action": row.action,
        "outcome": row.outcome,
        "actor": {"type": row.actor_type, "id": row.actor_id},
        "target": {"type": row.target_type, "id": row.target_id},
        "request_id": row.request_id,
        "status_code": row.status_code,
        "details": row.details or {},
        "prev_hash": row.prev_hash,
        "row_hash": row.row_hash,
        "hash_version": audit_chain.HASH_VERSION,
    }


async def record(
    db: AsyncSession,
    *,
    tenant_id: UUID,
    action: str,
    actor: Actor,
    outcome: str = SUCCESS,
    target_type: str | None = None,
    target_id: str | UUID | None = None,
    status_code: int | None = None,
    details: dict | None = None,
    request_id: str | None = None,
    now: datetime | None = None,
    export: bool = True,
) -> AuditEvent | None:
    """Append one row to `tenant_id`'s chain and queue its export. Flushes,
    does not commit: the row commits with the caller's change.

    Holds the tenant's chain lock until the transaction ends, so commit
    soon after (no network calls in between).
    """
    if not isinstance(db, AsyncSession):
        return None  # unit-test fakes, as in collector.rls
    await _lock_chain(db, tenant_id)
    seq, prev_hash = await _chain_head(db, tenant_id)
    row = AuditEvent(
        # The id is chained, so it is assigned before hashing.
        id=uuid7(),
        tenant_id=tenant_id,
        seq=seq + 1,
        created_at=now or datetime.now(UTC),
        action=action,
        outcome=outcome,
        actor_type=actor.type,
        actor_id=actor.id,
        target_type=target_type,
        target_id=str(target_id) if target_id is not None else None,
        request_id=request_id or current_request_id(),
        status_code=status_code,
        details=sanitize_details(details),
        prev_hash=prev_hash,
    )
    row.row_hash = audit_chain.compute_row_hash(prev_hash, chained_fields(row))
    db.add(row)
    await db.flush()
    if export and tenant_id != SYSTEM_TENANT_ID:
        await webhooks.enqueue_audit_event(
            db,
            tenant_id,
            webhooks.AUDIT_PREFIX + action,
            row.id,
            {"audit": audit_payload(row)},
            row.created_at,
        )
    return row


async def record_committed(db: AsyncSession, **kwargs) -> None:
    """``record`` and commit, for failure paths after a rollback. Best
    effort: an audit write error is logged and never fails the request."""
    try:
        await record(db, **kwargs)
        await db.commit()
    except Exception:
        logger.warning("audit trail write failed (%s)", kwargs.get("action"), exc_info=True)
        try:
            await db.rollback()
        except Exception:
            logger.debug("rollback after audit failure also failed", exc_info=True)


def _session_factory():
    from collector import app as app_module  # late: app imports the routers

    return app_module.SessionLocal


async def record_system_event(**kwargs) -> None:
    """Write to the system chain in a session of its own (the request's
    session may be scoped to a tenant, or not yet to anything). Best effort."""
    try:
        async with _session_factory()() as db:
            await rls.set_tenant(db, SYSTEM_TENANT_ID)
            await record(db, tenant_id=SYSTEM_TENANT_ID, export=False, **kwargs)
            await db.commit()
    except Exception:
        logger.warning(
            "audit trail system write failed (%s)", kwargs.get("action"), exc_info=True
        )


def route_template(request: Request) -> str:
    route = request.scope.get("route")
    return getattr(route, "path", None) or request.url.path


# ──────────────────────────────────────────────────────────────────────
# Authentication failures on admin routes (rate-limited)
# ──────────────────────────────────────────────────────────────────────


class AuthFailureLimiter:
    """Caps auth.failure rows so a credential-stuffing burst cannot flood
    the chain: at most `per_source` rows per source (realm and client
    address) and `total` overall per `window` seconds. Rows not written are
    counted and reported as `details.suppressed` on that source's next row.
    In process: each replica limits on its own."""

    def __init__(self, per_source: int = 5, total: int = 60, window: float = 60.0):
        self.per_source = per_source
        self.total = total
        self.window = window
        self._hits: dict[tuple, deque] = {}
        self._all: deque = deque()
        self._suppressed: dict[tuple, int] = {}

    def _trim(self, q: deque, now: float) -> None:
        while q and now - q[0] >= self.window:
            q.popleft()

    def allow(self, key: tuple, now: float | None = None) -> tuple[bool, int]:
        """(write a row?, rows suppressed for this key since its last row)."""
        now = time.monotonic() if now is None else now
        q = self._hits.setdefault(key, deque())
        self._trim(q, now)
        self._trim(self._all, now)
        if len(q) >= self.per_source or len(self._all) >= self.total:
            self._suppressed[key] = self._suppressed.get(key, 0) + 1
            return False, 0
        q.append(now)
        self._all.append(now)
        # Drop idle keys so the map stays small.
        if len(self._hits) > 10_000:
            for k in [k for k, v in self._hits.items() if not v]:
                self._hits.pop(k, None)
        return True, self._suppressed.pop(key, 0)

    def reset(self) -> None:
        self._hits.clear()
        self._all.clear()
        self._suppressed.clear()


auth_failure_limiter = AuthFailureLimiter()


async def record_auth_failure(
    request: Request,
    *,
    realm: str,
    reason: str,
    status_code: int,
    tenant_id: UUID | None = None,
    actor: Actor | None = None,
    db: AsyncSession | None = None,
) -> None:
    """auth.failure on an admin route. `realm`: operator | tenant_admin |
    partner. `reason`: missing_credentials | invalid_credentials |
    insufficient_scope (the gateway security-event vocabulary, #211).

    Unknown tenant (a bad operator token or tenant key): the system chain,
    with the presented key's lookup prefix as the actor when there is one.
    Known tenant (a partner without the scope): that tenant's chain, in
    `db` (the request's session, committed here).
    """
    client = request.client.host if request.client else "-"
    allowed, suppressed = auth_failure_limiter.allow((realm, client))
    if not allowed:
        return
    details: dict = {
        "realm": realm,
        "reason": reason,
        "route": route_template(request),
        "method": request.method,
    }
    if suppressed:
        details["suppressed"] = suppressed
    kwargs = {
        "action": "auth.failure",
        "outcome": FAILURE,
        "actor": actor or Actor("anonymous", bearer_key_prefix(request)),
        "status_code": status_code,
        "details": details,
    }
    if tenant_id is None or db is None:
        await record_system_event(**kwargs)
    else:
        await record_committed(db, tenant_id=tenant_id, **kwargs)


# ──────────────────────────────────────────────────────────────────────
# Verification
# ──────────────────────────────────────────────────────────────────────


@dataclass
class VerifyResult:
    tenant_id: str
    ok: bool
    rows: int = 0
    first_seq: int | None = None
    last_seq: int | None = None
    # row_hash of the newest row: record it outside the database to detect
    # rows later removed from the end, or a rewritten chain.
    head_hash: str | None = None
    # How the first row links back: genesis, checkpoint (a purge) or empty.
    anchor: str = "empty"
    first_break: dict | None = None


def _break(row: AuditEvent | None, seq: int | None, reason: str) -> dict:
    return {
        "seq": seq if row is None else row.seq,
        "id": str(row.id) if row is not None else None,
        "reason": reason,
    }


async def verify_chain(
    db: AsyncSession, tenant_id: UUID, *, batch_size: int = 1000
) -> VerifyResult:
    """Walk one tenant's chain in seq order and report the first break."""
    result = VerifyResult(tenant_id=str(tenant_id), ok=True)
    # seq counts from 1 and the walk starts there, so a row below 1 would
    # never be looked at. The CHECK constraint (migration 023) refuses new
    # ones; a row from before it, or with the constraint dropped, is a break.
    below = (
        await db.execute(
            select(AuditEvent)
            .where(AuditEvent.tenant_id == tenant_id, AuditEvent.seq < 1)
            .order_by(AuditEvent.seq)
            .limit(1)
        )
    ).scalar_one_or_none()
    if below is not None:
        result.ok = False
        result.first_break = _break(
            below, None, f"seq {below.seq} is below 1 (row added outside the chain)"
        )
        return result
    checkpoints = {
        (r.details or {}).get("through_seq"): (r.details or {}).get("through_hash")
        for r in (
            await db.execute(
                select(AuditEvent).where(
                    AuditEvent.tenant_id == tenant_id,
                    AuditEvent.action == CHECKPOINT_ACTION,
                )
            )
        ).scalars()
    }
    prev: AuditEvent | None = None
    after = 0
    while True:
        rows = (
            await db.execute(
                select(AuditEvent)
                .where(AuditEvent.tenant_id == tenant_id, AuditEvent.seq > after)
                .order_by(AuditEvent.seq)
                .limit(batch_size)
            )
        ).scalars().all()
        if not rows:
            break
        for row in rows:
            problem = None
            if prev is None:
                result.first_seq = row.seq
                if row.seq == 1:
                    result.anchor = "genesis"
                    if row.prev_hash != GENESIS_HASH:
                        problem = "first row's prev_hash is not the genesis hash"
                elif checkpoints.get(row.seq - 1) == row.prev_hash:
                    result.anchor = "checkpoint"
                else:
                    problem = (
                        f"rows before seq {row.seq} are missing and no audit.purge "
                        "checkpoint records their removal"
                    )
            elif row.seq != prev.seq + 1:
                problem = f"seq gap: expected {prev.seq + 1}, found {row.seq} (rows removed)"
            elif row.prev_hash != prev.row_hash:
                problem = f"prev_hash does not match the row_hash of seq {prev.seq}"
            if problem is None and row.row_hash != audit_chain.compute_row_hash(
                row.prev_hash, chained_fields(row)
            ):
                problem = "row_hash does not match the row's contents (row modified)"
            if problem is not None:
                result.ok = False
                result.first_break = _break(row, None, problem)
                result.rows += 1
                result.last_seq = row.seq
                return result
            result.rows += 1
            result.last_seq = row.seq
            result.head_hash = row.row_hash
            prev = row
        after = rows[-1].seq
        db.expunge_all()  # keep memory flat on long chains
    return result


async def chain_tenants(db: AsyncSession) -> list[UUID]:
    return list(
        (
            await db.execute(
                select(AuditEvent.tenant_id).distinct().order_by(AuditEvent.tenant_id)
            )
        ).scalars()
    )


async def verify_all(db: AsyncSession, tenant_id: UUID | None = None) -> list[VerifyResult]:
    """Verify one chain, or every chain the session can see."""
    tenants = [tenant_id] if tenant_id is not None else await chain_tenants(db)
    return [await verify_chain(db, t) for t in tenants]


# ──────────────────────────────────────────────────────────────────────
# Retention purge with checkpoints
# ──────────────────────────────────────────────────────────────────────


@dataclass
class PurgeResult:
    tenant_id: str
    rows_deleted: int
    through_seq: int | None = None
    checkpoint_seq: int | None = None
    extra: dict = field(default_factory=dict)


async def purge_chain(
    db: AsyncSession,
    tenant_id: UUID,
    cutoff: datetime,
    *,
    actor: Actor,
) -> PurgeResult:
    """Remove the prefix of one chain up to the newest row older than
    `cutoff`, after appending a checkpoint that names its last seq and
    row_hash. One transaction; commits."""
    await _lock_chain(db, tenant_id)
    last = (
        await db.execute(
            select(AuditEvent.seq, AuditEvent.row_hash)
            .where(AuditEvent.tenant_id == tenant_id, AuditEvent.created_at < cutoff)
            .order_by(AuditEvent.seq.desc())
            .limit(1)
        )
    ).one_or_none()
    if last is None:
        await db.commit()
        return PurgeResult(tenant_id=str(tenant_id), rows_deleted=0)
    count = (
        await db.execute(
            select(func.count())
            .select_from(AuditEvent)
            .where(AuditEvent.tenant_id == tenant_id, AuditEvent.seq <= last.seq)
        )
    ).scalar_one()
    checkpoint = await record(
        db,
        tenant_id=tenant_id,
        action=CHECKPOINT_ACTION,
        actor=actor,
        target_type="audit_chain",
        target_id=str(tenant_id),
        details={
            "through_seq": last.seq,
            "through_hash": last.row_hash,
            "rows_deleted": count,
            "cutoff": cutoff,
        },
    )
    if _is_postgres(db):
        await db.execute(
            text("SELECT set_config(:name, :tenant, true)"),
            {"name": audit_chain.PURGE_SETTING, "tenant": str(tenant_id)},
        )
    result = await db.execute(
        delete(AuditEvent).where(
            AuditEvent.tenant_id == tenant_id, AuditEvent.seq <= last.seq
        )
    )
    await db.commit()
    return PurgeResult(
        tenant_id=str(tenant_id),
        rows_deleted=result.rowcount or 0,
        through_seq=last.seq,
        checkpoint_seq=checkpoint.seq,
    )


async def purge_expired(
    db: AsyncSession,
    retention_days: int,
    *,
    now: datetime | None = None,
    tenant_id: UUID | None = None,
    actor: Actor | None = None,
) -> list[PurgeResult]:
    if retention_days <= 0:
        raise ValueError("retention_days must be positive")
    cutoff = (now or datetime.now(UTC)) - timedelta(days=retention_days)
    actor = actor or system_actor("audit-trail-purge")
    tenants = [tenant_id] if tenant_id is not None else await chain_tenants(db)
    return [await purge_chain(db, t, cutoff, actor=actor) for t in tenants]


# ──────────────────────────────────────────────────────────────────────
# CLI: python -m collector.audit_trail verify|purge
# ──────────────────────────────────────────────────────────────────────


async def _run_cli(args: argparse.Namespace) -> tuple[int, dict]:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from collector.config import settings

    engine = create_async_engine(settings.database_url)
    tenant = UUID(args.tenant) if args.tenant else None
    try:
        factory = async_sessionmaker(
            engine,
            class_=AsyncSession,
            expire_on_commit=False,
            info=rls.bypass_info(f"audit-trail-{args.command}-cli"),
        )
        async with factory() as db:
            if args.command == "verify":
                results = await verify_all(db, tenant)
                ok = all(r.ok for r in results)
                return (0 if ok else 1), {"ok": ok, "data": [asdict(r) for r in results]}
            days = args.days if args.days is not None else settings.audit_event_retention_days
            if days <= 0:
                return 0, {"skipped": "PS_AUDIT_EVENT_RETENTION_DAYS is 0 (keep forever)"}
            results = await purge_expired(
                db, days, tenant_id=tenant, actor=system_actor("audit-trail-purge-cli")
            )
            return 0, {"data": [asdict(r) for r in results]}
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m collector.audit_trail",
        description=(
            "Verify the hash-chained audit trail (exit 1 on a break), or purge "
            "rows older than PS_AUDIT_EVENT_RETENTION_DAYS with a checkpoint."
        ),
    )
    parser.add_argument("command", choices=("verify", "purge"))
    parser.add_argument("--tenant", help="only this tenant's chain")
    parser.add_argument("--days", type=int, default=None, help="purge: retention in days")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    code, out = asyncio.run(_run_cli(args))
    print(json.dumps(out, indent=2))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
