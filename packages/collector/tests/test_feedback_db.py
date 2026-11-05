"""Call feedback (#43) against a real PostgreSQL database
(PS_TEST_DATABASE_URL): ``call_id`` on ``/ingest/events`` and
``POST /ingest/feedback``. Row-level security for the table is covered in
test_rls_db.py, retention and erasure in test_retention.py."""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest
from collector.app import app
from collector.config import settings
from collector.feedback import MAX_BODY_BYTES, REASONS
from collector.partner_rate_limit import (
    FixedWindowRateLimiter,
    get_feedback_rate_limiter,
)
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

KEY = "ps-test-key-12345"
HEADERS = {"Authorization": f"Bearer {KEY}"}
CALL = "0b1e6a58-3f43-4c43-9d1e-2f0f6d3b9c11"


@pytest.fixture
async def client(seeded_db):
    # A fresh limiter per test, so counts never leak between tests.
    limiter = FixedWindowRateLimiter()
    app.dependency_overrides[get_feedback_rate_limiter] = lambda: limiter
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c
    app.dependency_overrides.pop(get_feedback_rate_limiter, None)


async def _rows(seeded_db, sql: str) -> list[dict]:
    async with seeded_db.connect() as conn:
        return [dict(r._mapping) for r in await conn.execute(text(sql))]


async def test_call_id_is_stored_on_the_usage_event(client, seeded_db):
    resp = await client.post("/ingest/events", headers=HEADERS, json={"events": [
        {"vendor": "openai", "model": "gpt-4o", "call_id": CALL},
        {"vendor": "openai", "model": "gpt-4o", "call_id": "bad id with spaces"},
        {"vendor": "openai", "model": "gpt-4o", "call_id": "x" * 129},
        {"vendor": "openai", "model": "gpt-4o"},
    ]})
    assert resp.status_code == 200, resp.text
    rows = await _rows(seeded_db, "SELECT call_id FROM ai_usage_events")
    assert sorted(r["call_id"] or "" for r in rows) == ["", "", "", CALL]

    resp = await client.get("/telemetry/loss", headers=HEADERS)
    assert resp.status_code == 200, resp.text
    assert "call_id:invalid" in json.dumps(resp.json())


async def test_feedback_is_stored_as_metadata_and_matched(client, seeded_db):
    await client.post("/ingest/events", headers=HEADERS, json={"events": [
        {"vendor": "openai", "model": "gpt-4o", "call_id": CALL}]})

    resp = await client.post("/ingest/feedback", headers=HEADERS, json={
        "call_id": CALL, "score": 0.25, "reason": "incomplete",
        "user_id": "a1b2c3d4e5f60718", "comment": "the answer missed the second half",
    })
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"call_id": CALL, "created": True, "matched": True}
    assert resp.headers["X-RateLimit-Limit"] == str(
        settings.feedback_user_rate_limit_per_minute
    )

    resp = await client.post("/ingest/feedback", headers=HEADERS,
                             json={"call_id": "not-seen-yet", "score": 1})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"call_id": "not-seen-yet", "created": True, "matched": False}

    rows = await _rows(seeded_db, "SELECT * FROM call_feedback ORDER BY call_id")
    assert [(r["call_id"], r["score"], r["reason"], r["user_id"], r["source"])
            for r in rows] == [
        (CALL, 0.25, "incomplete", "a1b2c3d4e5f60718", "sdk"),
        ("not-seen-yet", 1.0, None, None, "sdk"),
    ]
    # No free text is kept anywhere.
    assert "missed" not in json.dumps(rows, default=str)
    assert set(rows[0]) == {"id", "tenant_id", "call_id", "score", "reason",
                            "user_id", "source", "created_at", "updated_at"}


async def test_feedback_is_idempotent_per_call(client, seeded_db):
    body = {"call_id": CALL, "score": 1, "reason": "correct", "source": "gateway"}
    first = await client.post("/ingest/feedback", headers=HEADERS, json=body)
    again = await client.post("/ingest/feedback", headers=HEADERS, json=body)
    assert first.json()["created"] is True
    assert again.status_code == 200
    assert again.json()["created"] is False
    # A later feedback for the same call replaces the earlier one.
    changed = await client.post("/ingest/feedback", headers=HEADERS,
                                json={"call_id": CALL, "score": 0})
    assert changed.json()["created"] is False

    rows = await _rows(seeded_db, "SELECT * FROM call_feedback")
    assert len(rows) == 1
    row = rows[0]
    assert (row["score"], row["reason"], row["source"]) == (0.0, None, "sdk")
    assert row["created_at"] <= row["updated_at"]


