"""Partner change webhooks (#103).

Partners can be pushed asset changes instead of polling `/partner/v1/changes`.
A tenant admin subscribes one of the tenant's partner credentials to an
HTTPS endpoint (see `collector.webhook_admin`); the collector then POSTs a
signed JSON event for each asset change.

Events
  asset.created   a new asset was discovered (ingest, OTel ingest)
  asset.changed   discovery sources, confidence, owner, classification or
                  lifecycle state changed (including stale and reactivated)
  asset.retired   the lifecycle sweep retired the asset

Flow
  1. Producers (ingest, OTel ingest, lifecycle sweep) call
     `enqueue_asset_events` in the same transaction as the asset change.
     It writes one `webhook_deliveries` row per matching subscription: an
     outbox, so nothing is sent on the request path.
  2. A worker (`deliver_due`, run by `python -m collector.webhooks deliver`
     or the in-process loop when PS_WEBHOOK_WORKER_INTERVAL_SECONDS > 0)
     claims due rows with SKIP LOCKED, signs and POSTs them, and records
     the result. Failures retry with backoff (RETRY_SCHEDULE_SECONDS) up to
     PS_WEBHOOK_MAX_ATTEMPTS, then the row is `dead` (the dead-letter view).

Security
  - Destinations: https only (PS_WEBHOOK_ALLOW_HTTP for dev). The host is
    resolved and every address must be public; the request is sent to the
    checked address (no second lookup, so no DNS rebinding) with TLS SNI
    and certificate checks against the original host name. Redirects are
    not followed and proxy environment variables are ignored.
  - Signing: `Prompt-Shields-Signature: t=<unix>,v1=<hex>` where v1 is
    HMAC-SHA256(secret, "<t>." + body). Receivers reject stale timestamps
    (SIGNATURE_TOLERANCE_SECONDS) to stop replays; the event id is stable
    across retries for idempotency.
  - Secrets: per subscription, returned once, stored AES-GCM encrypted with
    a key derived from PS_WEBHOOK_SECRET_KEY and bound to the row id.
  - Scope: only partners that are active, unexpired and hold the scope
    for the event (EVENT_SCOPES; `registry:read` still covers it through
    the partner_scopes alias) get events, checked at enqueue and again at
    delivery.
    Payloads carry an allowlisted asset summary: no owner email, no prompt
    content, no usage events.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import hmac
import ipaddress
import json
import logging
import secrets
import socket
import time
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal
from urllib.parse import SplitResult, urlsplit, urlunsplit
from uuid import UUID, uuid4

import httpx
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from db.models import AIAsset, PartnerCredential, WebhookDelivery, WebhookSubscription
from sqlalchemy import delete, inspect, or_, select, update
from sqlalchemy.exc import NoInspectionAvailable
from sqlalchemy.ext.asyncio import AsyncSession

from collector import rls
from collector.config import settings
from collector.partner_scopes import expand_scopes

logger = logging.getLogger(__name__)

ASSET_CREATED = "asset.created"
ASSET_CHANGED = "asset.changed"
ASSET_RETIRED = "asset.retired"
EVENT_TYPES: tuple[str, ...] = (ASSET_CREATED, ASSET_CHANGED, ASSET_RETIRED)
EventType = Literal["asset.created", "asset.changed", "asset.retired"]

# Scope a partner must hold to receive each event type. Asset events carry
# the same asset data `/partner/v1/changes` returns under `changes:read`.
# Checked with partner_scopes.expand_scopes, so Phase 1 `registry:read`
# credentials qualify through the alias.
EVENT_SCOPES: dict[str, str] = {
    ASSET_CREATED: "changes:read",
    ASSET_CHANGED: "changes:read",
    ASSET_RETIRED: "changes:read",
}


def required_scope(event_type: str) -> str:
    return EVENT_SCOPES[event_type]


def partner_has_event_scope(partner: PartnerCredential, event_type: str) -> bool:
    return required_scope(event_type) in expand_scopes(partner.scopes)

SIGNATURE_HEADER = "Prompt-Shields-Signature"
EVENT_ID_HEADER = "Prompt-Shields-Event-Id"
EVENT_TYPE_HEADER = "Prompt-Shields-Event-Type"
ATTEMPT_HEADER = "Prompt-Shields-Delivery-Attempt"
SIGNATURE_TOLERANCE_SECONDS = 300

# Delay before attempt n+1, indexed by attempts made so far (last entry repeats).
RETRY_SCHEDULE_SECONDS: tuple[int, ...] = (60, 300, 1800, 7200, 21600, 43200, 86400)
# A claimed delivery is retried after this if the worker dies mid-attempt.
CLAIM_LEASE = timedelta(minutes=5)
MAX_SUBSCRIPTIONS_PER_PARTNER = 10
MAX_URL_LENGTH = 2048
MAX_ERROR_LENGTH = 200

# Asset fields whose change emits asset.changed. last_seen moves on every
# event and is deliberately not one of them.
WATCHED_FIELDS: tuple[str, ...] = (
    "discovery_source",
    "confidence",
    "owner_email",
    "data_classification",
    "lifecycle_state",
)

# Asset fields included in event payloads. owner_email (personal data) is
# left out; partners that need it read the asset through the API.
PAYLOAD_FIELDS: tuple[str, ...] = (
    "vendor",
    "model",
    "use_case_name",
    "business_unit",
    "environment",
    "status",
    "data_classification",
    "discovery_source",
    "confidence",
    "lifecycle_state",
)
PAYLOAD_TIMESTAMPS: tuple[str, ...] = (
    "first_seen",
    "last_seen",
    "lifecycle_changed_at",
)


class WebhooksNotConfigured(RuntimeError):
    """PS_WEBHOOK_SECRET_KEY is missing or too short."""


class DestinationNotAllowed(ValueError):
    """The webhook URL is malformed or points somewhere it must not."""


def _utcnow() -> datetime:
    return datetime.now(UTC)


# ──────────────────────────────────────────────────────────────────────
# Secrets: generation and encryption at rest
# ──────────────────────────────────────────────────────────────────────


def generate_signing_secret() -> str:
    return f"whsec_{secrets.token_urlsafe(32)}"


def _aead() -> AESGCM:
    key = settings.webhook_secret_key
    if len(key) < 32:
        raise WebhooksNotConfigured(
            "PS_WEBHOOK_SECRET_KEY must be set (at least 32 characters) to use webhooks"
        )
    derived = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=b"prompt-shields webhook signing secrets v1",
    ).derive(key.encode())
    return AESGCM(derived)


def encrypt_secret(plaintext: str, subscription_id: UUID) -> str:
    nonce = secrets.token_bytes(12)
    ct = _aead().encrypt(nonce, plaintext.encode(), subscription_id.bytes)
    return "v1." + base64.urlsafe_b64encode(nonce + ct).decode()


def decrypt_secret(token: str, subscription_id: UUID) -> str:
    """Raises ValueError if the token is malformed or was not encrypted
    for this subscription with the current key."""
    version, _, blob = token.partition(".")
    if version != "v1" or not blob:
        raise ValueError("unknown secret format")
    raw = base64.urlsafe_b64decode(blob.encode())
    try:
        pt = _aead().decrypt(raw[:12], raw[12:], subscription_id.bytes)
    except InvalidTag as exc:
        raise ValueError("secret could not be decrypted") from exc
    return pt.decode()


# ──────────────────────────────────────────────────────────────────────
# Signatures
# ──────────────────────────────────────────────────────────────────────


def _mac(secret: str, body: bytes, timestamp: int) -> str:
    return hmac.new(
        secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256
    ).hexdigest()


def sign_payload(secret: str, body: bytes, timestamp: int) -> str:
    """Value for the Prompt-Shields-Signature header."""
    return f"t={timestamp},v1={_mac(secret, body, timestamp)}"


def verify_signature(
    secret: str,
    body: bytes,
    header: str,
    *,
    tolerance: int = SIGNATURE_TOLERANCE_SECONDS,
    now: float | None = None,
) -> bool:
    """Reference receiver check (the docs carry the same code)."""
    try:
        parts = dict(p.split("=", 1) for p in header.split(","))
        timestamp = int(parts["t"])
    except (KeyError, ValueError):
        return False
    if abs((time.time() if now is None else now) - timestamp) > tolerance:
        return False
    expected = _mac(secret, body, timestamp)
    return hmac.compare_digest(expected, parts.get("v1", ""))


# ──────────────────────────────────────────────────────────────────────
# Destination checks (SSRF)
# ──────────────────────────────────────────────────────────────────────


def check_url(url: str) -> SplitResult:
    """Syntax checks only. Raises DestinationNotAllowed."""
    if len(url) > MAX_URL_LENGTH:
        raise DestinationNotAllowed("URL is too long")
    try:
        parts = urlsplit(url)
        parts.port  # noqa: B018 - raises ValueError on a bad port
    except ValueError as exc:
        raise DestinationNotAllowed("URL is not valid") from exc
    allowed = ("https", "http") if settings.webhook_allow_http else ("https",)
    if parts.scheme not in allowed:
        raise DestinationNotAllowed("URL must use https")
    if not parts.hostname:
        raise DestinationNotAllowed("URL must include a host")
    if parts.username is not None or parts.password is not None:
        raise DestinationNotAllowed("URL must not contain credentials")
    if parts.fragment:
        raise DestinationNotAllowed("URL must not contain a fragment")
    return parts


# IPv6 ranges that embed or stand for an IPv4 address, or are deprecated,
# and that `is_global` does not catch on every Python version:
# NAT64 well-known prefix (64:ff9b::/96 reaches the embedded IPv4 address
# through a NAT64 gateway, e.g. 64:ff9b::a9fe:a9fe -> 169.254.169.254),
# IPv4-compatible ::/96 and site-local fec0::/10.
_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_IPV4_COMPATIBLE = ipaddress.ip_network("::/96")
_SITE_LOCAL = ipaddress.ip_network("fec0::/10")


def ip_allowed(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True for publicly routable unicast addresses only."""
    if ip.version == 6:
        if ip in _IPV4_COMPATIBLE or ip in _SITE_LOCAL:
            return False
        embedded = ip.ipv4_mapped or ip.sixtofour or (ip.teredo[1] if ip.teredo else None)
        if embedded is None and ip in _NAT64:
            embedded = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
        if embedded is not None and not ip_allowed(embedded):
            return False
    if ip.is_unspecified or ip.is_multicast:
        return False
    if settings.webhook_allow_private_destinations:
        return True
    return ip.is_global


