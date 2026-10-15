"""instrument() lifecycle: lazy vendor patching, abandoned streams, cohere
clients created before instrument().

Real SDKs (skipped when not installed) over mocked HTTP, as in
test_instrumentation_vendors.
"""

import asyncio
import gc
import importlib.util
import json
import os
import subprocess
import sys
import textwrap
import threading

import httpx
import prompt_shields
import pytest
from prompt_shields import instrumentation as inst

from .test_instrumentation_vendor_streams import (
    COHERE_V1_EVENTS,
    COHERE_V2_EVENTS,
    CONVERSE_STREAM,
    _anthropic,
    _anthropic_transport,
    _event_stream_client,
    _gemini_client,
    _mistral_client,
    _sse,
)
from .test_instrumentation_vendors import (  # noqa: F401 - fixtures
    COHERE_V1_BODY,
    COHERE_V2_BODY,
    MESSAGES,
    _events,
    _json_transport,
    aws,
    recording,
)

SDK_DIR = os.path.dirname(os.path.dirname(os.path.abspath(inst.__file__)))


# --- lazy vendor patching ------------------------------------------------------


def _installed(*names):
    for name in names:
        try:
            if importlib.util.find_spec(name) is None:
                return False
        except ModuleNotFoundError:
            return False
    return True


@pytest.mark.skipif(
    not _installed("openai", "anthropic", "google.genai", "mistralai", "cohere",
                   "boto3"),
    reason="needs every provider SDK")
def test_instrument_does_not_import_lazy_vendors():
    # A fresh interpreter: instrument() imports only openai and anthropic;
    # the other SDKs are patched when the app imports them, and
    # uninstrument() removes the import hook and restores every method.
    code = textwrap.dedent("""
        import sys
        import prompt_shields
        from prompt_shields import instrumentation as inst

        prompt_shields.instrument(ps_api_key="x")
        lazy = ("google.genai", "mistralai", "cohere", "botocore")
        print(json.dumps([m for m in lazy if m in sys.modules]))
        before = len(inst._state.patches)

        import boto3, cohere
        from google import genai
        from mistralai.client import Mistral
        cohere.ClientV2, cohere.AsyncClient, genai.Client, Mistral

        from botocore.client import BaseClient
        from cohere.raw_base_client import RawBaseCohere
        from cohere.v2.raw_client import AsyncRawV2Client
        from google.genai.models import AsyncModels
        from mistralai.client.chat import Chat
        patched = [BaseClient._make_api_call, RawBaseCohere.chat_stream,
                   AsyncRawV2Client.chat, AsyncModels.generate_content_stream,
                   Chat.stream_async]
        print(json.dumps([before, all(hasattr(m, "__ps_original__")
                                      for m in patched)]))
        mod = sys.modules["cohere.raw_base_client"]
        print(json.dumps(type(mod.__loader__).__name__ == "SourceFileLoader"
                         and mod.__spec__.loader is mod.__loader__))

        prompt_shields.uninstrument()
        print(json.dumps([
            any(isinstance(f, inst._PatchOnImport) for f in sys.meta_path),
            any(hasattr(m, "__ps_original__") for m in (
                BaseClient._make_api_call, RawBaseCohere.chat_stream,
                AsyncRawV2Client.chat, AsyncModels.generate_content_stream,
                Chat.stream_async))]))
    """)
    out = subprocess.run(
        [sys.executable, "-c", "import json\n" + code], capture_output=True,
        text=True, check=True, env={**os.environ, "PYTHONPATH": SDK_DIR})
    lines = [json.loads(line) for line in out.stdout.splitlines()]
    assert lines[0] == []  # none of the lazy SDKs imported by instrument()
    assert lines[1] == [12, True]  # openai + anthropic only, then the rest
    assert lines[2] is True  # the module keeps its own loader
    assert lines[3] == [False, False]


