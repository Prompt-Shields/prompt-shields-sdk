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
  asset.attested  a person recorded an attestation for one of the asset's
                  fields (collector.attestation): field, state, actor type
                  and time. The free-text comment, the attested value and
                  `attested_by` (usually an email) are left out.

Flow
  1. Producers (ingest, OTel ingest, lifecycle sweep, attestation) call
     `enqueue_asset_events` (or `enqueue_attestation_event`) in the same
     transaction as the change.
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
    across retries for idempotency. During a secret rotation's grace
    period the header carries one v1 per valid secret, newest first;
    receivers accept the request if any v1 matches.
  - Secrets: per subscription, returned once, stored AES-GCM encrypted and
    bound to the row id. Keys come from the PS_WEBHOOK_SECRET_KEYS keyring
    ("kid:key,..."; first entry encrypts) and the token records the key
    id ("v2.<kid>.<data>"). Tokens without a key id ("v1.<data>") were
    written with the legacy PS_WEBHOOK_SECRET_KEY and still decrypt with
    it. Secrets under an older key are re-encrypted with the active key
    when used for a delivery, or all at once by
    `python -m collector.webhooks rekey`. A token whose key id this process
    does not have (a rolling restart that adds a key) is retried with the
    normal backoff rather than dead-lettered; see SecretKeyMissing.
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
import re
import secrets
import socket
import time
from collections.abc import Iterable, Sequence
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
from db.models import (
    AIAsset,
    AssetAttestation,
    PartnerCredential,
    WebhookDelivery,
    WebhookSubscription,
)
from sqlalchemy import delete, inspect, or_, select, update
from sqlalchemy.exc import NoInspectionAvailable
from sqlalchemy.ext.asyncio import AsyncSession

from collector import egress, rls
from collector.config import (
    WEBHOOK_CLAIM_LEASE_SECONDS,
    WEBHOOK_MAX_TIMEOUT_SECONDS,
    settings,
)
from collector.partner_scopes import expand_scopes

logger = logging.getLogger(__name__)

ASSET_CREATED = "asset.created"
ASSET_CHANGED = "asset.changed"
ASSET_RETIRED = "asset.retired"
ASSET_ATTESTED = "asset.attested"
EVENT_TYPES: tuple[str, ...] = (ASSET_CREATED, ASSET_CHANGED, ASSET_RETIRED, ASSET_ATTESTED)
EventType = Literal["asset.created", "asset.changed", "asset.retired", "asset.attested"]

# Scope a partner must hold to receive each event type. Asset events carry
# the same asset data `/partner/v1/changes` returns under `changes:read`;
# an attestation is reported by that feed too (it bumps the asset's
# updated_at and the asset carries its attestation summary), so
# asset.attested needs the same scope. Checked with
# partner_scopes.expand_scopes, so Phase 1 `registry:read` credentials
# qualify through the alias.
EVENT_SCOPES: dict[str, str] = {
    ASSET_CREATED: "changes:read",
    ASSET_CHANGED: "changes:read",
    ASSET_RETIRED: "changes:read",
    ASSET_ATTESTED: "changes:read",
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
CLAIM_LEASE = timedelta(seconds=WEBHOOK_CLAIM_LEASE_SECONDS)
# Longest overlap a secret rotation may keep the old secret valid for.
MAX_SECRET_GRACE = timedelta(days=7)
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
    """No usable encryption key: PS_WEBHOOK_SECRET_KEYS and
    PS_WEBHOOK_SECRET_KEY are both unset, a key is too short, or the
    keyring is malformed."""


class DestinationNotAllowed(ValueError):
    """The webhook URL is malformed or points somewhere it must not."""


class SecretKeyMissing(ValueError):
    """A stored secret names a key this process does not have: a key id
    missing from PS_WEBHOOK_SECRET_KEYS, or a legacy "v1." token without
    PS_WEBHOOK_SECRET_KEY.

    Usually a rolling restart that adds a key: processes already running
    the new keyring re-encrypt secrets with it, and the others cannot read
    them until they restart. Deliveries treat it as a retryable failure.
    """

    def __init__(self, key_id: str | None) -> None:
        self.key_id = key_id
        name = "legacy key PS_WEBHOOK_SECRET_KEY" if key_id is None else f"key id {key_id!r}"
        super().__init__(f"secret needs {name}, which this process does not have")


def _utcnow() -> datetime:
    return datetime.now(UTC)


# ──────────────────────────────────────────────────────────────────────
# Secrets: generation and encryption at rest
# ──────────────────────────────────────────────────────────────────────


def generate_signing_secret() -> str:
    return f"whsec_{secrets.token_urlsafe(32)}"


MIN_KEY_LENGTH = 32
_KEY_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,32}")
_INFO_LEGACY = b"prompt-shields webhook signing secrets v1"
_INFO_KEYRING = b"prompt-shields webhook signing secrets v2"


