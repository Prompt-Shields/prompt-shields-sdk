"""Claude on Google Vertex AI and Amazon Bedrock (#388).

The Anthropic SDK's `AnthropicVertex`, `AnthropicBedrock` and
`AnthropicBedrockMantle` clients (sync, async and streaming) replay
`fixtures/claude_platforms.json` through a mock transport, under
`instrument()` and through the `ShieldsClient` factories. Each event must
carry the platform vendor, the model, the tokens and the platform's price.
The Bedrock runtime stream (AWS event stream framing) needs botocore and is
skipped without it. No network, no cloud credentials.
"""

import asyncio
import base64
import binascii
import json
import struct
from pathlib import Path

import prompt_shields
import pytest
from prompt_shields import cloud_platforms
from prompt_shields.cloud_platforms import CloudPlatform, region_jurisdiction
from prompt_shields.instrumentation import FrameworkCall, apply_framework_fields
from prompt_shields.integrations._common import langgraph_tags, platform_vendor
from prompt_shields.pricing import normalise_claude_model, usage_cost

from .test_instrumentation import (  # noqa: F401
    MESSAGES,
    RecordingSender,
    _events,
    _http,
    recording,
)

FIXTURES = json.loads(
    (Path(__file__).parent / "fixtures" / "claude_platforms.json").read_text()
)
MODEL_IDS = json.loads(
    (Path(__file__).resolve().parents[3] / "schemas" / "pricing"
     / "claude-model-ids.v1.json").read_text()
)["ids"]

GLOBAL = 0.033          # 1000 x $3 + 2000 x $15 per 1M (Anthropic list)
REGIONAL = 0.0363       # the same, +10% on a regional endpoint
CACHED_REGIONAL = 0.003454  # (600 x $1 + 400 x $0.10 + 500 x $5) x 1.1


# --- model ids, regions, pricing -------------------------------------------------


@pytest.mark.parametrize("case", MODEL_IDS, ids=[c["platform_id"] or "empty"
                                                 for c in MODEL_IDS])
def test_model_ids_normalise_as_the_gateway_does(case):
    assert normalise_claude_model(case["platform_id"]) == case["anthropic_id"]


def test_platform_ids_are_priced_at_anthropic_list_prices():
    usage = {"tokens_in": 1000, "tokens_out": 2000}
    adapter = prompt_shields.providers.get_adapter("google-vertex-ai")
    model = adapter.pricing_model("claude-sonnet-4-5@20250929", None)
    assert model == "claude-sonnet-4-5"
    assert usage_cost("google-vertex-ai", model, usage) == pytest.approx(GLOBAL)
    assert usage_cost("google-vertex-ai", model, {
        **usage, "endpoint_scope": "regional"}) == pytest.approx(REGIONAL)
    # A custom pricing table is used as given: no premium on top of it.
    table = {("google-vertex-ai", "claude-sonnet-4-5"): (0.001, 0.002)}
    assert usage_cost("google-vertex-ai", "claude-sonnet-4-5", {
        **usage, "endpoint_scope": "regional"}, pricing_table=table) == 0.005


def test_bedrock_rows_still_price_their_own_ids():
    adapter = prompt_shields.providers.get_adapter("aws-bedrock")
    model = adapter.pricing_model(
        "us.anthropic.claude-3-5-haiku-20241022-v1:0", None)
    assert model == "anthropic.claude-3-5-haiku-20241022-v1:0"
    # The bedrock row's own cache-read rate.
    assert usage_cost("aws-bedrock", model, {
        "tokens_in": 1000, "tokens_out": 1000, "tokens_cached": 500,
    }) == pytest.approx(0.00444)


@pytest.mark.parametrize("region,expected", [
    ("global", None), ("us", "US"), ("eu", "EEA"), ("us-east5", "US"),
    ("europe-west1", "EEA"), ("europe-west2", "UK"), ("europe-west6", "CH"),
    ("eu-central-1", "EEA"), ("eu-west-2", "UK"), ("eu-central-2", "CH"),
    ("ap-northeast-1", "JP"), ("ca-central-1", "CA"), ("me-west1", None),
    (None, None),
])
def test_region_jurisdiction(region, expected):
    assert region_jurisdiction(region) == expected


