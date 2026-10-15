"""instrument() for Azure OpenAI, google-genai, mistralai, cohere and Bedrock.

Each test drives the real SDK (skipped when it is not installed) against a
mocked HTTP layer: httpx(2).MockTransport, or a botocore `before-send` hook
for boto3. No network: telemetry senders are replaced with recorders.
"""

import asyncio
import io
import json
from typing import ClassVar

import httpx
import prompt_shields
import pytest
from prompt_shields import instrumentation as inst

MESSAGES = [{"role": "user", "content": "email me at jane@example.com"}]


class RecordingSender:
    instances: ClassVar[list["RecordingSender"]] = []

    def __init__(self, *args, **kwargs):
        self.events: list[dict] = []
        RecordingSender.instances.append(self)

    def enqueue(self, event):
        self.events.append(event)

    def flush_sync(self):
        pass

    async def flush(self):
        pass


@pytest.fixture(autouse=True)
def recording(monkeypatch):
    RecordingSender.instances = []
    monkeypatch.setattr(inst, "TelemetrySender", RecordingSender)
    for var in ("PS_ATLAS_URL", "PS_ATLAS_API_KEY", *inst._METADATA_ENV.values()):
        monkeypatch.delenv(var, raising=False)
    yield
    prompt_shields.uninstrument()


def _events():
    return RecordingSender.instances[0].events if RecordingSender.instances else []


def _json_transport(http, body, status=200, seen=None):
    def handler(request):
        if seen is not None:
            seen.append(request)
        return http.Response(status, json=body)
    return http.MockTransport(handler)


# --- Azure OpenAI -------------------------------------------------------------

AZURE_CHAT = {
    "id": "c1", "object": "chat.completion", "created": 0,
    "model": "gpt-4o-2024-08-06",
    "choices": [{"index": 0, "finish_reason": "stop",
                 "message": {"role": "assistant", "content": "hi"}}],
    "usage": {"prompt_tokens": 1000, "completion_tokens": 100, "total_tokens": 1100},
}


def _openai_http(openai):
    return getattr(openai._base_client, "httpx2", None) or httpx


def _azure(openai, http, cls="AzureOpenAI", body=AZURE_CHAT, seen=None, **kw):
    client_cls = http.AsyncClient if cls.startswith("Async") else http.Client
    return getattr(openai, cls)(
        api_key="azure-key", api_version="2024-10-21",
        azure_endpoint="https://example.openai.azure.com",
        http_client=client_cls(transport=_json_transport(http, body, seen=seen)),
        **kw)


def test_azure_openai_tagged_and_priced_by_deployment_map():
    openai = pytest.importorskip("openai")
    http = _openai_http(openai)
    prompt_shields.instrument(ps_api_key="ps_key",
                              azure_deployments={"prod-chat": "gpt-4o-mini"})
    client = _azure(openai, http, azure_deployment="prod-chat")
    resp = client.chat.completions.create(model="ignored", messages=MESSAGES)

    assert resp.choices[0].message.content == "hi"
    event = _events()[0]
    assert event["vendor"] == "azure_openai"
    assert event["requested_model"] == "prod-chat"
    assert event["served_model"] == "gpt-4o-2024-08-06"
    assert (event["tokens_in"], event["tokens_out"]) == (1000, 100)
    assert event["detected_pii_types"] == ["email"]
    assert len(event["api_key_fingerprint"]) == 16
    # gpt-4o-mini rates (OpenAI fallback), not gpt-4o.
    assert event["cost"] == pytest.approx(1000 * 0.00015 / 1000 + 100 * 0.0006 / 1000)


def test_azure_unmapped_deployment_priced_by_served_model():
    openai = pytest.importorskip("openai")
    http = _openai_http(openai)
    prompt_shields.instrument(ps_api_key="ps_key")
    client = _azure(openai, http)
    client.chat.completions.create(model="my-deployment", messages=MESSAGES)
    event = _events()[0]
    assert event["requested_model"] == "my-deployment"
    assert event["cost"] == pytest.approx(1000 * 0.0025 / 1000 + 100 * 0.01 / 1000)