async def _resolve_host(host: str, port: int) -> list[str]:
    """All addresses `host` resolves to. Patched in tests."""
    infos = await asyncio.get_running_loop().getaddrinfo(
        host, port, type=socket.SOCK_STREAM
    )
    return list(dict.fromkeys(info[4][0] for info in infos))


async def resolve_destination(url: str) -> tuple[SplitResult, str]:
    """Check the URL and resolve its host. Returns the parsed URL and the
    address to connect to. Every resolved address must be allowed, so a
    host with one public and one private record is refused."""
    parts = check_url(url)
    port = parts.port or (443 if parts.scheme == "https" else 80)
    refused = DestinationNotAllowed("URL must resolve only to public addresses")
    try:
        addresses = await _resolve_host(parts.hostname, port)
    except OSError as exc:
        raise refused from exc
    if not addresses:
        raise refused
    for addr in addresses:
        try:
            ip = ipaddress.ip_address(addr.split("%", 1)[0])
        except ValueError as exc:
            raise refused from exc
        if not ip_allowed(ip):
            raise refused
    return parts, addresses[0]


async def post_signed(
    url: str,
    body: bytes,
    headers: dict[str, str],
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> int:
    """POST `body` to `url` via the checked address. Returns the status code.

    Raises DestinationNotAllowed, or httpx errors for network failures.
    The response body is never read. The whole attempt (resolve, connect,
    send, response headers) is bounded by PS_WEBHOOK_TIMEOUT_SECONDS:
    httpx timeouts apply per operation, so a server trickling its response
    headers could otherwise hold the attempt past the claim lease and get
    the delivery claimed and sent again by another worker.
    """
    try:
        async with asyncio.timeout(settings.webhook_timeout_seconds):
            return await _post_signed(url, body, headers, transport=transport)
    except TimeoutError as exc:
        raise httpx.TimeoutException("webhook attempt exceeded its deadline") from exc


async def _post_signed(
    url: str,
    body: bytes,
    headers: dict[str, str],
    *,
    transport: httpx.AsyncBaseTransport | None,
) -> int:
    parts, address = await resolve_destination(url)
    port = parts.port or (443 if parts.scheme == "https" else 80)
    host = f"[{address}]" if ":" in address else address
    target = urlunsplit(
        (parts.scheme, f"{host}:{port}", parts.path or "/", parts.query, "")
    )
    timeout = httpx.Timeout(settings.webhook_timeout_seconds)
    async with httpx.AsyncClient(
        transport=transport,
        follow_redirects=False,
        trust_env=False,
        timeout=timeout,
    ) as client, client.stream(
        "POST",
        target,
        content=body,
        headers={**headers, "Host": parts.netloc},
        extensions={"sni_hostname": parts.hostname},
    ) as resp:
        return resp.status_code


# ──────────────────────────────────────────────────────────────────────
# Producing events (outbox)
# ──────────────────────────────────────────────────────────────────────


def asset_payload(asset: AIAsset) -> dict:
    out: dict = {"id": str(asset.id)}
    for field in PAYLOAD_FIELDS:
        out[field] = getattr(asset, field, None)
    for field in PAYLOAD_TIMESTAMPS:
        value = getattr(asset, field, None)
        out[field] = value.isoformat() if value is not None else None
    return out


def changed_fields(asset: AIAsset) -> list[str]:
    """Watched fields with unflushed changes. Call it right after the
    change, before anything flushes the session."""
    try:
        state = inspect(asset)
    except NoInspectionAvailable:
        return []
    return [f for f in WATCHED_FIELDS if state.attrs[f].history.has_changes()]


class AssetChanges:
    """Collects asset events during an ingest batch; one event per asset.

    A created asset that also changes later in the batch is reported once,
    as created. Repeated changes merge their field lists.
    """

    def __init__(self) -> None:
        self._events: dict[int, tuple[str, AIAsset, set[str]]] = {}

    def record(self, asset: AIAsset, is_new: bool) -> None:
        key = id(asset)
        if is_new:
            self._events[key] = (ASSET_CREATED, asset, set())
            return
        fields = changed_fields(asset)
        if not fields:
            return
        event = self._events.setdefault(key, (ASSET_CHANGED, asset, set()))
        event[2].update(fields)

    async def enqueue(self, db: AsyncSession, tenant_id: UUID) -> int:
        events = [
            (etype, asset, sorted(fields) if etype == ASSET_CHANGED else None)
            for etype, asset, fields in self._events.values()
        ]
        self._events.clear()
        return await enqueue_asset_events(db, tenant_id, events)


def _eligible_partner_clause(now: datetime):
    """Active and unexpired. The per-event scope is checked in Python with
    partner_scopes.expand_scopes so aliases resolve in one place."""
    return (
        PartnerCredential.is_active == "true",
        or_(PartnerCredential.expires_at.is_(None), PartnerCredential.expires_at > now),
    )


async def enqueue_asset_events(
    db: AsyncSession,
    tenant_id: UUID,
    events: Iterable[tuple[str, AIAsset, list[str] | None]],
    *,
    now: datetime | None = None,
) -> int:
    """Write outbox rows for `events` (event_type, asset, changed_fields).

    Assets must have ids (flush first). Does not commit: the rows land in
    the caller's transaction with the change itself. Returns the number of
    deliveries queued.
    """
    events = list(events)
    if not events:
        return 0
    now = now or _utcnow()
    subs = (
        await db.execute(
            select(
                WebhookSubscription.id,
                WebhookSubscription.event_types,
                PartnerCredential.scopes,
            )
            .join(PartnerCredential, PartnerCredential.id == WebhookSubscription.partner_id)
            .where(
                WebhookSubscription.tenant_id == tenant_id,
                WebhookSubscription.is_active.is_(True),
                PartnerCredential.tenant_id == tenant_id,
                *_eligible_partner_clause(now),
            )
        )
    ).all()
    if not subs:
        return 0

    queued = 0
    for event_type, asset, fields in events:
        scope = required_scope(event_type)
        targets = [
            s.id
            for s in subs
            if event_type in s.event_types and scope in expand_scopes(s.scopes)
        ]
        if not targets:
            continue
        event_id = uuid4()
        data: dict = {"asset": asset_payload(asset)}
        if event_type == ASSET_CHANGED:
            data["changed_fields"] = list(fields or [])
        payload = {
            "id": str(event_id),
            "type": event_type,
            "created_at": now.isoformat(),
            "tenant_id": str(tenant_id),
            "data": data,
        }
        for sub_id in targets:
            db.add(
                WebhookDelivery(
                    tenant_id=tenant_id,
                    subscription_id=sub_id,
                    event_id=event_id,
                    event_type=event_type,
                    asset_id=asset.id,
                    payload=payload,
                    next_attempt_at=now,
                    created_at=now,
                )
            )
            queued += 1
    return queued


async def enqueue_lifecycle_events(
    db: AsyncSession, *, changed_at: datetime, tenant_id: UUID | None = None
) -> int:
    """Queue events for assets the lifecycle sweep moved at `changed_at`.

    retired -> asset.retired; stale and reactivated -> asset.changed.
    Does not commit.
    """
    scope = [WebhookSubscription.tenant_id == tenant_id] if tenant_id else []
    any_sub = (
        await db.execute(
            select(WebhookSubscription.id)
            .where(*scope, WebhookSubscription.is_active.is_(True))
            .limit(1)
        )
    ).scalar_one_or_none()
    if any_sub is None:
        return 0

    asset_scope = [AIAsset.tenant_id == tenant_id] if tenant_id else []
    assets = (
        await db.execute(
            select(AIAsset).where(*asset_scope, AIAsset.lifecycle_changed_at == changed_at)
        )
    ).scalars().all()

    by_tenant: dict[UUID, list] = {}
    for asset in assets:
        if asset.lifecycle_state == "retired":
            event = (ASSET_RETIRED, asset, None)
        else:
            event = (ASSET_CHANGED, asset, ["lifecycle_state"])
        by_tenant.setdefault(asset.tenant_id, []).append(event)

    queued = 0
    for tid, events in by_tenant.items():
        queued += await enqueue_asset_events(db, tid, events, now=changed_at)
    return queued


# ──────────────────────────────────────────────────────────────────────
# Delivering
# ──────────────────────────────────────────────────────────────────────


@dataclass
class DeliveryRun:
    claimed: int = 0
    succeeded: int = 0
    retrying: int = 0
    dead: int = 0


def retry_delay(attempts: int) -> timedelta:
    index = min(max(attempts, 1), len(RETRY_SCHEDULE_SECONDS)) - 1
    return timedelta(seconds=RETRY_SCHEDULE_SECONDS[index])


def partner_ineligible_reason(
    partner: PartnerCredential | None,
    now: datetime,
    event_types: Iterable[str] = EVENT_TYPES,
) -> str | None:
    """Why this partner may not receive `event_types`, or None if it may."""
    if partner is None or partner.is_active != "true":
        return "partner credential revoked"
    if partner.expires_at is not None and partner.expires_at <= now:
        return "partner credential expired"
    for event_type in event_types:
        if not partner_has_event_scope(partner, event_type):
            return f"partner credential lacks scope {required_scope(event_type)}"
    return None


def _ineligible(
    sub: WebhookSubscription | None,
    partner: PartnerCredential | None,
    now: datetime,
    event_type: str,
) -> str | None:
    if sub is None or not sub.is_active:
        return "subscription disabled"
    return partner_ineligible_reason(partner, now, (event_type,))


async def _attempt(
    session_factory,
    delivery_id: UUID,
    tenant_id: UUID,
    transport,
    sem: asyncio.Semaphore,
    lease_until: datetime,
) -> str:
    async with sem, session_factory() as db:
        # Row-level security: an attempt only ever touches its own tenant's
        # delivery, subscription and partner credential.
        await rls.set_tenant(db, tenant_id)
        # Not enough lease left to finish within the attempt deadline (e.g.
        # queued behind slow attempts): leave it, the lease expires and the
        # row is claimed again. Sending now could overlap another worker.
        deadline = timedelta(seconds=settings.webhook_timeout_seconds)
        if _utcnow() + deadline >= lease_until:
            return "skipped"
        d = await db.get(WebhookDelivery, delivery_id)
        if d is None or d.status != "pending":
            return "skipped"
        now = _utcnow()
        sub = await db.get(WebhookSubscription, d.subscription_id)
        partner = await db.get(PartnerCredential, sub.partner_id) if sub else None
        reason = _ineligible(sub, partner, now, d.event_type)
        if reason is None:
            try:
                secret = decrypt_secret(sub.secret_ciphertext, sub.id)
            except ValueError:
                reason = "signing secret unreadable; rotate the secret"
        if reason is not None:
            d.status = "dead"
            d.last_error = reason
            await db.commit()
            return "dead"

        body = json.dumps(d.payload, separators=(",", ":"), sort_keys=True).encode()
        d.attempts += 1
        d.last_attempt_at = now
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "PromptShields-Webhooks/1",
            SIGNATURE_HEADER: sign_payload(secret, body, int(time.time())),
            EVENT_ID_HEADER: str(d.event_id),
            EVENT_TYPE_HEADER: d.event_type,
            ATTEMPT_HEADER: str(d.attempts),
        }
        status: int | None = None
        try:
            status = await post_signed(sub.url, body, headers, transport=transport)
            error = None if 200 <= status < 300 else f"HTTP {status}"
        except DestinationNotAllowed as exc:
            error = f"destination not allowed: {exc}"
        except httpx.TimeoutException:
            error = "timeout"
        except httpx.HTTPError as exc:
            # Exception type only: messages can echo internal details.
            error = f"network error: {type(exc).__name__}"

        d.last_status_code = status
        d.last_error = error[:MAX_ERROR_LENGTH] if error else None
        if error is None:
            d.status = "succeeded"
            d.delivered_at = _utcnow()
            outcome = "succeeded"
        elif d.attempts >= settings.webhook_max_attempts:
            d.status = "dead"
            outcome = "dead"
        else:
            d.next_attempt_at = _utcnow() + retry_delay(d.attempts)
            outcome = "retrying"
        await db.commit()
        return outcome