@pytest.fixture
def fake_sdk(tmp_path, monkeypatch):
    """A package `ps_fake_sdk` whose `models.Models.generate_content` is a
    lazy (google) target, not yet imported."""
    pkg = tmp_path / "ps_fake_sdk"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "models.py").write_text(textwrap.dedent("""
        class Models:
            def generate_content(self, **kwargs):
                return None
    """))
    (pkg / "broken.py").write_text("raise RuntimeError('broken module')\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(inst, "_TARGETS", (
        ("google", "ps_fake_sdk.models", "Models", "generate_content", "gemini"),
        ("google", "ps_fake_sdk.broken", "Models", "generate_content", "gemini"),
    ))
    yield
    for name in ("ps_fake_sdk", "ps_fake_sdk.models", "ps_fake_sdk.broken"):
        sys.modules.pop(name, None)


@pytest.mark.usefixtures("fake_sdk")
def test_lazy_target_patched_on_first_import():
    prompt_shields.instrument(ps_api_key="ps_key")
    assert "ps_fake_sdk.models" not in sys.modules
    from ps_fake_sdk import models

    assert hasattr(models.Models.generate_content, "__ps_original__")
    assert type(models.__loader__).__name__ == "SourceFileLoader"
    assert models.__spec__.loader is models.__loader__
    with pytest.raises(RuntimeError, match="broken module"):
        importlib.import_module("ps_fake_sdk.broken")  # errors pass through

    prompt_shields.uninstrument()
    assert not hasattr(models.Models.generate_content, "__ps_original__")
    assert not any(isinstance(f, inst._PatchOnImport) for f in sys.meta_path)


@pytest.mark.usefixtures("fake_sdk")
def test_lazy_target_already_imported_is_patched_at_once():
    from ps_fake_sdk import models

    prompt_shields.instrument(ps_api_key="ps_key")
    assert hasattr(models.Models.generate_content, "__ps_original__")


@pytest.mark.usefixtures("fake_sdk")
def test_module_being_imported_by_another_thread_is_patched(tmp_path,
                                                            monkeypatch):
    # The other thread found the module before the hook went in, and is
    # still running it: instrument() waits for that import, then patches.
    (tmp_path / "ps_fake_sdk" / "gate.py").write_text(
        "import threading\nentered = threading.Event()\n"
        "release = threading.Event()\n")
    (tmp_path / "ps_fake_sdk" / "slow.py").write_text(textwrap.dedent("""
        from ps_fake_sdk import gate
        gate.entered.set()
        gate.release.wait(10)

        class Models:
            def generate_content(self, **kwargs):
                return None
    """))
    monkeypatch.setattr(inst, "_TARGETS", (
        ("google", "ps_fake_sdk.slow", "Models", "generate_content", "gemini"),))
    from ps_fake_sdk import gate

    importer = threading.Thread(target=importlib.import_module,
                                args=("ps_fake_sdk.slow",))
    importer.start()
    try:
        assert gate.entered.wait(10)
        threading.Timer(0.2, gate.release.set).start()
        prompt_shields.instrument(ps_api_key="ps_key")
    finally:
        gate.release.set()
        importer.join(10)
    slow = sys.modules["ps_fake_sdk.slow"]
    try:
        assert hasattr(slow.Models.generate_content, "__ps_original__")
    finally:
        for name in ("ps_fake_sdk.slow", "ps_fake_sdk.gate"):
            sys.modules.pop(name, None)


@pytest.mark.usefixtures("fake_sdk")
def test_import_after_uninstrument_is_not_patched():
    prompt_shields.instrument(ps_api_key="ps_key")
    prompt_shields.uninstrument()
    from ps_fake_sdk import models

    assert not hasattr(models.Models.generate_content, "__ps_original__")


@pytest.mark.usefixtures("fake_sdk")
def test_vendor_selection_skips_lazy_hook():
    prompt_shields.instrument(ps_api_key="ps_key", vendors=("openai",))
    assert inst._state.finder is None
    from ps_fake_sdk import models

    assert not hasattr(models.Models.generate_content, "__ps_original__")


# --- abandoned streams ---------------------------------------------------------


def _collect():
    for _ in range(3):
        gc.collect()


def _assert_one_abandoned_event():
    assert len(_events()) == 1
    event = _events()[0]
    assert event["stream"] is True
    assert event["stream_completed"] is False
    assert event["stream_abandoned"] is True
    return event


