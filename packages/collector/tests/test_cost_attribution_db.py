"""Cost attribution (#347) against PostgreSQL (PS_TEST_DATABASE_URL).

Covers: team/project/tags resolved and stored at ingest (by id and by name,
a project fixing its team, unknown and other tenants' references dropped),
the rollups' new dimensions and their tag allowlist, the usage series by
team, project, service and tag, the monthly showback (groups plus
unallocated equal the tenant total to the cent), moving a project
(future spend only), the FOCUS SubAccount columns and tags, and user
erasure of the new per-user data. Row-level security of the tables these
columns live on is covered in test_rls_db.py.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from collector import cost_attribution, focus_export, usage_rollups
from collector.admin_auth import ADMIN_SCOPES
from collector.app import app
from collector.config import settings
from collector.partner_auth import hash_secret, issue_access_token
from db.models import (
    AIUsageEvent,
    PartnerCredential,
    Project,
    Team,
    Tenant,
    UsageRollupDaily,
)
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

KEY = "ps-test-key-12345"
SERIES = "/partner/v1/usage/series"
SHOWBACK = "/partner/v1/cost/showback"
NOW = datetime.now(UTC)
PERIOD = f"{NOW:%Y-%m}"


@pytest.fixture(autouse=True)
def _config(monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_per_minute", 0)


def _client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


def _ev(**fields):
    base = {"vendor": "openai", "model": "gpt-4o", "use_case_name": "support", "source": "gateway"}
    return base | fields


@pytest.fixture
async def env(seeded_db):
    factory = async_sessionmaker(seeded_db, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s:
        a = (await s.execute(select(Tenant))).scalar_one()
        a.settings = {"cost_tag_keys": ["env"]}
        b = Tenant(name="B", settings={})
        s.add(b)
        await s.flush()
        payments = Team(tenant_id=a.id, name="payments")
        search = Team(tenant_id=a.id, name="search")
        foreign = Team(tenant_id=b.id, name="payments")
        s.add_all([payments, search, foreign])
        await s.flush()
        checkout = Project(tenant_id=a.id, team_id=payments.id, name="checkout")
        s.add(checkout)
        cred = PartnerCredential(
            tenant_id=a.id, partner_name="pa", client_id="pa-client",
            client_secret_hash=hash_secret("x"), scopes=["usage:read", "usage:export"],
            is_active="true",
        )
        s.add(cred)
        await s.commit()
    token, _ = issue_access_token(a.id, cred.id, cred.client_id, cred.scopes)
    return {
        "factory": factory,
        "a": a.id,
        "payments": payments.id,
        "search": search.id,
        "foreign": foreign.id,
        "checkout": checkout.id,
        "h": {"Authorization": f"Bearer {token}"},
    }


async def _ingest(events):
    async with _client() as c:
        r = await c.post(
            "/ingest/events", headers={"Authorization": f"Bearer {KEY}"}, json={"events": events}
        )
    assert r.status_code == 200, r.text


async def _events(env):
    async with env["factory"]() as db:
        rows = (
            await db.execute(select(AIUsageEvent).order_by(AIUsageEvent.cost))
        ).scalars().all()
    return rows


async def _refresh(env):
    async with env["factory"]() as db:
        return await usage_rollups.refresh_tenant(db, env["a"])


async def test_ingest_resolves_team_project_and_tags(env):
    await _ingest([
        _ev(cost=0.01, team="payments", tags="env=prod, owner=ana,ps:x=1,bad key=1"),
        _ev(cost=0.02, team=str(env["search"]), calling_service="api"),
        # The project fixes its team, whatever team is declared.
        _ev(cost=0.03, team="search", project="checkout"),
        # Unknown, other tenant's and malformed references: unattributed.
        _ev(cost=0.04, team="nope"),
        _ev(cost=0.05, team=str(env["foreign"])),
        _ev(cost=0.06, team="bad\nname", project=123, tags={"k": "v" * 200}),
    ])
    rows = await _events(env)
    assert [(r.team_id, r.project_id) for r in rows] == [
        (env["payments"], None),
        (env["search"], None),
        (env["payments"], env["checkout"]),
        (None, None),
        (None, None),
        (None, None),
    ]
    assert rows[0].tags == {"env": "prod", "owner": "ana"}
    assert rows[1].calling_service == "api"
    assert rows[5].tags is None


def test_parse_tags_bounds():
    # A repeated key keeps its first value; "," is not allowed in a value.
    assert cost_attribution.parse_tags(["A=1", "a=2", "b=x,y", "c=3"]) == {"a": "1", "c": "3"}
    many = {f"k{i}": "v" for i in range(30)}
    assert len(cost_attribution.parse_tags(many)) == cost_attribution.MAX_TAGS
    assert cost_attribution.parse_tags("x" * 5000) is None
    assert cost_attribution.parse_tags(42) is None
    assert cost_attribution.parse_tags({"ps:team": "x", "k": "a=b"}) is None


def test_allocate_cents_adds_up_to_the_total():
    amounts = [Decimal("0.333333")] * 3 + [Decimal("0.004")]
    cents = cost_attribution.allocate_cents(amounts)
    assert sum(cents) == 100  # 1.003999 -> 100 cents
    # The last cent goes to the largest remainder (0.4 of a cent).
    assert cents == [33, 33, 33, 1]
    assert cost_attribution.allocate_cents([]) == []


def test_settings_and_month_window():
    assert cost_attribution.settings_errors(["env", "cost-center"]) == []
    assert cost_attribution.settings_errors("env")
    assert cost_attribution.settings_errors(["ps:x"])
    assert cost_attribution.settings_errors(["a", "a"])
    assert cost_attribution.settings_errors([f"k{i}" for i in range(11)])
    assert cost_attribution.month_window("2026-12") == (
        datetime(2026, 12, 1, tzinfo=UTC), datetime(2027, 1, 1, tzinfo=UTC)
    )
    with pytest.raises(ValueError):
        cost_attribution.month_window("2026-13")


async def test_series_and_showback_by_team_project_service_and_tag(env):
    await _ingest([
        _ev(cost=0.333333, team="payments", tags="env=prod,owner=ana", calling_service="api"),
        _ev(cost=0.333333, team="search", tags="env=dev"),
        _ev(cost=0.333333, project="checkout", tags="env=prod"),
        _ev(cost=0.004),  # unattributed
        _ev(tokens_in=5),  # no cost
    ])
    await _refresh(env)
    async with env["factory"]() as db:
        tags = (await db.execute(select(UsageRollupDaily.tags))).scalars().all()
    # Only settings.cost_tag_keys are kept in the rollups.
    assert {None if t is None else frozenset(t.items()) for t in tags} == {
        frozenset({("env", "prod")}), frozenset({("env", "dev")}), None
    }

    async with _client() as c:
        r = await c.get(SERIES, params={"group_by": "team"}, headers=env["h"])
        assert r.status_code == 200, r.text
        by_team = {p["key"]: p for p in r.json()["data"]}
        r = await c.get(SERIES, params={"group_by": "tag:env"}, headers=env["h"])
        by_tag = {p["key"]: p["requests"] for p in r.json()["data"]}
        r = await c.get(SERIES, params={"group_by": "service"}, headers=env["h"])
        by_service = {p["key"]: p["requests"] for p in r.json()["data"]}
        bad = await c.get(SERIES, params={"group_by": "tag:owner"}, headers=env["h"])
        bad2 = await c.get(SERIES, params={"group_by": "bogus"}, headers=env["h"])
        sb = await c.get(SHOWBACK, params={"period": PERIOD}, headers=env["h"])
        sbp = await c.get(SHOWBACK, params={"group_by": "project", "period": PERIOD}, headers=env["h"])
        sbt = await c.get(SHOWBACK, params={"group_by": "tag:env", "period": PERIOD}, headers=env["h"])
        bad3 = await c.get(SHOWBACK, params={"group_by": "model"}, headers=env["h"])
        bad4 = await c.get(SHOWBACK, params={"period": "2026-1"}, headers=env["h"])
    pay, srch = str(env["payments"]), str(env["search"])
    assert by_team[pay]["name"] == "payments" and by_team[pay]["requests"] == 2
    assert by_team[srch]["name"] == "search"
    assert by_team[None]["requests"] == 2 and by_team[None]["name"] is None
    assert by_tag == {"prod": 2, "dev": 1, None: 2}
    assert by_service == {"api": 1, None: 4}
    assert bad.status_code == 400 and "cost_tag_keys" in bad.json()["detail"]["detail"]
    assert bad2.status_code == 400
    assert bad3.status_code == 400 and bad4.status_code == 400

    # Acceptance: team costs + unallocated = tenant total, to the cent.
    body = sb.json()
    assert sb.status_code == 200, sb.text
    cents = sum(g["cost_cents"] for g in body["data"]) + body["unallocated"]["cost_cents"]
    assert cents == body["total"]["cost_cents"] == 100
    assert Decimal(body["total"]["cost_usd"]) == Decimal("1.003999")
    assert {g["key"]: g["cost_cents"] for g in body["data"]} == {pay: 67, srch: 33}
    assert body["unallocated"]["cost_cents"] == 0
    assert body["unallocated"]["requests"] == 2
    assert body["total"]["requests"] == 5
    assert body["meta"]["period"] == PERIOD and body["meta"]["partial"] is False
    keys = set(body["data"][0])
    schema_keys = {"key", "name", "team_id", "team_name", "cost_usd", "cost_cents",
                   "requests", "cost_requests", "tokens_in", "tokens_out"}
    assert keys == schema_keys

    projects = sbp.json()["data"]
    assert projects == [
        projects[0] | {"key": str(env["checkout"]), "name": "checkout",
                       "team_id": pay, "team_name": "payments"}
    ]
    assert {g["key"] for g in sbt.json()["data"]} == {"prod", "dev"}


async def test_moving_a_project_reattributes_future_spend_only(env, monkeypatch):
    monkeypatch.setattr(settings, "admin_token", "op-secret")
    await _ingest([_ev(cost=1.0, project="checkout")])
    async with _client() as c:
        boot = await c.post(
            f"/api/v1/operator/tenants/{env['a']}/admin-tokens",
            headers={"Authorization": "Bearer op-secret"},
            json={"name": "boot", "scopes": list(ADMIN_SCOPES)},
        )
        assert boot.status_code == 201, boot.text
        admin = {"Authorization": f"Bearer {boot.json()['token']}"}
        r = await c.patch(
            f"/api/v1/admin/projects/{env['checkout']}",
            headers=admin,
            json={"team_id": str(env["search"])},
        )
        assert r.status_code == 200, r.text
        assert r.json()["team_id"] == str(env["search"])
        missing = await c.patch(
            f"/api/v1/admin/projects/{env['checkout']}",
            headers=admin,
            json={"team_id": str(env["foreign"])},
        )
        assert missing.status_code == 404
    await _ingest([_ev(cost=2.0, project="checkout")])
    rows = await _events(env)
    assert [(r.team_id, r.project_id) for r in rows] == [
        (env["payments"], env["checkout"]),
        (env["search"], env["checkout"]),
    ]


async def test_focus_export_sub_account_and_tags(env):
    await _ingest([
        _ev(cost=0.5, project="checkout", tags="env=prod,owner=ana", calling_service="api"),
        _ev(cost=0.25),
    ])
    start = NOW - timedelta(days=1)
    end = NOW + timedelta(hours=1)
    async with env["factory"]() as db:
        rows = (
            await db.execute(focus_export.focus_query(env["a"], start, end, "day"))
        ).mappings().all()
    out = sorted(
        (
            focus_export.focus_row(
                r, tenant_id=env["a"], tenant_name="Test Corp", window_start=start,
                window_end=end, granularity="day", cost_mode="estimated",
            )
            for r in rows
        ),
        key=lambda r: r["x_EstimatedCost"],
    )
    unattributed, attributed = out
    assert attributed["SubAccountId"] == str(env["payments"])
    assert attributed["SubAccountName"] == "payments"
    assert attributed["SubAccountType"] == "Team"
    assert attributed["Tags"] == {
        "env": "prod", "owner": "ana", "ps:use_case": "support",
        "ps:project_id": str(env["checkout"]), "ps:project": "checkout",
    }
    assert attributed["x_ProjectName"] == "checkout"
    assert attributed["x_CallingService"] == "api"
    assert unattributed["SubAccountId"] is None
    assert unattributed["SubAccountType"] is None
    assert unattributed["Tags"] == {"ps:use_case": "support"}
    assert list(attributed) == list(focus_export.COLUMNS)


async def test_user_erasure_nulls_tags_in_events_and_rollups(env):
    await _ingest([_ev(cost=0.1, team="payments", user_id="u-1", tags="env=prod")])
    await _refresh(env)
    async with _client() as c:
        r = await c.post(
            "/api/v1/admin/users/erase",
            headers={"Authorization": f"Bearer {KEY}"},
            json={"user_id": "u-1"},
        )
    assert r.status_code == 200, r.text
    async with env["factory"]() as db:
        ev = (await db.execute(select(AIUsageEvent))).scalar_one()
        rollups = (await db.execute(select(UsageRollupDaily))).scalars().all()
    assert ev.tags is None and ev.user_id_hash is None
    assert ev.team_id == env["payments"]  # attribution stays, totals stay
    assert [(r.tags, r.user_id_hash, r.team_id) for r in rollups] == [
        (None, None, env["payments"])
    ]


async def test_tag_allowlist_change_applies_on_rebuild(env):
    await _ingest([_ev(cost=0.1, tags="env=prod,owner=ana")])
    await _refresh(env)
    async with env["factory"]() as db:
        await db.execute(
            update(Tenant).where(Tenant.id == env["a"]).values(
                settings={"cost_tag_keys": ["owner"]}
            )
        )
        await db.commit()
        await usage_rollups.rebuild_tenant(db, env["a"])
        tags = (await db.execute(select(UsageRollupDaily.tags))).scalars().all()
    assert tags == [{"owner": "ana"}]
