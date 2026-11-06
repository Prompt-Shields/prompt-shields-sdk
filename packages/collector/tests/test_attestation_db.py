"""Human attestation (#98) against a real PostgreSQL database
(PS_TEST_DATABASE_URL), through the full app: tenant registry and Partner
API routes, tenant isolation, scopes, immutable history, audit rows and the
delta feed. Unit tests for the pure helpers are at the bottom."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from collector import attestation
from collector.app import app
from collector.embeddings import mock_embedding
from collector.partner_auth import hash_secret, issue_access_token
from db.models import (
    AIAsset,
    AssetAttestation,
    PartnerAuditLog,
    PartnerCredential,
    Tenant,
)
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

TENANT_KEY = {"Authorization": "Bearer ps-test-key-12345"}


async def _seed(engine):
    """Asset in the conftest tenant ("Test Corp") and one in another tenant,
    plus a read-only and a read+write partner for the conftest tenant."""
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s:
        tenant = (await s.execute(select(Tenant).where(Tenant.name == "Test Corp"))).scalar_one()
        other = Tenant(name="Other", settings={})
        s.add(other)
        await s.flush()
        asset = AIAsset(
            tenant_id=tenant.id,
            vendor="openai",
            model="gpt-4o",
            use_case_name="resume screening",
            owner_email="owner@test.com",
            data_classification="confidential",
            confidence="high",
            discovery_source=["sdk", "gateway"],
        )
        foreign = AIAsset(tenant_id=other.id, vendor="anthropic", discovery_source=["sdk"])
        reader = PartnerCredential(
            tenant_id=tenant.id, partner_name="ro", client_id="ro-client",
            client_secret_hash=hash_secret("x"), scopes=["registry:read"],
        )
        writer = PartnerCredential(
            tenant_id=tenant.id, partner_name="rw", client_id="rw-client",
            client_secret_hash=hash_secret("x"),
            scopes=["registry:read", "attestations:write"],
        )
        s.add_all([asset, foreign, reader, writer])
        await s.commit()
        ro, _ = issue_access_token(tenant.id, reader.id, "ro-client", ["registry:read"])
        rw, _ = issue_access_token(
            tenant.id, writer.id, "rw-client", ["registry:read", "attestations:write"]
        )
        return SimpleNamespace(
            factory=factory,
            tenant_id=tenant.id,
            asset_id=asset.id,
            foreign_id=foreign.id,
            writer_id=writer.id,
            ro={"Authorization": f"Bearer {ro}"},
            rw={"Authorization": f"Bearer {rw}"},
        )


def _client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_tenant_attests_and_confidence_is_unchanged(seeded_db):
    env = await _seed(seeded_db)
    async with _client() as c:
        before = (await c.get(f"/api/v1/registry/assets/{env.asset_id}", headers=TENANT_KEY)).json()
        assert before["confidence"] == "high"
        assert before["attestation"]["owner"]["state"] == "unreviewed"
        assert set(before["attestation"]) >= {"asset", "owner", "data_classification"}

        r = await c.post(
            f"/api/v1/registry/assets/{env.asset_id}/attestations",
            headers=TENANT_KEY,
            json={"field": "owner", "state": "attested", "attested_by": "ciso@test.com",
                  "comment": "confirmed in review"},
        )
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["data"]["state"] == "attested"
        assert body["data"]["value"] == "owner@test.com"
        assert body["data"]["attested_by"] == "ciso@test.com"
        assert body["data"]["actor_type"] == "tenant"
        assert body["meta"]["current"]["owner"]["state"] == "attested"
        assert body["meta"]["confidence"] == "high"

        after = (await c.get(f"/api/v1/registry/assets/{env.asset_id}", headers=TENANT_KEY)).json()
        listed = (await c.get("/api/v1/registry/assets", headers=TENANT_KEY)).json()["data"]

    # Attestation never changes confidence; both are shown.
    assert after["confidence"] == "high"
    assert after["attestation"]["owner"]["state"] == "attested"
    assert after["attestation"]["owner"]["attested_by"] == "ciso@test.com"
    assert after["attestation"]["owner"]["value_changed"] is False
    assert after["attestation"]["data_classification"]["state"] == "unreviewed"
    assert listed[0]["attestation"]["owner"]["state"] == "attested"
    assert listed[0]["confidence"] == "high"

    async with env.factory() as s:
        row = (await s.execute(select(AssetAttestation))).scalar_one()
        assert row.api_key_prefix  # the tenant key that made the call, hashed
        assert row.partner_id is None
        audit = (
            await s.execute(
                select(PartnerAuditLog).where(PartnerAuditLog.method == "POST")
            )
        ).scalar_one()
        assert audit.status_code == 201
        assert audit.endpoint == "/api/v1/registry/assets/{asset_id}/attestations"
        assert audit.asset_ids == [env.asset_id]


@pytest.mark.asyncio
async def test_history_is_append_only_and_flags_value_changes(seeded_db):
    env = await _seed(seeded_db)
    url = f"/api/v1/registry/assets/{env.asset_id}/attestations"
    async with _client() as c:
        await c.post(url, headers=TENANT_KEY,
                     json={"field": "owner", "state": "attested", "attested_by": "a@test.com"})
        disputed = await c.post(url, headers=TENANT_KEY,
                                json={"field": "owner", "state": "disputed", "attested_by": "b@test.com"})
        assert disputed.status_code == 400  # a dispute needs a comment
        await c.post(url, headers=TENANT_KEY,
                     json={"field": "owner", "state": "disputed", "attested_by": "b@test.com",
                           "comment": "owner left the company"})

        # The registry value moves away from what was reviewed.
        async with env.factory() as s:
            asset = await s.get(AIAsset, env.asset_id)
            asset.owner_email = "new-owner@test.com"
            await s.commit()

        hist = (await c.get(url, headers=TENANT_KEY)).json()
        only_owner = (await c.get(url + "?field=owner&limit=1", headers=TENANT_KEY)).json()

    assert [h["state"] for h in hist["data"]] == ["disputed", "attested"]
    assert [h["attested_by"] for h in hist["data"]] == ["b@test.com", "a@test.com"]
    current = hist["meta"]["current"]["owner"]
    assert current["state"] == "disputed"
    assert current["comment"] == "owner left the company"
    assert current["value_changed"] is True
    assert hist["meta"]["confidence"] == "high"
    assert len(only_owner["data"]) == 1

    # Rows cannot be rewritten, not even directly in the database.
    async with env.factory() as s:
        with pytest.raises(DBAPIError, match="immutable"):
            await s.execute(text("UPDATE asset_attestations SET state = 'attested'"))
        await s.rollback()
        # Nor deleted (delete + re-insert would rewrite history) ...
        with pytest.raises(DBAPIError, match="append-only"):
            await s.execute(text("DELETE FROM asset_attestations"))
        await s.rollback()
        # ... and erasure of a different tenant does not unlock this one.
        await s.execute(
            text("SELECT set_config('prompt_shields.erasing_tenant', :t, true)"),
            {"t": str(uuid.uuid4())},
        )
        with pytest.raises(DBAPIError, match="append-only"):
            await s.execute(text("DELETE FROM asset_attestations"))
        await s.rollback()
        assert len((await s.execute(select(AssetAttestation))).scalars().all()) == 2


@pytest.mark.asyncio
async def test_tenant_erasure_deletes_attestations(seeded_db):
    from collector.retention import erase_tenant

    env = await _seed(seeded_db)
    async with _client() as c:
        r = await c.post(
            f"/api/v1/registry/assets/{env.asset_id}/attestations",
            headers=TENANT_KEY,
            json={"field": "owner", "state": "attested", "attested_by": "a@test.com"},
        )
        assert r.status_code == 201
    async with env.factory() as s:
        result = await erase_tenant(s, env.tenant_id, batch_size=1)
    assert result.rows_deleted["asset_attestations"] == 1
    assert result.rows_deleted["ai_assets"] == 1
    async with env.factory() as s:
        assert (await s.execute(select(AssetAttestation))).scalars().all() == []
        # The setting is transaction-local: a later plain DELETE is refused again.
        assert (
            await s.execute(text("SELECT current_setting('prompt_shields.erasing_tenant', true)"))
        ).scalar() in (None, "")


@pytest.mark.asyncio
async def test_latest_attestations_accepts_more_ids_than_bind_limit(seeded_db):
    """The export passes every asset id of the tenant in one call."""
    env = await _seed(seeded_db)
    async with _client() as c:
        await c.post(
            f"/api/v1/registry/assets/{env.asset_id}/attestations",
            headers=TENANT_KEY,
            json={"field": "owner", "state": "attested", "attested_by": "a@test.com"},
        )
    ids = [uuid.uuid4() for _ in range(40_000)] + [env.asset_id, env.foreign_id]
    async with env.factory() as s:
        latest = await attestation.latest_attestations(s, env.tenant_id, ids)
    assert set(latest) == {env.asset_id}
    assert latest[env.asset_id]["owner"].attested_by == "a@test.com"


@pytest.mark.asyncio
async def test_bad_field_and_state_are_rejected(seeded_db):
    env = await _seed(seeded_db)
    url = f"/api/v1/registry/assets/{env.asset_id}/attestations"
    async with _client() as c:
        bad_field = await c.post(url, headers=TENANT_KEY,
                                 json={"field": "confidence", "state": "attested", "attested_by": "x"})
        bad_state = await c.post(url, headers=TENANT_KEY,
                                 json={"field": "owner", "state": "verified", "attested_by": "x"})
        no_who = await c.post(url, headers=TENANT_KEY, json={"field": "owner", "state": "attested"})
        no_auth = await c.post(url, json={"field": "owner", "state": "attested", "attested_by": "x"})
    assert bad_field.status_code == 400
    assert "Attestable fields" in bad_field.json()["detail"]["detail"]
    assert bad_state.status_code == 422
    assert no_who.status_code == 422
    assert no_auth.status_code == 401
    async with env.factory() as s:
        assert (await s.execute(select(AssetAttestation))).first() is None


@pytest.mark.asyncio
async def test_tenant_isolation(seeded_db):
    env = await _seed(seeded_db)
    async with _client() as c:
        tenant_post = await c.post(
            f"/api/v1/registry/assets/{env.foreign_id}/attestations",
            headers=TENANT_KEY,
            json={"field": "owner", "state": "attested", "attested_by": "x"},
        )
        tenant_get = await c.get(
            f"/api/v1/registry/assets/{env.foreign_id}/attestations", headers=TENANT_KEY
        )
        partner_post = await c.post(
            f"/partner/v1/assets/{env.foreign_id}/attestations",
            headers=env.rw,
            json={"field": "owner", "state": "attested", "attested_by": "x"},
        )
    assert tenant_post.status_code == 404
    assert tenant_get.status_code == 404
    assert partner_post.status_code == 404
    async with env.factory() as s:
        assert (await s.execute(select(AssetAttestation))).first() is None


@pytest.mark.asyncio
async def test_partner_scopes_and_delta_feed(seeded_db):
    env = await _seed(seeded_db)
    url = f"/partner/v1/assets/{env.asset_id}/attestations"
    since = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    payload = {"field": "data_classification", "state": "attested",
               "attested_by": "dpo@test.com", "comment": "checked against DPIA"}
    async with _client() as c:
        denied = await c.post(url, headers=env.ro, json=payload)
        ok = await c.post(url, headers=env.rw, json=payload)
        read = await c.get(url, headers=env.ro)
        asset = (await c.get(f"/partner/v1/assets/{env.asset_id}", headers=env.ro)).json()
        changes = (
            await c.get("/partner/v1/changes", headers=env.ro, params={"since": since})
        ).json()
        export = (await c.get("/partner/v1/export", headers=env.ro)).json()

    assert denied.status_code == 403
    assert ok.status_code == 201, ok.text
    assert ok.json()["data"]["actor_type"] == "partner"
    assert ok.json()["data"]["partner_id"] == str(env.writer_id)
    assert ok.json()["data"]["value"] == "confidential"
    assert read.status_code == 200
    assert read.json()["data"][0]["attested_by"] == "dpo@test.com"

    assert asset["confidence"] == "high"
    assert asset["attestation"]["data_classification"]["state"] == "attested"

    entry = next(d for d in changes["data"] if d["asset"]["id"] == str(env.asset_id))
    assert entry["attestation_changes"] == ["data_classification"]
    assert entry["asset"]["attestation"]["data_classification"]["attested_by"] == "dpo@test.com"
    assert export["data"][0]["attestation"]["data_classification"]["state"] == "attested"

    async with env.factory() as s:
        posts = (
            await s.execute(
                select(PartnerAuditLog.status_code, PartnerAuditLog.partner_id).where(
                    PartnerAuditLog.method == "POST"
                )
            )
        ).all()
    assert (201, env.writer_id) in posts


# ──────────────────────────────────────────────────────────────────────
# Pure helpers (no database)
# ──────────────────────────────────────────────────────────────────────


def _asset(**kw):
    base = {
        "vendor": "openai", "model": "gpt-4o", "use_case_name": None, "business_unit": None,
        "environment": None, "owner_email": "o@x.com", "data_classification": None,
    }
    base.update(kw)
    return SimpleNamespace(**base)


def test_validate():
    attestation.validate("owner", "attested", None)
    with pytest.raises(ValueError, match="Unknown field"):
        attestation.validate("nope", "attested", None)
    with pytest.raises(ValueError, match="Unknown state"):
        attestation.validate("owner", "verified", None)
    with pytest.raises(ValueError, match="comment is required"):
        attestation.validate("owner", "disputed", "   ")


def test_registered_field_is_attestable_and_summarized():
    attestation.register_attestable_field("test_only_field", lambda a: a.vendor)
    try:
        assert "test_only_field" in attestation.attestable_fields()
        summary = attestation.summarize(_asset(), {})
        assert summary["test_only_field"]["state"] == "unreviewed"
        row = SimpleNamespace(
            state="attested", attested_by="x", comment=None, value="anthropic",
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        summary = attestation.summarize(_asset(), {"test_only_field": row})
        assert summary["test_only_field"]["value_changed"] is True
        assert attestation.attestation_changes_since(
            summary, datetime(2025, 12, 31, tzinfo=UTC)
        ) == ["test_only_field"]
        assert attestation.attestation_changes_since(
            summary, datetime(2026, 1, 2, tzinfo=UTC)
        ) == []
    finally:
        attestation._FIELDS.pop("test_only_field")


def test_asset_snapshot_is_json_normalised():
    value = attestation.current_value(_asset(environment=uuid.UUID(int=1)), "asset")
    assert value["environment"] == str(uuid.UUID(int=1))


def test_write_scope_is_opt_in():
    from collector.partner_scopes import DEFAULT_SCOPES, SCOPES, expand_scopes

    assert "attestations:write" in SCOPES
    assert "attestations:write" not in DEFAULT_SCOPES
    assert "attestations:write" not in expand_scopes(["registry:read"])


# ──────────────────────────────────────────────────────────────────────
# Review queue filter, search and AI-BOM (#98, second slice)
# ──────────────────────────────────────────────────────────────────────


async def _second_asset(env) -> uuid.UUID:
    async with env.factory() as s:
        other = AIAsset(
            tenant_id=env.tenant_id, vendor="anthropic", owner_email="b@test.com",
            confidence="verified", discovery_source=["sdk", "gateway"],
            embedding=mock_embedding("contract review"),
        )
        s.add(other)
        await s.commit()
        return other.id


@pytest.mark.asyncio
async def test_list_filters_by_attestation_state(seeded_db):
    env = await _seed(seeded_db)
    second = await _second_asset(env)
    attest_url = f"/api/v1/registry/assets/{env.asset_id}/attestations"

    async def ids(c, path, headers, field, state):
        r = await c.get(path, headers=headers,
                        params={"attestation_field": field, "attestation_state": state})
        assert r.status_code == 200, r.text
        return {a["id"] for a in r.json()["data"]}, r.json()["meta"]["total"]

    async with _client() as c:
        await c.post(attest_url, headers=TENANT_KEY,
                     json={"field": "owner", "state": "attested", "attested_by": "a@test.com"})
        await c.post(attest_url, headers=TENANT_KEY,
                     json={"field": "owner", "state": "disputed", "attested_by": "b@test.com",
                           "comment": "left the company"})
        for path, headers in (("/api/v1/registry/assets", TENANT_KEY),
                              ("/partner/v1/assets", env.ro)):
            # The newest decision counts: disputed, not attested.
            assert await ids(c, path, headers, "owner", "disputed") == ({str(env.asset_id)}, 1)
            assert await ids(c, path, headers, "owner", "attested") == (set(), 0)
            # Never attested counts as unreviewed.
            assert await ids(c, path, headers, "owner", "unreviewed") == ({str(second)}, 1)
            assert (await ids(c, path, headers, "data_classification", "unreviewed"))[1] == 2

        tenant_bad = [
            await c.get("/api/v1/registry/assets", headers=TENANT_KEY, params=p)
            for p in ({"attestation_field": "owner"},
                      {"attestation_state": "attested"},
                      {"attestation_field": "confidence", "attestation_state": "attested"})
        ]
        partner_bad = await c.get("/partner/v1/assets", headers=env.ro,
                                  params={"attestation_field": "nope",
                                          "attestation_state": "attested"})
        bad_state = await c.get("/partner/v1/assets", headers=env.ro,
                                params={"attestation_field": "owner",
                                        "attestation_state": "verified"})
    assert [r.status_code for r in tenant_bad] == [400, 400, 400]
    assert "together" in tenant_bad[0].json()["detail"]["detail"]
    assert partner_bad.status_code == 400
    assert bad_state.status_code == 422
    async with env.factory() as s:
        refused = (
            await s.execute(
                select(PartnerAuditLog.endpoint).where(PartnerAuditLog.status_code == 400)
            )
        ).scalars().all()
    assert "/partner/v1/assets" in refused


async def _no_embedding(_text):
    return None


@pytest.mark.asyncio
async def test_search_results_carry_attestation(seeded_db, monkeypatch):
    from collector import registry

    env = await _seed(seeded_db)
    second = await _second_asset(env)
    async with _client() as c:
        await c.post(f"/api/v1/registry/assets/{second}/attestations", headers=TENANT_KEY,
                     json={"field": "owner", "state": "attested", "attested_by": "a@test.com"})
        semantic = (await c.get("/api/v1/registry/search", headers=TENANT_KEY,
                                params={"q": "contract review", "use_mock": "true"})).json()
        monkeypatch.setattr(registry, "get_embedding", _no_embedding)
        keyword = (await c.get("/api/v1/registry/search", headers=TENANT_KEY,
                               params={"q": "anthropic"})).json()
    assert semantic["meta"]["mode"] == "semantic"
    hit = next(a for a in semantic["data"] if a["id"] == str(second))
    assert hit["attestation"]["owner"]["state"] == "attested"
    assert hit["confidence"] == "verified"
    assert keyword["meta"]["mode"] == "keyword"
    assert keyword["data"][0]["attestation"]["owner"]["attested_by"] == "a@test.com"
    assert keyword["data"][0]["attestation"]["asset"]["state"] == "unreviewed"


@pytest.mark.asyncio
async def test_aibom_export_carries_attestation_next_to_confidence(seeded_db):
    env = await _seed(seeded_db)
    async with _client() as c:
        await c.post(f"/partner/v1/assets/{env.asset_id}/attestations", headers=env.rw,
                     json={"field": "owner", "state": "attested", "attested_by": "ciso@test.com",
                           "comment": "not exported"})
        r = await c.get("/partner/v1/export", headers=env.ro, params={"format": "cyclonedx"})
    assert r.status_code == 200, r.text
    comp = next(
        c for c in r.json()["components"]
        if c.get("bom-ref") == f"promptshields:asset:{env.asset_id}"
    )
    props = {p["name"]: p["value"] for p in comp["properties"]}
    assert props["promptshields:confidence"] == "high"
    assert props["promptshields:attestation:owner"] == "attested"
    assert props["promptshields:attestation:owner:attested_by"] == "ciso@test.com"
    assert props["promptshields:attestation:owner:value_changed"] == "false"
    assert "promptshields:attestation:owner:attested_at" in props
    assert props["promptshields:attestation:data_classification"] == "unreviewed"
    assert "promptshields:attestation:data_classification:attested_at" not in props
    assert "not exported" not in r.text