OPENAI_CHUNKS = [
    {"id": "c", "object": "chat.completion.chunk", "created": 0, "model": "gpt-4o",
     "choices": [{"index": 0, "delta": {"content": "yo"}}]},
    {"id": "c", "object": "chat.completion.chunk", "created": 0, "model": "gpt-4o",
     "choices": [{"index": 0, "delta": {"content": "!"}}]},
]


def _openai():
    openai = pytest.importorskip("openai")
    return openai, getattr(openai._base_client, "httpx2", None) or httpx


def test_openai_stream_abandoned_emits_once_on_gc():
    openai, http = _openai()
    prompt_shields.instrument(ps_api_key="ps_key")
    transport, _ = _sse(http, OPENAI_CHUNKS, done=True)
    client = openai.OpenAI(api_key="sk", http_client=http.Client(transport=transport))

    def read_one():
        for chunk in client.chat.completions.create(
                model="gpt-4o", messages=MESSAGES, stream=True):
            return chunk.choices[0].delta.content  # break out, no close()

    assert read_one() == "yo"
    _collect()
    event = _assert_one_abandoned_event()
    assert event["vendor"] == "openai"
    assert event["detected_pii_types"] == ["email"]
    _collect()
    assert len(_events()) == 1


def test_finished_stream_emits_nothing_more_on_gc():
    openai, http = _openai()
    prompt_shields.instrument(ps_api_key="ps_key")
    transport, _ = _sse(http, OPENAI_CHUNKS, done=True)
    client = openai.OpenAI(api_key="sk", http_client=http.Client(transport=transport))
    stream = client.chat.completions.create(model="gpt-4o", messages=MESSAGES,
                                            stream=True)
    next(stream)
    stream.close()
    del stream
    _collect()
    assert len(_events()) == 1
    assert "stream_abandoned" not in _events()[0]


def test_openai_async_stream_abandoned():
    openai, http = _openai()
    prompt_shields.instrument(ps_api_key="ps_key")
    transport, _ = _sse(http, OPENAI_CHUNKS, done=True)

    async def run():
        client = openai.AsyncOpenAI(api_key="sk", http_client=http.AsyncClient(
            transport=transport))
        stream = await client.chat.completions.create(
            model="gpt-4o", messages=MESSAGES, stream=True)
        async for _chunk in stream:
            break

    asyncio.run(run())
    _collect()
    assert _assert_one_abandoned_event()["vendor"] == "openai"


def test_anthropic_create_stream_abandoned():
    anthropic, http = _anthropic()
    prompt_shields.instrument(ps_api_key="ps_key")
    client = anthropic.Anthropic(api_key="sk-ant", http_client=http.Client(
        transport=_anthropic_transport(http)))
    stream = client.messages.create(model="claude-test", max_tokens=10,
                                    messages=MESSAGES, stream=True)
    next(iter(stream))
    del stream
    _collect()
    event = _assert_one_abandoned_event()
    assert event["tokens_in"] == 9  # from message_start


def test_anthropic_stream_helper_abandoned():
    anthropic, http = _anthropic()
    prompt_shields.instrument(ps_api_key="ps_key")
    client = anthropic.Anthropic(api_key="sk-ant", http_client=http.Client(
        transport=_anthropic_transport(http)))
    stream = client.messages.stream(model="claude-test", max_tokens=10,
                                    messages=MESSAGES).__enter__()
    next(iter(stream))
    del stream
    _collect()
    assert _assert_one_abandoned_event()["vendor"] == "anthropic"


def test_mistral_stream_abandoned():
    client = _mistral_client()
    prompt_shields.instrument(ps_api_key="ps_key")
    stream = client.chat.stream(model="mistral-small-latest", messages=MESSAGES)
    next(iter(stream))
    del stream
    _collect()
    assert _assert_one_abandoned_event()["vendor"] == "mistral"