def _derive(key: str, info: bytes) -> AESGCM:
    derived = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=info).derive(
        key.encode()
    )
    return AESGCM(derived)


def keyring() -> list[tuple[str, str]]:
    """(key id, key) pairs from PS_WEBHOOK_SECRET_KEYS, active key first.

    Raises WebhooksNotConfigured if the keyring is malformed. Messages name
    key ids, never key material.
    """
    raw = settings.webhook_secret_keys.strip()
    if not raw:
        return []
    ring: list[tuple[str, str]] = []
    for position, entry in enumerate(raw.split(","), start=1):
        kid, sep, key = (part.strip() for part in entry.partition(":"))
        if not sep or not _KEY_ID_RE.fullmatch(kid):
            raise WebhooksNotConfigured(
                f"PS_WEBHOOK_SECRET_KEYS entry {position} must be <key id>:<key>, "
                "with a key id of 1-32 characters from A-Z a-z 0-9 _ -"
            )
        if any(kid == seen for seen, _ in ring):
            raise WebhooksNotConfigured(f"PS_WEBHOOK_SECRET_KEYS repeats key id {kid!r}")
        if len(key) < MIN_KEY_LENGTH:
            raise WebhooksNotConfigured(
                f"PS_WEBHOOK_SECRET_KEYS key {kid!r} must be at least "
                f"{MIN_KEY_LENGTH} characters"
            )
        ring.append((kid, key))
    return ring


def active_key_id() -> str | None:
    """Key id new secrets are encrypted with; None means the legacy
    PS_WEBHOOK_SECRET_KEY. Raises WebhooksNotConfigured if there is none."""
    ring = keyring()
    if ring:
        return ring[0][0]
    if len(settings.webhook_secret_key) < MIN_KEY_LENGTH:
        raise WebhooksNotConfigured(
            "PS_WEBHOOK_SECRET_KEYS or PS_WEBHOOK_SECRET_KEY (at least "
            f"{MIN_KEY_LENGTH} characters) must be set to use webhooks"
        )
    return None


def _legacy_aead() -> AESGCM:
    if len(settings.webhook_secret_key) < MIN_KEY_LENGTH:
        raise SecretKeyMissing(None)
    return _derive(settings.webhook_secret_key, _INFO_LEGACY)


def _keyring_aad(subscription_id: UUID, kid: str) -> bytes:
    # The key id is authenticated too, so a token cannot be relabelled.
    return subscription_id.bytes + b"." + kid.encode()


def encrypt_secret(plaintext: str, subscription_id: UUID) -> str:
    """Encrypt with the active key, bound to the subscription id.

    "v2.<kid>.<data>" with a keyring, else the legacy "v1.<data>".
    Raises WebhooksNotConfigured.
    """
    kid = active_key_id()
    nonce = secrets.token_bytes(12)
    if kid is None:
        ct = _legacy_aead().encrypt(nonce, plaintext.encode(), subscription_id.bytes)
        return "v1." + base64.urlsafe_b64encode(nonce + ct).decode()
    key = dict(keyring())[kid]
    ct = _derive(key, _INFO_KEYRING).encrypt(
        nonce, plaintext.encode(), _keyring_aad(subscription_id, kid)
    )
    return f"v2.{kid}." + base64.urlsafe_b64encode(nonce + ct).decode()


def token_key_id(token: str) -> str | None:
    """Key id a stored token was encrypted with (None: the legacy key).
    Raises ValueError for an unknown format."""
    version, _, rest = token.partition(".")
    if version == "v1" and rest:
        return None
    if version == "v2":
        kid, _, blob = rest.partition(".")
        if kid and blob:
            return kid
    raise ValueError("unknown secret format")


def needs_rekey(token: str) -> bool:
    """True if `token` was not encrypted with the active key.
    Raises WebhooksNotConfigured."""
    active = active_key_id()
    try:
        return token_key_id(token) != active
    except ValueError:
        return False  # unreadable either way; nothing to re-encrypt


