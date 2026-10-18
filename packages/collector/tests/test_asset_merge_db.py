"""Duplicate-asset merge, oldest-match ordering and pull sightings, against
a real PostgreSQL database (PS_TEST_DATABASE_URL) with row-level security
enforced: `python -m collector.dedup merge-duplicates`, dedup's choice of
the oldest asset when a platform_signal asset is backdated, and the
lifecycle signals and webhooks of provider usage and cloud inventory
sightings."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from collector import asset_merge, dedup, rls
from collector import cloud_inventory as ci
from collector import provider_usage as pu
from collector.app import app
from collector.partner_auth import hash_secret
from db.api_keys import api_key_lookup_prefix, hash_api_key
from db.models import (
    AIAsset,
    AIUsageEvent,
    AssetAttestation,
    DataFlow,
    PartnerAuditLog,
    PartnerCredential,
    RetentionAuditLog,
    RiskMapping,
    Tenant,
    WebhookDelivery,
    WebhookSubscription,
)
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

KEY_A = {"Authorization": "Bearer ps-test-key-12345"}  # the seeded_db tenant
KEY_B = "ps-test-key-merge-b"
NOW = datetime.now(UTC)


@pytest.fixture
async def env(seeded_db):
    """Tenant A with a partner subscribed to every asset event, tenant B,
    RLS enforced."""
    admin = async_sessionmaker(
        seeded_db, class_=AsyncSession, expire_on_commit=False, info=rls.bypass_info("test-seed")
    )
    async with admin() as s:
        a = (await s.execute(select(Tenant.id))).scalar_one()
        b = Tenant(
            name="Other Corp",
            settings={},
            api_key_prefix=api_key_lookup_prefix(KEY_B),
            api_key_hash=hash_api_key(KEY_B),
        )
        partner = PartnerCredential(
            tenant_id=a,
            partner_name="ea-tool",
            client_id="merge-client",
            client_secret_hash=hash_secret("x"),
            scopes=["registry:read"],
            is_active="true",
        )
        s.add_all([b, partner])
        await s.flush()
        sub = WebhookSubscription(
            tenant_id=a,
            partner_id=partner.id,
            url="https://hooks.example/merge",
            event_types=["asset.created", "asset.changed", "asset.retired"],
            secret_ciphertext="unused",
            is_active=True,
        )
        s.add(sub)
        await s.commit()
        b, sub = b.id, sub.id
    async with seeded_db.begin() as conn:
        await rls.enable(conn)
    tenant = async_sessionmaker(seeded_db, class_=AsyncSession, expire_on_commit=False)
    return {"admin": admin, "tenant": tenant, "a": a, "b": b, "sub": sub}


def _asset(tenant_id, *, age_days: float, **kw) -> AIAsset:
    created = NOW - timedelta(days=age_days)
    kw.setdefault("vendor", "openai")
    kw.setdefault("model", "gpt-4o")
    kw.setdefault("discovery_source", ["sdk"])
    return AIAsset(
        tenant_id=tenant_id,
        created_at=created,
        first_seen=kw.pop("first_seen", created),
        last_seen=kw.pop("last_seen", created),
        **kw,
    )


async def _scoped(env, tenant_id):
    s = env["tenant"]()
    await rls.set_tenant(s, tenant_id)
    return s


async def _deliveries(env) -> list[WebhookDelivery]:
    async with env["admin"]() as s:
        return list(
            (await s.execute(select(WebhookDelivery).order_by(WebhookDelivery.created_at)))
            .scalars()
            .all()
        )


# ──────────────────────────────────────────────────────────────────────
# merge-duplicates
# ──────────────────────────────────────────────────────────────────────


@pytest.fixture
async def dups(env):
    """Tenant A: a race group (oldest, dup) with events, flows, risks and a
    delivery on the dup; a group whose newer member is attested; two
    platform_signal assets sharing a key. Tenant B: its own race group."""
    a, b = env["a"], env["b"]
    async with env["admin"]() as s:
        oldest = _asset(a, age_days=10, use_case_name="screening", discovery_source=["sdk"])
        dup = _asset(
            a,
            age_days=9,
            use_case_name="screening",
            discovery_source=["gateway"],
            data_classification="confidential",
            owner_email="owner@test.com",
            first_seen=NOW - timedelta(days=12),
            last_seen=NOW - timedelta(hours=1),
        )
        plain = _asset(a, age_days=5, model="gpt-4o-mini")
        attested = _asset(a, age_days=4, model="gpt-4o-mini")
        pulled = [
            _asset(
                a,
                age_days=3 - i,
                id=uuid.uuid5(uuid.NAMESPACE_URL, f"pulled-{i}"),
                model="o3",
                discovery_source=["platform_signal"],
            )
            for i in range(2)
        ]
        b_old = _asset(b, age_days=2, model="claude")
        b_new = _asset(b, age_days=1, model="claude")
        s.add_all([oldest, dup, plain, attested, *pulled, b_old, b_new])
        await s.flush()
        for asset, n in ((oldest, 2), (dup, 3)):
            s.add_all(
                AIUsageEvent(tenant_id=a, asset_id=asset.id, source="sdk", tokens_in=1)
                for _ in range(n)
            )
        flow = {"source_system": "crm", "destination_system": "openai/gpt-4o", "direction": "input"}
        s.add_all(
            [
                DataFlow(
                    tenant_id=a,
                    asset_id=oldest.id,
                    **flow,
                    event_count=2,
                    first_seen=NOW - timedelta(days=10),
                    last_seen=NOW - timedelta(days=5),
                    detected_pii_types=["email"],
                ),
                DataFlow(
                    tenant_id=a,
                    asset_id=dup.id,
                    **flow,
                    event_count=3,
                    first_seen=NOW - timedelta(days=12),
                    last_seen=NOW - timedelta(days=1),
                    detected_pii_types=["phone"],
                    data_classification="restricted",
                ),
                DataFlow(
                    tenant_id=a,
                    asset_id=dup.id,
                    source_system="openai/gpt-4o",
                    destination_system="slack",
                    direction="output",
                    event_count=1,
                ),
                RiskMapping(
                    tenant_id=a, asset_id=oldest.id, risk_category="pii", risk_level="high",
                    rule_id="r1", status="suggested", source="rules",
                ),
                RiskMapping(
                    tenant_id=a, asset_id=dup.id, risk_category="pii", risk_level="high",
                    rule_id="r1", status="confirmed", source="rules",
                ),
                RiskMapping(
                    tenant_id=a, asset_id=dup.id, risk_category="bias", risk_level="low",
                ),
                AssetAttestation(
                    tenant_id=a, asset_id=attested.id, field="owner", state="attested",
                    attested_by="x@test.com", actor_type="tenant",
                ),
            ]
        )
        await s.flush()
        s.add(
            WebhookDelivery(
                tenant_id=a,
                subscription_id=env["sub"],
                event_id=uuid.uuid4(),
                event_type="asset.created",
                asset_id=dup.id,
                payload={"data": {"asset": {"id": str(dup.id)}}},
            )
        )
        await s.commit()
        return {
            "oldest": oldest.id,
            "dup": dup.id,
            "plain": plain.id,
            "attested": attested.id,
            "pulled": [p.id for p in pulled],
            "b_old": b_old.id,
            "b_new": b_new.id,
        }


async def test_dry_run_reports_groups_and_changes_nothing(env, dups):
    async with await _scoped(env, env["a"]) as s:
        result = await asset_merge.merge_tenant_duplicates(s, env["a"])
    out = result.as_dict()
    assert out["applied"] is False
    groups = {g["survivor"]: g for g in out["groups"]}
    # Merged into the oldest; the attested asset survives its group even
    # though it is newer; platform_signal (uuid5) assets are never grouped.
    assert set(groups) == {str(dups["oldest"]), str(dups["attested"])}
    assert groups[str(dups["oldest"])]["merged"] == [str(dups["dup"])]
    assert groups[str(dups["attested"])]["merged"] == [str(dups["plain"])]
    async with env["admin"]() as s:
        assert (await s.execute(select(func.count(AIAsset.id)))).scalar_one() == 8
        assert (await s.execute(select(func.count(RetentionAuditLog.id)))).scalar_one() == 0
    assert await _deliveries(env) and len(await _deliveries(env)) == 1


async def test_apply_merges_into_the_oldest_and_is_idempotent(env, dups):
    async with await _scoped(env, env["a"]) as s:
        result = await asset_merge.merge_tenant_duplicates(s, env["a"], apply=True, actor="test")
    assert result.counts["assets_merged"] == 2
    assert result.counts["usage_events"] == 3
    assert result.counts["data_flows"] == 2
    assert result.counts["webhook_deliveries"] == 1

    async with env["admin"]() as s:
        ids = set((await s.execute(select(AIAsset.id))).scalars())
        assert dups["dup"] not in ids and dups["plain"] not in ids
        assert {dups["oldest"], dups["attested"], *dups["pulled"]} <= ids
        assert {dups["b_old"], dups["b_new"]} <= ids  # other tenant untouched

        survivor = await s.get(AIAsset, dups["oldest"])
        assert survivor.discovery_source == ["sdk", "gateway"]
        assert survivor.confidence == "verified"
        assert survivor.data_classification == "confidential"
        assert survivor.owner_email == "owner@test.com"
        assert survivor.first_seen == NOW - timedelta(days=12)
        assert survivor.last_seen == NOW - timedelta(hours=1)

        events = (
            await s.execute(
                select(func.count()).where(AIUsageEvent.asset_id == dups["oldest"])
            )
        ).scalar_one()
        assert events == 5
        flows = {
            f.direction: f
            for f in (
                await s.execute(select(DataFlow).where(DataFlow.asset_id == dups["oldest"]))
            ).scalars()
        }
        assert flows["input"].event_count == 5
        assert sorted(flows["input"].detected_pii_types) == ["email", "phone"]
        assert flows["input"].data_classification == "restricted"
        assert flows["input"].first_seen == NOW - timedelta(days=12)
        assert flows["input"].last_seen == NOW - timedelta(days=1)
        assert flows["output"].detected_pii_types is None
        risks = (
            await s.execute(select(RiskMapping).where(RiskMapping.asset_id == dups["oldest"]))
        ).scalars().all()
        # The reviewed row beats the survivor's suggestion for the same rule.
        assert sorted((r.risk_category, r.status) for r in risks) == [
            ("bias", "confirmed"),
            ("pii", "confirmed"),
        ]

        audit = (await s.execute(select(RetentionAuditLog))).scalars().one()
        assert audit.action == "merge_assets" and audit.actor == "test"
        assert audit.tenant_id == env["a"]
        assert audit.details["merged"] == {
            str(dups["oldest"]): [str(dups["dup"])],
            str(dups["attested"]): [str(dups["plain"])],
        }

    deliveries = await _deliveries(env)
    assert all(d.asset_id != dups["dup"] for d in deliveries)
    merged_events = {
        d.asset_id: d.payload["data"] for d in deliveries if d.event_type == "asset.changed"
    }
    assert merged_events[dups["oldest"]]["merged_asset_ids"] == [str(dups["dup"])]
    assert "discovery_source" in merged_events[dups["oldest"]]["changed_fields"]
    assert merged_events[dups["attested"]]["merged_asset_ids"] == [str(dups["plain"])]

    # Again: nothing left to merge, nothing written.
    async with await _scoped(env, env["a"]) as s:
        again = await asset_merge.merge_tenant_duplicates(s, env["a"], apply=True)
    assert again.groups == [] and again.counts == {}
    async with env["admin"]() as s:
        assert (await s.execute(select(func.count(RetentionAuditLog.id)))).scalar_one() == 1
    assert len(await _deliveries(env)) == len(deliveries)


async def test_second_attested_duplicate_is_skipped(env, dups):
    async with env["admin"]() as s:
        s.add(
            AssetAttestation(
                tenant_id=env["a"], asset_id=dups["plain"], field="asset", state="attested",
                attested_by="y@test.com", actor_type="tenant",
            )
        )
        await s.commit()
    async with await _scoped(env, env["a"]) as s:
        result = await asset_merge.merge_tenant_duplicates(s, env["a"], apply=True)
    groups = {g.survivor: g for g in result.groups}
    assert dups["plain"] not in groups and dups["attested"] not in groups
    async with env["admin"]() as s:
        ids = set((await s.execute(select(AIAsset.id))).scalars())
    assert {dups["plain"], dups["attested"]} <= ids


async def test_cli_merges_every_tenant_under_its_own_context(seeded_db, env, dups):
    url = seeded_db.url.render_as_string(hide_password=False)
    dry = await dedup._run_merge(None, False, url)
    assert {r["tenant_id"] for r in dry if r["groups"]} == {str(env["a"]), str(env["b"])}
    applied = await dedup._run_merge([env["b"]], True, url)
    assert applied[0]["groups"][0]["survivor"] == str(dups["b_old"])
    async with env["admin"]() as s:
        ids = set((await s.execute(select(AIAsset.id))).scalars())
        audit = (await s.execute(select(RetentionAuditLog))).scalars().one()
    assert dups["b_new"] not in ids and dups["dup"] in ids
    assert audit.tenant_id == env["b"]


def test_cli_parses_dry_run_by_default(monkeypatch, capsys):
    seen = {}

    async def fake(tenants, apply, url):
        seen.update(tenants=tenants, apply=apply)
        return []

    monkeypatch.setattr(dedup, "_run_merge", fake)
    tenant = uuid.uuid4()
    assert dedup.main(["merge-duplicates", "--tenant", str(tenant)]) == 0
    assert seen == {"tenants": [tenant], "apply": False}
    dedup.main(["merge-duplicates", "--apply"])
    assert seen == {"tenants": None, "apply": True}
    with pytest.raises(SystemExit):
        dedup.main(["merge-duplicates", "--apply", "--dry-run"])


# Every table with a foreign key to ai_assets.id, and what the merge does
# with its rows. A new reference has to be handled in asset_merge (re-point
# it, or fail the delete) before this list changes.
ASSET_REFERENCES = {
    "ai_usage_events": "re-pointed",
    "data_flows": "folded",
    "risk_mappings": "moved",
    "asset_attestations": "survivor only (append-only)",
}


async def test_every_asset_reference_survives_a_merge(env):
    """Rows in every table that references an asset, on both members of a
    duplicate pair: after the merge each still exists, now on the survivor;
    nothing pointed at the duplicate is lost or left dangling."""
    a = env["a"]
    async with env["admin"]() as s:
        fks = (
            await s.execute(
                text(
                    """
                    SELECT tc.table_name, rc.delete_rule
                    FROM information_schema.referential_constraints rc
                    JOIN information_schema.table_constraints tc
                      ON tc.constraint_name = rc.constraint_name
                    JOIN information_schema.constraint_column_usage ccu
                      ON ccu.constraint_name = rc.unique_constraint_name
                    WHERE ccu.table_name = 'ai_assets' AND ccu.column_name = 'id'
                    """
                )
            )
        ).all()
        # No cascade could delete a duplicate's rows behind the merge's back.
        assert {t for t, _ in fks} == set(ASSET_REFERENCES)
        assert {rule for _, rule in fks} == {"NO ACTION"}

        survivor = _asset(a, age_days=3, use_case_name="refs")
        dup = _asset(a, age_days=2, use_case_name="refs")
        s.add_all([survivor, dup])
        await s.flush()
        partner = (await s.execute(select(PartnerCredential.id))).scalar_one()
        for i, asset in enumerate((survivor, dup)):
            s.add_all(
                [
                    AIUsageEvent(tenant_id=a, asset_id=asset.id, source="sdk", tokens_in=1),
                    DataFlow(
                        tenant_id=a, asset_id=asset.id, source_system=f"src{i}",
                        destination_system="openai/gpt-4o", direction="input", event_count=1,
                    ),
                    RiskMapping(
                        tenant_id=a, asset_id=asset.id, risk_category=f"c{i}",
                        risk_level="low", rule_id=f"rule{i}", status="suggested",
                        source="rules",
                    ),
                    RiskMapping(  # hand-entered, no rule id
                        tenant_id=a, asset_id=asset.id, risk_category="manual",
                        risk_level="high",
                    ),
                    WebhookDelivery(
                        tenant_id=a, subscription_id=env["sub"], event_id=uuid.uuid4(),
                        event_type="asset.created", asset_id=asset.id,
                        payload={"data": {"asset": {"id": str(asset.id)}}},
                    ),
                    PartnerAuditLog(
                        tenant_id=a, partner_id=partner, endpoint="/partner/v1/assets",
                        method="GET", status_code=200, asset_ids=[asset.id],
                    ),
                ]
            )
        s.add(
            AssetAttestation(
                tenant_id=a, asset_id=survivor.id, field="owner", state="attested",
                attested_by="x@test.com", actor_type="tenant",
            )
        )
        await s.commit()
        survivor, dup = survivor.id, dup.id

    async def rows(s, model):
        return (await s.execute(select(func.count()).select_from(model))).scalar_one()

    tables = (AIUsageEvent, DataFlow, RiskMapping, AssetAttestation, PartnerAuditLog)
    async with env["admin"]() as s:
        before = {m.__tablename__: await rows(s, m) for m in tables}
        before_deliveries = await rows(s, WebhookDelivery)

    async with await _scoped(env, a) as s:
        result = await asset_merge.merge_tenant_duplicates(s, a, apply=True)
    assert [(g.survivor, g.merged) for g in result.groups] == [(survivor, [dup])]

    async with env["admin"]() as s:
        assert await s.get(AIAsset, dup) is None
        assert {m.__tablename__: await rows(s, m) for m in tables} == before
        # Plus the merge's own asset.changed.
        assert await rows(s, WebhookDelivery) == before_deliveries + 1
        for model in (AIUsageEvent, DataFlow, RiskMapping, AssetAttestation, WebhookDelivery):
            on_dup = select(func.count()).where(model.asset_id == dup)
            assert (await s.execute(on_dup)).scalar_one() == 0, model.__tablename__
        assert (
            await s.execute(
                select(func.count()).where(AIUsageEvent.asset_id == survivor)
            )
        ).scalar_one() == 2
        assert (
            await s.execute(select(func.count()).where(DataFlow.asset_id == survivor))
        ).scalar_one() == 2
        assert (
            await s.execute(select(func.count()).where(RiskMapping.asset_id == survivor))
        ).scalar_one() == 4
        # What partners read stays as it was read.
        audit_ids = (await s.execute(select(PartnerAuditLog.asset_ids))).scalars().all()
        assert sorted(audit_ids) == sorted([[survivor], [dup]])


# ──────────────────────────────────────────────────────────────────────
# Oldest-match ordering
# ──────────────────────────────────────────────────────────────────────


async def test_backdated_platform_signal_asset_does_not_take_over_a_key(env):
    event = {"vendor": "openai", "model": "gpt-4o-mini", "source": "sdk"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/ingest/events", json={"events": [event]}, headers=KEY_A)
        assert r.status_code == 200, r.text
        async with env["admin"]() as s:
            ingest_id = (await s.execute(select(AIAsset.id))).scalar_one()

        # The usage pull creates a platform_signal asset for the same model
        # whose first_seen is backdated to its earliest bucket, before the
        # ingest asset's.
        record = pu.UsageRecord(
            provider="openai", endpoint="completions",
            bucket_start=NOW - timedelta(days=6), bucket_end=NOW - timedelta(days=5),
            project_id="p", api_key_id="k", model="gpt-4o-mini",
            tokens_in=1, tokens_out=1, requests=1,
        )
        async with await _scoped(env, env["a"]) as s:
            await pu.store_usage(s, env["a"], [record])
            await s.commit()

        r = await c.post("/ingest/events", json={"events": [event]}, headers=KEY_A)
        assert r.status_code == 200, r.text
    async with env["admin"]() as s:
        counts = dict(
            (
                await s.execute(
                    select(AIUsageEvent.asset_id, func.count()).group_by(AIUsageEvent.asset_id)
                )
            ).all()
        )
    assert counts[ingest_id] == 2


# ──────────────────────────────────────────────────────────────────────
# Provider usage and cloud inventory sightings
# ──────────────────────────────────────────────────────────────────────


def _record(day_offset: float) -> pu.UsageRecord:
    start = NOW + timedelta(days=day_offset)
    return pu.UsageRecord(
        provider="openai", endpoint="completions",
        bucket_start=start, bucket_end=start + timedelta(hours=1),
        project_id="proj", api_key_id="key", model="gpt-4o",
        tokens_in=10, tokens_out=5, requests=1,
    )


async def _owner_retired_usage_asset(env, retired_days_ago: float) -> uuid.UUID:
    aid = pu.asset_id_for(env["a"], _record(-1))
    async with env["admin"]() as s:
        s.add(
            _asset(
                env["a"], age_days=20, id=aid, discovery_source=["platform_signal"],
                lifecycle_state="retired", retired_by="owner@test.com",
                lifecycle_changed_at=NOW - timedelta(days=retired_days_ago),
            )
        )
        await s.commit()
    return aid


async def test_usage_after_owner_retirement_raises_retired_in_use(env):
    aid = await _owner_retired_usage_asset(env, retired_days_ago=3)
    async with await _scoped(env, env["a"]) as s:
        await pu.store_usage(s, env["a"], [_record(-1)])
        await s.commit()
    async with env["admin"]() as s:
        asset = await s.get(AIAsset, aid)
        assert asset.retired_in_use_at is not None
        assert asset.lifecycle_state == "retired"
    deliveries = await _deliveries(env)
    assert [(d.event_type, d.payload["data"]["changed_fields"]) for d in deliveries] == [
        ("asset.changed", ["retired_in_use_at"])
    ]


async def test_usage_from_before_owner_retirement_is_not_in_use(env):
    aid = await _owner_retired_usage_asset(env, retired_days_ago=3)
    async with await _scoped(env, env["a"]) as s:
        await pu.store_usage(s, env["a"], [_record(-5)])
        await s.commit()
    async with env["admin"]() as s:
        assert (await s.get(AIAsset, aid)).retired_in_use_at is None
    assert await _deliveries(env) == []


async def test_new_usage_reactivates_a_stale_usage_asset(env):
    aid = pu.asset_id_for(env["a"], _record(-1))
    async with env["admin"]() as s:
        s.add(
            _asset(
                env["a"], age_days=20, id=aid, discovery_source=["platform_signal"],
                lifecycle_state="stale", lifecycle_changed_at=NOW - timedelta(days=2),
            )
        )
        await s.commit()
    async with await _scoped(env, env["a"]) as s:
        await pu.store_usage(s, env["a"], [_record(-1)])
        await s.commit()
    async with env["admin"]() as s:
        assert (await s.get(AIAsset, aid)).lifecycle_state == "active"
    assert [d.payload["data"]["changed_fields"] for d in await _deliveries(env)] == [
        ["lifecycle_state"]
    ]


def _deployment(name: str) -> ci.CloudDeployment:
    return ci.CloudDeployment(
        cloud="azure", resource_id=f"/subscriptions/s/deployments/{name}",
        region="westeurope", model="gpt-4o", model_format="OpenAI",
    )


async def test_cloud_sighting_of_owner_retired_asset_sends_webhook(env):
    d = _deployment("retired")
    aid = ci.asset_id_for(env["a"], d)
    async with env["admin"]() as s:
        s.add(
            _asset(
                env["a"], age_days=20, id=aid, vendor="microsoft",
                discovery_source=["platform_signal"], lifecycle_state="retired",
                retired_by="owner@test.com", lifecycle_changed_at=NOW - timedelta(days=1),
            )
        )
        await s.commit()
    async with await _scoped(env, env["a"]) as s:
        await ci.store_inventory(s, env["a"], [d], NOW)
        await s.commit()
    async with env["admin"]() as s:
        assert (await s.get(AIAsset, aid)).retired_in_use_at is not None
    assert [(d.event_type, d.payload["data"]["changed_fields"]) for d in await _deliveries(env)] == [
        ("asset.changed", ["retired_in_use_at"])
    ]


async def test_cloud_inventory_locks_existing_rows_before_writing(env):
    deployments = [_deployment("one"), _deployment("two")]
    ids = [ci.asset_id_for(env["a"], d) for d in deployments]
    async with env["admin"]() as s:
        s.add_all(
            _asset(env["a"], age_days=1, id=i, vendor="microsoft", discovery_source=["platform_signal"])
            for i in ids
        )
        await s.commit()

    holder = await _scoped(env, env["a"])
    try:
        # Reverse listing order: locks still go in id order, all up front.
        await ci.store_inventory(holder, env["a"], list(reversed(deployments)), NOW)
        async with await _scoped(env, env["a"]) as other:
            for i in sorted(ids):
                with pytest.raises(Exception, match="could not obtain lock"):
                    async with other.begin_nested():
                        await other.execute(
                            text("SELECT id FROM ai_assets WHERE id = :id FOR UPDATE NOWAIT"),
                            {"id": i},
                        )
            await other.rollback()
    finally:
        await holder.rollback()
        await holder.close()


async def test_concurrent_pulls_and_ingest_do_not_deadlock(env):
    """Ingest, a usage pull and a cloud inventory run touching the same
    assets at once all finish (none is aborted as a deadlock victim)."""
    record = _record(-1)
    usage_id = pu.asset_id_for(env["a"], record)
    deployments = [_deployment(f"d{i}") for i in range(5)]
    async with env["admin"]() as s:
        s.add(_asset(env["a"], age_days=3, id=usage_id, discovery_source=["platform_signal"]))
        s.add_all(
            _asset(
                env["a"], age_days=3, id=ci.asset_id_for(env["a"], d), vendor="microsoft",
                model="gpt-4o", discovery_source=["platform_signal"],
            )
            for d in deployments
        )
        await s.commit()

    async def pull():
        async with await _scoped(env, env["a"]) as s:
            await pu.store_usage(s, env["a"], [record])
            await s.commit()

    async def inventory(order):
        async with await _scoped(env, env["a"]) as s:
            await ci.store_inventory(s, env["a"], order, NOW)
            await s.commit()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:

        async def ingest():
            r = await c.post(
                "/ingest/events",
                json={"events": [{"vendor": "openai", "model": "gpt-4o", "source": "sdk"}]},
                headers=KEY_A,
            )
            assert r.status_code == 200, r.text

        await asyncio.gather(
            pull(), ingest(), inventory(deployments), inventory(deployments[::-1]), ingest()
        )
