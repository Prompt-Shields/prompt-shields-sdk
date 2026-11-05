"""Per-call ids and the feedback helper (#43)."""

import asyncio
import json
import uuid
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from prompt_shields import (
    FEEDBACK_REASONS,
    AsyncShieldsClient,
    FeedbackError,
    RouteHint,
    ShieldsClient,
    last_call_id,
    send_feedback,
)
from prompt_shields import feedback as fb


def _mock_openai_response():
    resp = MagicMock()
    resp.usage.prompt_tokens = 3
    resp.usage.completion_tokens = 4
    resp.choices = []
    return resp


def _client(**kw) -> ShieldsClient:
    client = ShieldsClient(api_key="sk-test", ps_api_key="ps-test", **kw)
    client._upstream = MagicMock()
    client._upstream.chat.completions.create.side_effect = (
        lambda **kw: _mock_openai_response())
    client.chat.completions._send = MagicMock()
    return client


def _sent_event(client) -> dict:
    return client.chat.completions._send.call_args[0][0]


# --- call_id on the event, upstream and returned ----------------------------


def test_every_call_gets_a_call_id_on_event_header_and_response():
    client = _client()
    first = client.chat.completions.create(
        model="gpt-4o", messages=[{"role": "user", "content": "hi"}])
    event = _sent_event(client)
    _, kwargs = client._upstream.chat.completions.create.call_args

    assert uuid.UUID(first.ps_call_id)
    assert event["call_id"] == first.ps_call_id
    # Straight to the provider: no X-PS-* header at all.
    assert "extra_headers" not in kwargs
    assert last_call_id() == first.ps_call_id

    second = client.chat.completions.create(
        model="gpt-4o", messages=[{"role": "user", "content": "again"}])
    assert second.ps_call_id != first.ps_call_id
    assert _sent_event(client)["call_id"] == second.ps_call_id


def test_call_id_header_goes_with_route_hints():
    client = _client()
    resp = client.chat.completions.create(
        model="auto", messages=[], route=RouteHint(quality="draft"))
    _, kwargs = client._upstream.chat.completions.create.call_args
    assert kwargs["extra_headers"]["X-PS-Call-Id"] == resp.ps_call_id
    assert kwargs["extra_headers"]["X-PS-Quality"] == "draft"


def test_caller_can_supply_the_call_id():
    client = _client()
    resp = client.chat.completions.create(
        model="gpt-4o", messages=[], ps_metadata={"call_id": "order-42.1"})
    assert resp.ps_call_id == "order-42.1"
    assert _sent_event(client)["call_id"] == "order-42.1"
    # An unusable one is replaced, not sent.
    resp = client.chat.completions.create(
        model="gpt-4o", messages=[], ps_metadata={"call_id": "has spaces"})
    assert uuid.UUID(resp.ps_call_id)


def test_call_id_is_not_a_response_field():
    openai = pytest.importorskip("openai")
    from openai.types.chat import ChatCompletion

    response = ChatCompletion(id="x", choices=[], created=0, model="m",
                              object="chat.completion")
    client = _client()
    client._upstream.chat.completions.create.side_effect = lambda **kw: response
    out = client.chat.completions.create(model="m", messages=[])
    assert out is response
    assert uuid.UUID(out.ps_call_id)
    assert "ps_call_id" not in out.model_dump()
    assert "ps_call_id" not in out.to_json()
    assert openai  # imported for the skip


def test_stream_wrapper_carries_the_call_id():
    client = _client()
    client._upstream.chat.completions.create.side_effect = lambda **kw: iter([])
    stream = client.chat.completions.create(model="gpt-4o", messages=[],
                                            stream=True)
    list(stream)
    assert _sent_event(client)["call_id"] == stream.ps_call_id


def test_attach_is_best_effort():
    assert fb.attach_call_id({"a": 1}, "x") == {"a": 1}
    assert fb.attach_call_id(None, "x") is None


@pytest.mark.asyncio
async def test_async_client_call_id():
    client = AsyncShieldsClient(api_key="sk-test", ps_api_key="ps-test")
    client._upstream = MagicMock()
    client._upstream.chat.completions.create = AsyncMock(
        return_value=_mock_openai_response())
    client.chat.completions._send = AsyncMock()
    resp = await client.chat.completions.create(model="gpt-4o", messages=[])
    event = client.chat.completions._send.call_args[0][0]
    assert event["call_id"] == resp.ps_call_id
    assert last_call_id() == resp.ps_call_id


