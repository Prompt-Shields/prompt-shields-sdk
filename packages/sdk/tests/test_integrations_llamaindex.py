"""Tests for prompt_shields.integrations.llamaindex, and the cross-framework
and global-registration paths of the LangChain integration.

Skipped when llama-index-core (or langchain-core, for those tests) is not
installed. No network: LLMs are local subclasses that answer themselves or
call the fake provider resources from test_instrumentation; the
llama-index-llms-openai test uses httpx.MockTransport.
"""

import asyncio
import concurrent.futures
import json
from types import SimpleNamespace as S
from typing import Any

import prompt_shields
import pytest
from prompt_shields import instrumentation as inst
from prompt_shields.instrumentation import _FRAMEWORK_CALL

from .test_instrumentation import (  # noqa: F401
    MESSAGES,
    _events,
    _http,
    fakes,
    recording,
)

li_core = pytest.importorskip("llama_index.core")

from llama_index.core.base.llms.types import (
    ChatMessage,
    ChatResponse,
    CompletionResponse,
    LLMMetadata,
    ToolCallBlock,
)
from llama_index.core.instrumentation import get_dispatcher
from llama_index.core.llms import LLM, CustomLLM
from llama_index.core.llms.callbacks import (
    llm_chat_callback,
    llm_completion_callback,
)
from prompt_shields.integrations import llamaindex as li

PROMPT = [ChatMessage(role="user", content="mail jane@example.com")]


def _raw(tokens_in=100, tokens_out=20):
    return S(model="gpt-4o-2024-08-06",
             usage=S(prompt_tokens=tokens_in, completion_tokens=tokens_out),
             choices=[S(message=S(tool_calls=[S(function=S(name="lookup"))]))])


def _chat_response(raw):
    return ChatResponse(message=ChatMessage(role="assistant", content="ok"),
                        raw=raw)


class LocalLLM(LLM):
    """Answers locally, or through `resource` (a patched provider SDK).

    Streams call the provider lazily, on first iteration, like
    llama-index-llms-openai does.
    """

    resource: Any = None
    fail: bool = False
    fail_stream: bool = False

    @classmethod
    def class_name(cls):
        return "openai_llm"

    @property
    def metadata(self):
        return LLMMetadata(model_name="gpt-4o", is_chat_model=True)

    def _call(self):
        if self.fail:
            raise RuntimeError("upstream down")
        if self.resource is None:
            return _raw()
        return self.resource.create(model="gpt-4o", messages=MESSAGES)

    async def _acall(self):
        if self.fail:
            raise RuntimeError("upstream down")
        if self.resource is None:
            return _raw()
        return await self.resource.create(model="gpt-4o", messages=MESSAGES)

    def _stream(self):
        if self.resource is None:
            yield _raw()
            return
        last = None
        for chunk in self.resource.create(model="gpt-4o", messages=MESSAGES,
                                          stream=True):
            last = chunk
            if self.fail_stream:
                raise RuntimeError("stream broke")
        yield last

    async def _astream(self):
        if self.resource is None:
            yield _raw()
            return
        last = None
        async for chunk in await self.resource.create(
                model="gpt-4o", messages=MESSAGES, stream=True):
            last = chunk
        yield last

    @llm_chat_callback()
    def chat(self, messages, **kwargs):
        return _chat_response(self._call())

    @llm_chat_callback()
    async def achat(self, messages, **kwargs):
        return _chat_response(await self._acall())

    @llm_chat_callback()
    def stream_chat(self, messages, **kwargs):
        def gen():
            for raw in self._stream():
                yield _chat_response(raw)
        return gen()

    @llm_chat_callback()
    async def astream_chat(self, messages, **kwargs):
        async def gen():
            async for raw in self._astream():
                yield _chat_response(raw)
        return gen()

    @llm_completion_callback()
    def complete(self, prompt, formatted=False, **kwargs):
        return CompletionResponse(text="ok", raw=self._call())

    @llm_completion_callback()
    async def acomplete(self, prompt, formatted=False, **kwargs):
        return CompletionResponse(text="ok", raw=await self._acall())

    @llm_completion_callback()
    def stream_complete(self, prompt, formatted=False, **kwargs):
        def gen():
            for raw in self._stream():
                yield CompletionResponse(text="ok", delta="ok", raw=raw)
        return gen()

    @llm_completion_callback()
    async def astream_complete(self, prompt, formatted=False, **kwargs):
        async def gen():
            async for raw in self._astream():
                yield CompletionResponse(text="ok", delta="ok", raw=raw)
        return gen()