def test_bedrock_profile_and_arn_fields():
    platform = CloudPlatform("aws-bedrock", "us-east-1")
    arn = ("arn:aws:bedrock:eu-west-1:123456789012:inference-profile/"
           "eu.anthropic.claude-sonnet-4-5-20250929-v1:0")
    assert platform.fields(arn) == {
        "model_family": "anthropic", "region": "us-east-1",
        "served_jurisdiction": "EEA", "cloud_account": "123456789012",
    }
    assert platform.endpoint_scope(arn) == "regional"
    # A global profile may run anywhere: no jurisdiction from the region.
    global_id = "global.anthropic.claude-sonnet-4-5-20250929-v1:0"
    assert "served_jurisdiction" not in platform.fields(global_id)
    assert platform.endpoint_scope(global_id) == "global"
    # boto3 calls to other makers keep their family.
    boto = CloudPlatform("bedrock", "us-east-1")
    assert boto.fields("us.amazon.nova-lite-v1:0")["model_family"] == "amazon"


# --- real Anthropic SDK clients under instrument() --------------------------------


def _anthropic():
    return pytest.importorskip("anthropic")


def _json(http, body, seen=None):
    def handle(request):
        if seen is not None:
            seen.append(request)
        return http.Response(200, json=body)
    return http.MockTransport(handle)


def _sse(http, events, seen=None):
    def handle(request):
        if seen is not None:
            seen.append(request)
        text = "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n"
                       for e in events)
        return http.Response(200, text=text,
                             headers={"content-type": "text/event-stream"})
    return http.MockTransport(handle)


def _vertex(anthropic, transport, region, *, is_async=False):
    http = _http(anthropic)
    cls = anthropic.AsyncAnthropicVertex if is_async else anthropic.AnthropicVertex
    client_cls = http.AsyncClient if is_async else http.Client
    return cls(project_id="ps-test-project", region=region,
               access_token="ya29.test",
               http_client=client_cls(transport=transport))


def _bedrock(anthropic, transport, *, is_async=False, mantle=False):
    http = _http(anthropic)
    if mantle:
        cls = (getattr(anthropic, "AsyncAnthropicBedrockMantle", None) if is_async
               else getattr(anthropic, "AnthropicBedrockMantle", None))
    else:
        cls = anthropic.AsyncAnthropicBedrock if is_async else anthropic.AnthropicBedrock
    if cls is None:
        pytest.skip("this anthropic version has no Bedrock Mantle client")
    client_cls = http.AsyncClient if is_async else http.Client
    try:
        return cls(api_key="bedrock-api-key-test", aws_region="eu-central-1",
                   http_client=client_cls(transport=transport))
    except TypeError:
        pytest.skip("this anthropic version takes no Bedrock API key")


def test_vertex_sync_global(recording):  # noqa: F811
    anthropic = _anthropic()
    prompt_shields.instrument(ps_api_key="ps_key")
    http = _http(anthropic)
    seen = []
    client = _vertex(anthropic, _json(http, FIXTURES["message"], seen), "global")
    reply = client.messages.create(model="claude-sonnet-4-5@20250929",
                                   max_tokens=10, messages=MESSAGES)
    assert reply.content[0].text == "hi"
    assert "/projects/ps-test-project/locations/global/" in str(seen[0].url)
    (event,) = _events()
    assert event["vendor"] == "google-vertex-ai"
    assert event["model_family"] == "anthropic"
    assert event["model"] == "claude-sonnet-4-5@20250929"
    assert event["served_model"] == "claude-sonnet-4-5-20250929"
    assert (event["tokens_in"], event["tokens_out"]) == (1000, 2000)
    assert event["cost"] == pytest.approx(GLOBAL)
    assert event["cost_source"] == "pricing_table"
    assert event["endpoint_scope"] == "global"
    assert event["region"] == "global"
    assert event["cloud_project"] == "ps-test-project"
    assert "served_jurisdiction" not in event