@pytest.mark.parametrize(
    "body",
    [
        {"score": 1},
        {"call_id": "", "score": 1},
        {"call_id": "a b", "score": 1},
        {"call_id": "x" * 129, "score": 1},
        {"call_id": CALL},
        {"call_id": CALL, "score": "1"},
        {"call_id": CALL, "score": True},
        {"call_id": CALL, "score": 1.5},
        {"call_id": CALL, "score": -0.1},
        {"call_id": CALL, "score": 2},
        {"call_id": CALL, "score": 10**400},
        {"call_id": CALL, "score": 1, "reason": "the model was rude to me"},
        {"call_id": CALL, "score": 1, "reason": 3},
        {"call_id": CALL, "score": 1, "user_id": ""},
        {"call_id": CALL, "score": 1, "user_id": "a" * 65},
        {"call_id": CALL, "score": 1, "user_id": "line\nbreak"},
        # Raw identifiers are never stored: hex hashes only.
        {"call_id": CALL, "score": 1, "user_id": "alice@example.com"},
        {"call_id": CALL, "score": 1, "user_id": "A1B2C3D4E5F60718"},
        {"call_id": CALL, "score": 1, "user_id": "abc123"},
        {"call_id": CALL, "score": 1, "user_id": 12345678901234567},
        {"call_id": CALL, "score": 1, "source": "browser"},
        [{"call_id": CALL, "score": 1}],
    ],
)
async def test_invalid_feedback_is_refused(client, seeded_db, body):
    resp = await client.post("/ingest/feedback", headers=HEADERS, json=body)
    assert resp.status_code == 422, resp.text
    assert await _rows(seeded_db, "SELECT id FROM call_feedback") == []


@pytest.mark.parametrize(
    "raw",
    [
        '{"call_id": "a\\u0000b", "score": 1}',
        '{"call_id": "ok", "score": 1, "user_id": "u\\u0000"}',
        '{"call_id": "ok", "score": 1, "user_id": "\\ud800"}',
        '{"call_id": "ok", "score": 1, "extra": "\\u0000"}',
    ],
)
async def test_unstorable_text_is_refused(client, seeded_db, raw):
    resp = await client.post(
        "/ingest/feedback",
        headers={**HEADERS, "Content-Type": "application/json"},
        content=raw,
    )
    assert resp.status_code == 422, resp.text
    assert await _rows(seeded_db, "SELECT id FROM call_feedback") == []


async def test_body_is_bounded(client, seeded_db):
    big = json.dumps({"call_id": CALL, "score": 1, "pad": "x" * MAX_BODY_BYTES})
    resp = await client.post(
        "/ingest/feedback",
        headers={**HEADERS, "Content-Type": "application/json"},
        content=big,
    )
    assert resp.status_code == 413
    resp = await client.post(
        "/ingest/feedback",
        headers={**HEADERS, "Content-Type": "application/json"},
        content="{not json",
    )
    assert resp.status_code == 400
    deep = "[" * 100 + "]" * 100
    resp = await client.post(
        "/ingest/feedback",
        headers={**HEADERS, "Content-Type": "application/json"},
        content=deep,
    )
    assert resp.status_code == 400


async def test_requires_a_tenant_key(client):
    resp = await client.post("/ingest/feedback", json={"call_id": CALL, "score": 1})
    assert resp.status_code == 401
    resp = await client.post(
        "/ingest/feedback",
        headers={"Authorization": "Bearer wrong"},
        json={"call_id": CALL, "score": 1},
    )
    assert resp.status_code == 401


async def test_rate_limited_per_user_and_per_tenant(client, seeded_db, monkeypatch):
    monkeypatch.setattr(settings, "feedback_user_rate_limit_per_minute", 2)
    monkeypatch.setattr(settings, "feedback_rate_limit_per_minute", 5)

    def fb(i: int, user: str | None):
        body = {"call_id": f"call-{i}", "score": 1}
        if user is not None:
            body["user_id"] = user
        return client.post("/ingest/feedback", headers=HEADERS, json=body)

    alice, bob = "a" * 32, "b" * 32
    assert (await fb(1, alice)).status_code == 200
    assert (await fb(2, alice)).status_code == 200
    limited = await fb(3, alice)
    assert limited.status_code == 429
    assert int(limited.headers["Retry-After"]) >= 1
    # Another user has their own budget, until the tenant's is used up.
    assert (await fb(4, bob)).status_code == 200
    assert (await fb(5, None)).status_code == 200
    assert (await fb(6, None)).status_code == 429  # tenant limit (5)

    rows = await _rows(seeded_db, "SELECT call_id FROM call_feedback")
    assert sorted(r["call_id"] for r in rows) == ["call-1", "call-2", "call-4", "call-5"]


def test_reasons_match_the_sdk():
    sdk = Path(__file__).resolve().parents[2] / "sdk" / "prompt_shields" / "feedback.py"
    for node in ast.walk(ast.parse(sdk.read_text())):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "FEEDBACK_REASONS" for t in node.targets
        ):
            assert set(ast.literal_eval(node.value)) == REASONS
            return
    raise AssertionError("FEEDBACK_REASONS not found in the SDK")