async def _drain(gen):
    return [x async for x in gen]


def _run_mode(llm, mode):
    """Make one LLM call in `mode`, consuming streams to the end."""
    if mode == "chat":
        llm.chat(PROMPT)
    elif mode == "complete":
        llm.complete("mail jane@example.com")
    elif mode == "stream_chat":
        list(llm.stream_chat(PROMPT))
    elif mode == "stream_complete":
        list(llm.stream_complete("x"))
    elif mode == "achat":
        asyncio.run(llm.achat(PROMPT))
    elif mode == "acomplete":
        asyncio.run(llm.acomplete("x"))
    elif mode == "astream_chat":
        async def run():
            await _drain(await llm.astream_chat(PROMPT))
        asyncio.run(run())
    elif mode == "astream_complete":
        async def run():
            await _drain(await llm.astream_complete("x"))
        asyncio.run(run())


MODES = ["chat", "complete", "stream_chat", "stream_complete", "achat",
         "acomplete", "astream_chat", "astream_complete"]


@pytest.fixture
def handler():
    events = li.register()
    yield events
    li.unregister()


def _ours(dispatcher):
    return [h for h in dispatcher.event_handlers + dispatcher.span_handlers
            if isinstance(h, (li.PromptShieldsEventHandler,
                              li.PromptShieldsSpanHandler))]


def _assert_clean(handler):
    assert not handler._runs
    current = _FRAMEWORK_CALL.get()
    assert current is None or current.finished


# --- one event per call --------------------------------------------------------


def test_emits_usage_vendor_tools_and_pii(fakes, handler):  # noqa: F811
    prompt_shields.instrument(ps_api_key="ps_key", vendors=())
    LocalLLM().chat(PROMPT)
    (event,) = _events()
    assert event["framework"] == "llamaindex"
    assert event["vendor"] == "openai"
    assert event["model"] == "gpt-4o"
    assert event["served_model"] == "gpt-4o-2024-08-06"
    assert (event["tokens_in"], event["tokens_out"]) == (100, 20)
    assert event["tool_calls_used"] == [{"name": "lookup", "type": "function"}]
    assert event["detected_pii_types"] == ["email"]
    assert event["cost"] is not None
    assert event.get("use_case_name") is None
    _assert_clean(handler)


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("instrumented", [False, True])
def test_exactly_one_event_per_call(fakes, handler, mode, instrumented):  # noqa: F811
    is_async = mode.startswith("a")
    if instrumented:
        prompt_shields.instrument(ps_api_key="ps_key")
        resource = (fakes.chat.AsyncCompletions() if is_async
                    else fakes.chat.Completions())
        llm = LocalLLM(resource=resource)
    else:
        prompt_shields.instrument(ps_api_key="ps_key", vendors=())
        llm = LocalLLM()
    _run_mode(llm, mode)
    (event,) = _events()
    assert event["framework"] == "llamaindex"
    if not instrumented:
        assert event["tokens_in"] == 100
    elif "stream" in mode:
        assert event["tokens_in"] == 12  # the provider stream's usage chunk
    else:
        assert event["tokens_in"] == 11  # the provider response
    _assert_clean(handler)


@pytest.mark.parametrize("instrumented", [False, True])
def test_concurrent_async_calls(fakes, handler, instrumented):  # noqa: F811
    prompt_shields.instrument(ps_api_key="ps_key",
                              **({} if instrumented else {"vendors": ()}))
    llm = LocalLLM(resource=fakes.chat.AsyncCompletions() if instrumented else None)

    async def run():
        streams = [await llm.astream_chat(PROMPT) for _ in range(3)]
        await asyncio.gather(*(llm.achat(PROMPT) for _ in range(5)),
                             *(_drain(s) for s in streams))

    asyncio.run(run())
    events = _events()
    assert len(events) == 8
    assert all(e["framework"] == "llamaindex" for e in events)
    assert not handler._runs