def test_vertex_async_regional(recording):  # noqa: F811
    anthropic = _anthropic()
    prompt_shields.instrument(ps_api_key="ps_key", vendors=("anthropic",))
    http = _http(anthropic)

    async def run():
        client = _vertex(anthropic, _json(http, FIXTURES["message"]),
                         "europe-west1", is_async=True)
        return await client.messages.create(
            model="claude-sonnet-4-5@20250929", max_tokens=10, messages=MESSAGES)

    assert asyncio.run(run()).content[0].text == "hi"
    (event,) = _events()
    assert event["vendor"] == "google-vertex-ai"
    assert event["cost"] == pytest.approx(REGIONAL)
    assert event["endpoint_scope"] == "regional"
    assert event["served_jurisdiction"] == "EEA"


def test_vertex_streaming_create_and_helper(recording):  # noqa: F811
    anthropic = _anthropic()
    prompt_shields.instrument(ps_api_key="ps_key")
    http = _http(anthropic)
    client = _vertex(anthropic, _sse(http, FIXTURES["stream"]), "us-east5")
    stream = client.messages.create(model="claude-sonnet-4-5@20250929",
                                    max_tokens=10, messages=MESSAGES, stream=True)
    assert [e.type for e in stream][-1] == "message_stop"
    with client.messages.stream(model="claude-sonnet-4-5@20250929",
                                max_tokens=10, messages=MESSAGES) as helper:
        assert helper.get_final_text() == "hi"
    events = _events()
    assert len(events) == 2
    for event in events:
        assert event["vendor"] == "google-vertex-ai"
        assert (event["tokens_in"], event["tokens_out"]) == (1000, 2000)
        assert event["cost"] == pytest.approx(REGIONAL)
        assert event["served_jurisdiction"] == "US"
        assert event["stream"] is True


def test_vertex_async_streaming(recording):  # noqa: F811
    anthropic = _anthropic()
    prompt_shields.instrument(ps_api_key="ps_key")
    http = _http(anthropic)

    async def run():
        client = _vertex(anthropic, _sse(http, FIXTURES["stream"]), "global",
                         is_async=True)
        stream = await client.messages.create(
            model="claude-sonnet-4-5@20250929", max_tokens=10,
            messages=MESSAGES, stream=True)
        return [e.type async for e in stream]

    assert asyncio.run(run())[-1] == "message_stop"
    (event,) = _events()
    assert event["vendor"] == "google-vertex-ai"
    assert event["cost"] == pytest.approx(GLOBAL)


def test_bedrock_sync_and_async(recording):  # noqa: F811
    anthropic = _anthropic()
    prompt_shields.instrument(ps_api_key="ps_key")
    http = _http(anthropic)
    seen = []
    client = _bedrock(anthropic, _json(http, FIXTURES["message_cached"], seen))
    model = "eu.anthropic.claude-haiku-4-5-20251001-v1:0"
    client.messages.create(model=model, max_tokens=10, messages=MESSAGES)
    assert f"/model/{model}/invoke" in str(seen[0].url)

    async def run():
        aclient = _bedrock(anthropic, _json(http, FIXTURES["message"]),
                           is_async=True)
        return await aclient.messages.create(
            model="global.anthropic.claude-sonnet-4-5-20250929-v1:0",
            max_tokens=10, messages=MESSAGES)

    asyncio.run(run())
    regional, global_ = _events()
    assert regional["vendor"] == global_["vendor"] == "aws-bedrock"
    assert regional["model"] == model
    assert (regional["tokens_in"], regional["tokens_out"]) == (1000, 500)
    assert regional["tokens_cached"] == 400
    assert regional["cost"] == pytest.approx(CACHED_REGIONAL)
    assert regional["region"] == "eu-central-1"
    assert regional["served_jurisdiction"] == "EEA"
    assert global_["cost"] == pytest.approx(GLOBAL)
    assert global_["endpoint_scope"] == "global"
    assert "served_jurisdiction" not in global_


def test_bedrock_mantle_streaming(recording):  # noqa: F811
    anthropic = _anthropic()
    prompt_shields.instrument(ps_api_key="ps_key")
    http = _http(anthropic)
    client = _bedrock(anthropic, _sse(http, FIXTURES["stream"]), mantle=True)
    stream = client.messages.create(model="anthropic.claude-sonnet-4-5",
                                    max_tokens=10, messages=MESSAGES, stream=True)
    list(stream)
    (event,) = _events()
    assert event["vendor"] == "aws-bedrock"
    assert (event["tokens_in"], event["tokens_out"]) == (1000, 2000)
    assert event["cost"] == pytest.approx(REGIONAL)


