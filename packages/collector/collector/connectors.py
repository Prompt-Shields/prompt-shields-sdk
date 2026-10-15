"""Shared plumbing for outbound EA-tool connectors (LeanIX, ServiceNow, ...).

A connector reads the tenant's registry through the Partner API, maps it
to the target tool and pushes it there. This module holds what every
connector needs and nothing tool specific:

  - `GuardedClient`: an HTTP client pinned to one ``https`` host. Every
    request is checked against that host (scheme, host and port), the host
    must resolve only to public addresses (SSRF guard shared with the
    webhook sender, re-checked on each request) and the connection goes
    to the checked address (TLS still verified for the host name), so DNS
    rebinding cannot redirect it; redirects are never followed, timeouts are fixed and 429,
    5xx and transport errors are retried a bounded number of times,
    honouring ``Retry-After``. Errors carry the status code only, never a
    response body or a credential.
  - `BearerToken`: an OAuth access token for a `GuardedClient`, fetched on
    first use, refreshed shortly before ``expires_in`` runs out and once
    more if the server answers 401 anyway, so long syncs outlive a token.
  - `PartnerApiClient`: reads ``/partner/v1/export``, ``/changes`` and
    ``/assets/{id}/data-flows`` with a scoped partner API key, so a
    connector sees exactly what its credential's scopes allow
    (``export:read``, ``changes:read``, ``data-flows:read``) and every read
    lands in the partner audit log. Every response must name the tenant
    being synced (``meta.tenant_id``), so a key paired with the wrong
    tenant fails closed. `read_registry` / `next_cursor` turn it into one
    run's assets, flows and next cursor.
  - Sync state (cursor, last run) per tenant and connector, kept in
    ``tenants.settings["connectors"][<name>]``. Never holds secrets.

Credentials are not stored here: each connector reads its own from the
operator environment (``SecretStr``, never logged or returned).
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Self
from urllib.parse import quote
from uuid import UUID

import httpx
from db.models import Tenant
from pydantic import BaseModel, SecretStr, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from collector import egress

logger = logging.getLogger("prompt_shields.connectors")

Sleep = Callable[[float], Awaitable[None]]
# (host, port) -> resolved IP address strings.
Resolver = Callable[[str, int], Awaitable[list[str]]]

DEFAULT_TIMEOUT_S = 30.0
DEFAULT_MAX_RETRIES = 4
MAX_RETRY_WAIT_S = 60.0
MAX_PAGES = 1000

_HOSTNAME = re.compile(
    r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$"
)


class ConnectorError(Exception):
    """A connector call failed. The message never contains credentials or
    response bodies."""


# ──────────────────────────────────────────────────────────────────────
# Host validation and SSRF guard
# ──────────────────────────────────────────────────────────────────────


def validate_hostname(host: str, allowed_suffixes: tuple[str, ...] = ()) -> str:
    """A DNS name (no IP literal, port, path or userinfo), lower-cased.

    With ``allowed_suffixes`` the host must equal one of them (without the
    leading dot) or end with one.
    """
    h = (host or "").strip().lower().rstrip(".")
    if not _HOSTNAME.match(h):
        raise ValueError("must be a DNS host name, e.g. acme.leanix.net")
    if allowed_suffixes and not any(
        h == s.lstrip(".").lower() or h.endswith("." + s.lstrip(".").lower())
        for s in allowed_suffixes
    ):
        raise ValueError("host is not in the allowed host suffixes")
    return h


def is_public_ip(value: str) -> bool:
    """Same rules as the webhook sender (`collector.egress.ip_is_public`):
    refuses private, loopback, link-local / metadata, CGNAT, reserved,
    multicast, and IPv6 forms embedding them (mapped, 6to4, Teredo, NAT64,
    IPv4-compatible) plus site-local."""
    try:
        ip = ipaddress.ip_address(value.split("%", 1)[0])
    except ValueError:
        return False
    return egress.ip_is_public(ip)


default_resolver: Resolver = egress.resolve_host


async def assert_public_host(
    host: str, port: int, resolver: Resolver, *, allow_private: bool = False
) -> str:
    """Resolve ``host`` and return the address to connect to; every
    resolved address must pass the SSRF check."""
    try:
        addresses = await resolver(host, port)
    except OSError as exc:
        raise ConnectorError(f"{host}: DNS lookup failed ({type(exc).__name__})")
    if not addresses:
        raise ConnectorError(f"{host}: DNS lookup returned no address")
    try:
        return egress.check_addresses(addresses, allow_private=allow_private)
    except egress.AddressNotAllowed:
        raise ConnectorError(f"{host}: resolves to a non-public address; refused")


def _retry_after(resp: httpx.Response) -> float | None:
    value = resp.headers.get("retry-after")
    if value is None:
        return None
    try:
        return max(0.0, min(float(value), MAX_RETRY_WAIT_S))
    except ValueError:
        return None


class GuardedClient:
    """HTTP client bound to one ``https://<host>[:port]`` origin."""

    def __init__(
        self,
        base_url: str,
        *,
        name: str,
        transport: httpx.AsyncBaseTransport | None = None,
        resolver: Resolver = default_resolver,
        sleep: Sleep = asyncio.sleep,
        timeout: float = DEFAULT_TIMEOUT_S,
        max_retries: int = DEFAULT_MAX_RETRIES,
        allow_private_network: bool = False,
    ):
        url = httpx.URL(base_url)
        if url.scheme != "https" or not url.host or url.userinfo:
            raise ConnectorError(f"{name}: base URL must be https://<host>")
        self.name = name
        self._origin = (url.scheme, url.host, url.port)
        self._base = url.copy_with(query=None, fragment=None)
        self._resolver = resolver
        self._sleep = sleep
        self._max_retries = max_retries
        self._allow_private = allow_private_network
        self._client = httpx.AsyncClient(
            transport=transport,
            timeout=httpx.Timeout(timeout, connect=min(timeout, 10.0)),
            follow_redirects=False,
            # No proxy or .netrc from the environment: the connection must
            # go to the checked address.
            trust_env=False,
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc) -> None:
        await self._client.aclose()

    def url(self, path: str) -> httpx.URL:
        base_path = self._base.path.rstrip("/")
        url = self._base.copy_with(path=base_path + "/" + path.lstrip("/"))
        return url

    async def request(
        self,
        method: str,
        path: str,
        *,
        what: str,
        expected: tuple[int, ...] = (200,),
        **kwargs: Any,
    ) -> httpx.Response:
        url = self.url(path)
        if (url.scheme, url.host, url.port) != self._origin:
            raise ConnectorError(f"{self.name}: {what}: refusing off-host request")
        # Resolve once, check every address, then connect to the checked
        # address (Host header and TLS SNI / certificate check keep the
        # host name), so httpx never resolves the name again: no DNS
        # rebinding between the check and the connection.
        address = await assert_public_host(
            url.host,
            url.port or 443,
            self._resolver,
            allow_private=self._allow_private,
        )
        target, pin_headers, extensions = egress.pin_request(url, address)
        headers = {**(kwargs.pop("headers", None) or {}), **pin_headers}
        for attempt in range(self._max_retries + 1):
            try:
                resp = await self._client.request(
                    method, target, headers=headers, extensions=extensions, **kwargs
                )
            except httpx.TransportError as exc:
                if attempt == self._max_retries:
                    raise ConnectorError(
                        f"{self.name}: {what}: transport error ({type(exc).__name__})"
                    )
                await self._sleep(min(2.0**attempt, MAX_RETRY_WAIT_S))
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt == self._max_retries:
                    break
                wait = _retry_after(resp) or min(2.0**attempt, MAX_RETRY_WAIT_S)
                logger.info(
                    "%s: %s returned %s; retrying in %.1fs",
                    self.name,
                    what,
                    resp.status_code,
                    wait,
                )
                await self._sleep(wait)
                continue
            if resp.status_code not in expected:
                # The body is not echoed: auth errors can quote the request.
                raise ConnectorError(
                    f"{self.name}: {what} returned HTTP {resp.status_code}"
                )
            return resp
        raise ConnectorError(
            f"{self.name}: {what} still failing after {self._max_retries} retries"
        )

    async def json(self, method: str, path: str, *, what: str, **kwargs: Any) -> Any:
        resp = await self.request(method, path, what=what, **kwargs)
        return self.decode(resp, what)

    def decode(self, resp: httpx.Response, what: str) -> Any:
        try:
            return resp.json()
        except ValueError:
            raise ConnectorError(f"{self.name}: {what} returned invalid JSON")