@pytest.mark.parametrize("instrumented", [False, True])
def test_concurrent_threads_and_interleaved_streams(fakes, handler, instrumented):  # noqa: F811
    prompt_shields.instrument(ps_api_key="ps_key",
                              **({} if instrumented else {"vendors": ()}))
    llm = LocalLLM(resource=fakes.chat.Completions() if instrumented else None)
    with concurrent.futures.ThreadPoolExecutor(4) as pool:
        list(pool.map(lambda _: llm.chat(PROMPT), range(8)))
    # Two streams opened before either is read.
    first, second = llm.stream_chat(PROMPT), llm.stream_complete("x")
    list(second)
    list(first)
    events = _events()
    assert len(events) == 10
    assert all(e["framework"] == "llamaindex" for e in events)
    _assert_clean(handler)


@pytest.mark.parametrize("instrumented", [False, True])
def test_stream_consumed_in_another_context(fakes, handler, instrumented):  # noqa: F811
    import threading
    prompt_shields.instrument(ps_api_key="ps_key",
                              **({} if instrumented else {"vendors": ()}))
    llm = LocalLLM(resource=fakes.chat.Completions() if instrumented else None)
    stream = llm.stream_chat(PROMPT)
    llm.stream_chat(PROMPT)  # abandoned: never read, never reported
    _FRAMEWORK_CALL.set(None)  # e.g. a worker that does not copy the context
    thread = threading.Thread(target=lambda: list(stream))
    thread.start()
    thread.join()
    llm.chat(PROMPT)
    events = _events()
    assert len(events) == 2
    assert all(e["framework"] == "llamaindex" for e in events)


def test_unregister_drops_pending_streams(fakes):  # noqa: F811
    prompt_shields.instrument(ps_api_key="ps_key")
    before = len(inst._deferred_calls)
    li.register()
    llm = LocalLLM(resource=fakes.chat.Completions())
    abandoned = [llm.stream_chat(PROMPT) for _ in range(3)]  # never read
    assert len(inst._deferred_calls) == before + 3
    li.unregister()
    assert len(inst._deferred_calls) == before
    assert all(s is not None for s in abandoned)
    llm.chat(PROMPT)  # reported by instrument() alone
    (event,) = _events()
    assert "framework" not in event


@pytest.mark.parametrize("instrumented", [False, True])
def test_llm_call_made_while_a_stream_is_read_reports_once(
        fakes, handler, instrumented):  # noqa: F811
    # A stream whose generator calls another LLM (not a span child of it).
    prompt_shields.instrument(ps_api_key="ps_key",
                              **({} if instrumented else {"vendors": ()}))
    inner = LocalLLM(resource=fakes.chat.Completions() if instrumented else None)

    class Router(LocalLLM):
        @llm_chat_callback()
        def stream_chat(self, messages, **kwargs):
            def gen():
                yield inner.chat(messages)
            return gen()

    list(Router().stream_chat(PROMPT))
    (event,) = _events()
    assert event["framework"] == "llamaindex"
    assert event["tokens_in"] == (11 if instrumented else 100)
    _assert_clean(handler)


@pytest.mark.parametrize("instrumented", [False, True])
@pytest.mark.parametrize("mode", ["chat", "stream_chat", "achat"])
def test_nested_llm_calls_report_once(fakes, handler, instrumented, mode):  # noqa: F811
    # CustomLLM.chat calls complete: both are LLM calls to LlamaIndex.
    prompt_shields.instrument(ps_api_key="ps_key",
                              **({} if instrumented else {"vendors": ()}))
    resource = fakes.chat.Completions() if instrumented else None

    class Inner(CustomLLM):
        @property
        def metadata(self):
            return LLMMetadata(model_name="gpt-4o")

        @classmethod
        def class_name(cls):
            return "openai_llm"

        @llm_completion_callback()
        def complete(self, prompt, formatted=False, **kwargs):
            raw = (resource.create(model="gpt-4o", messages=MESSAGES)
                   if resource is not None else _raw(7, 3))
            return CompletionResponse(text="ok", raw=raw)

        @llm_completion_callback()
        def stream_complete(self, prompt, formatted=False, **kwargs):
            def gen():
                yield self.complete(prompt)
            return gen()

    _run_mode(Inner(), mode)
    events = _events()
    assert len(events) == 1, events
    assert events[0]["framework"] == "llamaindex"
    assert events[0]["tokens_in"] == (11 if instrumented else 7)
    _assert_clean(handler)