def test_last_call_id_is_per_thread_or_task():
    async def one(meta):
        fb.new_call_id(meta)
        await asyncio.sleep(0)
        return last_call_id()

    async def main():
        return await asyncio.gather(one({"call_id": "a"}), one({"call_id": "b"}))

    assert asyncio.run(main()) == ["a", "b"]


# --- feedback ---------------------------------------------------------------


@pytest.fixture
def collector(monkeypatch):
    """Captures feedback requests; answers like the collector."""
    seen: list[httpx.Request] = []
    status = {"code": 200, "headers": {}}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if status["code"] != 200:
            return httpx.Response(
                status["code"], headers=status["headers"],
                json={"detail": {"status": status["code"], "detail": "nope"}})
        body = json.loads(request.content)
        return httpx.Response(200, json={"call_id": body["call_id"],
                                         "created": True, "matched": True})

    transport = httpx.MockTransport(handler)
    real_client = httpx.Client
    real_async = httpx.AsyncClient

    monkeypatch.setattr(httpx, "post", lambda *a, **kw: real_client(
        transport=transport).post(*a, **kw))
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real_async(
        transport=transport, **kw))
    return {"seen": seen, "status": status}


def test_client_feedback_sends_metadata_only(collector):
    client = _client(ps_collector_url="http://collector:8000/")
    resp = client.chat.completions.create(model="gpt-4o", messages=[])
    out = client.feedback(resp.ps_call_id, 0.5, "incomplete", user_id="u-1")
    assert out == {"call_id": resp.ps_call_id, "created": True, "matched": True}

    req = collector["seen"][0]
    assert str(req.url) == "http://collector:8000/ingest/feedback"
    assert req.headers["Authorization"] == "Bearer ps-test"
    assert json.loads(req.content) == {
        "call_id": resp.ps_call_id, "score": 0.5, "reason": "incomplete",
        "user_id": "u-1", "source": "sdk",
    }


def test_bool_score_and_module_helper(collector):
    send_feedback("c-1", True, ps_api_key="k", ps_collector_url="http://c")
    send_feedback("c-2", False, ps_api_key="k", ps_collector_url="http://c")
    scores = [json.loads(r.content)["score"] for r in collector["seen"]]
    assert scores == [1.0, 0.0]


@pytest.mark.asyncio
async def test_async_client_feedback(collector):
    client = AsyncShieldsClient(api_key="sk-test", ps_api_key="ps-test",
                                ps_collector_url="http://c")
    out = await client.feedback("c-9", 1, "correct")
    assert out["call_id"] == "c-9"
    assert json.loads(collector["seen"][0].content)["reason"] == "correct"


@pytest.mark.parametrize(
    "args",
    [
        ("", 1),
        ("has space", 1),
        ("x" * 129, 1),
        ("c", 1.5),
        ("c", -0.01),
        ("c", float("nan")),
        ("c", "1"),
        ("c", None),
        ("c", 1, "the answer was rude"),
        ("c", 1, None, ""),
        ("c", 1, None, "a\nb"),
    ],
)
def test_invalid_feedback_raises_before_sending(collector, args):
    call_id, score, *rest = args
    reason = rest[0] if rest else None
    user_id = rest[1] if len(rest) > 1 else None
    with pytest.raises((ValueError, TypeError)):
        send_feedback(call_id, score, reason, user_id=user_id, ps_api_key="k")
    assert collector["seen"] == []


def test_refused_feedback_raises_with_status(collector):
    collector["status"].update(code=429, headers={"Retry-After": "17"})
    with pytest.raises(FeedbackError) as err:
        send_feedback("c", 1, ps_api_key="k")
    assert err.value.status_code == 429
    assert err.value.retry_after == 17.0
    assert "nope" in str(err.value)


def test_unreachable_collector_raises(monkeypatch):
    def boom(*a, **kw):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(httpx, "post", boom)
    with pytest.raises(FeedbackError) as err:
        send_feedback("c", 1, ps_api_key="k")
    assert err.value.status_code is None


def test_reasons_are_labels():
    assert all(r.isidentifier() and r == r.lower() for r in FEEDBACK_REASONS)