def test_lazy_stream_never_read_sends_nothing_and_releases_claim():
    # google-genai sends the request on the first read: a stream dropped
    # unread made no call, so it emits nothing and gives its claim back.
    client = _gemini_client()
    prompt_shields.instrument(ps_api_key="ps_key")
    fc = inst.FrameworkCall("langchain")
    token = inst._FRAMEWORK_CALL.set(fc)
    try:
        stream = client.models.generate_content_stream(
            model="gemini-2.5-flash", contents="hi")
        assert fc.captured
        del stream
        _collect()
    finally:
        inst._FRAMEWORK_CALL.reset(token)
    assert _events() == []
    assert not fc.captured


def test_gemini_stream_read_then_abandoned():
    client = _gemini_client()
    prompt_shields.instrument(ps_api_key="ps_key")
    stream = client.models.generate_content_stream(
        model="gemini-2.5-flash", contents="hi")
    next(stream)
    del stream
    _collect()
    assert _assert_one_abandoned_event()["vendor"] == "google"


@pytest.mark.usefixtures("aws")
def test_bedrock_event_stream_abandoned():
    prompt_shields.instrument(ps_api_key="ps_key")
    resp = _event_stream_client(CONVERSE_STREAM).converse_stream(
        modelId="us.amazon.nova-lite-v1:0", messages=[])
    next(iter(resp["stream"]))
    del resp
    _collect()
    assert _assert_one_abandoned_event()["vendor"] == "bedrock"


def test_cohere_stream_loop_exited_emits_once():
    # cohere's `chat_stream` is a generator: dropping it closes the observed
    # event stream inside it, a plain early close.
    cohere = pytest.importorskip("cohere")
    transport, _ = _sse(httpx, COHERE_V2_EVENTS)
    prompt_shields.instrument(ps_api_key="ps_key")
    client = cohere.ClientV2(api_key="co-key",
                             httpx_client=httpx.Client(transport=transport))
    for _event in client.chat_stream(model="command-r-08-2024", messages=MESSAGES):
        break
    _collect()
    assert len(_events()) == 1
    assert _events()[0]["stream_completed"] is False


def test_abandon_errors_never_raise(monkeypatch):
    openai, http = _openai()
    prompt_shields.instrument(ps_api_key="ps_key")
    transport, _ = _sse(http, OPENAI_CHUNKS, done=True)
    client = openai.OpenAI(api_key="sk", http_client=http.Client(transport=transport))
    unraisable = []
    monkeypatch.setattr(sys, "unraisablehook", unraisable.append)
    monkeypatch.setattr(inst, "_stream_event", lambda *a: 1 / 0)
    stream = client.chat.completions.create(model="gpt-4o", messages=MESSAGES,
                                            stream=True)
    next(stream)
    del stream
    _collect()
    assert unraisable == []
    assert _events() == []


@pytest.mark.filterwarnings("ignore:coroutine .* was never awaited")
def test_unentered_stream_manager_releases_its_claim():
    anthropic, http = _anthropic()
    prompt_shields.instrument(ps_api_key="ps_key")
    client = anthropic.Anthropic(api_key="sk-ant", http_client=http.Client(
        transport=_anthropic_transport(http)))
    for sdk_async in (False, True):
        fc = inst.FrameworkCall("langchain")
        token = inst._FRAMEWORK_CALL.set(fc)
        try:
            resource = (anthropic.AsyncAnthropic(api_key="sk-ant") if sdk_async
                        else client)
            manager = resource.messages.stream(model="claude-test", max_tokens=10,
                                               messages=MESSAGES)
            assert fc.captured
            del manager
            _collect()
            assert not fc.captured
        finally:
            inst._FRAMEWORK_CALL.reset(token)

    # An entered one keeps it.
    fc = inst.FrameworkCall("langchain")
    token = inst._FRAMEWORK_CALL.set(fc)
    try:
        with client.messages.stream(model="claude-test", max_tokens=10,
                                    messages=MESSAGES) as stream:
            list(stream)
        del stream
        _collect()
    finally:
        inst._FRAMEWORK_CALL.reset(token)
    assert fc.captured
    assert len(_events()) == 1


# --- cohere clients created before instrument() -------------------------------


