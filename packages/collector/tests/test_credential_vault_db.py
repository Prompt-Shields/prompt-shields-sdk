"""Inference credential vault (#342) against a real PostgreSQL database
(PS_TEST_DATABASE_URL).

Covers: the write-only admin routes (keys never in a response, error, log
line, audit row or webhook payload), scopes and roles (an ingest key and a
developer never get in), envelope encryption (per-row data key, ciphertext
bound to its row), the internal ``get_credential`` API and its use audit,
KEK rotation (local keyring, local to Azure Key Vault, Azure key versions)
rewrapping every data key without touching the ciphertext, the CLI, tenant
isolation with row-level security enforced and the migration's scope
backfill. Azure Key Vault is the in-process mock from tests/test_kms.py.
"""

from __future__ import annotations

import base64
import importlib.util
import logging
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from collector import audit_trail, kms, rls
from collector import credential_vault as cv
from collector.admin_auth import ADMIN_SCOPES, TENANT_KEY_SCOPES
from collector.app import app
from collector.config import settings
from db.api_keys import api_key_lookup_prefix, hash_api_key
from db.models import AdminToken, AuditEvent, InferenceCredential, Tenant
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.test_kms import KEY_URL, V1, V2, FakeKeyVault, azure_provider

pytestmark = pytest.mark.scoped_admin_default

TENANT_KEY = "ps-test-key-12345"
KEY_B = "ps-test-key-tenant-b"
OP_TOKEN = "op-secret-token"
K1 = "1" * 40
K2 = "2" * 40
API_KEY = "sk-proj-INFERENCE-secret-0001"
API_KEY_2 = "sk-proj-ROTATED-secret-0002"
BASE = "/api/v1/admin/inference-credentials"
ACTOR = audit_trail.system_actor("vault-test")


def H(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    monkeypatch.setattr(settings, "admin_token", OP_TOKEN)
    monkeypatch.setattr(settings, "credential_kek_provider", "local")
    monkeypatch.setattr(settings, "credential_kek_keys", f"k1:{K1}")
    audit_trail.auth_failure_limiter.reset()
    kms.reset()
    yield
    kms.reset()


def _admin(engine):
    return async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False, info=rls.bypass_info("test")
    )


async def _tenant_id(engine, name="Test Corp") -> uuid.UUID:
    async with _admin(engine)() as s:
        return (await s.execute(select(Tenant.id).where(Tenant.name == name))).scalar_one()


async def _add_tenant_b(engine) -> uuid.UUID:
    async with _admin(engine)() as s:
        b = Tenant(
            name="Other Corp",
            settings={},
            api_key_prefix=api_key_lookup_prefix(KEY_B),
            api_key_hash=hash_api_key(KEY_B),
        )
        s.add(b)
        await s.commit()
        return b.id


