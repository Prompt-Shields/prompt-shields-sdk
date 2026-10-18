"""Provider admin credentials and key fingerprint corroboration (#93).

Unit tests for the sealing helpers first, then the full app against a real
PostgreSQL database (PS_TEST_DATABASE_URL): write-only credential routes
(tenant admin and Partner API), rotation, RLS, the usage pull using stored
credentials with env fallback, and fingerprint corroboration.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
from collector import provider_credentials as pc
from collector import provider_usage as pu
from collector import rls, secret_keyring, webhooks
from collector.app import app
from collector.config import settings
from collector.partner_auth import hash_secret, issue_access_token
from collector.partner_scopes import SCOPES, expand_scopes
from db.api_keys import api_key_lookup_prefix, hash_api_key
from db.models import (
    AIAsset,
    PartnerAuditLog,
    PartnerCredential,
    ProviderCredential,
    Tenant,
)
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

K1 = "1" * 40
K2 = "2" * 40
TENANT_KEY = {"Authorization": "Bearer ps-test-key-12345"}
KEY_B = "ps-test-key-tenant-b"
ADMIN_KEY = "sk-admin-STORED-secret-0001"
ADMIN_KEY_2 = "sk-admin-ROTATED-secret-0002"
ENV_KEY = "sk-admin-ENV-secret-0003"
SDK_KEY = "sk-proj-the-project-key-the-sdk-wraps"
SDK_FP = hashlib.sha256(SDK_KEY.encode()).hexdigest()[:16]

START = datetime(2026, 10, 1, tzinfo=UTC)
END = datetime(2026, 10, 3, tzinfo=UTC)
DAY1 = int(START.timestamp())


@pytest.fixture(autouse=True)
def _keyring(monkeypatch):
    monkeypatch.setattr(settings, "webhook_secret_keys", f"k1:{K1}")
    monkeypatch.setattr(settings, "webhook_secret_key", "")
    monkeypatch.delenv("PS_PROVIDER_ADMIN_KEYS", raising=False)


# ── sealing (unit) ────────────────────────────────────────────────────


def _row(
    token: str | None = None, cred_id: uuid.UUID | None = None
) -> ProviderCredential:
    cred_id = cred_id or uuid.uuid4()
    return ProviderCredential(
        id=cred_id,
        provider="openai",
        secret_ciphertext=token or pc.seal_credential(ADMIN_KEY, cred_id),
        key_hint=pc.key_hint(ADMIN_KEY),
    )


def test_round_trip_names_key_id_and_hides_the_key():
    row = _row()
    assert row.secret_ciphertext.startswith("v2.k1.")
    assert ADMIN_KEY not in row.secret_ciphertext
    assert pc.open_credential(row) == ADMIN_KEY
    out = pc.serialize_credential(row)
    assert out["key_hint"] == "****0001" and out["key_id"] == "k1"
    assert ADMIN_KEY not in repr(out) and row.secret_ciphertext not in repr(out)


def test_token_is_bound_to_row_and_purpose():
    row = _row()
    with pytest.raises(ValueError):
        pc.open_credential(_row(row.secret_ciphertext))  # copied to another row
    # Same keyring, same binding, webhook purpose: does not open.
    hook = webhooks.encrypt_secret(ADMIN_KEY, row.id)
    with pytest.raises(ValueError):
        pc.open_credential(_row(hook, row.id))
    with pytest.raises(ValueError):
        webhooks.decrypt_secret(row.secret_ciphertext, row.id)


def test_keyring_rotation_old_tokens_open_and_rekey(monkeypatch):
    row = _row()
    monkeypatch.setattr(settings, "webhook_secret_keys", f"k2:{K2},k1:{K1}")
    assert pc.open_credential(row) == ADMIN_KEY
    assert pc.rekey_if_needed(row, ADMIN_KEY)
    assert row.secret_ciphertext.startswith("v2.k2.")
    assert not pc.rekey_if_needed(row, ADMIN_KEY)
    monkeypatch.setattr(settings, "webhook_secret_keys", f"k2:{K2}")
    assert pc.open_credential(row) == ADMIN_KEY
    monkeypatch.setattr(settings, "webhook_secret_keys", f"k3:{K1}")
    with pytest.raises(secret_keyring.SecretKeyMissing, match="k2"):
        pc.open_credential(row)


def test_no_keyring_is_not_configured(monkeypatch):
    monkeypatch.setattr(settings, "webhook_secret_keys", "")
    # The legacy single webhook key is not used for provider credentials.
    monkeypatch.setattr(settings, "webhook_secret_key", K1)
    with pytest.raises(pc.CredentialsNotConfigured):
        pc.seal_credential(ADMIN_KEY, uuid.uuid4())


@pytest.mark.parametrize(
    "bad", ["short", "sk-admin with spaces 12345", "sk-ädmin-0123456789012"]
)
def test_invalid_admin_key_message_never_echoes_it(bad):
    with pytest.raises(ValueError) as exc:
        pc.validate_admin_key(bad)
    assert bad not in str(exc.value)


# ── API ───────────────────────────────────────────────────────────────


def _client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _env(engine):
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s:
        tenant = (await s.execute(select(Tenant))).scalar_one()
        other = Tenant(
            name="Other",
            settings={},
            api_key_prefix=api_key_lookup_prefix(KEY_B),
            api_key_hash=hash_api_key(KEY_B),
        )
        manager = PartnerCredential(
            tenant_id=tenant.id,
            partner_name="mgr",
            client_id="mgr-client",
            client_secret_hash=hash_secret("x"),
            scopes=["provider-credentials:manage"],
        )
        reader = PartnerCredential(
            tenant_id=tenant.id,
            partner_name="ro",
            client_id="ro-client",
            client_secret_hash=hash_secret("x"),
            scopes=["registry:read"],
        )
        s.add_all([other, manager, reader])
        await s.commit()
        mgr, _ = issue_access_token(
            tenant.id, manager.id, "mgr-client", ["provider-credentials:manage"]
        )
        ro, _ = issue_access_token(tenant.id, reader.id, "ro-client", ["registry:read"])
        return SimpleNamespace(
            factory=factory,
            tenant_id=tenant.id,
            other_id=other.id,
            mgr={"Authorization": f"Bearer {mgr}"},
            ro={"Authorization": f"Bearer {ro}"},
        )


async def test_tenant_credential_lifecycle_never_returns_the_key(seeded_db):
    env = await _env(seeded_db)
    base = "/api/v1/admin/provider-credentials"
    seen: list[str] = []
    async with _client() as c:
        r = await c.post(
            f"{base}/openai", headers=TENANT_KEY, json={"admin_key": ADMIN_KEY}
        )
        assert r.status_code == 201, r.text
        seen.append(r.text)
        body = r.json()
        assert set(body) == {
            "provider",
            "key_hint",
            "key_id",
            "created_at",
            "rotated_at",
        }
        assert body["key_hint"] == "****0001" and body["key_id"] == "k1"

        r = await c.post(
            f"{base}/openai", headers=TENANT_KEY, json={"admin_key": ADMIN_KEY_2}
        )
        assert r.status_code == 409

        for path in (base, f"{base}/openai"):
            r = await c.get(path, headers=TENANT_KEY)
            assert r.status_code == 200
            seen.append(r.text)

        r = await c.post(
            f"{base}/openai/rotate", headers=TENANT_KEY, json={"admin_key": ADMIN_KEY_2}
        )
        assert r.status_code == 200, r.text
        seen.append(r.text)
        assert r.json()["key_hint"] == "****0002" and r.json()["rotated_at"]

        async with env.factory() as s:
            (row,) = (await s.execute(select(ProviderCredential))).scalars().all()
            assert ADMIN_KEY_2 not in row.secret_ciphertext
            assert pc.open_credential(row) == ADMIN_KEY_2

        # Invalid key: 400, and the response does not echo it.
        r = await c.post(
            f"{base}/anthropic", headers=TENANT_KEY, json={"admin_key": "sk-bad"}
        )
        assert r.status_code == 400 and "sk-bad" not in r.text
        r = await c.post(
            f"{base}/mistral", headers=TENANT_KEY, json={"admin_key": ADMIN_KEY}
        )
        assert r.status_code == 422 and ADMIN_KEY not in r.text

        # Other tenant sees nothing of it.
        r = await c.get(base, headers={"Authorization": f"Bearer {KEY_B}"})
        assert r.json()["data"] == []

        r = await c.delete(f"{base}/openai", headers=TENANT_KEY)
        assert r.status_code == 204
        r = await c.get(f"{base}/openai", headers=TENANT_KEY)
        assert r.status_code == 404
        r = await c.post(
            f"{base}/openai/rotate", headers=TENANT_KEY, json={"admin_key": ADMIN_KEY}
        )
        assert r.status_code == 404

    for text_ in seen:
        assert ADMIN_KEY not in text_ and ADMIN_KEY_2 not in text_
        assert "v2.k1." not in text_  # nor the ciphertext
    async with env.factory() as s:
        audit = (await s.execute(select(PartnerAuditLog))).scalars().all()
        assert audit and all(ADMIN_KEY not in repr(a.query_params) for a in audit)


async def test_store_without_keyring_is_503(seeded_db, monkeypatch):
    monkeypatch.setattr(settings, "webhook_secret_keys", "")
    async with _client() as c:
        r = await c.post(
            "/api/v1/admin/provider-credentials/openai",
            headers=TENANT_KEY,
            json={"admin_key": ADMIN_KEY},
        )
    assert r.status_code == 503


async def test_partner_routes_need_the_scope(seeded_db):
    env = await _env(seeded_db)
    base = "/partner/v1/provider-credentials"
    async with _client() as c:
        r = await c.post(
            f"{base}/anthropic", headers=env.ro, json={"admin_key": ADMIN_KEY}
        )
        assert r.status_code == 403
        r = await c.get(base, headers=env.ro)
        assert r.status_code == 403

        r = await c.post(
            f"{base}/anthropic", headers=env.mgr, json={"admin_key": ADMIN_KEY}
        )
        assert r.status_code == 201, r.text
        assert ADMIN_KEY not in r.text
        r = await c.get(base, headers=env.mgr)
        assert [d["provider"] for d in r.json()["data"]] == ["anthropic"]
        assert ADMIN_KEY not in r.text
        r = await c.delete(f"{base}/anthropic", headers=env.mgr)
        assert r.status_code == 204
    assert "provider-credentials:manage" in SCOPES
    assert "provider-credentials:manage" not in expand_scopes(["registry:read"])


async def test_rls_enforced_isolates_credentials(seeded_db):
    env = await _env(seeded_db)
    admin = async_sessionmaker(
        seeded_db,
        class_=AsyncSession,
        expire_on_commit=False,
        info=rls.bypass_info("test-seed"),
    )
    async with admin() as s:
        for tid in (env.tenant_id, env.other_id):
            await pc.create_credential(s, tid, "openai", ADMIN_KEY)
        await s.commit()
    async with seeded_db.begin() as conn:
        await rls.enable(conn)

    plain = async_sessionmaker(seeded_db, class_=AsyncSession, expire_on_commit=False)
    async with plain() as s:
        assert (
            await s.execute(text("SELECT tenant_id FROM provider_credentials"))
        ).all() == []
        await rls.set_tenant(s, env.tenant_id)
        ids = (
            await s.execute(text("SELECT tenant_id FROM provider_credentials"))
        ).scalars()
        assert set(ids) == {env.tenant_id}

    # The app still works with RLS enforced, for its own tenant only.
    async with _client() as c:
        r = await c.get("/api/v1/admin/provider-credentials", headers=TENANT_KEY)
        assert r.status_code == 200 and len(r.json()["data"]) == 1
        r = await c.put(
            "/api/v1/admin/provider-key-fingerprints/openai/key_a",
            headers=TENANT_KEY,
            json={"api_key_fingerprint": SDK_FP},
        )
        assert r.status_code == 201, r.text


# ── usage pull: stored credential, env fallback, corroboration ───────


def _openai_handler(seen_auth: list[str]):
    def handler(request: httpx.Request) -> httpx.Response:
        seen_auth.append(request.headers["authorization"])
        if request.url.path.endswith("/embeddings"):
            return httpx.Response(200, json={"data": [], "has_more": False})
        results = [
            {
                "input_tokens": 100,
                "output_tokens": 50,
                "num_model_requests": 3,
                "project_id": "proj_a",
                "api_key_id": key,
                "model": "gpt-4o-mini",
            }
            for key in ("key_linked", "key_shadow")
        ]
        return httpx.Response(
            200,
            json={
                "data": [
                    {"start_time": DAY1, "end_time": DAY1 + 86400, "results": results}
                ],
                "has_more": False,
            },
        )

    return handler


async def _no_sleep(_s: float) -> None:
    return None


async def _pull(env, seen_auth, settings_=None):
    async with env.factory() as db:
        return await pu.pull_provider_usage(
            db,
            env.tenant_id,
            start=START,
            end=END,
            providers=["openai"],
            settings=settings_ or pu.ProviderUsageSettings(),
            transport=httpx.MockTransport(_openai_handler(seen_auth)),
            sleep=_no_sleep,
        )


async def test_pull_prefers_stored_credential_and_falls_back_to_env(seeded_db):
    env = await _env(seeded_db)
    env_settings = pu.ProviderUsageSettings(
        provider_admin_keys={str(env.tenant_id): {"openai": ENV_KEY}}
    )
    auth: list[str] = []
    result = await _pull(env, auth, env_settings)
    assert result["providers"]["openai"]["credential_source"] == "env"
    assert set(auth) == {f"Bearer {ENV_KEY}"}

    async with env.factory() as s:
        await pc.create_credential(s, env.tenant_id, "openai", ADMIN_KEY)
        await s.commit()
    auth.clear()
    result = await _pull(env, auth, env_settings)
    assert result["providers"]["openai"]["credential_source"] == "stored"
    assert set(auth) == {f"Bearer {ADMIN_KEY}"}
    assert ADMIN_KEY not in repr(result)


async def test_pull_rekeys_stored_credential_and_reports_unreadable_one(
    seeded_db, monkeypatch
):
    env = await _env(seeded_db)
    async with env.factory() as s:
        await pc.create_credential(s, env.tenant_id, "openai", ADMIN_KEY)
        await s.commit()

    monkeypatch.setattr(settings, "webhook_secret_keys", f"k2:{K2},k1:{K1}")
    await _pull(env, [])
    async with env.factory() as s:
        row = (await s.execute(select(ProviderCredential))).scalar_one()
        assert row.secret_ciphertext.startswith("v2.k2.")

    # The key it is under is gone: an error for that provider, no env fallback.
    monkeypatch.setattr(settings, "webhook_secret_keys", f"k1:{K1}")
    env_settings = pu.ProviderUsageSettings(
        provider_admin_keys={str(env.tenant_id): {"openai": ENV_KEY}}
    )
    auth: list[str] = []
    result = await _pull(env, auth, env_settings)
    assert result["providers"]["openai"]["status"] == "error"
    assert "k2" in result["providers"]["openai"]["error"]
    assert auth == []


async def _ingest_sdk_event(c, fingerprint):
    event = {
        "vendor": "openai",
        "model": "gpt-4o-mini",
        "source": "sdk",
        "use_case_name": "support-bot",
        "tokens_in": 10,
        "api_key_fingerprint": fingerprint,
    }
    r = await c.post("/ingest/events", headers=TENANT_KEY, json={"events": [event]})
    assert r.status_code == 200, r.text


async def _assets(env) -> dict[str, AIAsset]:
    async with env.factory() as s:
        rows = (await s.execute(select(AIAsset))).scalars().all()
    return {(a.calling_service or "sdk").rpartition("key=")[2]: a for a in rows}


async def test_fingerprint_link_corroborates_sdk_and_platform_assets(seeded_db):
    env = await _env(seeded_db)
    async with _client() as c:
        await _ingest_sdk_event(c, SDK_FP)
    await _pull(
        env,
        [],
        pu.ProviderUsageSettings(
            provider_admin_keys={str(env.tenant_id): {"openai": ENV_KEY}}
        ),
    )
    before = await _assets(env)
    assert before["sdk"].confidence == "high"
    assert before["key_linked"].confidence == "medium"
    assert before["key_shadow"].confidence == "medium"

    async with _client() as c:
        r = await c.put(
            "/api/v1/admin/provider-key-fingerprints/openai/key_linked",
            headers=TENANT_KEY,
            json={"api_key_fingerprint": SDK_FP.upper()},
        )
        assert r.status_code == 201, r.text
        assert r.json()["meta"]["corroborated_assets"] == 1
        assert r.json()["data"]["api_key_fingerprint"] == SDK_FP

        r = await c.get("/api/v1/admin/provider-key-fingerprints", headers=TENANT_KEY)
        assert [d["api_key_id"] for d in r.json()["data"]] == ["key_linked"]
        r = await c.put(
            "/api/v1/admin/provider-key-fingerprints/openai/key_linked",
            headers=TENANT_KEY,
            json={"api_key_fingerprint": "sk-proj-not-a-hash"},
        )
        assert r.status_code == 400

    after = await _assets(env)
    assert after["key_linked"].confidence == "verified"
    assert sorted(after["key_linked"].discovery_source) == ["platform_signal", "sdk"]
    assert after["sdk"].confidence == "verified"
    assert sorted(after["sdk"].discovery_source) == ["platform_signal", "sdk"]
    # A key nobody linked stays a medium-confidence shadow key.
    assert after["key_shadow"].confidence == "medium"


async def test_pull_corroborates_when_link_exists_first(seeded_db):
    env = await _env(seeded_db)
    async with _client() as c:
        await _ingest_sdk_event(c, SDK_FP)
        # A different key's fingerprint on the same model does not count.
        await _ingest_sdk_event(c, "fedcba9876543210")
    async with env.factory() as s:
        await pc.put_link(s, env.tenant_id, "openai", "key_linked", SDK_FP)
        await pc.put_link(s, env.tenant_id, "openai", "key_shadow", "0000000000000000")
        await s.commit()
    result = await _pull(
        env,
        [],
        pu.ProviderUsageSettings(
            provider_admin_keys={str(env.tenant_id): {"openai": ENV_KEY}}
        ),
    )
    assert result["corroborated_assets"] == 1
    after = await _assets(env)
    assert after["key_linked"].confidence == "verified"
    assert after["key_shadow"].confidence == "medium"

    # Idempotent: a second pull changes nothing more.
    result = await _pull(
        env,
        [],
        pu.ProviderUsageSettings(
            provider_admin_keys={str(env.tenant_id): {"openai": ENV_KEY}}
        ),
    )
    assert result["corroborated_assets"] == 0


async def test_ingest_drops_a_fingerprint_that_is_not_a_hash(seeded_db):
    env = await _env(seeded_db)
    async with _client() as c:
        await _ingest_sdk_event(c, SDK_KEY)  # a raw key must never be stored
        loss = (await c.get("/telemetry/loss", headers=TENANT_KEY)).json()
    async with env.factory() as s:
        stored = (
            (await s.execute(text("SELECT api_key_fingerprint FROM ai_usage_events")))
            .scalars()
            .all()
        )
    assert stored == [None]
    (stats,) = loss["clients"]
    assert stats["ignored_fields"].get("api_key_fingerprint:invalid") == 1


async def test_erasure_removes_credentials_and_links(seeded_db):
    from collector import retention

    env = await _env(seeded_db)
    async with env.factory() as s:
        await pc.create_credential(s, env.tenant_id, "openai", ADMIN_KEY)
        await pc.put_link(s, env.tenant_id, "openai", "key_x", SDK_FP)
        await s.commit()
    async with env.factory() as s:
        result = await retention.erase_tenant(s, env.other_id)  # other tenant: no-op
        assert result.rows_deleted["provider_credentials"] == 0
        result = await retention.erase_tenant(s, env.tenant_id)
    assert result.rows_deleted["provider_credentials"] == 1
    assert result.rows_deleted["provider_key_fingerprints"] == 1