def _aws_event_frame(payload: dict) -> bytes:
    """One AWS event stream message (vnd.amazon.eventstream), as Bedrock's
    InvokeModelWithResponseStream sends each chunk."""
    def header(name, value):
        n, v = name.encode(), value.encode()
        return bytes([len(n)]) + n + b"\x07" + struct.pack(">H", len(v)) + v

    headers = (header(":event-type", "chunk")
               + header(":content-type", "application/json")
               + header(":message-type", "event"))
    body = json.dumps({"bytes": base64.b64encode(
        json.dumps(payload).encode()).decode()}).encode()
    total = 12 + len(headers) + len(body) + 4
    prelude = struct.pack(">II", total, len(headers))
    prelude += struct.pack(">I", binascii.crc32(prelude) & 0xFFFFFFFF)
    message = prelude + headers + body
    return message + struct.pack(">I", binascii.crc32(message) & 0xFFFFFFFF)


def test_bedrock_runtime_streaming(recording):  # noqa: F811
    anthropic = _anthropic()
    pytest.importorskip("botocore")
    prompt_shields.instrument(ps_api_key="ps_key")
    http = _http(anthropic)
    frames = b"".join(_aws_event_frame(e) for e in FIXTURES["stream"])
    transport = http.MockTransport(lambda request: http.Response(
        200, content=frames,
        headers={"content-type": "application/vnd.amazon.eventstream"}))
    client = _bedrock(anthropic, transport)
    stream = client.messages.create(
        model="us.anthropic.claude-sonnet-4-5-20250929-v1:0", max_tokens=10,
        messages=MESSAGES, stream=True)
    assert [e.type for e in stream][-1] == "message_stop"
    (event,) = _events()
    assert event["vendor"] == "aws-bedrock"
    assert (event["tokens_in"], event["tokens_out"]) == (1000, 2000)
    assert event["cost"] == pytest.approx(REGIONAL)
    assert event["served_jurisdiction"] == "US"


def test_vendor_filter(recording):  # noqa: F811
    anthropic = _anthropic()
    http = _http(anthropic)
    prompt_shields.instrument(ps_api_key="ps_key", vendors=("openai",))
    _vertex(anthropic, _json(http, FIXTURES["message"]), "global").messages.create(
        model="claude-sonnet-4-5@20250929", max_tokens=10, messages=MESSAGES)
    assert _events() == []
    prompt_shields.uninstrument()
    RecordingSender.instances = []
    prompt_shields.instrument(ps_api_key="ps_key", vendors=("google-vertex-ai",))
    _vertex(anthropic, _json(http, FIXTURES["message"]), "global").messages.create(
        model="claude-sonnet-4-5@20250929", max_tokens=10, messages=MESSAGES)
    assert _events()[0]["vendor"] == "google-vertex-ai"


def test_unknown_claude_model_is_flagged_not_zero(recording, caplog):  # noqa: F811
    anthropic = _anthropic()
    prompt_shields.instrument(ps_api_key="ps_key")
    http = _http(anthropic)
    body = {**FIXTURES["message"], "model": "claude-unknown-9-20990101"}
    _vertex(anthropic, _json(http, body), "global").messages.create(
        model="claude-unknown-9@20990101", max_tokens=10, messages=MESSAGES)
    (event,) = _events()
    assert event["cost"] is None
    assert "cost_source" not in event
    assert event["model_unpriced"] is True
    assert "claude-unknown-9@20990101" in caplog.text


def test_first_party_client_is_unchanged(recording):  # noqa: F811
    anthropic = _anthropic()
    prompt_shields.instrument(ps_api_key="ps_key")
    http = _http(anthropic)
    client = anthropic.Anthropic(
        api_key="sk-ant-test",
        http_client=http.Client(transport=_json(http, FIXTURES["message"])))
    client.messages.create(model="claude-sonnet-4-5", max_tokens=10,
                           messages=MESSAGES)
    (event,) = _events()
    assert event["vendor"] == "anthropic"
    for key in ("region", "model_family", "endpoint_scope", "served_jurisdiction"):
        assert key not in event
    assert event["cost"] == pytest.approx(GLOBAL)


# --- ShieldsClient / AsyncShieldsClient factories ---------------------------------