# ──────────────────────────────────────────────────────────────────────
# OAuth access token
# ──────────────────────────────────────────────────────────────────────

# Refresh this long before the token's ``expires_in`` runs out (at most a
# tenth of its lifetime for short-lived tokens).
TOKEN_REFRESH_MARGIN_S = 60.0

Clock = Callable[[], float]


class BearerToken:
    """An OAuth access token for one `GuardedClient`.

    ``fetch`` performs the token request and returns its JSON body. The
    token is fetched on first use and again once ``expires_in`` (less a
    margin) has passed, so a sync that outlives the token keeps working.
    If a call still answers 401, the token is dropped, fetched again and
    the call repeated once; a second 401 fails the call. There is no other
    retry. The token is never logged or put in an error message.
    """

    def __init__(
        self,
        http: GuardedClient,
        fetch: Callable[[], Awaitable[Any]],
        *,
        clock: Clock = time.monotonic,
    ):
        self._http = http
        self._fetch = fetch
        self._clock = clock
        self._token: str | None = None
        self._refresh_at: float | None = None

    def invalidate(self) -> None:
        self._token = None
        self._refresh_at = None

    async def _get(self) -> str:
        if self._token is not None and (
            self._refresh_at is None or self._clock() < self._refresh_at
        ):
            return self._token
        self.invalidate()
        body = await self._fetch()
        token = body.get("access_token") if isinstance(body, dict) else None
        if not isinstance(token, str) or not token:
            raise ConnectorError(
                f"{self._http.name}: token response had no access_token"
            )
        lifetime = _expires_in(body.get("expires_in"))
        self._token = token
        if lifetime is not None:
            margin = min(TOKEN_REFRESH_MARGIN_S, lifetime / 10)
            self._refresh_at = self._clock() + lifetime - margin
        return token

    async def request(
        self,
        method: str,
        path: str,
        *,
        what: str,
        expected: tuple[int, ...] = (200,),
        headers: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> httpx.Response:
        """`GuardedClient.request` with ``Authorization: Bearer``."""
        for attempt in range(2):
            auth = {**(headers or {}), "Authorization": f"Bearer {await self._get()}"}
            resp = await self._http.request(
                method,
                path,
                what=what,
                expected=(*expected, 401),
                headers=auth,
                **kwargs,
            )
            if resp.status_code != 401 or 401 in expected:
                return resp
            if attempt == 0:
                logger.info(
                    "%s: %s returned 401; refreshing token", self._http.name, what
                )
                self.invalidate()
        raise ConnectorError(f"{self._http.name}: {what} returned HTTP 401")

    async def json(self, method: str, path: str, *, what: str, **kwargs: Any) -> Any:
        resp = await self.request(method, path, what=what, **kwargs)
        return self._http.decode(resp, what)


def _expires_in(value: Any) -> float | None:
    """Seconds from a token response's ``expires_in`` (number or numeric
    string); None when absent or unusable."""
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        try:
            value = float(value)
        except ValueError:
            return None
    if isinstance(value, int | float) and 0 < value < 10**9:
        return float(value)
    return None


# ──────────────────────────────────────────────────────────────────────
# Partner API source
# ──────────────────────────────────────────────────────────────────────


class PartnerApiConnection(BaseModel):
    """Where a connector reads the registry from: a Partner API base URL and
    a partner API key holding ``export:read``, ``changes:read`` and, for
    data flows, ``data-flows:read``."""

    base_url: str
    api_key: SecretStr
    # For a collector reached over an internal network (same cluster).
    # Still https only.
    allow_private_network: bool = False

    @field_validator("base_url")
    @classmethod
    def _https(cls, v: str) -> str:
        url = httpx.URL(v)
        if url.scheme != "https" or not url.host or url.userinfo or url.query:
            raise ValueError("base_url must be an https URL without credentials")
        return str(url).rstrip("/")


class PartnerApiClient:
    """Reads the registry through ``/partner/v1``.

    ``tenant_id`` is the tenant the connector syncs. Every response must
    name that tenant in ``meta.tenant_id`` (the tenant of the credential
    that authenticated the read); otherwise the read fails, so a partner
    key configured under the wrong tenant can never move one tenant's
    registry into another tenant's target or sync state. A response
    without ``meta.tenant_id`` fails too (fail closed).
    """

    def __init__(
        self,
        conn: PartnerApiConnection,
        *,
        tenant_id: UUID,
        transport: httpx.AsyncBaseTransport | None = None,
        resolver: Resolver = default_resolver,
        sleep: Sleep = asyncio.sleep,
    ):
        self._tenant_id = str(UUID(str(tenant_id)))
        self._http = GuardedClient(
            conn.base_url,
            name="partner-api",
            transport=transport,
            resolver=resolver,
            sleep=sleep,
            allow_private_network=conn.allow_private_network,
        )
        self._headers = {
            "Authorization": f"Bearer {conn.api_key.get_secret_value()}",
            "Accept": "application/json",
        }

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc) -> None:
        await self._http.__aexit__(*exc)

    async def _get(self, path: str, what: str, params: dict | None = None) -> dict:
        body = await self._http.json(
            "GET", path, what=what, params=params, headers=self._headers
        )
        if not isinstance(body, dict):
            raise ConnectorError(f"partner-api: {what} returned an unexpected body")
        meta = body.get("meta")
        tenant = meta.get("tenant_id") if isinstance(meta, dict) else None
        if not isinstance(tenant, str) or tenant.lower() != self._tenant_id:
            # Neither tenant id is echoed: the message names the cause only.
            raise ConnectorError(
                f"partner-api: {what}: the partner API key does not belong to "
                "the configured tenant; refused"
            )
        return body

    async def export_assets(self) -> tuple[list[dict], str | None]:
        """Every asset (``/export``) and the server's sync token."""
        body = await self._get("/partner/v1/export", "export", {"format": "json"})
        meta = body.get("meta") or {}
        return _dicts(body.get("data")), meta.get("sync_token")

    async def changes(self, since: str, page_size: int = 200) -> list[dict]:
        """Every ``/changes`` entry since ``since``, following the cursor."""
        out: list[dict] = []
        after: str | None = None
        for _ in range(MAX_PAGES):
            params = {"since": since, "limit": page_size}
            if after:
                params["after"] = after
            body = await self._get("/partner/v1/changes", "changes", params)
            out.extend(_dicts(body.get("data")))
            meta = body.get("meta") or {}
            after = meta.get("next_cursor")
            if not meta.get("has_more") or not after:
                return out
            UUID(str(after))  # a cursor is an asset id; refuse anything else
        raise ConnectorError(f"partner-api: changes: more than {MAX_PAGES} pages")

    async def data_flows(self, asset_id: str) -> list[dict]:
        # str(): a non-string id from the API must raise ValueError (a failed
        # run), not the AttributeError UUID() raises for it (HTTP 500).
        path = f"/partner/v1/assets/{quote(str(UUID(str(asset_id))))}/data-flows"
        return _dicts((await self._get(path, "data flows")).get("data"))