# --- failures --------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["chat", "achat"])
def test_error_emits_nothing_and_restores_context(fakes, handler, mode):  # noqa: F811
    prompt_shields.instrument(ps_api_key="ps_key", vendors=())
    with pytest.raises(RuntimeError, match="upstream down"):
        _run_mode(LocalLLM(fail=True), mode)
    assert _events() == []
    _assert_clean(handler)


def test_stream_error_emits_nothing(fakes, handler):  # noqa: F811
    prompt_shields.instrument(ps_api_key="ps_key", vendors=())
    llm = LocalLLM(resource=fakes.chat.Completions(), fail_stream=True)
    with pytest.raises(RuntimeError, match="stream broke"):
        list(llm.stream_chat(PROMPT))
    assert _events() == []
    _assert_clean(handler)


def test_handler_never_raises(fakes, handler, monkeypatch):  # noqa: F811
    prompt_shields.instrument(ps_api_key="ps_key", vendors=())

    def boom(*args, **kwargs):
        raise RuntimeError("telemetry broke")

    monkeypatch.setattr(inst, "build_event", boom)
    assert LocalLLM().chat(PROMPT).message.content == "ok"
    # Junk events and spans are swallowed as well.
    handler.handle(object())
    handler.handle(S(span_id=object()))
    for name in ("LLMChatStartEvent", "LLMChatEndEvent", "SpanDropEvent"):
        handler.handle(type(name, (), {"span_id": [], "tags": 5,
                                       "model_dict": 3, "messages": 7})())
    asyncio.run(handler.ahandle(None))
    handler._span_enter([], object(), [])


def test_without_instrument_is_noop(fakes, handler):  # noqa: F811
    assert LocalLLM().chat(PROMPT).message.content == "ok"
    assert inst._state is None


# --- use-case hints ----------------------------------------------------------------


def test_hint_from_register_and_instrument_tags(fakes):  # noqa: F811
    from llama_index.core.instrumentation.dispatcher import instrument_tags
    prompt_shields.instrument(ps_api_key="ps_key", vendors=())
    li.register()
    with instrument_tags({"ps_use_case": "from-tags"}):
        LocalLLM().chat(PROMPT)
    li.unregister()
    li.register(use_case="fixed")
    with instrument_tags({"ps_use_case": "from-tags"}):
        LocalLLM().chat(PROMPT)
    li.unregister()
    assert [e["use_case_name"] for e in _events()] == ["from-tags", "fixed"]


def test_configured_use_case_wins(fakes, handler):  # noqa: F811
    prompt_shields.instrument(ps_api_key="ps_key", vendors=(), use_case="configured")
    LocalLLM().chat(PROMPT)
    assert _events()[0]["use_case_name"] == "configured"


def test_hint_from_workflows_and_agents(fakes, handler):  # noqa: F811
    from llama_index.core.agent.workflow import FunctionAgent
    from llama_index.core.llms.mock import MockFunctionCallingLLM
    from llama_index.core.workflow import StartEvent, StopEvent, Workflow, step

    prompt_shields.instrument(ps_api_key="ps_key", vendors=())
    llm = LocalLLM()

    class SupportFlow(Workflow):
        @step
        async def answer(self, ev: StartEvent) -> StopEvent:
            await llm.achat(PROMPT)
            return StopEvent(result="done")

    async def run():
        await SupportFlow().run()
        await FunctionAgent(name="claims_triage", llm=MockFunctionCallingLLM(),
                            tools=[]).run(user_msg="hi")
        # The default agent name and LlamaIndex's class names are skipped.
        await FunctionAgent(llm=MockFunctionCallingLLM(), tools=[]).run(
            user_msg="hi")

    asyncio.run(run())
    hints = [e.get("use_case_name") for e in _events()]
    assert hints[0] == "SupportFlow"
    assert "claims_triage" in hints[1:]
    assert hints[-1] is None
    assert len(handler._spans) == 0