def test_azure_async_and_vendor_filter():
    openai = pytest.importorskip("openai")
    http = _openai_http(openai)
    prompt_shields.instrument(ps_api_key="ps_key", vendors=("azure_openai",))

    plain = openai.OpenAI(api_key="sk", http_client=http.Client(
        transport=_json_transport(http, AZURE_CHAT)))
    plain.chat.completions.create(model="gpt-4o", messages=MESSAGES)
    assert _events() == []  # plain OpenAI is not selected

    async def run():
        client = _azure(openai, http, cls="AsyncAzureOpenAI")
        return await client.chat.completions.create(
            model="dep", messages=MESSAGES)

    asyncio.run(run())
    assert [e["vendor"] for e in _events()] == ["azure_openai"]


def test_azure_stream_is_wrapped_without_stream_options():
    openai = pytest.importorskip("openai")
    http = _openai_http(openai)
    seen = []

    def sse(request):
        seen.append(json.loads(request.content))
        chunk = {"id": "c", "object": "chat.completion.chunk", "created": 0,
                 "model": "gpt-4o", "choices": [{"index": 0,
                                                 "delta": {"content": "hello"}}]}
        return http.Response(200, text=f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n",
                             headers={"content-type": "text/event-stream"})

    prompt_shields.instrument(ps_api_key="ps_key")
    client = openai.AzureOpenAI(
        api_key="k", api_version="2024-10-21",
        azure_endpoint="https://example.openai.azure.com",
        http_client=http.Client(transport=http.MockTransport(sse)))
    chunks = list(client.chat.completions.create(
        model="dep", messages=MESSAGES, stream=True))
    assert len(chunks) == 1
    assert "stream_options" not in seen[0]
    event = _events()[0]
    assert event["vendor"] == "azure_openai"
    assert event["stream_completed"] is True
    assert event["tokens_estimated"] is True  # no usage chunk was requested


# --- google-genai -------------------------------------------------------------

GEMINI_BODY = {
    "candidates": [{"content": {"role": "model", "parts": [
        {"text": "hi"}, {"functionCall": {"name": "lookup", "args": {}}}]},
        "finishReason": "STOP"}],
    "usageMetadata": {"promptTokenCount": 8, "candidatesTokenCount": 2,
                      "thoughtsTokenCount": 1, "totalTokenCount": 11},
    "modelVersion": "gemini-2.5-flash",
}


def test_gemini_generate_content_sync_and_async():
    pytest.importorskip("google.genai")
    from google import genai
    from google.genai import types

    prompt_shields.instrument(ps_api_key="ps_key", send_prompt_text=True)
    seen = []
    client = genai.Client(api_key="g-key", http_options=types.HttpOptions(
        httpx_client=httpx.Client(transport=_json_transport(httpx, GEMINI_BODY,
                                                            seen=seen)),
        httpx_async_client=httpx.AsyncClient(
            transport=_json_transport(httpx, GEMINI_BODY, seen=seen))))
    resp = client.models.generate_content(
        model="models/gemini-2.5-flash",
        contents=["call 555-123-4567", types.Part(text="thanks")],
        config=types.GenerateContentConfig(system_instruction="be brief"))
    assert resp.candidates[0].content.parts[0].text == "hi"
    assert len(seen) == 1

    event = _events()[0]
    assert event["vendor"] == "google"
    assert event["served_model"] == "gemini-2.5-flash"
    assert (event["tokens_in"], event["tokens_out"]) == (8, 3)
    assert event["tokens_reasoning"] == 1
    assert event["tool_calls_used"] == [{"name": "lookup", "type": "function"}]
    assert event["prompt_text"] == "be brief call 555-123-4567 thanks"
    assert event["detected_pii_types"] == ["phone"]
    assert event["cost"] is not None  # "models/" prefix dropped for pricing
    assert len(event["api_key_fingerprint"]) == 16

    async def run():
        return await client.aio.models.generate_content(
            model="gemini-2.5-flash",
            contents=types.Content(role="user", parts=[types.Part(text="hey")]))

    asyncio.run(run())
    assert len(_events()) == 2
    assert _events()[1]["tokens_in"] == 8