def worker_session_factory(session_factory):
    """Sessions that bypass row-level security, for the cross-tenant claim
    and purge. The bypass is logged once, when this is called."""
    info = rls.bypass_info("webhook-worker")
    return lambda: session_factory(info=info)


async def deliver_due(
    session_factory,
    *,
    limit: int = 50,
    concurrency: int = 8,
    transport: httpx.AsyncBaseTransport | None = None,
    now: datetime | None = None,
    claim_factory=None,
) -> DeliveryRun:
    """Claim up to `limit` due deliveries and attempt each once.

    The claim spans tenants (``claim_factory``, an RLS bypass; built from
    ``session_factory`` when not given). Each attempt runs as its tenant.
    """
    _aead()  # fail fast when the key is missing, without burning attempts
    now = now or _utcnow()
    claim_factory = claim_factory or worker_session_factory(session_factory)
    async with claim_factory() as db:
        claimed = (
            await db.execute(
                select(WebhookDelivery.id, WebhookDelivery.tenant_id)
                .where(
                    WebhookDelivery.status == "pending",
                    WebhookDelivery.next_attempt_at <= now,
                )
                .order_by(WebhookDelivery.next_attempt_at)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        ).all()
        ids = [row.id for row in claimed]
        if ids:
            await db.execute(
                update(WebhookDelivery)
                .where(WebhookDelivery.id.in_(ids))
                .values(next_attempt_at=now + CLAIM_LEASE)
            )
        await db.commit()

    run = DeliveryRun(claimed=len(ids))
    sem = asyncio.Semaphore(concurrency)
    outcomes = await asyncio.gather(
        *(
            _attempt(session_factory, row.id, row.tenant_id, transport, sem, now + CLAIM_LEASE)
            for row in claimed
        ),
        return_exceptions=True,
    )
    for outcome in outcomes:
        if isinstance(outcome, BaseException):
            # e.g. the subscription was deleted mid-attempt. The claim lease
            # expires and the row (if it still exists) is retried.
            logger.warning("webhook delivery attempt failed: %r", outcome)
        elif outcome in ("succeeded", "retrying", "dead"):
            setattr(run, outcome, getattr(run, outcome) + 1)
    return run


async def purge_old_deliveries(db: AsyncSession, *, now: datetime | None = None) -> int:
    """Delete succeeded and dead deliveries older than the retention window."""
    cutoff = (now or _utcnow()) - timedelta(days=settings.webhook_delivery_retention_days)
    result = await db.execute(
        delete(WebhookDelivery).where(
            WebhookDelivery.status.in_(("succeeded", "dead")),
            WebhookDelivery.created_at < cutoff,
        )
    )
    await db.commit()
    return result.rowcount or 0


async def run_worker(session_factory, interval: float) -> None:
    """Deliver forever, every `interval` seconds; purge once an hour."""
    last_purge = 0.0
    claim_factory = worker_session_factory(session_factory)
    while True:
        try:
            await deliver_due(session_factory, claim_factory=claim_factory)
            if time.monotonic() - last_purge > 3600:
                async with claim_factory() as db:
                    await purge_old_deliveries(db)
                last_purge = time.monotonic()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("webhook delivery run failed")
        await asyncio.sleep(interval)


# ──────────────────────────────────────────────────────────────────────
# CLI: python -m collector.webhooks deliver|purge
# ──────────────────────────────────────────────────────────────────────


async def _run_cli(command: str, loop: bool, interval: float) -> None:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(settings.database_url)
    try:
        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        if command == "purge":
            async with worker_session_factory(factory)() as db:
                print(f"webhook deliveries purged: {await purge_old_deliveries(db)}")
        elif loop:
            await run_worker(factory, interval)
        else:
            print(f"webhook delivery run: {asdict(await deliver_due(factory))}")
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="python -m collector.webhooks",
        description="Deliver queued partner webhooks, or purge old delivery rows.",
    )
    parser.add_argument("command", choices=("deliver", "purge"))
    parser.add_argument("--loop", action="store_true", help="keep delivering")
    parser.add_argument("--interval", type=float, default=5.0)
    args = parser.parse_args(argv)
    asyncio.run(_run_cli(args.command, args.loop, args.interval))


if __name__ == "__main__":
    main()