# --- registration -------------------------------------------------------------------


def test_register_is_fail_open_when_internals_differ(monkeypatch, caplog):
    root = get_dispatcher()
    before = len(root.event_handlers), len(root.span_handlers)

    def boom(self, handler):
        raise AttributeError("no event_handlers here")

    # The span handler is attached, then adding the event handler fails.
    monkeypatch.setattr(type(root), "add_event_handler", boom)
    with caplog.at_level("WARNING", logger="prompt_shields.integrations.llamaindex"):
        assert li.register() is None
    assert "not registered" in caplog.text
    assert (len(root.event_handlers), len(root.span_handlers)) == before
    assert li._registered is None

    monkeypatch.setattr(li, "get_dispatcher", lambda: 1 / 0)
    assert li.register() is None
    monkeypatch.setattr(li, "_remove", lambda handlers: 1 / 0)
    li.unregister()  # logged, not raised
    assert li._registered is None


def test_register_is_idempotent_and_unregister_removes_handlers(fakes):  # noqa: F811
    prompt_shields.instrument(ps_api_key="ps_key", vendors=())
    root = get_dispatcher()
    before = len(root.event_handlers), len(root.span_handlers)
    assert li.register() is li.register()
    assert len(_ours(root)) == 2
    LocalLLM().chat(PROMPT)
    li.unregister()
    li.unregister()
    assert (len(root.event_handlers), len(root.span_handlers)) == before
    for dispatcher in li._root_manager.dispatchers.values():
        assert _ours(dispatcher) == []
    LocalLLM().chat(PROMPT)
    assert len(_events()) == 1
    # Re-registering after unregister reports once per call, not per register.
    li.register()
    LocalLLM().chat(PROMPT)
    li.unregister()
    assert len(_events()) == 2


def test_extract_from_dict_raw_and_llamaindex_counts():
    # llama-index-llms-anthropic passes the response as a dict.
    raw = {"model": "claude-3-5-haiku-20241022",
           "usage": {"input_tokens": 9, "output_tokens": 4,
                     "cache_read_input_tokens": 2}}
    fields = li._extract(_chat_response(raw), "anthropic")
    assert (fields["tokens_in"], fields["tokens_out"]) == (11, 4)  # + cache reads
    assert fields["served_model"] == "claude-3-5-haiku-20241022"
    # No adapter for the vendor: LlamaIndex's counts and tool-call blocks.
    response = ChatResponse(
        message=ChatMessage(role="assistant", blocks=[
            ToolCallBlock(tool_name="search", tool_kwargs={"q": "secret"})]),
        additional_kwargs={"prompt_tokens": 5, "completion_tokens": 6})
    fields = li._extract(response, "ollama")
    assert (fields["tokens_in"], fields["tokens_out"]) == (5, 6)
    assert fields["tool_calls_used"] == [{"name": "search", "type": "function"}]


@pytest.mark.parametrize(("class_name", "vendor"), [
    ("openai_llm", "openai"), ("azure_openai_llm", "azure_openai"),
    ("Anthropic_LLM", "anthropic"), ("Bedrock_Converse_LLM", "bedrock"),
    ("GoogleGenAI", "google"), ("Vertex", "google"),
    ("MistralAI_LLM", "mistral"), ("custom_llm", "custom"), (None, "custom"),
])
def test_vendor_from_class_name(class_name, vendor):
    assert li._vendor(class_name) == vendor


# --- other frameworks ----------------------------------------------------------------


@pytest.fixture
def lc():
    pytest.importorskip("langchain_core")
    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.messages import AIMessage
    from langchain_core.outputs import ChatGeneration, ChatResult
    from prompt_shields.integrations import langchain as lc_mod

    class LocalChat(BaseChatModel):
        inner: Any = None  # a LlamaIndex LLM called from this model

        @property
        def _llm_type(self):
            return "local"

        def _get_ls_params(self, stop=None, **kwargs):
            return {"ls_provider": "openai", "ls_model_name": "gpt-4o",
                    "ls_model_type": "chat"}

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            if self.inner is not None:
                self.inner.chat(PROMPT)
            return ChatResult(generations=[ChatGeneration(message=AIMessage(
                content="ok", usage_metadata={"input_tokens": 1, "output_tokens": 1,
                                              "total_tokens": 2}))])

    yield S(mod=lc_mod, Chat=LocalChat)
    lc_mod.unregister()


