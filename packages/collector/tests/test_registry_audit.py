"""Unit tests for the registry read audit log (collector.audit).

No Postgres: rows are built from hand-made ASGI scopes and written to a
fake session.
"""

from __future__ import annotations

import inspect
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from collector import audit, partner_registry, registry
from db.api_keys import api_key_lookup_prefix
from fastapi.routing import APIRoute
from starlette.requests import Request

TENANT_KEY = "ps-test-key-12345"
TENANT_ID = uuid.uuid4()


class FakeSession:
    def __init__(self, fail_commit: bool = False, rowcount: int = 0):
        self.added: list = []
        self.commits = 0
        self.rollbacks = 0
        self.executed: list = []
        self.fail_commit = fail_commit
        self.rowcount = rowcount

    def add(self, obj) -> None:
        self.added.append(obj)

    async def commit(self) -> None:
        if self.fail_commit:
            raise RuntimeError("db down")
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1

    async def execute(self, stmt):
        self.executed.append(stmt)
        return SimpleNamespace(rowcount=self.rowcount)


def make_request(
    path: str = "/api/v1/registry/assets",
    query: str = "",
    route_path: str | None = None,
    path_params: dict | None = None,
    token: str | None = TENANT_KEY,
    db: FakeSession | None = None,
) -> Request:
    headers = [(b"user-agent", b"pytest")]
    if token is not None:
        headers.append((b"authorization", f"Bearer {token}".encode()))
    scope = {
        "type": "http",
        "method": "GET",
        "path": path,
        "query_string": query.encode(),
        "headers": headers,
        "client": ("10.0.0.1", 1234),
        "path_params": path_params or {},
        "state": {"db": db or FakeSession()},
    }
    if route_path:
        scope["route"] = SimpleNamespace(path=route_path)
    return Request(scope)


class TestRedaction:
    def test_secrets_redacted(self):
        out = audit.redact_query_params(
            [
                ("api_key", "ps-abc"),
                ("access_token", "t"),
                ("client_secret", "s"),
                ("vendor", "openai"),
            ]
        )
        assert out == {
            "api_key": audit.REDACTED,
            "access_token": audit.REDACTED,
            "client_secret": audit.REDACTED,
            "vendor": "openai",
        }

    def test_free_text_hashed_not_stored(self):
        out = audit.redact_query_params(
            [("q", "hiring tools used by Alice"), ("limit", "10")]
        )
        assert out["limit"] == "10"
        assert out["q"].startswith("sha256:")
        assert "Alice" not in out["q"]
        # Same text, same hash: equal searches can be correlated.
        assert out["q"] == audit.hash_free_text("hiring tools used by Alice")

    def test_repeated_params_become_list(self):
        out = audit.redact_query_params(
            [("status", "a"), ("status", "b"), ("status", "c")]
        )
        assert out == {"status": ["a", "b", "c"]}