def test_gemini_error_keeps_type_and_emits_nothing():
    pytest.importorskip("google.genai")
    from google import genai
    from google.genai import errors, types

    prompt_shields.instrument(ps_api_key="ps_key")
    body = {"error": {"code": 400, "message": "bad", "status": "INVALID_ARGUMENT"}}
    client = genai.Client(api_key="g-key", http_options=types.HttpOptions(
        httpx_client=httpx.Client(transport=_json_transport(httpx, body, 400))))
    with pytest.raises(errors.ClientError):
        client.models.generate_content(model="gemini-2.5-flash", contents="hi")
    assert _events() == []


# --- mistralai ----------------------------------------------------------------

MISTRAL_BODY = {
    "id": "x", "object": "chat.completion", "created": 0,
    "model": "mistral-small-latest",
    "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
        "role": "assistant", "content": "",
        "tool_calls": [{"id": "t", "type": "function",
                        "function": {"name": "lookup", "arguments": "{}"}}]}}],
    "usage": {"prompt_tokens": 7, "completion_tokens": 2, "total_tokens": 9},
}


def _mistral():
    mistralai = pytest.importorskip("mistralai")
    import httpx2
    from mistralai.client import Mistral
    return mistralai, Mistral, httpx2


def test_mistral_complete_sync_and_async():
    _, Mistral, httpx2 = _mistral()
    prompt_shields.instrument(ps_api_key="ps_key")
    client = Mistral(
        api_key="m-key",
        client=httpx2.Client(transport=_json_transport(httpx2, MISTRAL_BODY)),
        async_client=httpx2.AsyncClient(
            transport=_json_transport(httpx2, MISTRAL_BODY)))
    resp = client.chat.complete(model="mistral-small-latest", messages=MESSAGES)
    assert resp.usage.prompt_tokens == 7

    event = _events()[0]
    assert event["vendor"] == "mistral"
    assert (event["tokens_in"], event["tokens_out"]) == (7, 2)
    assert event["tool_calls_used"] == [{"name": "lookup", "type": "function"}]
    assert event["detected_pii_types"] == ["email"]
    assert event["cost"] is not None
    assert len(event["api_key_fingerprint"]) == 16

    async def run():
        return await client.chat.complete_async(
            model="mistral-small-latest",
            messages=[{"role": "user", "content": [
                {"type": "text", "text": "hi jane@example.com"}]}])

    asyncio.run(run())
    assert len(_events()) == 2
    assert _events()[1]["detected_pii_types"] == ["email"]


def test_mistral_error_keeps_type():
    _, Mistral, httpx2 = _mistral()
    from mistralai.client import errors

    prompt_shields.instrument(ps_api_key="ps_key")
    client = Mistral(api_key="m-key", client=httpx2.Client(
        transport=_json_transport(httpx2, {"message": "no"}, 401)))
    with pytest.raises(errors.SDKError):
        client.chat.complete(model="mistral-small-latest", messages=MESSAGES)
    assert _events() == []


def test_mistral_stream_flag_passes_through_untouched():
    prompt_shields.instrument(ps_api_key="ps_key")
    resource = type("Chat", (), {})()
    assert inst._prepare(resource, "mistral", "mistral", (),
                         {"model": "m", "messages": [], "stream": True}) is None


# --- cohere -------------------------------------------------------------------

COHERE_V2_BODY = {
    "id": "x", "finish_reason": "COMPLETE",
    "message": {"role": "assistant", "content": [{"type": "text", "text": "hi"}],
                "tool_calls": [{"id": "t", "type": "function", "function": {
                    "name": "lookup", "arguments": "{}"}}]},
    "usage": {"billed_units": {"input_tokens": 5, "output_tokens": 2},
              "tokens": {"input_tokens": 50, "output_tokens": 2}},
}
COHERE_V1_BODY = {
    "text": "hi", "generation_id": "g", "response_id": "r",
    "meta": {"billed_units": {"input_tokens": 4, "output_tokens": 1}},
}


def test_cohere_v2_chat_emits_once():
    cohere = pytest.importorskip("cohere")
    prompt_shields.instrument(ps_api_key="ps_key")
    client = cohere.ClientV2(api_key="co-key", httpx_client=httpx.Client(
        transport=_json_transport(httpx, COHERE_V2_BODY)))
    resp = client.chat(model="command-r-08-2024", messages=MESSAGES)
    assert resp.message.content[0].text == "hi"

    assert len(_events()) == 1
    event = _events()[0]
    assert event["vendor"] == "cohere"
    assert (event["tokens_in"], event["tokens_out"]) == (5, 2)
    assert event["tool_calls_used"] == [{"name": "lookup", "type": "function"}]
    assert event["detected_pii_types"] == ["email"]
    assert event["cost"] is not None
    assert len(event["api_key_fingerprint"]) == 16


