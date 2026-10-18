"""Granular partner scopes and rate-limit tiers (#106), without a database.

Every Partner API route is mounted with a stub `resolve_partner`; a
principal holding every scope except the route's own must get 403 before
the route touches the database.
"""

from __future__ import annotations

import uuid

import pytest
from collector import partner_auth
from collector.partner_auth import PartnerPrincipal, require_scope
from collector.partner_registry import router
from collector.partner_scopes import (
    DEFAULT_SCOPES,
    LEGACY_REGISTRY_READ,
    RATE_LIMIT_TIERS,
    SCOPES,
    effective_rate_limit,
    expand_scopes,
    unknown_scopes,
)
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

ASSET = uuid.uuid4()

# (path, scope the route must require)
ROUTES = [
    ("/partner/v1/assets", "assets:read"),
    (f"/partner/v1/assets/{ASSET}", "assets:read"),
    ("/partner/v1/vendors", "assets:read"),
    ("/partner/v1/models", "assets:read"),
    ("/partner/v1/business-units", "assets:read"),
    (f"/partner/v1/assets/{ASSET}/data-flows", "data-flows:read"),
    (f"/partner/v1/assets/{ASSET}/risks", "risks:read"),
    (f"/partner/v1/assets/{ASSET}/usage-summary", "usage:read"),
    ("/partner/v1/telemetry/coverage", "usage:read"),
    ("/partner/v1/changes?since=2026-01-01T00:00:00", "changes:read"),
    ("/partner/v1/export", "export:read"),
]


class TestExpandScopes:
    def test_legacy_registry_read_maps_to_phase1_read_scopes(self):
        assert expand_scopes(["registry:read"]) == LEGACY_REGISTRY_READ
        for _, scope in ROUTES:
            assert scope in expand_scopes(["registry:read"])

    def test_new_scopes_are_not_in_the_legacy_alias(self):
        assert "webhooks:manage" in SCOPES
        assert "webhooks:manage" not in expand_scopes(["registry:read"])

    def test_unknown_scopes_grant_nothing(self):
        assert expand_scopes(["admin", "*", "registry:write"]) == frozenset()
        assert expand_scopes(None) == frozenset()

    def test_unknown_scopes_reported(self):
        assert unknown_scopes(["assets:read", "registry:read", "nope", "*"]) == ["*", "nope"]

    def test_default_scopes_are_read_only(self):
        assert set(DEFAULT_SCOPES) == LEGACY_REGISTRY_READ


class TestPrincipal:
    def _p(self, scopes):
        return PartnerPrincipal(uuid.uuid4(), uuid.uuid4(), "c", set(scopes))

    def test_legacy_principal_keeps_every_read_scope(self):
        p = self._p({"registry:read"})
        assert all(p.has_scope(s) for s in LEGACY_REGISTRY_READ)
        assert not p.has_scope("webhooks:manage")

    def test_granular_principal(self):
        p = self._p({"assets:read"})
        assert p.has_scope("assets:read")
        assert not p.has_scope("export:read")

    def test_require_scope_rejects_unknown_scope_names(self):
        with pytest.raises(ValueError):
            require_scope("registry:raed")
        with pytest.raises(ValueError):
            require_scope("registry:read")  # alias, not a route scope


class TestRateLimitTiers:
    def test_tier_wins(self):
        assert effective_rate_limit("basic", 1000) == RATE_LIMIT_TIERS["basic"]
        assert effective_rate_limit("enterprise", None) == RATE_LIMIT_TIERS["enterprise"]

    def test_no_tier_keeps_custom_or_phase1_limit(self):
        assert effective_rate_limit(None, 250) == 250
        assert effective_rate_limit(None, None) == 1000

    def test_unknown_tier_falls_back_to_stored_limit(self):
        assert effective_rate_limit("retired-tier", 300) == 300


class _AuditOnly:
    """Accepts the 403 audit row; any query means the route ran past its
    scope check."""

    def __init__(self):
        self.added = []

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        pass

    async def rollback(self):
        pass

    async def execute(self, *a, **kw):
        raise AssertionError("route queried the database before its scope check")


@pytest.fixture
def make_client(monkeypatch):
    def _make(scopes, db) -> TestClient:
        principal = PartnerPrincipal(uuid.uuid4(), uuid.uuid4(), "c", set(scopes))

        async def fake_resolve(request: Request) -> PartnerPrincipal:
            return principal

        monkeypatch.setattr(partner_auth, "resolve_partner", fake_resolve)
        app = FastAPI()

        @app.middleware("http")
        async def attach_db(request: Request, call_next):
            request.state.db = db
            return await call_next(request)

        app.include_router(router)
        return TestClient(app)

    return _make


@pytest.mark.parametrize(("path", "scope"), ROUTES)
def test_route_without_its_scope_is_403(make_client, path, scope):
    db = _AuditOnly()
    client = make_client(set(SCOPES) - {scope}, db)
    resp = client.get(path)
    assert resp.status_code == 403, resp.text
    assert scope in resp.json()["detail"]["detail"]
    (audit,) = db.added
    assert audit.status_code == 403
    assert audit.endpoint == path.split("?")[0]


def test_no_scopes_is_403_everywhere(make_client):
    client = make_client(set(), _AuditOnly())
    for path, _ in ROUTES:
        assert client.get(path).status_code == 403, path


def test_every_partner_route_is_scoped():
    """A new /partner/v1 route must declare a scope (health excepted)."""
    for route in router.routes:
        if route.path == "/partner/v1/health":
            continue
        deps = [d.call for d in route.dependant.dependencies]
        assert any(
            getattr(d, "__qualname__", "").startswith("require_scope") for d in deps
        ), route.path