def _lc_chain(model, name="support_bot"):
    from langchain_core.prompts import ChatPromptTemplate
    return (ChatPromptTemplate.from_messages([("user", "{q}")]) | model) \
        .with_config(run_name=name)


def test_llamaindex_inside_langchain_reports_once(fakes, handler, lc):  # noqa: F811
    prompt_shields.instrument(ps_api_key="ps_key", vendors=())
    _lc_chain(lc.Chat(inner=LocalLLM())).invoke(
        {"q": "x"}, config={"callbacks": [lc.mod.PromptShieldsCallbackHandler()]})
    (event,) = _events()
    assert event["framework"] == "llamaindex"
    assert event["use_case_name"] == "support_bot"
    assert event["tokens_in"] == 100


def test_langchain_inside_llamaindex_reports_once(fakes, handler, lc):  # noqa: F811
    prompt_shields.instrument(ps_api_key="ps_key", vendors=())
    lc_handler = lc.mod.PromptShieldsCallbackHandler()
    chat_model = lc.Chat()

    class LangChainLLM(CustomLLM):  # like llama-index-llms-langchain
        @property
        def metadata(self):
            return LLMMetadata(model_name="gpt-4o")

        @llm_completion_callback()
        def complete(self, prompt, formatted=False, **kwargs):
            chat_model.invoke(prompt, config={"callbacks": [lc_handler]})
            return CompletionResponse(text="ok")

        @llm_completion_callback()
        def stream_complete(self, prompt, formatted=False, **kwargs):
            raise NotImplementedError

    LangChainLLM().chat(PROMPT)
    (event,) = _events()
    assert event["framework"] == "langchain"
    assert event["tokens_in"] == 1
    _assert_clean(handler)


def test_langchain_register_attaches_to_every_run(fakes, lc):  # noqa: F811
    prompt_shields.instrument(ps_api_key="ps_key", vendors=())
    assert lc.mod.register(use_case="global") is lc.mod.register()
    chain = _lc_chain(lc.Chat())
    chain.invoke({"q": "x"})
    asyncio.run(chain.ainvoke({"q": "x"}))
    # A run with its own handler keeps only that one: still one event.
    chain.invoke({"q": "x"},
                 config={"callbacks": [lc.mod.PromptShieldsCallbackHandler()]})
    lc.mod.unregister()
    chain.invoke({"q": "x"})
    hints = [e["use_case_name"] for e in _events()]
    assert hints == ["global", "global", "support_bot"]


# --- llama-index-llms-openai against the real openai SDK ------------------------------


def _openai_llm(openai, handler_fn):
    from llama_index.llms.openai import OpenAI
    http = _http(openai)
    return OpenAI(model="gpt-4o", api_key="sk-test", max_retries=0,
                  http_client=http.Client(transport=http.MockTransport(handler_fn)))


def _completion_body():
    return {"id": "c1", "object": "chat.completion", "created": 0,
            "model": "gpt-4o-2024-08-06",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": "hi"}}],
            "usage": {"prompt_tokens": 9, "completion_tokens": 2,
                      "total_tokens": 11}}


