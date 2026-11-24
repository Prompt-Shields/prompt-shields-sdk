"""Opt-in payload logging (#352) against a real PostgreSQL database
(PS_TEST_DATABASE_URL) with row-level security enforced: off by default,
the tenant admin setting (full mode needs an owner, every change audited),
ingest per mode, redaction before anything is written (storage scanned for
raw values), sampling, the audited partner read, retention and legal hold,
tenant erasure and erasure by end user."""

from __future__ import annotations

import random
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from collector import audit_trail, payload_logging, retention, rls, user_erasure
from collector.admin_auth import ADMIN_SCOPES
from collector.app import app
from collector.config import settings
from db.models import AuditEvent, CallPayload, PartnerCredential
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text, update

from tests.test_rls_db import (  # noqa: F401 - fixture and keys
    KEY_A,
    KEY_B,
    PARTNER_KEY_A,
    PARTNER_KEY_B,
    rls_db,
)

OP_TOKEN = "op-payload-token"
A = "/api/v1/admin"
KEYRING = "k1:" + "p" * 40


def H(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    monkeypatch.setattr(settings, "admin_token", OP_TOKEN)
    monkeypatch.setattr(settings, "webhook_secret_keys", KEYRING)
    audit_trail.auth_failure_limiter.reset()


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c


async def _bootstrap(c, tenant_id) -> str:
    """An unbound token holding every admin scope (owner-equivalent)."""
    resp = await c.post(
        f"/api/v1/operator/tenants/{tenant_id}/admin-tokens",
        headers=H(OP_TOKEN),
        json={"name": f"boot-{uuid.uuid4().hex[:6]}", "scopes": list(ADMIN_SCOPES)},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["token"]


async def _member_token(c, boot, role: str) -> str:
    """An admin token, every admin scope, for a principal with ``role``."""
    p = await c.post(f"{A}/principals", headers=H(boot), json={"kind": "user", "display_name": role})
    assert p.status_code == 201, p.text
    pid = p.json()["id"]
    m = await c.post(f"{A}/memberships", headers=H(boot), json={"principal_id": pid, "role": role})
    assert m.status_code == 201, m.text
    t = await c.post(
        f"{A}/tokens",
        headers=H(boot),
        json={"name": f"t-{uuid.uuid4().hex[:6]}", "scopes": list(ADMIN_SCOPES), "principal_id": pid},
    )
    assert t.status_code == 201, t.text
    return t.json()["token"]


async def _set(c, token, status=200, **body) -> dict:
    resp = await c.put(f"{A}/payload-logging", headers=H(token), json=body)
    assert resp.status_code == status, resp.text
    return resp.json()


async def _ingest(c, key, call_id, status=202, **over) -> dict:
    body = {
        "call_id": call_id,
        "route": "/v1/chat/completions",
        "status_code": 200,
        "request": {"messages": [{"role": "user", "content": "hello"}]},
        "response": {"choices": [{"message": {"content": "hi"}}]},
    }
    body.update(over)
    resp = await c.post("/ingest/payloads", headers=H(key), json=body)
    assert resp.status_code == status, resp.text
    return resp.json()


async def _rows(factory, tenant) -> list[CallPayload]:
    async with factory() as s:
        await rls.set_tenant(s, tenant)
        return list(
            (
                await s.execute(
                    select(CallPayload)
                    .where(CallPayload.call_id.not_like("seed-%"))
                    .order_by(CallPayload.call_id)
                )
            ).scalars()
        )


async def _actions(factory, tenant, action) -> list[AuditEvent]:
    async with factory() as s:
        await rls.set_tenant(s, tenant)
        return list(
            (
                await s.execute(
                    select(AuditEvent).where(AuditEvent.action == action).order_by(AuditEvent.seq)
                )
            ).scalars()
        )


async def test_off_by_default_stores_nothing(rls_db, client):  # noqa: F811
    c = client
    resp = await c.get(f"{A}/payload-logging", headers=H(KEY_A))
    assert resp.status_code == 200
    assert resp.json() == {
        "mode": "off",
        "sample_rate": 1.0,
        "on_error_only": False,
        "routes": [],
        "retention_days": 30,
    }
    out = await _ingest(c, KEY_A, "c-off")
    assert out == {
        "call_id": "c-off", "stored": False, "mode": "off", "reason": "off", "redactions": {}
    }
    assert await _rows(rls_db["plain"], rls_db["a"]) == []
    # Ingest needs a key; the body is validated without echoing values.
    assert (await c.post("/ingest/payloads", json={"call_id": "x"})).status_code == 401
    bad = await c.post(
        "/ingest/payloads", headers=H(KEY_A), json={"call_id": "has space jane@x.io"}
    )
    assert bad.status_code == 400 and "jane" not in bad.text
    bad = await c.post("/ingest/payloads", headers=H(KEY_A), json={"call_id": "c", "extra": 1})
    assert bad.status_code == 400


async def test_full_mode_needs_an_owner_and_every_change_is_audited(rls_db, client, monkeypatch):  # noqa: F811
    c, a = client, rls_db["a"]
    boot = await _bootstrap(c, a)
    owner = await _member_token(c, boot, "owner")
    admin = await _member_token(c, boot, "admin")

    # An admin, and an ingest key, cannot turn full logging on.
    await _set(c, admin, 403, mode="full")
    await _set(c, KEY_A, 403, mode="full")
    # Redacted is fine for an admin.
    out = await _set(c, admin, mode="redacted", sample_rate=0.5, routes=["/v1/*"], retention_days=7)
    assert out == {
        "mode": "redacted", "sample_rate": 0.5, "on_error_only": False,
        "routes": ["/v1/*"], "retention_days": 7,
    }
    # An owner can, and so can the unbound bootstrap token.
    assert (await _set(c, owner, mode="full"))["mode"] == "full"
    assert (await _set(c, boot, mode="full", on_error_only=True))["on_error_only"] is True
    # Turning it down needs no owner.
    assert (await _set(c, admin, mode="metadata"))["mode"] == "metadata"
    # Content modes need the keyring.
    monkeypatch.setattr(settings, "webhook_secret_keys", "")
    await _set(c, owner, 409, mode="redacted")
    assert (await _set(c, owner, mode="off"))["mode"] == "off"
    # Validation.
    await _set(c, owner, 422, mode="loud")
    await _set(c, owner, 422, mode="metadata", sample_rate=1.5)

    rows = await _actions(rls_db["plain"], a, payload_logging.SET_ACTION)
    assert [(r.details["previous_mode"], r.details["mode"]) for r in rows] == [
        ("off", "redacted"), ("redacted", "full"), ("full", "full"),
        ("full", "metadata"), ("metadata", "off"),
    ]
    assert rows[0].details["retention_days"] == 7 and rows[0].details["routes"] == 1

    # The operator's settings PATCH cannot set it behind the owner's back.
    resp = await c.patch(
        f"/api/v1/operator/tenants/{a}",
        headers=H(OP_TOKEN),
        json={"settings": {"payload_logging": {"mode": "full"}}},
    )
    assert resp.status_code == 400


async def test_modes_store_what_they_say_and_off_stops_writes_at_once(rls_db, client):  # noqa: F811
    c, a, b = client, rls_db["a"], rls_db["b"]
    boot = await _bootstrap(c, a)

    await _set(c, boot, mode="metadata")
    out = await _ingest(c, KEY_A, "c-meta")
    assert out["stored"] and out["mode"] == "metadata"
    await _set(c, boot, mode="full")
    resp = await c.post(
        "/ingest/payloads",
        headers=H(KEY_A),
        json={"call_id": "c-full", "request": {"q": "mail jane@example.com"}, "response": "ok"},
    )
    assert resp.status_code == 202 and resp.json()["stored"]
    assert resp.headers["x-ps-logging"] == "full"
    # Resent: stored once.
    assert (await _ingest(c, KEY_A, "c-full"))["reason"] == "duplicate"
    # Tenant b is still off; the same call id is its own.
    assert (await _ingest(c, KEY_B, "c-full"))["reason"] == "off"
    assert "x-ps-logging" not in (
        await c.post("/ingest/payloads", headers=H(KEY_B), json={"call_id": "c-b"})
    ).headers

    # Route filter and on_error_only.
    await _set(c, boot, mode="metadata", routes=["/v1/embeddings"], on_error_only=True)
    assert (await _ingest(c, KEY_A, "c-route"))["reason"] == "route_not_logged"
    assert (await _ingest(c, KEY_A, "c-ok", route="/v1/embeddings"))["reason"] == "not_error"
    assert (await _ingest(c, KEY_A, "c-err", route="/v1/embeddings", status_code=502))["stored"]

    # Off: the very next payload is not stored.
    await _set(c, boot, mode="off")
    assert (await _ingest(c, KEY_A, "c-after-off"))["reason"] == "off"

    rows = {r.call_id: r for r in await _rows(rls_db["plain"], a)}
    assert sorted(rows) == ["c-err", "c-full", "c-meta"]
    assert rows["c-meta"].ciphertext is None and rows["c-meta"].request_bytes > 0
    assert rows["c-err"].is_error and rows["c-err"].status_code == 502
    full = rows["c-full"]
    assert full.mode == "full" and full.key_id == "k1" and full.storage == "database"
    assert "jane" not in full.ciphertext
    assert payload_logging.unseal_payload(a, "c-full", full.ciphertext) == {
        "request": {"q": "mail jane@example.com"}, "response": "ok",
    }
    # Sealed per tenant and call: it does not open as another tenant's.
    with pytest.raises(ValueError):
        payload_logging.unseal_payload(b, "c-full", full.ciphertext)
    assert await _rows(rls_db["plain"], b) == []


def _card(rng: random.Random) -> str:
    """A random Luhn-valid 16-digit card number."""
    digits = [4] + [rng.randrange(10) for _ in range(14)]
    total = 0
    for i, d in enumerate(reversed(digits)):
        d = d * 2 if i % 2 == 0 else d
        total += d - 9 if d > 9 else d
    return "".join(map(str, digits)) + str((10 - total % 10) % 10)


async def test_redacted_mode_never_stores_raw_emails_or_cards(rls_db, client):  # noqa: F811
    c, a = client, rls_db["a"]
    boot = await _bootstrap(c, a)
    await _set(c, boot, mode="redacted")
    rng = random.Random(352)
    secrets: list[str] = []
    for i in range(60):
        email = f"user.{rng.randrange(10**9)}+t@corp{i}.example.com"
        card = _card(rng)
        grouped = " ".join(card[j : j + 4] for j in range(0, 16, 4))
        dashed = "-".join(card[j : j + 4] for j in range(0, 16, 4))
        secrets += [email, card, grouped, dashed, card[-12:]]
        out = await _ingest(
            c,
            KEY_A,
            f"r-{i}",
            request={
                "messages": [
                    {"role": "user", "content": f"I am {email}, card {grouped}."},
                    {"role": "user", "content": f"or {dashed} / {card}"},
                ],
                "metadata": {email: int(card)},
            },
            response={"text": f"Noted {email} and {card}"},
        )
        assert out["stored"], out
        assert out["redactions"]["email"] == 3 and out["redactions"]["card"] == 5
    # Scan storage: every column of every row, and the decrypted content.
    async with rls_db["plain"]() as s:
        await rls.set_tenant(s, a)
        dumped = (
            await s.execute(text("SELECT row_to_json(c)::text FROM call_payloads c"))
        ).scalars().all()
    rows = await _rows(rls_db["plain"], a)
    plain = [payload_logging.unseal_payload(a, r.call_id, r.ciphertext) for r in rows]
    haystack = "\n".join(dumped) + "\n".join(map(str, plain))
    leaked = [s for s in secrets if s in haystack]
    assert leaked == []
    assert plain[0]["request"]["messages"][0]["content"] == "I am [EMAIL], card [CARD]."
    assert plain[0]["request"]["metadata"] == {"[EMAIL]": "[CARD]"}
    assert rows[0].redactions == {"card": 5, "email": 3}


async def test_sample_rate_keeps_about_a_tenth(rls_db, client):  # noqa: F811
    c, a = client, rls_db["a"]
    boot = await _bootstrap(c, a)
    await _set(c, boot, mode="metadata", sample_rate=0.1)
    calls = 10_000
    async with rls_db["plain"]() as s:
        await rls.set_tenant(s, a)
        for i in range(calls):
            await payload_logging.store_payload(
                s, a, payload_logging.parse_payload({"call_id": f"s-{uuid.uuid4().hex}"})
            )
            await rls.set_tenant(s, a)
    stored = len(await _rows(rls_db["plain"], a))
    assert abs(stored / calls - 0.1) <= 0.02, stored


async def test_partner_read_needs_logs_read_and_every_read_is_audited(rls_db, client):  # noqa: F811
    c, a, b = client, rls_db["a"], rls_db["b"]
    boot = await _bootstrap(c, a)
    await _set(c, boot, mode="redacted")
    await _ingest(c, KEY_A, "p-1", request={"q": "call +1 415 555 0100"}, user_id="u-1")

    r = await c.get("/partner/v1/calls/p-1", headers=H(PARTNER_KEY_A))
    assert r.status_code == 403  # registry:read does not grant it
    async with rls_db["admin"]() as s:
        await s.execute(update(PartnerCredential).values(scopes=["logs:read"]))
        await s.commit()
    r = await c.get("/partner/v1/calls/p-1", headers=H(PARTNER_KEY_A))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["request"] == {"q": "call [PHONE]"} and body["mode"] == "redacted"
    assert body["redactions"] == {"phone": 1} and body["error"] is False
    assert set(body) == {
        "call_id", "mode", "received_at", "route", "status_code", "error",
        "request_bytes", "response_bytes", "redactions", "request", "response",
    }
    assert "user_id_hash" not in body
    # Not found, and another tenant's call, are 404 and audited too.
    assert (await c.get("/partner/v1/calls/nope", headers=H(PARTNER_KEY_A))).status_code == 404
    assert (await c.get("/partner/v1/calls/p-1", headers=H(PARTNER_KEY_B))).status_code == 404
    assert (await c.get("/partner/v1/calls/bad%20id", headers=H(PARTNER_KEY_A))).status_code == 400

    reads = await _actions(rls_db["plain"], a, payload_logging.READ_ACTION)
    assert [(r.target_id, r.status_code, r.actor_type) for r in reads] == [
        ("p-1", 200, "partner"), ("nope", 404, "partner"),
    ]
    reads_b = await _actions(rls_db["plain"], b, payload_logging.READ_ACTION)
    assert [(r.target_id, r.status_code) for r in reads_b] == [("p-1", 404)]


async def test_read_fails_closed_when_the_audit_cannot_be_written(rls_db, client, monkeypatch):  # noqa: F811
    c, a = client, rls_db["a"]
    boot = await _bootstrap(c, a)
    await _set(c, boot, mode="full")
    await _ingest(c, KEY_A, "f-1")
    async with rls_db["admin"]() as s:
        await s.execute(update(PartnerCredential).values(scopes=["logs:read"]))
        await s.commit()

    async def boom(*args, **kwargs):
        raise RuntimeError("audit down")

    monkeypatch.setattr(payload_logging.audit_trail, "record", boom)
    r = await c.get("/partner/v1/calls/f-1", headers=H(PARTNER_KEY_A))
    assert r.status_code == 503 and "choices" not in r.text


async def test_retention_purges_payloads_unless_on_legal_hold(rls_db, client):  # noqa: F811
    c, a, b = client, rls_db["a"], rls_db["b"]
    boot = await _bootstrap(c, a)
    await _set(c, boot, mode="metadata", retention_days=10)
    await _ingest(c, KEY_A, "old")
    await _ingest(c, KEY_A, "new")
    async with rls_db["admin"]() as s:
        await s.execute(
            update(CallPayload)
            .where(CallPayload.call_id == "old")
            .values(received_at=datetime.now(UTC) - timedelta(days=11))
        )
        await s.commit()
        await s.execute(update(PartnerCredential).values(scopes=["logs:read"]))
        await s.commit()
    # Past retention, not yet purged: no longer readable.
    assert (await c.get("/partner/v1/calls/old", headers=H(PARTNER_KEY_A))).status_code == 404

    hold = await c.put(f"{A}/legal-hold", headers=H(boot), json={"enabled": True})
    assert hold.status_code == 200 and hold.json()["legal_hold"] is True
    assert (await c.get(f"{A}/legal-hold", headers=H(KEY_A))).json()["since"]
    # Held: readable again and the purge deletes nothing (usage events too).
    assert (await c.get("/partner/v1/calls/old", headers=H(PARTNER_KEY_A))).status_code == 200
    async with rls_db["admin"]() as s:
        result = await retention.purge_tenant_events(s, a)
    assert result.legal_hold and result.payloads_deleted == 0 and result.events_deleted == 0
    assert [r.call_id for r in await _rows(rls_db["plain"], a)] == ["new", "old"]

    lift = await c.put(f"{A}/legal-hold", headers=H(boot), json={"enabled": False})
    assert lift.json() == {"legal_hold": False, "since": None}
    async with rls_db["admin"]() as s:
        result = await retention.purge_tenant_events(s, a)
        other = await retention.purge_tenant_events(s, b)
    assert not result.legal_hold and result.payloads_deleted == 1
    assert result.payload_retention_days == 10 and other.payload_retention_days == 30
    assert [r.call_id for r in await _rows(rls_db["plain"], a)] == ["new"]
    holds = await _actions(rls_db["plain"], a, payload_logging.LEGAL_HOLD_ACTION)
    assert [r.details["legal_hold"] for r in holds] == [True, False]
    # The operator cannot place or lift it behind the tenant's back.
    resp = await c.patch(
        f"/api/v1/operator/tenants/{a}", headers=H(OP_TOKEN), json={"settings": {"legal_hold": None}}
    )
    assert resp.status_code == 400


async def test_tenant_erasure_and_erasure_by_user_remove_payloads(rls_db, client):  # noqa: F811
    c, a, b = client, rls_db["a"], rls_db["b"]
    for tenant, key in ((a, KEY_A), (b, KEY_B)):
        boot = await _bootstrap(c, tenant)
        await _set(c, boot, mode="full")
        # By the stored hash, and by a call id of the user's usage event.
        await _ingest(c, key, "u-direct", user_id="alice")
        await _ingest(c, key, "u-linked")
        await _ingest(c, key, "u-other", user_id="bob")
        resp = await c.post(
            "/ingest/events",
            headers=H(key),
            json={"events": [{"vendor": "openai", "model": "gpt-4o", "source": "gateway",
                              "user_id": "alice", "call_id": "u-linked"}]},
        )
        assert resp.status_code == 200, resp.text

    resp = await c.post(f"{A}/users/erase", headers=H(KEY_A), json={"user_id": "alice"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["rows_erased"]["call_payloads"] == 2
    assert [r.call_id for r in await _rows(rls_db["plain"], a)] == ["u-other"]
    assert len(await _rows(rls_db["plain"], b)) == 3
    # Idempotent.
    async with rls_db["plain"]() as s:
        await rls.set_tenant(s, a)
        again = await user_erasure.erase_user(
            s, a, user_id="alice", actor=audit_trail.system_actor("test")
        )
    assert again.rows_erased["call_payloads"] == 0

    async with rls_db["admin"]() as s:
        erased = await retention.erase_tenant(s, b)
    assert erased.rows_deleted["call_payloads"] == 4  # 3 + the rls seed row
    async with rls_db["admin"]() as s:
        left = (
            await s.execute(select(CallPayload.id).where(CallPayload.tenant_id == b))
        ).all()
    assert left == []