def _record(client):
    client._telemetry = RecordingSender()
    return client._telemetry


def test_sync_factories():
    anthropic = _anthropic()
    http = _http(anthropic)
    vertex = prompt_shields.ShieldsAnthropicVertex(
        ps_api_key="ps_key", project_id="ps-test-project", region="us-east5",
        access_token="ya29.test",
        http_client=http.Client(transport=_json(http, FIXTURES["message"])))
    assert isinstance(vertex._upstream, anthropic.AnthropicVertex)
    sent = _record(vertex)
    vertex.chat.completions.create(model="claude-sonnet-4-5@20250929",
                                   messages=MESSAGES, max_tokens=10)
    (event,) = sent.events
    assert event["vendor"] == "google-vertex-ai"
    assert event["cost"] == pytest.approx(REGIONAL)
    assert event["served_jurisdiction"] == "US"
    assert event["cloud_project"] == "ps-test-project"

    try:
        bedrock = prompt_shields.ShieldsAnthropicBedrock(
            ps_api_key="ps_key", api_key="bedrock-api-key-test",
            aws_region="eu-central-1",
            http_client=http.Client(transport=_sse(http, FIXTURES["stream"])))
    except TypeError:
        pytest.skip("this anthropic version takes no Bedrock API key")
    assert isinstance(bedrock._upstream, anthropic.AnthropicBedrock)
    sent = _record(bedrock)
    # A JSON body is enough for the non-streaming path; stream through Mantle
    # is covered above.
    bedrock._upstream = anthropic.AnthropicBedrock(
        api_key="bedrock-api-key-test", aws_region="eu-central-1",
        http_client=http.Client(transport=_json(http, FIXTURES["message_cached"])))
    bedrock.chat.completions.create(
        model="eu.anthropic.claude-haiku-4-5-20251001-v1:0", messages=MESSAGES,
        max_tokens=10)
    (event,) = sent.events
    assert event["vendor"] == "aws-bedrock"
    assert event["cost"] == pytest.approx(CACHED_REGIONAL)


def test_async_factories():
    anthropic = _anthropic()
    http = _http(anthropic)

    async def run():
        vertex = prompt_shields.AsyncShieldsAnthropicVertex(
            ps_api_key="ps_key", project_id="ps-test-project", region="global",
            access_token="ya29.test",
            http_client=http.AsyncClient(transport=_json(http, FIXTURES["message"])))
        assert isinstance(vertex._upstream, anthropic.AsyncAnthropicVertex)
        vertex_sent = _record(vertex)
        await vertex.chat.completions.create(
            model="claude-sonnet-4-5@20250929", messages=MESSAGES, max_tokens=10)
        stream = await vertex.chat.completions.create(
            model="claude-sonnet-4-5@20250929", messages=MESSAGES, max_tokens=10,
            stream=True)
        vertex._upstream = anthropic.AsyncAnthropicVertex(
            project_id="ps-test-project", region="global", access_token="ya29.test",
            http_client=http.AsyncClient(transport=_sse(http, FIXTURES["stream"])))
        stream = await vertex.chat.completions.create(
            model="claude-sonnet-4-5@20250929", messages=MESSAGES, max_tokens=10,
            stream=True)
        async for _ in stream:
            pass
        try:
            bedrock = prompt_shields.AsyncShieldsAnthropicBedrock(
                ps_api_key="ps_key", api_key="bedrock-api-key-test",
                aws_region="eu-central-1",
                http_client=http.AsyncClient(
                    transport=_json(http, FIXTURES["message_cached"])))
        except TypeError:
            return vertex_sent, None
        assert isinstance(bedrock._upstream, anthropic.AsyncAnthropicBedrock)
        bedrock_sent = _record(bedrock)
        await bedrock.chat.completions.create(
            model="eu.anthropic.claude-haiku-4-5-20251001-v1:0",
            messages=MESSAGES, max_tokens=10)
        return vertex_sent, bedrock_sent

    vertex_sent, bedrock_sent = asyncio.run(run())
    plain, streamed = vertex_sent.events[0], vertex_sent.events[-1]
    assert plain["vendor"] == streamed["vendor"] == "google-vertex-ai"
    assert plain["cost"] == pytest.approx(GLOBAL)
    assert (streamed["tokens_in"], streamed["tokens_out"]) == (1000, 2000)
    assert streamed["cost"] == pytest.approx(GLOBAL)
    if bedrock_sent is not None:
        (event,) = bedrock_sent.events
        assert event["vendor"] == "aws-bedrock"
        assert event["cost"] == pytest.approx(CACHED_REGIONAL)