def decrypt_secret(token: str, subscription_id: UUID) -> str:
    """Decrypt with the key the token names (the legacy key for "v1.").

    Raises SecretKeyMissing (a ValueError) if the token's key is not
    configured in this process, and ValueError if the token is malformed or
    was not encrypted for this subscription with that key.
    Raises WebhooksNotConfigured if the keyring itself is malformed (so a
    configuration mistake never dead-letters deliveries).
    """
    kid = token_key_id(token)
    blob = token.rsplit(".", 1)[1]
    if kid is None:
        aead, aad = _legacy_aead(), subscription_id.bytes
    else:
        key = dict(keyring()).get(kid)
        if key is None:
            raise SecretKeyMissing(kid)
        aead, aad = _derive(key, _INFO_KEYRING), _keyring_aad(subscription_id, kid)
    try:
        raw = base64.urlsafe_b64decode(blob.encode())
        pt = aead.decrypt(raw[:12], raw[12:], aad)
    except (InvalidTag, ValueError) as exc:  # binascii.Error is a ValueError
        raise ValueError("secret could not be decrypted") from exc
    return pt.decode()


# ──────────────────────────────────────────────────────────────────────
# Signatures
# ──────────────────────────────────────────────────────────────────────


def _mac(secret: str, body: bytes, timestamp: int) -> str:
    return hmac.new(
        secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256
    ).hexdigest()


def sign_payload(secret: str | Sequence[str], body: bytes, timestamp: int) -> str:
    """Value for the Prompt-Shields-Signature header: one v1 per secret, in
    order (the current secret first, then one still in its grace period)."""
    secret_list = [secret] if isinstance(secret, str) else list(secret)
    v1 = ",".join(f"v1={_mac(s, body, timestamp)}" for s in secret_list)
    return f"t={timestamp},{v1}"


def verify_signature(
    secret: str,
    body: bytes,
    header: str,
    *,
    tolerance: int = SIGNATURE_TOLERANCE_SECONDS,
    now: float | None = None,
) -> bool:
    """Reference receiver check (the docs carry the same logic; a test runs
    the documented samples against this). Accepts the request if any v1 in
    the header matches. A malformed header (missing or non-numeric `t`,
    items without "=", non-ASCII) returns False and never raises."""
    if not isinstance(header, str):
        return False
    timestamp: str | None = None
    signatures: list[str] = []
    for item in header.split(","):
        key, sep, value = item.partition("=")
        if not sep:
            continue
        if key == "t" and timestamp is None:
            timestamp = value
        elif key == "v1":
            signatures.append(value)
    # ASCII digits only, and short enough to stay an ordinary number.
    if (
        timestamp is None
        or not signatures
        or not (timestamp.isascii() and timestamp.isdigit())
        or len(timestamp) > 12
    ):
        return False
    ts = int(timestamp)
    if abs((time.time() if now is None else now) - ts) > tolerance:
        return False
    expected = _mac(secret, body, ts)
    # Compare against every candidate so timing does not reveal which matched.
    matched = False
    for value in signatures:
        matched |= value.isascii() and hmac.compare_digest(expected, value)
    return matched


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