def _dicts(value: Any) -> list[dict]:
    return [v for v in value or [] if isinstance(v, dict)]


# Re-read this much before the newest change seen, so a change committed
# while a sync was paging is not skipped. Upserts make the overlap harmless.
CURSOR_OVERLAP = timedelta(minutes=5)


def _parse_ts(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def next_cursor(
    since: str | None, changes: list[dict], server_token: str | None
) -> str | None:
    """Cursor for the next incremental run.

    After a full export: the server's sync token less the overlap. After
    ``/changes``: the newest ``changed_at`` less the overlap, never going
    back before ``since``.
    """
    previous = _parse_ts(since)
    if previous is None:
        token = _parse_ts(server_token)
        return (token - CURSOR_OVERLAP).isoformat() if token else None
    newest = max(
        (t for t in (_parse_ts(c.get("changed_at")) for c in changes) if t),
        default=None,
    )
    if newest is None:
        return previous.isoformat()
    return max(previous, newest - CURSOR_OVERLAP).isoformat()


@dataclass
class RegistryRead:
    """What one sync run read: the assets to push, their flows (when
    requested) and the cursor to store once the push succeeded."""

    assets: list[dict]
    flows: dict[str, list[dict]] = field(default_factory=dict)
    cursor: str | None = None


async def read_registry(
    partner: PartnerApiClient, since: str | None, *, include_data_flows: bool
) -> RegistryRead:
    """``/export`` when there is no cursor, else ``/changes`` since it, plus
    each asset's data flows when ``include_data_flows``."""
    if since:
        changes = await partner.changes(since)
        assets = [c["asset"] for c in changes if isinstance(c.get("asset"), dict)]
        cursor = next_cursor(since, changes, None)
    else:
        assets, token = await partner.export_assets()
        cursor = next_cursor(None, [], token)
    flows: dict[str, list[dict]] = {}
    if include_data_flows:
        for a in assets:
            if a.get("id"):
                flows[a["id"]] = await partner.data_flows(a["id"])
    return RegistryRead(assets=assets, flows=flows, cursor=cursor)


# ──────────────────────────────────────────────────────────────────────
# Sync state
# ──────────────────────────────────────────────────────────────────────

SETTINGS_KEY = "connectors"


async def load_sync_state(
    db: AsyncSession, tenant_id: UUID, connector: str
) -> dict[str, Any] | None:
    """The connector's state for a tenant; None when the tenant is absent."""
    tenant = await db.get(Tenant, tenant_id)
    if tenant is None:
        return None
    return dict(((tenant.settings or {}).get(SETTINGS_KEY) or {}).get(connector) or {})


async def save_sync_state(
    db: AsyncSession, tenant_id: UUID, connector: str, state: dict[str, Any]
) -> None:
    """Replace the connector's state and commit. ``state`` must be JSON
    serialisable and must never contain credentials."""
    tenant = await db.get(Tenant, tenant_id)
    if tenant is None:
        return
    new_settings = dict(tenant.settings or {})
    connectors = dict(new_settings.get(SETTINGS_KEY) or {})
    connectors[connector] = state
    new_settings[SETTINGS_KEY] = connectors
    tenant.settings = new_settings  # reassign so the JSONB change is flushed
    await db.commit()