class TestBuildRow:
    def test_tenant_read_records_who_what_when(self):
        req = make_request(
            query="business_unit=HR&q=secret%20project",
            route_path="/api/v1/registry/assets",
        )
        ids = [uuid.uuid4(), uuid.uuid4()]
        row = audit.build_audit_row(
            req,
            tenant_id=TENANT_ID,
            status_code=200,
            response_count=2,
            asset_ids=[str(i) for i in ids],
        )
        assert row.tenant_id == TENANT_ID
        assert row.partner_id is None
        assert row.api_key_prefix == api_key_lookup_prefix(TENANT_KEY)
        assert row.endpoint == "/api/v1/registry/assets"
        assert row.method == "GET"
        assert row.status_code == 200
        assert row.response_count == 2
        assert row.asset_ids == ids
        assert row.query_params["business_unit"] == "HR"
        assert row.query_params["q"].startswith("sha256:")
        assert row.ip_address == "10.0.0.1"
        assert row.user_agent == "pytest"
        assert row.timestamp.tzinfo is not None

    def test_full_key_and_search_text_never_stored(self):
        req = make_request(query="q=secret%20project&token=abc")
        row = audit.build_audit_row(
            req, tenant_id=TENANT_ID, status_code=200, response_count=0
        )
        stored = " ".join(str(getattr(row, c)) for c in row.__table__.columns.keys())  # noqa: SIM118
        assert TENANT_KEY not in stored
        assert "secret project" not in stored
        assert "abc" not in str(row.query_params)

    def test_path_asset_id_uses_route_template(self):
        asset_id = uuid.uuid4()
        req = make_request(
            path=f"/api/v1/registry/assets/{asset_id}/risks",
            route_path="/api/v1/registry/assets/{asset_id}/risks",
            path_params={"asset_id": str(asset_id)},
        )
        row = audit.build_audit_row(
            req, tenant_id=TENANT_ID, status_code=200, response_count=3
        )
        assert row.endpoint == "/api/v1/registry/assets/{asset_id}/risks"
        assert row.asset_ids == [asset_id]

    def test_asset_ids_capped(self):
        req = make_request()
        ids = [uuid.uuid4() for _ in range(audit.MAX_ASSET_IDS + 50)]
        row = audit.build_audit_row(
            req,
            tenant_id=TENANT_ID,
            status_code=200,
            response_count=len(ids),
            asset_ids=ids,
        )
        assert len(row.asset_ids) == audit.MAX_ASSET_IDS
        assert row.response_count == len(ids)

    def test_no_query_no_ids_store_null(self):
        row = audit.build_audit_row(
            make_request(), tenant_id=TENANT_ID, status_code=200, response_count=0
        )
        assert row.asset_ids is None
        assert row.query_params is None

    def test_partner_read_identified_by_partner_id(self):
        partner_id = uuid.uuid4()
        req = make_request(path="/partner/v1/export", token="ps-pk-ardoq-xyz")
        row = audit.build_audit_row(
            req,
            tenant_id=TENANT_ID,
            partner_id=partner_id,
            endpoint="/partner/v1/export",
            status_code=200,
        )
        assert row.partner_id == partner_id
        assert row.api_key_prefix is None
        assert row.endpoint == "/partner/v1/export"


class TestRecordRead:
    async def test_writes_and_commits(self):
        db = FakeSession()
        await audit.record_read(
            make_request(db=db), tenant_id=TENANT_ID, status_code=200, response_count=1
        )
        assert len(db.added) == 1
        assert db.commits == 1

    async def test_failure_never_raises(self):
        db = FakeSession(fail_commit=True)
        await audit.record_read(
            make_request(db=db), tenant_id=TENANT_ID, status_code=200, response_count=1
        )
        assert db.rollbacks == 1

    async def test_partner_audit_wrapper_uses_shared_writer(self):
        db = FakeSession()
        principal = SimpleNamespace(tenant_id=TENANT_ID, partner_id=uuid.uuid4())
        await partner_registry._audit(
            make_request(path="/partner/v1/assets", db=db),
            principal,
            "/partner/v1/assets",
            "GET",
            200,
            1,
        )
        (row,) = db.added
        assert row.partner_id == principal.partner_id
        assert row.endpoint == "/partner/v1/assets"


class TestPurge:
    async def test_deletes_rows_older_than_retention(self):
        db = FakeSession(rowcount=7)
        now = datetime(2026, 10, 13, tzinfo=UTC)
        assert await audit.purge_expired(db, 30, now=now) == 7
        (stmt,) = db.executed
        assert stmt.table.name == "partner_audit_log"
        cutoff = stmt.whereclause.right.value
        assert cutoff == now - timedelta(days=30)
        assert db.commits == 1

    async def test_rejects_non_positive_retention(self):
        with pytest.raises(ValueError):
            await audit.purge_expired(FakeSession(), 0)


def _get_routes(router):
    return [r for r in router.routes if isinstance(r, APIRoute) and "GET" in r.methods]


@pytest.mark.parametrize("route", _get_routes(registry.router), ids=lambda r: r.path)
def test_every_tenant_registry_read_is_audited(route):
    assert "record_read(" in inspect.getsource(route.endpoint)


@pytest.mark.parametrize(
    "route",
    [r for r in _get_routes(partner_registry.router) if r.path != "/partner/v1/health"],
    ids=lambda r: r.path,
)
def test_every_partner_read_is_audited(route):
    assert "_audit(" in inspect.getsource(route.endpoint)
