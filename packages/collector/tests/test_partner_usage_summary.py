"""Tests for GET /partner/v1/assets/{asset_id}/usage-summary.

These run without PostgreSQL. The router is mounted on a bare FastAPI app
whose `request.state.db` is an in-memory fake session. The fake reads the
WHERE clause of each statement and applies it to in-memory rows, so a
missing tenant or time predicate in the endpoint shows up as leaked data.
"""

from __future__ import annotations

import operator
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from collector import partner_auth
from collector.partner_auth import PartnerPrincipal
from collector.partner_registry import router
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy.sql import operators as sa_ops

TENANT_A = uuid.uuid4()
TENANT_B = uuid.uuid4()
ASSET_A = uuid.uuid4()  # belongs to tenant A
ASSET_B = uuid.uuid4()  # belongs to tenant B
NOW = datetime.now(UTC)


@dataclass
class Row:
    """A plain stand-in for an ORM row: attribute per column."""

    values: dict

    def __getattr__(self, name):
        try:
            return self.values[name]
        except KeyError as exc:
            raise AttributeError(name) from exc


_OPS = {
    sa_ops.eq: operator.eq,
    sa_ops.ge: operator.ge,
    sa_ops.gt: operator.gt,
    sa_ops.le: operator.le,
    sa_ops.lt: operator.lt,
}


def _matches(row: Row, criteria) -> bool:
    for crit in criteria:
        op = _OPS[crit.operator]
        if not op(row.values.get(crit.left.key), crit.right.value):
            return False
    return True


class _Result:
    def __init__(self, rows: list):
        self._rows = rows

    def scalar(self):
        return self._rows[0] if self._rows else None

    def scalar_one_or_none(self):
        assert len(self._rows) <= 1
        return self.scalar()

    def one_or_none(self):
        return self.scalar_one_or_none()

    def mappings(self):
        return self

    def one(self):
        assert len(self._rows) == 1
        return self._rows[0]


@dataclass
class FakeSession:
    tables: dict[str, list[Row]]
    added: list = field(default_factory=list)

    async def execute(self, stmt):
        criteria = list(stmt._where_criteria)
        table = criteria[0].left.table.name
        rows = [r for r in self.tables.get(table, []) if _matches(r, criteria)]
        if table == "partner_credentials":
            return _Result([])  # rate limit falls back to the default
        if table == "tenants":
            return _Result([])  # retention lookup: no clamp (DB test covers it)
        if table == "ai_assets":
            return _Result([r.id for r in rows])
        if table == "ai_usage_events":
            return _Result([_aggregate(stmt, rows)])
        raise AssertionError(f"unexpected table {table}")

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        pass

    async def rollback(self):
        pass


def _aggregate(stmt, rows: list[Row]) -> dict:
    """Compute what Postgres would return for the labelled aggregates."""

    def present(col):
        return [r.values[col] for r in rows if r.values.get(col) is not None]

    def total(col):
        vals = present(col)
        return sum(vals) if vals else None

    latencies = present("latency_ms")
    computed = {
        "total_requests": len(rows),
        "total_tokens_in": total("tokens_in"),
        "total_tokens_out": total("tokens_out"),
        "estimated_cost_usd": total("cost"),
        "unique_sessions": len(set(present("session_id"))),
        "avg_latency_ms": (
            Decimal(sum(latencies)) / len(latencies) if latencies else None
        ),
    }
    labels = [c.name for c in stmt.selected_columns]
    assert set(labels) == set(computed), labels
    return {k: computed[k] for k in labels}


def _event(asset, tenant, days_ago, **values) -> Row:
    return Row(
        {
            "id": uuid.uuid4(),
            "asset_id": asset,
            "tenant_id": tenant,
            "timestamp": NOW - timedelta(days=days_ago),
            "tokens_in": None,
            "tokens_out": None,
            "cost": None,
            "latency_ms": None,
            "session_id": None,
            **values,
        }
    )


def _seed() -> dict[str, list[Row]]:
    return {
        "ai_assets": [
            Row({"id": ASSET_A, "tenant_id": TENANT_A}),
            Row({"id": ASSET_B, "tenant_id": TENANT_B}),
        ],
        "ai_usage_events": [
            _event(
                ASSET_A,
                TENANT_A,
                1,
                tokens_in=100,
                tokens_out=200,
                cost=Decimal("0.010000"),
                latency_ms=300,
                session_id="s1",
            ),
            _event(
                ASSET_A,
                TENANT_A,
                3,
                tokens_in=50,
                tokens_out=70,
                cost=Decimal("0.005000"),
                latency_ms=501,
                session_id="s1",
            ),
            _event(
                ASSET_A,
                TENANT_A,
                20,
                tokens_in=1000,
                tokens_out=2000,
                cost=Decimal("0.100000"),
                latency_ms=200,
                session_id="s2",
            ),
            _event(ASSET_A, TENANT_A, 60, tokens_in=7, tokens_out=9, session_id="s3"),
            _event(ASSET_A, TENANT_A, 120, tokens_in=999_999),  # outside 90d
            # Same asset id under another tenant must never be counted.
            _event(ASSET_A, TENANT_B, 1, tokens_in=555_555, session_id="x"),
            _event(ASSET_B, TENANT_B, 1, tokens_in=1, tokens_out=1),
        ],
    }


@pytest.fixture
def session() -> FakeSession:
    return FakeSession(_seed())