def test_factory_rejects_other_vendors():
    with pytest.raises(ValueError):
        cloud_platforms.make_client("openai", None, is_async=False)


# --- LangGraph node attribution ---------------------------------------------------


def test_langgraph_tags():
    assert langgraph_tags({"langgraph_node": "draft"}) == {"langgraph_node": "draft"}
    assert langgraph_tags({"langgraph_node": "a,b=c"}) == {"langgraph_node": "a_b_c"}
    assert langgraph_tags({"langgraph_node": "x" * 300})["langgraph_node"] == "x" * 128
    assert langgraph_tags({}) is None
    assert langgraph_tags(None) is None


def test_framework_tags_merge_under_the_events_own():
    fc = FrameworkCall("langchain", tags={"langgraph_node": "draft", "team": "a"})
    event = {"tags": {"team": "b"}}
    apply_framework_fields(event, fc)
    assert event["tags"] == {"langgraph_node": "draft", "team": "b"}
    event = {}
    apply_framework_fields(event, fc)
    assert event["tags"] == {"langgraph_node": "draft", "team": "a"}
    event = {"tags": "team=b"}  # the header form is left as sent
    apply_framework_fields(event, fc)
    assert event["tags"] == "team=b"


def test_claude_through_a_google_framework_provider_is_vertex():
    assert platform_vendor("google", "claude-sonnet-4-5@20250929") == "google-vertex-ai"
    assert platform_vendor("google", "gemini-2.5-pro") == "google"
    assert platform_vendor("anthropic", "claude-sonnet-4-5") == "anthropic"


def test_langgraph_two_node_graph_reports_usage_per_node(recording):  # noqa: F811
    """The example in demo/demo_langgraph.py, with a local chat model: each
    node's model call is one event tagged with its node."""
    pytest.importorskip("langgraph")
    pytest.importorskip("langchain_core")
    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.messages import AIMessage
    from langchain_core.outputs import ChatGeneration, ChatResult
    from langgraph.graph import END, START, StateGraph
    from prompt_shields.integrations.langchain import PromptShieldsCallbackHandler
    from typing_extensions import TypedDict

    class LocalClaude(BaseChatModel):
        tokens_out: int = 10

        @property
        def _llm_type(self):
            return "local"

        def _get_ls_params(self, stop=None, **kwargs):
            return {"ls_provider": "google_vertexai",
                    "ls_model_name": "claude-sonnet-4-5@20250929",
                    "ls_model_type": "chat"}

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            message = AIMessage(content="ok", usage_metadata={
                "input_tokens": 1000, "output_tokens": self.tokens_out,
                "total_tokens": 1000 + self.tokens_out})
            return ChatResult(generations=[ChatGeneration(message=message)])

    class State(TypedDict):
        text: str

    drafter, reviewer = LocalClaude(tokens_out=2000), LocalClaude(tokens_out=500)

    def draft(state):
        return {"text": drafter.invoke(state["text"]).content}

    def review(state):
        return {"text": reviewer.invoke(state["text"]).content}

    graph = StateGraph(State)
    graph.add_node("draft", draft)
    graph.add_node("review", review)
    graph.add_edge(START, "draft")
    graph.add_edge("draft", "review")
    graph.add_edge("review", END)

    prompt_shields.instrument(ps_api_key="ps_key")
    graph.compile().invoke({"text": "hi"},
                           config={"callbacks": [PromptShieldsCallbackHandler()]})
    by_node = {e["tags"]["langgraph_node"]: e for e in _events()}
    assert set(by_node) == {"draft", "review"}
    assert by_node["draft"]["tokens_out"] == 2000
    assert by_node["review"]["tokens_out"] == 500
    assert by_node["draft"]["vendor"] == "google-vertex-ai"
    assert by_node["draft"]["cost"] == pytest.approx(GLOBAL)
    assert all(e["framework"] == "langchain" for e in by_node.values())
