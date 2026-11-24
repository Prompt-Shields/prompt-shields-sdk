"""Payload logging policy (#352): the tenant's setting, payload retention
and legal hold, read from ``tenants.settings``.

Kept apart from collector.payload_logging (routes, storage) so retention
and erasure can use it without importing the API modules. See that module
for what the settings mean.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from db.models import Tenant
from sqlalchemy import select

from collector.config import settings

SETTINGS_KEY = "payload_logging"
LEGAL_HOLD_KEY = "legal_hold"
MODES = ("off", "metadata", "redacted", "full")
MAX_ROUTES = 50
MIN_RETENTION_DAYS = 1
MAX_RETENTION_DAYS = 36500
ROUTE_RE = re.compile(r"[\x21-\x7e]{1,256}")


@dataclass(frozen=True)
class PayloadLoggingConfig:
    mode: str = "off"
    sample_rate: float = 1.0
    on_error_only: bool = False
    routes: tuple[str, ...] = ()
    retention_days: int | None = None

    def as_dict(self) -> dict:
        return {
            "mode": self.mode,
            "sample_rate": self.sample_rate,
            "on_error_only": self.on_error_only,
            "routes": list(self.routes),
            "retention_days": self.retention_days or default_retention_days(),
        }


OFF = PayloadLoggingConfig()


def _valid_days(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if MIN_RETENTION_DAYS <= value <= MAX_RETENTION_DAYS else None


def default_retention_days() -> int:
    days = _valid_days(settings.payload_retention_days_default)
    if days is None:
        raise ValueError(
            "PS_PAYLOAD_RETENTION_DAYS_DEFAULT must be between "
            f"{MIN_RETENTION_DAYS} and {MAX_RETENTION_DAYS}"
        )
    return days


def config_from_settings(tenant_settings: Any) -> PayloadLoggingConfig:
    """The tenant's payload logging setting. Anything absent or invalid is
    ``off``: a bad setting never turns logging on or widens it."""
    if not isinstance(tenant_settings, dict):
        return OFF
    raw = tenant_settings.get(SETTINGS_KEY)
    if not isinstance(raw, dict) or raw.get("mode") not in MODES:
        return OFF
    rate = raw.get("sample_rate", 1.0)
    if isinstance(rate, bool) or not isinstance(rate, (int, float)) or not 0 <= rate <= 1:
        return OFF
    on_error_only = raw.get("on_error_only", False)
    routes = raw.get("routes", [])
    if not isinstance(on_error_only, bool) or not isinstance(routes, list):
        return OFF
    if len(routes) > MAX_ROUTES or not all(
        isinstance(r, str) and ROUTE_RE.fullmatch(r) for r in routes
    ):
        return OFF
    return PayloadLoggingConfig(
        mode=raw["mode"],
        sample_rate=float(rate),
        on_error_only=on_error_only,
        routes=tuple(routes),
        retention_days=_valid_days(raw.get("retention_days")),
    )


def retention_days_for(tenant_settings: Any) -> int:
    return config_from_settings(tenant_settings).retention_days or default_retention_days()


def payload_cutoff(tenant_settings: Any, now: datetime | None = None) -> datetime:
    """Oldest ``received_at`` still kept for this tenant's payloads."""
    now = now or datetime.now(UTC)
    return now - timedelta(days=retention_days_for(tenant_settings))


def legal_hold_since(tenant_settings: Any) -> str | None:
    """When the tenant's legal hold started (ISO 8601), or None."""
    if not isinstance(tenant_settings, dict):
        return None
    hold = tenant_settings.get(LEGAL_HOLD_KEY)
    if isinstance(hold, dict) and isinstance(hold.get("since"), str):
        return hold["since"]
    return None


def on_legal_hold(tenant_settings: Any) -> bool:
    return legal_hold_since(tenant_settings) is not None


def sampled(tenant_id: UUID, call_id: str, rate: float) -> bool:
    """Whether ``call_id`` is in the ``rate`` share of the tenant's calls.
    Deterministic, so a resent call gets the same answer."""
    if rate >= 1:
        return True
    if rate <= 0:
        return False
    digest = hashlib.sha256(f"{tenant_id}:{call_id}".encode()).digest()
    return int.from_bytes(digest[:8], "big") / 2**64 < rate


def route_logged(route: str | None, patterns: tuple[str, ...]) -> bool:
    if not patterns:
        return True
    if route is None:
        return False
    for pattern in patterns:
        if pattern.endswith("*"):
            if route.startswith(pattern[:-1]):
                return True
        elif route == pattern:
            return True
    return False


async def held_tenant_ids(db) -> set[UUID]:
    """Tenants on legal hold: every purge skips them."""
    rows = (await db.execute(select(Tenant.id, Tenant.settings))).all()
    return {tid for tid, tenant_settings in rows if on_legal_hold(tenant_settings or {})}