def ip_allowed(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True for publicly routable unicast addresses only (see
    `collector.egress.ip_is_public`)."""
    return egress.ip_is_public(
        ip, allow_private=settings.webhook_allow_private_destinations
    )


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


def attempt_timeout_seconds() -> float:
    """PS_WEBHOOK_TIMEOUT_SECONDS, capped at WEBHOOK_MAX_TIMEOUT_SECONDS.

    The setting is validated at startup; the cap also covers a value set
    at runtime. A deadline at or above the claim lease would make every
    attempt skip itself (see `_attempt`), so nothing would ever be sent.
    """
    return min(settings.webhook_timeout_seconds, WEBHOOK_MAX_TIMEOUT_SECONDS)


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
        async with asyncio.timeout(attempt_timeout_seconds()):
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
    timeout = httpx.Timeout(attempt_timeout_seconds())
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
    extra = [
        (etype, asset, {"changed_fields": list(fields or [])} if etype == ASSET_CHANGED else {})
        for etype, asset, fields in events
    ]
    return await _enqueue(db, tenant_id, extra, now or _utcnow())


async def enqueue_attestation_event(
    db: AsyncSession,
    tenant_id: UUID,
    asset: AIAsset,
    attestation: AssetAttestation,
    *,
    now: datetime | None = None,
) -> int:
    """Queue asset.attested for a just-recorded attestation (flushed, so it
    has an id). Does not commit.

    Carries the field, state, actor type and time. Left out: `comment`
    (free text that can name people), `value` (the `owner` field's value is
    the owner's email) and `attested_by` (the person, usually an email);
    partners read those from the attestation history endpoint.
    """
    attested_at = attestation.created_at or now or _utcnow()
    data = {
        "attestation": {
            "id": str(attestation.id),
            "field": attestation.field,
            "state": attestation.state,
            "actor_type": attestation.actor_type,
            "attested_at": attested_at.isoformat(),
        }
    }
    return await _enqueue(db, tenant_id, [(ASSET_ATTESTED, asset, data)], now or attested_at)


async def _enqueue(
    db: AsyncSession,
    tenant_id: UUID,
    events: list[tuple[str, AIAsset, dict]],
    now: datetime,
) -> int:
    """Outbox rows for (event_type, asset, extra data) to every eligible
    subscription of the tenant. `data` is {"asset": ..., **extra}."""
    if not events:
        return 0
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
    for event_type, asset, extra in events:
        scope = required_scope(event_type)
        targets = [
            s.id
            for s in subs
            if event_type in s.event_types and scope in expand_scopes(s.scopes)
        ]
        if not targets:
            continue
        event_id = uuid4()
        data: dict = {"asset": asset_payload(asset), **extra}
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
        deadline = timedelta(seconds=attempt_timeout_seconds())
        if _utcnow() + deadline >= lease_until:
            return "skipped"
        d = await db.get(WebhookDelivery, delivery_id)
        if d is None or d.status != "pending":
            return "skipped"
        now = _utcnow()
        sub = await db.get(WebhookSubscription, d.subscription_id)
        partner = await db.get(PartnerCredential, sub.partner_id) if sub else None
        reason = _ineligible(sub, partner, now, d.event_type)
        missing_key: SecretKeyMissing | None = None
        if reason is None:
            try:
                secrets_to_sign = signing_secrets(sub, now)
            except SecretKeyMissing as exc:
                missing_key = exc
            except ValueError:
                reason = "signing secret unreadable; rotate the secret"
        if reason is not None:
            d.status = "dead"
            d.last_error = reason
            await db.commit()
            return "dead"
        if missing_key is not None:
            # Most likely a rolling restart: another process already uses a
            # key this one lacks. Count the attempt and back off as for any
            # failure, without sending; a process that has the key (or this
            # one, once restarted) delivers it.
            _log_missing_key(missing_key)
            d.attempts += 1
            d.last_attempt_at = now
            d.last_status_code = None
            d.last_error = _missing_key_error(missing_key)
            outcome = _fail(d)
            if outcome == "dead":
                d.last_error = _missing_key_error(missing_key, final=True)
            await db.commit()
            return outcome

        body = json.dumps(d.payload, separators=(",", ":"), sort_keys=True).encode()
        d.attempts += 1
        d.last_attempt_at = now
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "PromptShields-Webhooks/1",
            SIGNATURE_HEADER: sign_payload(secrets_to_sign, body, int(time.time())),
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
        else:
            outcome = _fail(d)
        # Re-encrypt secrets stored under an older key, and drop an expired
        # previous secret. After the POST so the row lock is held briefly.
        try:
            await refresh_stored_secrets(db, sub, now)
        except ValueError:
            pass  # current secret readable (we just signed); previous skipped
        await db.commit()
        return outcome


def _fail(d: WebhookDelivery) -> str:
    """Schedule a failed attempt's retry, or dead-letter it after the last."""
    if d.attempts >= settings.webhook_max_attempts:
        d.status = "dead"
        return "dead"
    d.next_attempt_at = _utcnow() + retry_delay(d.attempts)
    return "retrying"


# Keys this process already warned about (None: the legacy key), so a
# rollout logs once per key rather than once per delivery.
_missing_keys_logged: set[str | None] = set()


def _missing_key_error(exc: SecretKeyMissing, *, final: bool = False) -> str:
    key = "the legacy key" if exc.key_id is None else f"key id {exc.key_id}"
    if final:
        return (
            f"signing secret uses {key}, not configured on this worker; "
            "gave up after the last attempt (add the key or rotate the secret)"
        )
    return f"signing secret uses {key}, not configured on this worker; will retry"


def _log_missing_key(exc: SecretKeyMissing) -> None:
    if exc.key_id in _missing_keys_logged:
        return
    _missing_keys_logged.add(exc.key_id)
    logger.warning(
        "webhook signing secrets use %s, which this process does not have; "
        "their deliveries are retried with backoff. Add a new key to every "
        "process (after the active key) before making it active.",
        "the legacy PS_WEBHOOK_SECRET_KEY" if exc.key_id is None else f"key id {exc.key_id!r}",
    )


def previous_secret_valid(sub: WebhookSubscription, now: datetime) -> bool:
    """True while a rotation's grace period keeps the old secret signing."""
    return (
        sub.previous_secret_ciphertext is not None
        and sub.previous_secret_expires_at is not None
        and sub.previous_secret_expires_at > now
    )


def signing_secrets(sub: WebhookSubscription, now: datetime) -> list[str]:
    """Secrets to sign a delivery with: the current one, then the previous
    one while its grace period lasts. Raises ValueError if the current one
    is unreadable; an unreadable previous one is skipped."""
    out = [decrypt_secret(sub.secret_ciphertext, sub.id)]
    if previous_secret_valid(sub, now):
        try:
            out.append(decrypt_secret(sub.previous_secret_ciphertext, sub.id))
        except SecretKeyMissing as exc:
            _log_missing_key(exc)
        except ValueError:
            logger.warning("webhook subscription %s: previous secret unreadable", sub.id)
    return out


async def refresh_stored_secrets(
    db: AsyncSession, sub: WebhookSubscription, now: datetime
) -> bool:
    """Re-encrypt `sub`'s secrets that are not under the active key, and
    clear a previous secret whose grace period is over. Does not commit.

    Compare-and-set on the ciphertexts that were read, so a rotation that
    lands in between is never overwritten; `updated_at` is left alone.
    Returns True if the row was updated. Raises ValueError if the current
    secret is unreadable (an unreadable previous secret is left as is).
    """
    values: dict = {}
    if needs_rekey(sub.secret_ciphertext):
        plain = decrypt_secret(sub.secret_ciphertext, sub.id)
        values["secret_ciphertext"] = encrypt_secret(plain, sub.id)
    previous = sub.previous_secret_ciphertext
    if previous is not None:
        if not previous_secret_valid(sub, now):
            values["previous_secret_ciphertext"] = None
            values["previous_secret_expires_at"] = None
        elif needs_rekey(previous):
            try:
                plain = decrypt_secret(previous, sub.id)
            except ValueError:
                pass
            else:
                values["previous_secret_ciphertext"] = encrypt_secret(plain, sub.id)
    if not values:
        return False
    result = await db.execute(
        update(WebhookSubscription)
        .where(
            WebhookSubscription.id == sub.id,
            WebhookSubscription.secret_ciphertext == sub.secret_ciphertext,
            WebhookSubscription.previous_secret_ciphertext.is_not_distinct_from(previous),
        )
        .values(**values, updated_at=WebhookSubscription.updated_at)
        .execution_options(synchronize_session=False)
    )
    return result.rowcount == 1


def worker_session_factory(session_factory, reason: str = "webhook-worker"):
    """Sessions that bypass row-level security, for the cross-tenant claim,
    purge and rekey listing. The bypass is logged once, when this is called."""
    info = rls.bypass_info(reason)
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
    active_key_id()  # fail fast when no key is set, without burning attempts
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


async def clear_expired_previous_secrets(
    session_factory, *, now: datetime | None = None, list_factory=None
) -> int:
    """Drop rotated-out secrets whose grace period is over.

    Deliveries already do this for the subscriptions they use; this covers
    idle ones, so an old secret is not kept at rest much past its grace
    period. Listing the tenants is a logged RLS bypass (``list_factory``);
    each tenant's rows are cleared under that tenant's context.
    `updated_at` is left alone. Returns the number of rows cleared.
    """
    now = now or _utcnow()
    list_factory = list_factory or worker_session_factory(
        session_factory, "webhook-secret-sweep"
    )
    expired = (
        WebhookSubscription.previous_secret_ciphertext.is_not(None),
        or_(
            WebhookSubscription.previous_secret_expires_at.is_(None),
            WebhookSubscription.previous_secret_expires_at <= now,
        ),
    )
    async with list_factory() as db:
        tenant_ids = (
            await db.execute(select(WebhookSubscription.tenant_id).where(*expired).distinct())
        ).scalars().all()

    cleared = 0
    for tenant_id in tenant_ids:
        async with session_factory() as db:
            await rls.set_tenant(db, tenant_id)
            result = await db.execute(
                update(WebhookSubscription)
                .where(WebhookSubscription.tenant_id == tenant_id, *expired)
                .values(
                    previous_secret_ciphertext=None,
                    previous_secret_expires_at=None,
                    updated_at=WebhookSubscription.updated_at,
                )
                .execution_options(synchronize_session=False)
            )
            await db.commit()
            cleared += result.rowcount or 0
    return cleared


async def run_worker(session_factory, interval: float) -> None:
    """Deliver forever, every `interval` seconds. Once an hour, purge old
    deliveries and clear expired rotated-out secrets."""
    last_purge = 0.0
    claim_factory = worker_session_factory(session_factory)
    sweep_factory = worker_session_factory(session_factory, "webhook-secret-sweep")
    while True:
        try:
            await deliver_due(session_factory, claim_factory=claim_factory)
            if time.monotonic() - last_purge > 3600:
                async with claim_factory() as db:
                    await purge_old_deliveries(db)
                await clear_expired_previous_secrets(
                    session_factory, list_factory=sweep_factory
                )
                last_purge = time.monotonic()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("webhook delivery run failed")
        await asyncio.sleep(interval)


@dataclass
class RekeyRun:
    checked: int = 0
    rekeyed: int = 0  # re-encrypted, or an expired previous secret dropped
    unchanged: int = 0
    unreadable: int = 0  # current secret cannot be decrypted: rotate it


async def rekey_all(
    session_factory, *, now: datetime | None = None, list_factory=None
) -> RekeyRun:
    """Re-encrypt every subscription secret not under the active key.

    Run after adding a key at the front of PS_WEBHOOK_SECRET_KEYS; once it
    reports no unreadable rows (and `unchanged == checked` on a second run)
    the old key can be removed. Listing the tenants with subscriptions is a
    logged RLS bypass (``list_factory``); each tenant's rows are updated
    under that tenant's context, one transaction per tenant.
    """
    active_key_id()  # WebhooksNotConfigured before touching anything
    now = now or _utcnow()
    list_factory = list_factory or worker_session_factory(session_factory, "webhook-rekey")
    async with list_factory() as db:
        tenant_ids = (
            await db.execute(select(WebhookSubscription.tenant_id).distinct())
        ).scalars().all()

    run = RekeyRun()
    for tenant_id in tenant_ids:
        async with session_factory() as db:
            await rls.set_tenant(db, tenant_id)
            subs = (
                await db.execute(
                    select(WebhookSubscription)
                    .where(WebhookSubscription.tenant_id == tenant_id)
                    .order_by(WebhookSubscription.id)
                )
            ).scalars().all()
            for sub in subs:
                run.checked += 1
                try:
                    decrypt_secret(sub.secret_ciphertext, sub.id)
                    changed = await refresh_stored_secrets(db, sub, now)
                except ValueError:
                    run.unreadable += 1
                    logger.warning("webhook subscription %s: secret unreadable", sub.id)
                    continue
                if changed:
                    run.rekeyed += 1
                else:
                    run.unchanged += 1
            await db.commit()
    return run


# ──────────────────────────────────────────────────────────────────────
# CLI: python -m collector.webhooks deliver|purge|rekey
# ──────────────────────────────────────────────────────────────────────


async def _run_cli(command: str, loop: bool, interval: float) -> None:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(settings.database_url)
    try:
        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        if command == "purge":
            async with worker_session_factory(factory)() as db:
                print(f"webhook deliveries purged: {await purge_old_deliveries(db)}")
            cleared = await clear_expired_previous_secrets(factory)
            print(f"expired rotated-out secrets cleared: {cleared}")
        elif command == "rekey":
            print(f"webhook secret rekey: {asdict(await rekey_all(factory))}")
        elif loop:
            await run_worker(factory, interval)
        else:
            print(f"webhook delivery run: {asdict(await deliver_due(factory))}")
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="python -m collector.webhooks",
        description=(
            "Deliver queued partner webhooks, purge old delivery rows and "
            "expired rotated-out secrets, or re-encrypt signing secrets with "
            "the active PS_WEBHOOK_SECRET_KEYS key."
        ),
    )
    parser.add_argument("command", choices=("deliver", "purge", "rekey"))
    parser.add_argument("--loop", action="store_true", help="keep delivering")
    parser.add_argument("--interval", type=float, default=5.0)
    args = parser.parse_args(argv)
    asyncio.run(_run_cli(args.command, args.loop, args.interval))


if __name__ == "__main__":
    main()