def test_cohere_v1_chat_and_async_v2():
    cohere = pytest.importorskip("cohere")
    prompt_shields.instrument(ps_api_key="ps_key", send_prompt_text=True)
    client = cohere.Client(api_key="co-key", httpx_client=httpx.Client(
        transport=_json_transport(httpx, COHERE_V1_BODY)))
    client.chat(message="ring 555-123-4567", preamble="be brief",
                chat_history=[{"role": "USER", "message": "earlier"}])
    event = _events()[0]
    assert (event["tokens_in"], event["tokens_out"]) == (4, 1)
    assert event["prompt_text"] == "be brief earlier ring 555-123-4567"
    assert event["model"] is None  # v1 model is optional

    async def run():
        client = cohere.AsyncClientV2(api_key="co-key", httpx_client=httpx.AsyncClient(
            transport=_json_transport(httpx, COHERE_V2_BODY)))
        return await client.chat(model="command-r-08-2024", messages=MESSAGES)

    asyncio.run(run())
    assert len(_events()) == 2
    assert _events()[1]["tokens_in"] == 5


def test_cohere_error_keeps_type():
    cohere = pytest.importorskip("cohere")
    from cohere.core.api_error import ApiError

    prompt_shields.instrument(ps_api_key="ps_key")
    client = cohere.ClientV2(api_key="co-key", httpx_client=httpx.Client(
        transport=_json_transport(httpx, {"message": "bad"}, 400)))
    with pytest.raises(ApiError):
        client.chat(model="command-r-08-2024", messages=MESSAGES)
    assert _events() == []


# --- Bedrock (boto3) ----------------------------------------------------------


class _RawBody:
    """The urllib3 response surface botocore reads (`stream` and `read`)."""

    def __init__(self, data: bytes):
        self._buf = io.BytesIO(data)
        self.reads = 0

    def stream(self, *args, **kwargs):
        self.reads += 1
        yield self._buf.read()

    def read(self, *args, **kwargs):
        self.reads += 1
        return self._buf.read(*args)


@pytest.fixture
def aws(monkeypatch):
    pytest.importorskip("boto3")
    for name, value in (("AWS_ACCESS_KEY_ID", "AKIDEXAMPLE"),
                        ("AWS_SECRET_ACCESS_KEY", "secret"),
                        ("AWS_DEFAULT_REGION", "us-east-1")):
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("AWS_PROFILE", raising=False)


def _boto_client(service, status, body: bytes, headers=None, raws=None):
    import boto3
    from botocore.awsrequest import AWSResponse

    client = boto3.client(service, region_name="us-east-1")

    def send(request, **kwargs):
        raw = _RawBody(body)
        if raws is not None:
            raws.append(raw)
        return AWSResponse(request.url, status, {
            "content-type": "application/json", **(headers or {})}, raw)

    client.meta.events.register(f"before-send.{service}", send)
    return client


CONVERSE_BODY = {
    "output": {"message": {"role": "assistant", "content": [
        {"text": "hi"}, {"toolUse": {"toolUseId": "t", "name": "lookup",
                                     "input": {}}}]}},
    "stopReason": "tool_use",
    "usage": {"inputTokens": 10, "outputTokens": 3, "totalTokens": 25,
              "cacheReadInputTokens": 12},
    "metrics": {"latencyMs": 5},
}


def test_bedrock_converse(aws):
    prompt_shields.instrument(ps_api_key="ps_key", send_prompt_text=True)
    client = _boto_client("bedrock-runtime", 200, json.dumps(CONVERSE_BODY).encode())
    resp = client.converse(
        modelId="us.amazon.nova-lite-v1:0",
        system=[{"text": "be brief"}],
        messages=[{"role": "user", "content": [{"text": "jane@example.com"}]}])
    assert resp["usage"]["inputTokens"] == 10

    event = _events()[0]
    assert event["vendor"] == "bedrock"
    assert event["requested_model"] == "us.amazon.nova-lite-v1:0"
    assert (event["tokens_in"], event["tokens_out"]) == (22, 3)
    assert event["tokens_cached"] == 12
    assert event["tool_calls_used"] == [{"name": "lookup", "type": "tool_use"}]
    assert event["prompt_text"] == "be brief jane@example.com"
    assert event["detected_pii_types"] == ["email"]
    assert event["cost"] is not None  # profile prefix dropped for pricing
    assert event["api_key_fingerprint"] is None