def test_cohere_clients_created_before_instrument():
    cohere = pytest.importorskip("cohere")
    v2 = cohere.ClientV2(api_key="co-key", httpx_client=httpx.Client(
        transport=_json_transport(httpx, COHERE_V2_BODY)))
    v1 = cohere.Client(api_key="co-key", httpx_client=httpx.Client(
        transport=_json_transport(httpx, COHERE_V1_BODY)))
    transport, _ = _sse(httpx, COHERE_V2_EVENTS)
    v2_stream = cohere.ClientV2(api_key="co-key",
                                httpx_client=httpx.Client(transport=transport))
    v1_stream = cohere.Client(api_key="co-key", httpx_client=httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(
            200, text="".join(json.dumps(e) + "\n" for e in COHERE_V1_EVENTS)))))
    prompt_shields.instrument(ps_api_key="ps_key")

    v2.chat(model="command-r-08-2024", messages=MESSAGES)
    v1.chat(message="hi")
    assert len(list(v2_stream.chat_stream(model="command-r-08-2024",
                                          messages=MESSAGES))) == 4
    assert len(list(v1_stream.chat_stream(message="hi"))) == 3

    events = _events()
    assert [e["vendor"] for e in events] == ["cohere"] * 4
    assert [(e["tokens_in"], e["tokens_out"]) for e in events] == [
        (5, 2), (4, 1), (5, 2), (4, 1)]
    assert [e.get("stream", False) for e in events] == [False, False, True, True]
    assert events[0]["detected_pii_types"] == ["email"]
    assert len(events[0]["api_key_fingerprint"]) == 16


def test_cohere_async_client_created_before_instrument():
    cohere = pytest.importorskip("cohere")

    async def run():
        client = cohere.AsyncClientV2(api_key="co-key", httpx_client=httpx.AsyncClient(
            transport=_json_transport(httpx, COHERE_V2_BODY)))
        transport, _ = _sse(httpx, COHERE_V2_EVENTS)
        streamer = cohere.AsyncClientV2(api_key="co-key", httpx_client=httpx.AsyncClient(
            transport=transport))
        prompt_shields.instrument(ps_api_key="ps_key")
        await client.chat(model="command-r-08-2024", messages=MESSAGES)
        return [e async for e in streamer.chat_stream(
            model="command-r-08-2024", messages=MESSAGES)]

    assert len(asyncio.run(run())) == 4
    assert [(e["tokens_in"], e.get("stream", False)) for e in _events()] == [
        (5, False), (5, True)]


def test_cohere_raw_response_is_reported_once():
    cohere = pytest.importorskip("cohere")
    prompt_shields.instrument(ps_api_key="ps_key")
    client = cohere.ClientV2(api_key="co-key", httpx_client=httpx.Client(
        transport=_json_transport(httpx, COHERE_V2_BODY)))
    raw = client.with_raw_response.chat(model="command-r-08-2024",
                                        messages=MESSAGES)
    assert raw.data.message.content[0].text == "hi"
    assert len(_events()) == 1
    assert _events()[0]["tokens_in"] == 5


def test_cohere_public_methods_patched_without_raw_clients(monkeypatch):
    # cohere versions without raw clients: the public methods are patched.
    cohere = pytest.importorskip("cohere")
    monkeypatch.setattr(inst, "_TARGETS", tuple(
        t for t in inst._TARGETS if not t[1].endswith("raw_base_client")
        and not t[1].endswith("raw_client")))
    monkeypatch.setattr(inst, "_SUPERSEDED_BY", {})
    prompt_shields.instrument(ps_api_key="ps_key")
    from cohere.v2.client import V2Client

    assert hasattr(V2Client.chat, "__ps_original__")
    client = cohere.ClientV2(api_key="co-key", httpx_client=httpx.Client(
        transport=_json_transport(httpx, COHERE_V2_BODY)))
    client.chat(model="command-r-08-2024", messages=MESSAGES)
    assert len(_events()) == 1


def test_cohere_public_methods_not_patched_with_raw_clients():
    pytest.importorskip("cohere")
    prompt_shields.instrument(ps_api_key="ps_key")
    from cohere.base_client import BaseCohere
    from cohere.raw_base_client import RawBaseCohere

    assert hasattr(RawBaseCohere.chat, "__ps_original__")
    assert not hasattr(BaseCohere.chat, "__ps_original__")
