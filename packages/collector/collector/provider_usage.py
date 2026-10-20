"""Platform signal: organisation usage pulled from provider admin APIs.

Discovers AI usage that no SDK or gateway reports, by reading the
organisation-level usage reports of the providers themselves:

  - OpenAI   ``GET /v1/organization/usage/{completions,embeddings}``
  - Anthropic ``GET /v1/organizations/usage_report/messages``

Each (project/workspace, API key, model) seen in a report becomes an
``AIAsset`` with ``discovery_source=["platform_signal"]`` (``medium``
confidence until another source corroborates it), and each daily bucket
becomes one ``AIUsageEvent`` with ``source="platform_signal"``.

Re-runs are idempotent: asset and event ids are UUIDv5 values derived
from the tenant, provider, project, key, model and bucket, so pulling an
overlapping window overwrites the same rows instead of adding to them.

Credentials: a tenant's admin key is stored encrypted per tenant and
provider (collector.provider_credentials, managed through the admin and
Partner APIs). ``PS_PROVIDER_ADMIN_KEYS`` (JSON, see
``ProviderUsageSettings``) is the fallback for a provider with no stored
key. Keys are held as ``SecretStr`` and are never logged or returned. HTTP
clients only call the fixed vendor hosts below.

After storing, `provider_credentials.corroborate_by_fingerprint` links
these assets to SDK sightings of the same key (registered
key id -> api_key_fingerprint links) and model.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from uuid import UUID

import httpx
from db.models import AIAsset, AIUsageEvent
from pydantic import SecretStr
from pydantic_settings import BaseSettings
from sqlalchemy.ext.asyncio import AsyncSession

from collector import audit_trail, provider_credentials
from collector.dedup import compute_confidence
from collector.lifecycle import lock_assets, record_sighting
from collector.webhooks import AssetChanges

logger = logging.getLogger("prompt_shields.provider_usage")

SOURCE = "platform_signal"

# Fixed vendor hosts. Never derived from tenant input.
OPENAI_BASE_URL = "https://api.openai.com"
ANTHROPIC_BASE_URL = "https://api.anthropic.com"
ANTHROPIC_VERSION = "2023-06-01"

OPENAI_USAGE_ENDPOINTS = ("completions", "embeddings")
SUPPORTED_PROVIDERS = ("openai", "anthropic")

MAX_PAGES = 500
MAX_RETRIES = 5
MAX_RETRY_WAIT_S = 60.0
DAILY_PAGE_LIMIT = 31  # max buckets per page for bucket_width=1d on both APIs

# Stable namespace for deterministic asset / event ids.
_NAMESPACE = uuid.UUID("6f3b1c52-2d4e-5a8b-9c7d-93a0e4f1b2c3")


class ProviderUsageSettings(BaseSettings):
    """``PS_PROVIDER_ADMIN_KEYS='{"<tenant uuid>": {"openai": "...", "anthropic": "..."}}'``"""

    provider_admin_keys: dict[str, dict[str, SecretStr]] = {}

    # A malformed PS_PROVIDER_ADMIN_KEYS (e.g. a bare key string where a
    # {provider: key} map belongs) must not echo the key in the error.
    model_config = {"env_prefix": "PS_", "extra": "ignore", "hide_input_in_errors": True}


def admin_keys_for_tenant(
    tenant_id: UUID, settings: ProviderUsageSettings | None = None
) -> dict[str, SecretStr]:
    settings = settings or ProviderUsageSettings()
    keys = settings.provider_admin_keys.get(str(tenant_id), {})
    return {
        p: k
        for p, k in keys.items()
        if p in SUPPORTED_PROVIDERS and k.get_secret_value()
    }


async def resolve_admin_keys(
    db: AsyncSession, tenant_id: UUID, settings: ProviderUsageSettings | None = None
) -> tuple[dict[str, SecretStr], dict[str, str], dict[str, str]]:
    """Admin keys for a tenant: stored credentials first, env as fallback.

    Returns (keys, source per provider: "stored" | "env", errors per
    provider). A stored key that cannot be decrypted is an error for that
    provider, not a silent fall back to env. A stored key under an older
    keyring key is re-encrypted with the active one (not committed here).
    """
    keys = dict(admin_keys_for_tenant(tenant_id, settings))
    sources = {p: "env" for p in keys}
    errors: dict[str, str] = {}
    for row in await provider_credentials.list_credentials(db, tenant_id):
        if row.provider not in SUPPORTED_PROVIDERS:
            continue
        keys.pop(row.provider, None)
        sources.pop(row.provider, None)
        try:
            plain = provider_credentials.open_credential(row)
            provider_credentials.rekey_if_needed(row, plain)
        except (ValueError, provider_credentials.CredentialsNotConfigured) as exc:
            # Messages name key ids at most, never key material.
            errors[row.provider] = f"stored credential unusable: {exc}"
            await _audit_use(db, tenant_id, row.provider, audit_trail.FAILURE)
            continue
        keys[row.provider] = SecretStr(plain)
        sources[row.provider] = "stored"
        await _audit_use(db, tenant_id, row.provider, audit_trail.SUCCESS)
    return keys, sources, errors


async def _audit_use(db: AsyncSession, tenant_id: UUID, provider: str, outcome: str) -> None:
    """provider_credential.use: a stored key was decrypted for a pull.
    The env fallback (PS_PROVIDER_ADMIN_KEYS) is operator-held and not a
    tenant credential, so it is not recorded here."""
    await audit_trail.record(
        db,
        tenant_id=tenant_id,
        action="provider_credential.use",
        outcome=outcome,
        actor=audit_trail.system_actor("provider-usage-pull"),
        target_type="provider_credential",
        target_id=provider,
        details={"provider": provider, "purpose": "usage_pull"},
    )


class ProviderUsageError(Exception):
    """A provider call failed. The message never contains credentials."""


@dataclass(frozen=True)
class UsageRecord:
    provider: str
    endpoint: str
    bucket_start: datetime
    bucket_end: datetime
    project_id: str | None
    api_key_id: str | None
    model: str | None
    tokens_in: int
    tokens_out: int
    requests: int


Sleep = Callable[[float], Awaitable[None]]


async def _get_json(
    client: httpx.AsyncClient,
    path: str,
    params: list[tuple[str, str]],
    headers: dict[str, str],
    provider: str,
    sleep: Sleep,
) -> dict:
    """GET with retry on 429/5xx, honouring Retry-After."""
    for attempt in range(MAX_RETRIES + 1):
        try:
            resp = await client.get(path, params=params, headers=headers)
        except httpx.TransportError as exc:
            if attempt == MAX_RETRIES:
                raise ProviderUsageError(
                    f"{provider}: transport error ({type(exc).__name__})"
                )
            await sleep(min(2.0**attempt, MAX_RETRY_WAIT_S))
            continue
        if resp.status_code == 429 or resp.status_code >= 500:
            if attempt == MAX_RETRIES:
                break
            wait = _retry_after(resp) or min(2.0**attempt, MAX_RETRY_WAIT_S)
            logger.info(
                "%s usage API returned %s; retrying in %.1fs",
                provider,
                resp.status_code,
                wait,
            )
            await sleep(wait)
            continue
        if resp.status_code != 200:
            raise ProviderUsageError(
                f"{provider}: usage API returned HTTP {resp.status_code}"
            )
        return resp.json()
    raise ProviderUsageError(
        f"{provider}: usage API still failing after {MAX_RETRIES} retries"
    )


def _retry_after(resp: httpx.Response) -> float | None:
    value = resp.headers.get("retry-after")
    if value is None:
        return None
    try:
        return max(0.0, min(float(value), MAX_RETRY_WAIT_S))
    except ValueError:
        return None


async def _paginate(
    client: httpx.AsyncClient,
    path: str,
    params: list[tuple[str, str]],
    headers: dict[str, str],
    provider: str,
    sleep: Sleep,
):
    page: str | None = None
    for _ in range(MAX_PAGES):
        page_params = params + ([("page", page)] if page else [])
        body = await _get_json(client, path, page_params, headers, provider, sleep)
        for bucket in body.get("data") or []:
            yield bucket
        page = body.get("next_page")
        if not body.get("has_more") or not page:
            return
    raise ProviderUsageError(
        f"{provider}: more than {MAX_PAGES} pages; narrow the window"
    )


def _ts(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


async def fetch_openai_usage(
    admin_key: SecretStr,
    start: datetime,
    end: datetime,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    sleep: Sleep = asyncio.sleep,
) -> list[UsageRecord]:
    headers = {"Authorization": f"Bearer {admin_key.get_secret_value()}"}
    records: list[UsageRecord] = []
    async with httpx.AsyncClient(
        base_url=OPENAI_BASE_URL,
        transport=transport,
        timeout=30.0,
        follow_redirects=False,
    ) as client:
        for endpoint in OPENAI_USAGE_ENDPOINTS:
            params = [
                ("start_time", str(int(_ts(start).timestamp()))),
                ("end_time", str(int(_ts(end).timestamp()))),
                ("bucket_width", "1d"),
                ("group_by", "project_id"),
                ("group_by", "api_key_id"),
                ("group_by", "model"),
                ("limit", str(DAILY_PAGE_LIMIT)),
            ]
            path = f"/v1/organization/usage/{endpoint}"
            async for bucket in _paginate(
                client, path, params, headers, "openai", sleep
            ):
                b_start = datetime.fromtimestamp(int(bucket["start_time"]), tz=UTC)
                b_end = datetime.fromtimestamp(int(bucket["end_time"]), tz=UTC)
                for r in bucket.get("results") or []:
                    records.append(
                        UsageRecord(
                            provider="openai",
                            endpoint=endpoint,
                            bucket_start=b_start,
                            bucket_end=b_end,
                            project_id=r.get("project_id"),
                            api_key_id=r.get("api_key_id"),
                            model=r.get("model"),
                            tokens_in=int(r.get("input_tokens") or 0),
                            tokens_out=int(r.get("output_tokens") or 0),
                            requests=int(r.get("num_model_requests") or 0),
                        )
                    )
    return records


def _parse_rfc3339(value: str) -> datetime:
    return _ts(datetime.fromisoformat(value))


async def fetch_anthropic_usage(
    admin_key: SecretStr,
    start: datetime,
    end: datetime,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    sleep: Sleep = asyncio.sleep,
) -> list[UsageRecord]:
    headers = {
        "x-api-key": admin_key.get_secret_value(),
        "anthropic-version": ANTHROPIC_VERSION,
    }
    params = [
        ("starting_at", _ts(start).strftime("%Y-%m-%dT%H:%M:%SZ")),
        ("ending_at", _ts(end).strftime("%Y-%m-%dT%H:%M:%SZ")),
        ("bucket_width", "1d"),
        ("group_by[]", "workspace_id"),
        ("group_by[]", "api_key_id"),
        ("group_by[]", "model"),
        ("limit", str(DAILY_PAGE_LIMIT)),
    ]
    records: list[UsageRecord] = []
    async with httpx.AsyncClient(
        base_url=ANTHROPIC_BASE_URL,
        transport=transport,
        timeout=30.0,
        follow_redirects=False,
    ) as client:
        path = "/v1/organizations/usage_report/messages"
        async for bucket in _paginate(
            client, path, params, headers, "anthropic", sleep
        ):
            b_start = _parse_rfc3339(bucket["starting_at"])
            b_end = _parse_rfc3339(bucket["ending_at"])
            for r in bucket.get("results") or []:
                cache_creation = r.get("cache_creation") or {}
                tokens_in = (
                    int(r.get("uncached_input_tokens") or 0)
                    + int(r.get("cache_read_input_tokens") or 0)
                    + sum(int(v or 0) for v in cache_creation.values())
                )
                records.append(
                    UsageRecord(
                        provider="anthropic",
                        endpoint="messages",
                        bucket_start=b_start,
                        bucket_end=b_end,
                        project_id=r.get("workspace_id"),
                        api_key_id=r.get("api_key_id"),
                        model=r.get("model"),
                        tokens_in=tokens_in,
                        tokens_out=int(r.get("output_tokens") or 0),
                        requests=0,
                    )
                )
    return records


FETCHERS = {"openai": fetch_openai_usage, "anthropic": fetch_anthropic_usage}


def _key(*parts: object) -> str:
    return "|".join("" if p is None else str(p) for p in parts)


def asset_id_for(tenant_id: UUID, r: UsageRecord) -> UUID:
    return uuid.uuid5(
        _NAMESPACE,
        _key("asset", tenant_id, r.provider, r.project_id, r.api_key_id, r.model),
    )


def event_id_for(tenant_id: UUID, r: UsageRecord) -> UUID:
    return uuid.uuid5(
        _NAMESPACE,
        _key(
            "event",
            tenant_id,
            r.provider,
            r.endpoint,
            r.project_id,
            r.api_key_id,
            r.model,
            r.bucket_start.isoformat(),
        ),
    )


def calling_service_for(r: UsageRecord) -> str:
    scope = "workspace" if r.provider == "anthropic" else "project"
    return (
        f"{r.provider}:{scope}={r.project_id or 'default'}:key={r.api_key_id or 'none'}"
    )


def aggregate(records: list[UsageRecord]) -> list[UsageRecord]:
    """Sum records sharing an event identity, so one bucket maps to one event."""
    merged: dict[tuple, UsageRecord] = {}
    for r in records:
        k = (
            r.provider,
            r.endpoint,
            r.project_id,
            r.api_key_id,
            r.model,
            r.bucket_start,
        )
        prev = merged.get(k)
        if prev is None:
            merged[k] = r
        else:
            merged[k] = replace(
                prev,
                tokens_in=prev.tokens_in + r.tokens_in,
                tokens_out=prev.tokens_out + r.tokens_out,
                requests=prev.requests + r.requests,
            )
    return list(merged.values())


async def store_usage(
    db: AsyncSession, tenant_id: UUID, records: list[UsageRecord]
) -> dict:
    """Upsert assets and usage events for ``records``. Does not commit.

    Each bucket is a sighting of its asset at the bucket's end
    (``lifecycle.record_sighting``): it reactivates a stale or retired
    asset, or raises "retired asset still in use" on an owner-retired one,
    when the usage happened after the asset's last lifecycle change, and
    queues the same ``asset.changed`` webhook ingest sends. New assets send
    no ``asset.created`` (the pull is a bulk backfill).
    """
    assets_created = events_created = events_updated = 0
    now = datetime.now(UTC)
    changes = AssetChanges()
    work = [
        (asset_id_for(tenant_id, r), event_id_for(tenant_id, r), r)
        for r in aggregate(records)
        if r.tokens_in or r.tokens_out or r.requests
    ]
    # Lock the existing asset rows up front in id order, the order ingest
    # and the cloud inventory take them in, and write events in a fixed
    # order too, so overlapping pulls never deadlock.
    work.sort(key=lambda w: (w[0], w[1]))
    await lock_assets(db, {aid for aid, _, _ in work})
    for aid, eid, r in work:
        asset = await db.get(AIAsset, aid)
        if asset is None:
            asset = AIAsset(
                id=aid,
                tenant_id=tenant_id,
                vendor=r.provider,
                model=r.model,
                calling_service=calling_service_for(r),
                status="active",
                discovery_source=[SOURCE],
                confidence=compute_confidence([SOURCE]),
                first_seen=r.bucket_start,
                last_seen=r.bucket_end,
            )
            db.add(asset)
            await db.flush()
            assets_created += 1
        else:
            if SOURCE not in (asset.discovery_source or []):
                asset.discovery_source = list(asset.discovery_source or []) + [SOURCE]
                asset.confidence = compute_confidence(asset.discovery_source)
            if asset.first_seen is None or r.bucket_start < asset.first_seen:
                asset.first_seen = r.bucket_start
            if asset.last_seen is None or r.bucket_end > asset.last_seen:
                asset.last_seen = r.bucket_end
            # The current day's bucket ends in the future.
            record_sighting(asset, min(r.bucket_end, now), now)
            # Before anything flushes the session (changed_fields).
            changes.record(asset, is_new=False)

        event = await db.get(AIUsageEvent, eid)
        if event is None:
            db.add(
                AIUsageEvent(
                    id=eid,
                    tenant_id=tenant_id,
                    asset_id=aid,
                    timestamp=r.bucket_start,
                    tokens_in=r.tokens_in,
                    tokens_out=r.tokens_out,
                    source=SOURCE,
                    session_id=f"{r.provider}:{r.endpoint}:usage",
                )
            )
            events_created += 1
        else:
            # Replace, never add: a partial bucket from an earlier run is
            # superseded by the provider's latest total for that bucket.
            event.tokens_in = r.tokens_in
            event.tokens_out = r.tokens_out
            events_updated += 1
    await changes.enqueue(db, tenant_id)
    return {
        "assets_created": assets_created,
        "events_created": events_created,
        "events_updated": events_updated,
    }


async def pull_provider_usage(
    db: AsyncSession,
    tenant_id: UUID,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    providers: list[str] | None = None,
    settings: ProviderUsageSettings | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    sleep: Sleep = asyncio.sleep,
) -> dict:
    """Pull usage for one tenant from every configured provider and commit it.

    Defaults to the last 7 days. A failing provider is reported in the
    result and does not stop the others.
    """
    end = _ts(end or datetime.now(UTC))
    start = _ts(start or end - timedelta(days=7))
    if start >= end:
        raise ValueError("start must be before end")

    keys, sources, key_errors = await resolve_admin_keys(db, tenant_id, settings)
    # Commit the provider_credential.use rows (and any re-encryption) now:
    # writing them holds the tenant's audit chain lock, which must not stay
    # held across the provider API calls below.
    await db.commit()
    wanted = [p for p in (providers or SUPPORTED_PROVIDERS) if p in SUPPORTED_PROVIDERS]
    result: dict[str, dict] = {}
    for provider in wanted:
        if provider in key_errors:
            result[provider] = {"status": "error", "error": key_errors[provider]}
            continue
        key = keys.get(provider)
        if key is None:
            result[provider] = {"status": "not_configured"}
            continue
        try:
            records = await FETCHERS[provider](
                key, start, end, transport=transport, sleep=sleep
            )
        except ProviderUsageError as exc:
            logger.warning(
                "provider usage pull failed for tenant %s: %s", tenant_id, exc
            )
            result[provider] = {"status": "error", "error": str(exc)}
            continue
        counts = await store_usage(db, tenant_id, records)
        await db.commit()
        logger.info(
            "provider usage pull: tenant=%s provider=%s %s", tenant_id, provider, counts
        )
        result[provider] = {
            "status": "ok",
            "credential_source": sources[provider],
            "records": len(records),
            **counts,
        }
    corroborated = await provider_credentials.corroborate_by_fingerprint(db, tenant_id)
    # Also persists any credential re-encrypted under the active key.
    await db.commit()
    return {
        "tenant_id": str(tenant_id),
        "start": start.isoformat(),
        "end": end.isoformat(),
        "providers": result,
        "corroborated_assets": corroborated,
    }