@pytest.fixture
def make_client(session, monkeypatch):
    def _make(tenant=TENANT_A, scopes=("registry:read",)) -> TestClient:
        principal = PartnerPrincipal(
            tenant_id=tenant,
            partner_id=uuid.uuid4(),
            client_id="ps-partner-test",
            scopes=set(scopes),
        )

        async def fake_resolve(request: Request) -> PartnerPrincipal:
            return principal

        monkeypatch.setattr(partner_auth, "resolve_partner", fake_resolve)

        app = FastAPI()

        @app.middleware("http")
        async def attach_db(request: Request, call_next):
            request.state.db = session
            return await call_next(request)

        app.include_router(router)
        return TestClient(app)

    return _make


def _url(asset_id, period: str | None = None) -> str:
    url = f"/partner/v1/assets/{asset_id}/usage-summary"
    return f"{url}?period={period}" if period else url


class TestUsageSummary:
    def test_default_period_is_30d(self, make_client):
        r = make_client().get(_url(ASSET_A))
        assert r.status_code == 200
        assert r.json() == {
            "total_requests": 3,
            "total_tokens_in": 1150,
            "total_tokens_out": 2270,
            "estimated_cost_usd": 0.115,
            "unique_sessions": 2,
            "avg_latency_ms": 334,
            "period": "30d",
        }

    def test_7d_window(self, make_client):
        body = make_client().get(_url(ASSET_A, "7d")).json()
        assert body["period"] == "7d"
        assert body["total_requests"] == 2
        assert body["total_tokens_in"] == 150
        assert body["unique_sessions"] == 1
        assert body["avg_latency_ms"] == 400  # (300 + 501) / 2, rounded

    def test_90d_window_excludes_older_events(self, make_client):
        body = make_client().get(_url(ASSET_A, "90d")).json()
        assert body["total_requests"] == 4
        assert body["total_tokens_in"] == 1157
        assert body["unique_sessions"] == 3

    def test_invalid_period_is_422(self, make_client):
        assert make_client().get(_url(ASSET_A, "1y")).status_code == 422

    def test_matches_documented_schema(self, make_client):
        """Contract check: keys and types match `UsageSummary` in openapi.yaml."""
        yaml = pytest.importorskip("yaml")
        spec_path = Path(__file__).resolve().parents[3] / "docs" / "openapi.yaml"
        spec = yaml.safe_load(spec_path.read_text())
        op = spec["paths"]["/partner/v1/assets/{asset_id}/usage-summary"]["get"]
        period_param = next(p for p in op["parameters"] if p["name"] == "period")
        assert period_param["schema"]["default"] == "30d"
        schema = spec["components"]["schemas"]["UsageSummary"]["properties"]

        for period in period_param["schema"]["enum"]:
            body = make_client().get(_url(ASSET_A, period)).json()
            assert set(body) == set(schema)
            types = {"integer": int, "number": (int, float), "string": str}
            for key, prop in schema.items():
                if body[key] is not None:
                    assert isinstance(body[key], types[prop["type"]]), key

    def test_no_events_gives_zeros_and_nulls(self, make_client, session):
        session.tables["ai_usage_events"] = []
        body = make_client().get(_url(ASSET_A)).json()
        assert body == {
            "total_requests": 0,
            "total_tokens_in": 0,
            "total_tokens_out": 0,
            "estimated_cost_usd": None,
            "unique_sessions": 0,
            "avg_latency_ms": None,
            "period": "30d",
        }


class TestTenantIsolation:
    def test_other_tenants_asset_is_404(self, make_client):
        r = make_client(tenant=TENANT_A).get(_url(ASSET_B))
        assert r.status_code == 404
        assert r.json()["detail"]["detail"] == "Asset not found"

    def test_unknown_asset_is_same_404(self, make_client):
        other = make_client(tenant=TENANT_A).get(_url(ASSET_B))
        missing = make_client(tenant=TENANT_A).get(_url(uuid.uuid4()))
        assert missing.status_code == other.status_code == 404
        assert missing.json() == other.json()

    def test_events_from_other_tenant_not_counted(self, make_client):
        body = make_client(tenant=TENANT_B).get(_url(ASSET_B, "7d")).json()
        assert body["total_requests"] == 1
        assert body["total_tokens_in"] == 1

    def test_404_is_audited(self, make_client, session):
        make_client(tenant=TENANT_A).get(_url(ASSET_B))
        assert [a.status_code for a in session.added] == [404]


class TestAuthAndRateLimit:
    def test_missing_scope_is_403(self, make_client):
        r = make_client(scopes=("admin:other",)).get(_url(ASSET_A))
        assert r.status_code == 403

    def test_rate_limit_headers_set(self, make_client):
        r = make_client().get(_url(ASSET_A))
        assert r.headers["X-RateLimit-Limit"] == "1000"
        assert "X-RateLimit-Remaining" in r.headers

    def test_rate_limit_exceeded_is_429(self, make_client, monkeypatch):
        from collector import partner_registry

        class Denied:
            allowed, limit, remaining, reset_at, retry_after = False, 1, 0, 0, 30

        class Limiter:
            def check(self, *_):
                return Denied()

        monkeypatch.setattr(partner_registry, "get_rate_limiter", lambda: Limiter())
        r = make_client().get(_url(ASSET_A))
        assert r.status_code == 429
        assert r.headers["Retry-After"] == "30"
