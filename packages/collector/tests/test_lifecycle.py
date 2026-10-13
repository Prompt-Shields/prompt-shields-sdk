"""Tests for asset lifecycle (stale / retired detection).

All but the last test need no Postgres: the sweep runs against a fake
session that records the UPDATE statements it is given.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from collector import lifecycle
from collector.dedup import find_or_create_asset
from collector.lifecycle import (
    ACTIVE,
    RETIRED,
    STALE,
    compute_lifecycle_state,
    lifecycle_change_for,
    reactivate,
    resolve_thresholds,
    sweep_asset_lifecycle,
)
from collector.partner_registry import _asset_dict
from collector.registry import _serialize_asset
from db.models import AIAsset
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient
from sqlalchemy.dialects import postgresql

NOW = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)


class FakeSession:
    """Records executed statements; returns a fixed rowcount per call."""

    def __init__(self, rowcounts=(0, 0, 0), scalar=None):
        self.statements = []
        self._rowcounts = list(rowcounts)
        self._scalar = scalar
        self.committed = False

    async def execute(self, stmt):
        self.statements.append(stmt)
        rowcount = self._rowcounts.pop(0) if self._rowcounts else 0
        return SimpleNamespace(
            rowcount=rowcount, scalar_one_or_none=lambda: self._scalar
        )

    async def commit(self):
        self.committed = True

    def add(self, obj):
        pass


def _sql(stmt) -> str:
    return str(stmt.compile(dialect=postgresql.dialect()))


def _params(stmt) -> dict:
    return stmt.compile(dialect=postgresql.dialect()).params


# ──────────────────────────────────────────────────────────────────────
# Pure helpers
# ──────────────────────────────────────────────────────────────────────


class TestComputeLifecycleState:
    @pytest.mark.parametrize(
        "age_days, expected",
        [
            (0, ACTIVE),
            (29, ACTIVE),
            (30, STALE),
            (89, STALE),
            (90, RETIRED),
            (400, RETIRED),
        ],
    )
    def test_thresholds(self, age_days, expected):
        last_seen = NOW - timedelta(days=age_days)
        assert compute_lifecycle_state(last_seen, NOW, 30, 90) == expected

    def test_never_seen_is_active(self):
        assert compute_lifecycle_state(None, NOW, 30, 90) == ACTIVE


class TestResolveThresholds:
    def test_defaults_from_settings(self, monkeypatch):
        monkeypatch.setattr(lifecycle.settings, "asset_stale_after_days", 7)
        monkeypatch.setattr(lifecycle.settings, "asset_retire_after_days", 21)
        assert resolve_thresholds() == (7, 21)

    def test_overrides(self):
        assert resolve_thresholds(5, 10) == (5, 10)

    @pytest.mark.parametrize("stale, retire", [(0, 10), (10, 10), (10, 5)])
    def test_invalid(self, stale, retire):
        with pytest.raises(ValueError):
            resolve_thresholds(stale, retire)


class TestReactivate:
    @pytest.mark.parametrize("state", [STALE, RETIRED])
    def test_reactivates(self, state):
        asset = AIAsset(lifecycle_state=state)
        assert reactivate(asset, NOW) is True
        assert asset.lifecycle_state == ACTIVE
        assert asset.lifecycle_changed_at == NOW

    def test_active_is_noop(self):
        asset = AIAsset(lifecycle_state=ACTIVE)
        assert reactivate(asset, NOW) is False
        assert asset.lifecycle_changed_at is None


class TestLifecycleChangeFor:
    since = NOW - timedelta(days=1)

    def test_no_change(self):
        assert lifecycle_change_for(AIAsset(lifecycle_state=ACTIVE), self.since) is None

    def test_change_before_since_is_ignored(self):
        asset = AIAsset(
            lifecycle_state=STALE, lifecycle_changed_at=NOW - timedelta(days=2)
        )
        assert lifecycle_change_for(asset, self.since) is None

    @pytest.mark.parametrize(
        "state, expected",
        [(STALE, "stale"), (RETIRED, "retired"), (ACTIVE, "reactivated")],
    )
    def test_reports_transition(self, state, expected):
        asset = AIAsset(lifecycle_state=state, lifecycle_changed_at=NOW)
        assert lifecycle_change_for(asset, self.since) == expected


# ──────────────────────────────────────────────────────────────────────
# Sweep
# ──────────────────────────────────────────────────────────────────────


class TestSweep:
    async def test_issues_three_idempotent_updates(self):
        db = FakeSession(rowcounts=(2, 3, 1))
        result = await sweep_asset_lifecycle(
            db, now=NOW, stale_after_days=30, retire_after_days=90
        )

        assert (result.marked_retired, result.marked_stale, result.reactivated) == (
            2,
            3,
            1,
        )
        assert result.stale_after_days == 30
        assert result.retire_after_days == 90
        assert not db.committed  # caller owns the transaction

        retire, stale, active = db.statements
        for stmt, target in ((retire, RETIRED), (stale, STALE), (active, ACTIVE)):
            sql = _sql(stmt)
            assert sql.startswith("UPDATE ai_assets SET")
            assert "lifecycle_changed_at" in sql and "updated_at" in sql
            assert "ai_assets.lifecycle_state != " in sql
            params = _params(stmt)
            assert params["lifecycle_state"] == target
            assert params["lifecycle_changed_at"] == NOW
            assert params["updated_at"] == NOW
            assert "tenant_id" not in sql

        stale_cutoff = NOW - timedelta(days=30)
        retire_cutoff = NOW - timedelta(days=90)
        assert retire_cutoff in _params(retire).values()
        assert {stale_cutoff, retire_cutoff} <= set(_params(stale).values())
        assert stale_cutoff in _params(active).values()

    async def test_tenant_scope(self):
        tenant_id = uuid.uuid4()
        db = FakeSession()
        await sweep_asset_lifecycle(db, now=NOW, tenant_id=tenant_id)
        for stmt in db.statements:
            assert "ai_assets.tenant_id = " in _sql(stmt)
            assert tenant_id in _params(stmt).values()

    async def test_invalid_thresholds_raise(self):
        with pytest.raises(ValueError):
            await sweep_asset_lifecycle(
                FakeSession(), stale_after_days=10, retire_after_days=5
            )


# ──────────────────────────────────────────────────────────────────────
# Ingest reactivation (via dedup) and serializers
# ──────────────────────────────────────────────────────────────────────


async def test_telemetry_reactivates_existing_asset():
    asset = AIAsset(
        vendor="openai",
        model="gpt-4o",
        discovery_source=["sdk"],
        confidence="high",
        lifecycle_state=RETIRED,
        last_seen=NOW - timedelta(days=120),
    )
    db = FakeSession(scalar=asset)
    found, is_new = await find_or_create_asset(
        db=db,
        tenant_id=uuid.uuid4(),
        vendor="openai",
        model="gpt-4o",
        use_case_name=None,
        business_unit=None,
        calling_service=None,
        source="sdk",
    )
    assert found is asset and is_new is False
    assert asset.lifecycle_state == ACTIVE
    assert asset.lifecycle_changed_at is not None


@pytest.mark.parametrize("serialize", [_serialize_asset, _asset_dict])
def test_serializers_expose_lifecycle(serialize):
    asset = AIAsset(
        id=uuid.uuid4(),
        vendor="openai",
        discovery_source=["sdk"],
        lifecycle_state=STALE,
        lifecycle_changed_at=NOW,
    )
    out = serialize(asset)
    assert out["lifecycle_state"] == STALE
    assert out["lifecycle_changed_at"] == NOW.isoformat()
    # Existing fields are untouched.
    assert out["vendor"] == "openai"


# ──────────────────────────────────────────────────────────────────────
# Admin endpoint
# ──────────────────────────────────────────────────────────────────────


def _admin_app(db: FakeSession, tenant_id: uuid.UUID) -> FastAPI:
    app = FastAPI()

    @app.middleware("http")
    async def _db(request: Request, call_next):
        request.state.db = db
        return await call_next(request)

    app.include_router(lifecycle.router)
    return app


async def test_admin_sweep_endpoint(monkeypatch):
    tenant_id = uuid.uuid4()

    async def fake_resolve_tenant(request):
        return tenant_id

    monkeypatch.setattr(lifecycle, "resolve_tenant", fake_resolve_tenant)
    db = FakeSession(rowcounts=(1, 2, 0))
    transport = ASGITransport(app=_admin_app(db, tenant_id))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/admin/asset-lifecycle/sweep?stale_after_days=7&retire_after_days=30"
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["marked_retired"] == 1
    assert body["marked_stale"] == 2
    assert body["stale_after_days"] == 7
    assert body["retire_after_days"] == 30
    assert db.committed
    assert all(tenant_id in _params(s).values() for s in db.statements)


async def test_admin_sweep_rejects_bad_thresholds(monkeypatch):
    async def fake_resolve_tenant(request):
        return uuid.uuid4()

    monkeypatch.setattr(lifecycle, "resolve_tenant", fake_resolve_tenant)
    db = FakeSession()
    transport = ASGITransport(app=_admin_app(db, uuid.uuid4()))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/admin/asset-lifecycle/sweep?stale_after_days=30&retire_after_days=10"
        )

    assert resp.status_code == 400
    assert not db.committed


# ──────────────────────────────────────────────────────────────────────
# End to end against Postgres (uses the shared `seeded_db` fixture)
# ──────────────────────────────────────────────────────────────────────

HEADERS = {"Authorization": "Bearer ps-test-key-12345"}


async def test_sweep_filter_and_reactivate_end_to_end(seeded_db):
    from collector.app import app
    from sqlalchemy import update

    event = {"vendor": "openai", "model": "gpt-4o", "use_case_name": "screening"}
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post("/ingest/events", headers=HEADERS, json={"events": [event]})

        # Backdate telemetry so the asset is past the stale threshold.
        async with seeded_db.begin() as conn:
            await conn.execute(
                update(AIAsset).values(last_seen=datetime.now(UTC) - timedelta(days=45))
            )

        resp = await client.post(
            "/api/v1/admin/asset-lifecycle/sweep?stale_after_days=30&retire_after_days=90",
            headers=HEADERS,
        )
        assert resp.status_code == 200
        assert resp.json()["marked_stale"] == 1

        stale = await client.get(
            "/api/v1/registry/assets?lifecycle_state=stale", headers=HEADERS
        )
        assert [a["lifecycle_state"] for a in stale.json()["data"]] == [STALE]

        await client.post("/ingest/events", headers=HEADERS, json={"events": [event]})
        active = await client.get(
            "/api/v1/registry/assets?lifecycle_state=active", headers=HEADERS
        )
        assert len(active.json()["data"]) == 1