@pytest.mark.parametrize("instrumented", [False, True])
def test_real_llama_index_openai(recording, instrumented):  # noqa: F811
    openai = pytest.importorskip("openai")
    pytest.importorskip("llama_index.llms.openai")
    prompt_shields.instrument(ps_api_key="ps_key",
                              vendors=("openai",) if instrumented else ())
    li.register()
    try:
        http = _http(openai)

        def sse(request):
            if not json.loads(request.content).get("stream"):
                return http.Response(200, json=_completion_body())
            chunks = [
                {"id": "c2", "object": "chat.completion.chunk", "created": 0,
                 "model": "gpt-4o",
                 "choices": [{"index": 0, "delta": {"content": "yo"}}]},
                {"id": "c2", "object": "chat.completion.chunk", "created": 0,
                 "model": "gpt-4o", "choices": [],
                 "usage": {"prompt_tokens": 5, "completion_tokens": 1,
                           "total_tokens": 6}},
            ]
            text = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks)
            return http.Response(200, text=text + "data: [DONE]\n\n",
                                 headers={"content-type": "text/event-stream"})

        llm = _openai_llm(openai, sse)
        assert llm.chat(PROMPT).message.content == "hi"
        assert "".join(r.delta or "" for r in llm.stream_chat(PROMPT)) == "yo"
    finally:
        li.unregister()
    first, second = _events()
    for event in (first, second):
        assert event["framework"] == "llamaindex"
        assert event["vendor"] == "openai"
    assert (first["tokens_in"], first["tokens_out"]) == (9, 2)
    assert first["served_model"] == "gpt-4o-2024-08-06"
    if instrumented:
        assert (second["tokens_in"], second["tokens_out"]) == (5, 1)


# --- an instrumented vendor stream helper under a LlamaIndex stream ------------


_ANTHROPIC_SSE = [
    ("message_start", {"type": "message_start", "message": {
        "id": "m1", "type": "message", "role": "assistant", "content": [],
        "model": "claude-test", "stop_reason": None, "stop_sequence": None,
        "usage": {"input_tokens": 9, "output_tokens": 1}}}),
    ("content_block_start", {"type": "content_block_start", "index": 0,
                             "content_block": {"type": "text", "text": ""}}),
    ("content_block_delta", {"type": "content_block_delta", "index": 0,
                             "delta": {"type": "text_delta", "text": "hello"}}),
    ("content_block_stop", {"type": "content_block_stop", "index": 0}),
    ("message_delta", {"type": "message_delta",
                       "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                       "usage": {"output_tokens": 5}}),
    ("message_stop", {"type": "message_stop"}),
]


@pytest.mark.parametrize("close_early", [False, True])
def test_anthropic_stream_helper_under_llamaindex_stream(recording, close_early):  # noqa: F811
    # The LlamaIndex stream opens Anthropic's `messages.stream()` (observed
    # in place by instrument()) only when it is read, here in a thread that
    # does not share the caller's context. One event, from instrument().
    import threading

    anthropic = pytest.importorskip("anthropic")
    prompt_shields.instrument(ps_api_key="ps_key")
    http = _http(anthropic)
    text = "".join(f"event: {name}\ndata: {json.dumps(data)}\n\n"
                   for name, data in _ANTHROPIC_SSE)
    client = anthropic.Anthropic(api_key="sk-ant", http_client=http.Client(
        transport=http.MockTransport(lambda r: http.Response(
            200, text=text, headers={"content-type": "text/event-stream"}))))

    class ClaudeLLM(LocalLLM):
        @classmethod
        def class_name(cls):
            return "Anthropic_LLM"

        @llm_chat_callback()
        def stream_chat(self, messages, **kwargs):
            def gen():
                with client.messages.stream(model="claude-test", max_tokens=10,
                                            messages=MESSAGES) as s:
                    for delta in s.text_stream:
                        yield ChatResponse(message=ChatMessage(
                            role="assistant", content=delta), delta=delta)
            return gen()

    handler = li.register()
    try:
        stream = ClaudeLLM().stream_chat(PROMPT)
        _FRAMEWORK_CALL.set(None)

        def consume():
            if close_early:
                next(stream)
                stream.close()
            else:
                list(stream)

        thread = threading.Thread(target=consume)
        thread.start()
        thread.join()
        LocalLLM().chat(PROMPT)  # unrelated, reported by the integration
    finally:
        li.unregister()
    stream_event, chat_event = _events()
    assert stream_event["framework"] == chat_event["framework"] == "llamaindex"
    assert stream_event["vendor"] == "anthropic"
    assert stream_event["stream_completed"] is not close_early
    assert stream_event["tokens_in"] == 9
    assert chat_event["tokens_in"] == 100
    assert not handler._runs
    assert not inst._deferred_calls