def test_bedrock_invoke_model_reads_headers_never_the_body(aws):
    prompt_shields.instrument(ps_api_key="ps_key")
    raws = []
    payload = json.dumps({"content": [{"type": "text", "text": "hi"}]}).encode()
    client = _boto_client(
        "bedrock-runtime", 200, payload, raws=raws, headers={
            "x-amzn-bedrock-input-token-count": "21",
            "x-amzn-bedrock-output-token-count": "4"})
    resp = client.invoke_model(
        modelId="anthropic.claude-3-5-haiku-20241022-v1:0",
        body=json.dumps({"anthropic_version": "bedrock-2023-05-31",
                         "max_tokens": 10, "messages": [
                             {"role": "user", "content": "jane@example.com"}]}))

    assert raws[0].reads == 0  # instrumentation did not touch the stream
    event = _events()[0]
    assert (event["tokens_in"], event["tokens_out"]) == (21, 4)
    assert event["detected_pii_types"] == ["email"]
    assert event["cost"] is not None
    assert json.loads(resp["body"].read()) == json.loads(payload)


def test_bedrock_error_keeps_type_and_other_services_pass_through(aws):
    from botocore.exceptions import ClientError

    prompt_shields.instrument(ps_api_key="ps_key")
    error = json.dumps({"message": "bad model"}).encode()
    client = _boto_client("bedrock-runtime", 400, error, headers={
        "x-amzn-errortype": "ValidationException"})
    with pytest.raises(client.exceptions.ValidationException) as info:
        client.converse(modelId="m", messages=[])
    assert isinstance(info.value, ClientError)

    # Other operations and services are left alone.
    control = _boto_client("bedrock", 200, b'{"modelSummaries": []}')
    control.list_foundation_models()
    assert _events() == []


def test_bedrock_vendor_filter_and_uninstrument_restore(aws):
    from botocore.client import BaseClient

    original = BaseClient.__dict__["_make_api_call"]
    prompt_shields.instrument(ps_api_key="ps_key", vendors=("openai",))
    assert BaseClient.__dict__["_make_api_call"] is original

    prompt_shields.uninstrument()
    prompt_shields.instrument(ps_api_key="ps_key")
    assert BaseClient.__dict__["_make_api_call"] is not original
    prompt_shields.uninstrument()
    assert BaseClient.__dict__["_make_api_call"] is original


def test_suppression_covers_new_vendors(aws):
    prompt_shields.instrument(ps_api_key="ps_key")
    client = _boto_client("bedrock-runtime", 200, json.dumps(CONVERSE_BODY).encode())
    with prompt_shields.suppress_instrumentation():
        client.converse(modelId="m", messages=[])
    assert _events() == []


def test_telemetry_failure_never_breaks_bedrock_call(aws, monkeypatch):
    prompt_shields.instrument(ps_api_key="ps_key")
    monkeypatch.setattr(inst, "_event", lambda *a, **k: 1 / 0)
    client = _boto_client("bedrock-runtime", 200, json.dumps(CONVERSE_BODY).encode())
    assert client.converse(modelId="m", messages=[])["usage"]["outputTokens"] == 3


# --- message flattening -------------------------------------------------------


def test_bedrock_body_messages_shapes():
    assert inst._bedrock_body_messages(io.BytesIO(b"{}")) == []
    assert inst._bedrock_body_messages(b"not json") == []
    assert inst._bedrock_body_messages(json.dumps({
        "system": [{"text": "sys"}],
        "messages": [{"role": "user", "content": [{"text": "a"}, {"image": {}}]}],
    })) == [{"role": "system", "content": "sys"}, {"role": "user", "content": "a"}]
    assert inst._bedrock_body_messages(b'{"prompt": "p"}') == [
        {"role": "user", "content": "p"}]