async def _token(c, tenant_id, scopes) -> str:
    resp = await c.post(
        f"/api/v1/operator/tenants/{tenant_id}/admin-tokens",
        headers=H(OP_TOKEN),
        json={"name": f"t-{uuid.uuid4().hex[:6]}", "scopes": scopes},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["token"]


@pytest.fixture
async def env(seeded_db):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        tid = await _tenant_id(seeded_db)
        tok = await _token(c, tid, ["admin:inference-credentials"])
        yield {"c": c, "engine": seeded_db, "tid": tid, "tok": tok, "f": _admin(seeded_db)}


async def _create(c, tok, provider="openai", label="prod", key=API_KEY, status=201, **extra):
    resp = await c.post(
        BASE, headers=H(tok), json={"provider": provider, "label": label, "api_key": key, **extra}
    )
    assert resp.status_code == status, resp.text
    return resp.json()


async def _rows(f) -> list[InferenceCredential]:
    async with f() as s:
        return list(
            (await s.execute(select(InferenceCredential).order_by(InferenceCredential.label)))
            .scalars()
        )


async def _all_text(f) -> str:
    async with f() as s:
        parts = [
            *(await s.execute(text("SELECT row_to_json(a)::text FROM audit_events a"))).scalars(),
            *(await s.execute(text("SELECT payload::text FROM webhook_deliveries"))).scalars(),
            *(
                await s.execute(text("SELECT row_to_json(a)::text FROM partner_audit_log a"))
            ).scalars(),
        ]
        return "\n".join(parts)


# ── write-only lifecycle ──────────────────────────────────────────────


async def test_lifecycle_never_returns_logs_or_audits_the_key(env, caplog):
    caplog.set_level(logging.DEBUG)
    c, tok, f = env["c"], env["tok"], env["f"]
    seen: list[str] = []
    future = (datetime.now(UTC) + timedelta(days=30)).isoformat()
    body = await _create(c, tok, expires_at=future)
    seen.append(str(body))
    assert set(body) == {
        "id",
        "provider",
        "label",
        "status",
        "key_hint",
        "key_fingerprint",
        "kek_provider",
        "kek_ref",
        "expires_at",
        "disabled_at",
        "created_at",
        "updated_at",
        "rotated_at",
        "rewrapped_at",
        "last_used_at",
    }
    assert body["key_hint"] == "****0001" and body["status"] == "active"
    assert body["key_fingerprint"] == cv.key_fingerprint(API_KEY)
    assert (body["kek_provider"], body["kek_ref"]) == ("local", "local:k1")
    cid = body["id"]

    # Same provider and label: conflict. Another label: fine.
    await _create(c, tok, status=409)
    other = await _create(c, tok, label="batch")
    for path in (BASE, f"{BASE}/{cid}", f"{BASE}?provider=openai"):
        r = await c.get(path, headers=H(tok))
        assert r.status_code == 200, r.text
        seen.append(r.text)
    assert {d["label"] for d in (await c.get(BASE, headers=H(tok))).json()["data"]} == {
        "prod",
        "batch",
    }

    r = await c.post(f"{BASE}/{cid}/rotate", headers=H(tok), json={"api_key": API_KEY_2})
    assert r.status_code == 200, r.text
    seen.append(r.text)
    assert r.json()["key_hint"] == "****0002" and r.json()["rotated_at"]
    assert r.json()["expires_at"] == body["expires_at"]  # kept

    r = await c.patch(f"{BASE}/{cid}", headers=H(tok), json={"disabled": True, "label": "old"})
    assert r.status_code == 200 and r.json()["status"] == "disabled"
    r = await c.patch(f"{BASE}/{cid}", headers=H(tok), json={"label": "batch"})
    assert r.status_code == 409
    r = await c.patch(f"{BASE}/{cid}", headers=H(tok), json={"disabled": False})
    assert r.json()["status"] == "active" and r.json()["label"] == "old"

    assert (await c.delete(f"{BASE}/{other['id']}", headers=H(tok))).status_code == 204
    assert (await c.get(f"{BASE}/{other['id']}", headers=H(tok))).status_code == 404

    for text_ in seen:
        assert API_KEY not in text_ and API_KEY_2 not in text_
    rows = await _rows(f)
    assert len(rows) == 1
    for col in (rows[0].sealed_key, rows[0].wrapped_dek, rows[0].kek_ref):
        assert API_KEY not in col and API_KEY_2 not in col
    assert API_KEY not in caplog.text and API_KEY_2 not in caplog.text
    everything = await _all_text(f)
    assert API_KEY not in everything and API_KEY_2 not in everything and tok not in everything

    async with f() as s:
        audit = (
            await s.execute(
                select(AuditEvent)
                .where(AuditEvent.tenant_id == env["tid"], AuditEvent.action.like("inference%"))
                .order_by(AuditEvent.seq)
            )
        ).scalars().all()
        assert [(a.action, a.outcome, a.status_code) for a in audit] == [
            ("inference_credential.create", "success", 201),
            ("inference_credential.create", "failure", 409),
            ("inference_credential.create", "success", 201),
            ("inference_credential.rotate", "success", 200),
            ("inference_credential.update", "success", 200),
            ("inference_credential.update", "failure", 409),
            ("inference_credential.update", "success", 200),
            ("inference_credential.delete", "success", 204),
        ]
        assert audit[0].actor_type == "admin_token" and audit[0].target_id == cid
        assert audit[0].details == {"provider": "openai"}
        assert audit[4].details == {"fields": ["disabled", "label"]}
        assert (await audit_trail.verify_chain(s, env["tid"])).ok


@pytest.mark.parametrize(
    "payload",
    [
        {"provider": "openai", "label": "x", "api_key": f"has space {API_KEY}"},
        {"provider": "openai", "label": "x", "api_key": "short"},
        {"provider": "openai", "label": "x", "api_key": [API_KEY]},
        {"provider": "openai", "label": "x", "api_key": {"k": API_KEY}},
        {"provider": "openai", "label": "x", "apikey": API_KEY},
        {"provider": "openai", "label": "x", "api_key": API_KEY, API_KEY: "x"},
        {"provider": API_KEY.upper() + " !", "label": "x", "api_key": API_KEY},
        {"provider": "openai", "label": "x", "api_key": API_KEY, "expires_at": API_KEY},
        {"provider": "openai", "label": "x", "api_key": API_KEY, "expires_at": "2001-01-01T00:00:00Z"},
    ],
)
async def test_validation_errors_never_echo_the_key(env, payload, caplog):
    caplog.set_level(logging.DEBUG)
    c, tok = env["c"], env["tok"]
    r = await c.post(BASE, headers=H(tok), json=payload)
    assert r.status_code in (400, 422), r.text
    assert API_KEY not in r.text and API_KEY.upper() not in r.text
    assert API_KEY not in caplog.text
    assert API_KEY not in await _all_text(env["f"])
    assert await _rows(env["f"]) == []


async def test_rotate_validation_and_unknown_ids(env):
    c, tok = env["c"], env["tok"]
    cid = (await _create(c, tok))["id"]
    r = await c.post(f"{BASE}/{cid}/rotate", headers=H(tok), json={"api_key": "bad key " + API_KEY_2})
    assert r.status_code == 400 and API_KEY_2 not in r.text
    missing = uuid.uuid4()
    assert (await c.get(f"{BASE}/{missing}", headers=H(tok))).status_code == 404
    r = await c.post(f"{BASE}/{missing}/rotate", headers=H(tok), json={"api_key": API_KEY_2})
    assert r.status_code == 404
    assert (await c.get(f"{BASE}/not-a-uuid", headers=H(tok))).status_code == 422


async def test_503_without_a_key_provider(env, monkeypatch):
    monkeypatch.setattr(settings, "credential_kek_keys", "")
    # Not the webhook keyring: setting it does not make the vault work.
    monkeypatch.setattr(settings, "webhook_secret_keys", f"k1:{K1}")
    r = await env["c"].post(
        BASE, headers=H(env["tok"]), json={"provider": "openai", "label": "x", "api_key": API_KEY}
    )
    assert r.status_code == 503 and "PS_CREDENTIAL_KEK_KEYS" in r.text
    assert API_KEY not in r.text


# ── scopes and roles ──────────────────────────────────────────────────


async def test_scopes_ingest_keys_and_roles(env, monkeypatch):
    c, tid = env["c"], env["tid"]
    assert "admin:inference-credentials" in ADMIN_SCOPES
    assert "admin:inference-credentials" not in TENANT_KEY_SCOPES
    body = {"provider": "openai", "label": "x", "api_key": API_KEY}
    # A tenant ingest key cannot write, even with scoped tokens not required
    # (its admin:read still lists masked metadata, like every admin GET).
    monkeypatch.setattr(settings, "admin_require_scoped_tokens", False)
    assert (await c.post(BASE, headers=H(TENANT_KEY), json=body)).status_code == 403
    monkeypatch.setattr(settings, "admin_require_scoped_tokens", True)
    assert (await c.get(BASE, headers=H(TENANT_KEY))).status_code == 403
    monkeypatch.setattr(settings, "admin_require_scoped_tokens", True)
    reader = await _token(c, tid, ["admin:read"])
    creds = await _token(c, tid, ["admin:provider-credentials"])
    assert (await c.get(BASE, headers=H(reader))).status_code == 200
    assert (await c.post(BASE, headers=H(reader), json=body)).status_code == 403
    assert (await c.get(BASE, headers=H(creds))).status_code == 403

    # Roles: admin holds the scope, developer does not.
    boot = await _token(c, tid, list(ADMIN_SCOPES))
    for role, expected in (("admin", 201), ("developer", 403), ("viewer", 403)):
        p = (
            await c.post(
                "/api/v1/admin/principals",
                headers=H(boot),
                json={"kind": "user", "display_name": role},
            )
        ).json()
        r = await c.post(
            "/api/v1/admin/memberships",
            headers=H(boot),
            json={"principal_id": p["id"], "role": role},
        )
        assert r.status_code == 201, r.text
        r = await c.post(
            "/api/v1/admin/tokens",
            headers=H(boot),
            json={"name": f"{role}-tok", "scopes": list(ADMIN_SCOPES), "principal_id": p["id"]},
        )
        assert r.status_code == 201, r.text
        resp = await c.post(BASE, headers=H(r.json()["token"]), json={**body, "label": role})
        assert resp.status_code == expected, (role, resp.text)


# ── envelope encryption and the internal API ─────────────────────────


async def test_each_row_has_its_own_data_key_bound_to_the_row(env):
    c, tok, f, tid = env["c"], env["tok"], env["f"], env["tid"]
    a = await _create(c, tok, label="a")
    await _create(c, tok, label="b")
    ra, rb = await _rows(f)
    assert ra.wrapped_dek != rb.wrapped_dek
    assert await cv.open_row(ra) == API_KEY == await cv.open_row(rb)
    # Ciphertext and wrapped key copied to another row: does not open.
    rb.sealed_key, rb.wrapped_dek = ra.sealed_key, ra.wrapped_dek
    with pytest.raises((ValueError, kms.KeyProviderError)):
        await cv.open_row(rb)
    # Same row moved to another tenant or provider: does not open.
    ra2 = InferenceCredential(**{k: getattr(ra, k) for k in ("id", "tenant_id", "provider",
                                 "label", "sealed_key", "wrapped_dek", "kek_ref")})
    ra2.tenant_id = uuid.uuid4()
    with pytest.raises((ValueError, kms.KeyProviderError)):
        await cv.open_row(ra2)
    ra2.tenant_id, ra2.provider = tid, "anthropic"
    with pytest.raises((ValueError, kms.KeyProviderError)):
        await cv.open_row(ra2)
    assert str(uuid.UUID(a["id"])) == str(ra.id)


async def test_get_credential_decrypts_and_audits_every_use(env):
    c, tok, f, tid = env["c"], env["tok"], env["f"], env["tid"]
    cid = uuid.UUID((await _create(c, tok))["id"])
    async with f() as s:
        await rls.set_tenant(s, tid)
        cred = await cv.get_credential(s, tid, cid, actor=ACTOR, purpose="virtual_key")
        await s.commit()
    assert cred.secret.get_secret_value() == API_KEY
    assert API_KEY not in repr(cred) and API_KEY not in str(cred)

    await c.patch(f"{BASE}/{cid}", headers=H(tok), json={"disabled": True})
    async with f() as s:
        with pytest.raises(cv.CredentialUnavailable) as err:
            await cv.get_credential(s, tid, cid, actor=ACTOR, purpose="virtual_key")
        await s.commit()
    assert err.value.reason == "disabled"
    async with f() as s:
        with pytest.raises(cv.CredentialUnavailable, match="not_found"):
            await cv.get_credential(s, uuid.uuid4(), cid, actor=ACTOR, purpose="virtual_key")
        await s.rollback()
    await c.patch(f"{BASE}/{cid}", headers=H(tok), json={"disabled": False})
    async with f() as s:
        await s.execute(
            update(InferenceCredential).values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
        await s.commit()
    async with f() as s:
        with pytest.raises(cv.CredentialUnavailable, match="expired"):
            await cv.get_credential(s, tid, cid, actor=ACTOR, purpose="virtual_key")
        await s.commit()
    assert (await c.get(f"{BASE}/{cid}", headers=H(tok))).json()["status"] == "expired"

    async with f() as s:
        uses = (
            await s.execute(
                select(AuditEvent)
                .where(AuditEvent.action == "inference_credential.use")
                .order_by(AuditEvent.seq)
            )
        ).scalars().all()
    assert [(u.outcome, u.details) for u in uses] == [
        ("success", {"purpose": "virtual_key"}),
        ("failure", {"purpose": "virtual_key", "reason": "disabled"}),
        ("failure", {"purpose": "virtual_key", "reason": "expired"}),
    ]
    assert uses[0].actor_id == "vault-test" and uses[0].target_id == str(cid)
    assert (await _rows(f))[0].last_used_at is not None
    assert API_KEY not in await _all_text(f)


# ── KEK rotation ──────────────────────────────────────────────────────


async def test_local_kek_rotation_rewraps_every_credential(env, monkeypatch):
    c, tok, f, tid = env["c"], env["tok"], env["f"], env["tid"]
    keys = {f"l{i}": f"sk-proj-key-number-{i}-secret" for i in range(3)}
    for label, key in keys.items():
        await _create(c, tok, label=label, key=key)
    before = {r.label: (r.sealed_key, r.wrapped_dek) for r in await _rows(f)}

    monkeypatch.setattr(settings, "credential_kek_keys", f"k2:{K2},k1:{K1}")
    async with f() as s:
        await rls.set_tenant(s, tid)
        dry = await cv.rewrap_tenant(s, tid, actor=ACTOR, dry_run=True)
    assert dry["pending"] == 3 and dry["rewrapped"] == 0
    async with f() as s:
        await rls.set_tenant(s, tid)
        result = await cv.rewrap_tenant(s, tid, actor=ACTOR)
    assert (result["rewrapped"], result["failed"], result["skipped"]) == (3, 0, 0)
    assert result["target_kek_ref"] == "local:k2"

    # k1 removed entirely: every credential still decrypts.
    monkeypatch.setattr(settings, "credential_kek_keys", f"k2:{K2}")
    for row in await _rows(f):
        assert row.kek_ref == "local:k2" and row.rewrapped_at is not None
        assert row.sealed_key == before[row.label][0]  # payload untouched
        assert row.wrapped_dek != before[row.label][1]
        assert await cv.open_row(row) == keys[row.label]
    # Nothing left to do: no audit row, no change.
    async with f() as s:
        await rls.set_tenant(s, tid)
        assert (await cv.rewrap_tenant(s, tid, actor=ACTOR))["pending"] == 0
        rewraps = (
            await s.execute(
                select(AuditEvent).where(AuditEvent.action == "inference_credential.rewrap")
            )
        ).scalars().all()
    assert len(rewraps) == 1
    assert rewraps[0].details == {
        "rewrapped": 3,
        "skipped": 0,
        "failed": 0,
        "from_kek_providers": ["local"],
        "to_kek_provider": "local",
    }
    everything = await _all_text(f)
    assert all(k not in everything for k in keys.values())


async def test_rewrap_from_local_to_azure_and_across_key_versions(env, monkeypatch):
    c, tok, f, tid = env["c"], env["tok"], env["f"], env["tid"]
    await _create(c, tok, label="a")
    await _create(c, tok, label="b", key=API_KEY_2)
    fake = FakeKeyVault()
    fake.add_version("credential-kek", V1)
    kms.use_provider(azure_provider(fake, KEY_URL))
    monkeypatch.setattr(settings, "credential_kek_provider", "azure-keyvault")

    async with f() as s:
        await rls.set_tenant(s, tid)
        result = await cv.rewrap_tenant(s, tid, actor=ACTOR)
    assert result["rewrapped"] == 2
    rows = await _rows(f)
    assert {r.kek_ref for r in rows} == {f"azure-keyvault:RSA-OAEP-256:{KEY_URL}/{V1}"}
    # Only 32-byte data keys were sent to Key Vault, never a stored key.
    for req in fake.requests:
        assert API_KEY.encode() not in req.content and API_KEY_2.encode() not in req.content
    # The local keyring can go away now.
    monkeypatch.setattr(settings, "credential_kek_keys", "")
    assert sorted([await cv.open_row(r) for r in rows]) == sorted([API_KEY, API_KEY_2])

    # New credentials are wrapped in Key Vault directly.
    created = await _create(c, tok, label="c")
    assert created["kek_provider"] == "azure-keyvault"

    # Key Vault rotates the key: rewrap moves everything to V2.
    fake.add_version("credential-kek", V2)
    async with f() as s:
        await rls.set_tenant(s, tid)
        result = await cv.rewrap_tenant(s, tid, actor=ACTOR)
    assert result["rewrapped"] == 3
    rows = await _rows(f)
    assert {r.kek_ref for r in rows} == {f"azure-keyvault:RSA-OAEP-256:{KEY_URL}/{V2}"}
    del fake.keys["credential-kek"][V1]  # old version disabled
    assert sorted([await cv.open_row(r) for r in rows]) == sorted([API_KEY, API_KEY_2, API_KEY])


async def test_rewrap_failures_and_concurrent_rotation_are_safe(env, monkeypatch):
    c, tok, f, tid = env["c"], env["tok"], env["f"], env["tid"]
    a = await _create(c, tok, label="a")
    await _create(c, tok, label="b")
    monkeypatch.setattr(settings, "credential_kek_keys", f"k2:{K2},k1:{K1}")
    target = kms.LocalKeyProvider()

    class RotatesMidway:
        """Wraps like the target, but a rotate of row ``a`` lands between
        the read and the write of the rewrap."""

        name = "local"

        async def current_ref(self):
            return await target.current_ref()

        async def wrap(self, dek, context):
            wrapped = await target.wrap(dek, context)
            if uuid.UUID(a["id"]).bytes in context:
                r = await c.post(
                    f"{BASE}/{a['id']}/rotate", headers=H(tok), json={"api_key": API_KEY_2}
                )
                assert r.status_code == 200
            return wrapped

    async with f() as s:
        await rls.set_tenant(s, tid)
        result = await cv.rewrap_tenant(s, tid, actor=ACTOR, key_provider=RotatesMidway())
    assert (result["rewrapped"], result["skipped"]) == (1, 1)
    by_label = {r.label: r for r in await _rows(f)}
    assert await cv.open_row(by_label["a"]) == API_KEY_2  # the rotate won
    assert await cv.open_row(by_label["b"]) == API_KEY

    # A data key the provider cannot unwrap is reported and left alone.
    async with f() as s:
        await s.execute(
            update(InferenceCredential)
            .where(InferenceCredential.label == "b")
            .values(kek_ref="local:k1", wrapped_dek=base64.urlsafe_b64encode(b"x" * 60).decode())
        )
        await s.commit()
    async with f() as s:
        await rls.set_tenant(s, tid)
        result = await cv.rewrap_tenant(s, tid, actor=ACTOR)
    assert result["failed"] == 1 and result["errors"][0]["error"] == "KeyProviderError"


async def test_cli_rewraps_every_tenant_under_its_own_context(env, monkeypatch):
    c, tok, tid, engine = env["c"], env["tok"], env["tid"], env["engine"]
    b = await _add_tenant_b(engine)
    tok_b = await _token(c, b, ["admin:inference-credentials"])
    await _create(c, tok, label="a")
    await _create(c, tok_b, label="b")
    monkeypatch.setattr(settings, "credential_kek_keys", f"k2:{K2},k1:{K1}")
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await rls.enable(conn)
    try:
        status = await cv.run(factory, "status", None)
        assert status["tenants"] == {
            str(tid): {"kek_refs": {"local:k1": 1}},
            str(b): {"kek_refs": {"local:k1": 1}},
        }
        out = await cv.run(factory, "rewrap", None)
        assert out["failed"] == 0
        assert {t["rewrapped"] for t in out["tenants"].values()} == {1}
        out = await cv.run(factory, "status", b)
        assert out["tenants"] == {str(b): {"kek_refs": {"local:k2": 1}}}
    finally:
        async with engine.begin() as conn:
            await rls.disable(conn)


# ── tenant isolation ──────────────────────────────────────────────────


async def test_tenants_cannot_reach_each_others_credentials_under_rls(env):
    c, tok, engine, tid = env["c"], env["tok"], env["engine"], env["tid"]
    b = await _add_tenant_b(engine)
    tok_b = await _token(c, b, ["admin:inference-credentials"])
    a_cred = await _create(c, tok, label="shared")
    async with engine.begin() as conn:
        await rls.enable(conn)
    try:
        b_cred = await _create(c, tok_b, label="shared")  # same label, other tenant
        assert [d["id"] for d in (await c.get(BASE, headers=H(tok_b))).json()["data"]] == [
            b_cred["id"]
        ]
        one = f"{BASE}/{a_cred['id']}"
        assert (await c.get(one, headers=H(tok_b))).status_code == 404
        assert (await c.patch(one, headers=H(tok_b), json={"disabled": True})).status_code == 404
        r = await c.post(one + "/rotate", headers=H(tok_b), json={"api_key": API_KEY_2})
        assert r.status_code == 404
        assert (await c.delete(one, headers=H(tok_b))).status_code == 404
        # get_credential in B's context cannot open A's credential.
        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with factory() as s:
            await rls.set_tenant(s, b)
            with pytest.raises(cv.CredentialUnavailable, match="not_found"):
                await cv.get_credential(
                    s, b, uuid.UUID(a_cred["id"]), actor=ACTOR, purpose="virtual_key"
                )
            await s.rollback()
        assert (await c.get(one, headers=H(tok))).json()["status"] == "active"
    finally:
        async with engine.begin() as conn:
            await rls.disable(conn)
    assert tid != b


# ── migration ─────────────────────────────────────────────────────────


def _load_migration_044():
    pytest.importorskip("alembic")
    path = (
        Path(__file__).resolve().parents[2]
        / "db"
        / "alembic"
        / "versions"
        / "044_inference_credentials.py"
    )
    spec = importlib.util.spec_from_file_location("migration_044", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_044_covers_every_scope():
    m = _load_migration_044()
    assert m.NEW_SCOPE == "admin:inference-credentials"
    assert set(m.PREVIOUS_SCOPES) | {m.NEW_SCOPE} == set(ADMIN_SCOPES)


async def test_migration_044_upgrade_and_downgrade(env):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    m = _load_migration_044()
    c, tid, engine, f = env["c"], env["tid"], env["engine"], env["f"]
    full = await _token(c, tid, list(m.PREVIOUS_SCOPES))
    await _token(c, tid, ["admin:read"])

    def run(sync_conn, step):
        ctx = MigrationContext.configure(sync_conn)
        with Operations.context(ctx):
            step()

    async def token_scopes() -> list[set[str]]:
        async with f() as s:
            rows = await s.execute(select(AdminToken.scopes).order_by(AdminToken.created_at))
            return [set(x) for x in rows.scalars()]

    # The test schema comes from the models; let the migration build it.
    async with engine.begin() as conn:
        await conn.run_sync(run, m.downgrade)  # drops the table, strips the scope
    assert all("admin:inference-credentials" not in s for s in await token_scopes())
    body = {"provider": "openai", "label": "x", "api_key": API_KEY}
    assert (await c.post(BASE, headers=H(full), json=body)).status_code == 403
    async with engine.begin() as conn:
        await conn.run_sync(run, m.upgrade)
        policies = (
            await conn.execute(
                text("SELECT policyname FROM pg_policies WHERE tablename = 'inference_credentials'")
            )
        ).scalars().all()
    assert policies == ["tenant_isolation"]
    scopes = await token_scopes()
    assert set(ADMIN_SCOPES) <= scopes[-2]  # full admin stays full admin
    assert scopes[-1] == {"admin:read"}  # narrower tokens gain nothing
    assert (await c.get(BASE, headers=H(full))).status_code == 200
    await _create(c, full, label="after-migration")
