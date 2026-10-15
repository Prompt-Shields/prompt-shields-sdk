"""Tests for prompt_shields.integrations (LangChain, LiteLLM).

The handshake tests need no framework. LangChain and LiteLLM tests are
skipped when those packages are not installed. No network: provider calls
go to the fake resources from test_instrumentation, LangChain models are
local fakes and LiteLLM calls use `mock_response`.
"""

import asyncio
import datetime
import subprocess
import sys
import uuid

import prompt_shields
import pytest
from prompt_shields import instrumentation as inst
from prompt_shields.instrumentation import _FRAMEWORK_CALL, FrameworkCall

from .test_instrumentation import MESSAGES, _events, fakes  # noqa: F401


def test_importing_prompt_shields_imports_no_framework():
    code = (
        "import sys, prompt_shields, prompt_shields.integrations\n"
        "bad = [m for m in ('langchain_core', 'langchain', 'litellm', "
        "'llama_index') if m in sys.modules]\n"
        "assert not bad, bad\n"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


# --- instrument() <-> framework handshake --------------------------------------


def test_patched_call_claims_framework_call(fakes):  # noqa: F811
    prompt_shields.instrument(ps_api_key="ps_key")
    fc = FrameworkCall("langchain", "support_bot")
    token = _FRAMEWORK_CALL.set(fc)
    try:
        fakes.chat.Completions().create(model="gpt-4o", messages=MESSAGES)
        # Single claim: a second call in the same scope is not tagged.
        fakes.chat.Completions().create(model="gpt-4o", messages=MESSAGES)
    finally:
        _FRAMEWORK_CALL.reset(token)
    assert fc.captured
    first, second = _events()
    assert first["framework"] == "langchain"
    assert first["use_case_name"] == "support_bot"
    assert "framework" not in second


def test_configured_use_case_wins_over_hint(fakes):  # noqa: F811
    prompt_shields.instrument(ps_api_key="ps_key", use_case="configured")
    token = _FRAMEWORK_CALL.set(FrameworkCall("langchain", "hint"))
    try:
        fakes.chat.Completions().create(model="gpt-4o", messages=MESSAGES)
    finally:
        _FRAMEWORK_CALL.reset(token)
    assert _events()[0]["use_case_name"] == "configured"
    assert _events()[0]["framework"] == "langchain"


@pytest.mark.parametrize("fc", [
    FrameworkCall("litellm", model="claude-3"),  # different model
    FrameworkCall("litellm"),                    # already finished
])
def test_stale_or_mismatched_framework_call_not_claimed(fakes, fc):  # noqa: F811
    if fc.model is None:
        fc.finished = True
    prompt_shields.instrument(ps_api_key="ps_key")
    token = _FRAMEWORK_CALL.set(fc)
    try:
        fakes.chat.Completions().create(model="gpt-4o", messages=MESSAGES)
    finally:
        _FRAMEWORK_CALL.reset(token)
    assert not fc.captured
    assert "framework" not in _events()[0]


def test_provider_prefixed_model_matches():
    assert inst._same_model("openai/gpt-4o", "gpt-4o")
    assert inst._same_model("gpt-4o", "gpt-4o")
    assert not inst._same_model("gpt-4o", "gpt-4o-mini")


def test_async_patched_call_claims_framework_call(fakes):  # noqa: F811
    prompt_shields.instrument(ps_api_key="ps_key")
    fc = FrameworkCall("langchain", "bot")

    async def run():
        _FRAMEWORK_CALL.set(fc)
        await fakes.chat.AsyncCompletions().create(model="gpt-4o",
                                                   messages=MESSAGES)

    asyncio.run(run())
    assert fc.captured
    assert _events()[0]["framework"] == "langchain"


# --- LangChain -------------------------------------------------------------------


@pytest.fixture
def lc():
    pytest.importorskip("langchain_core")
    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.messages import AIMessage
    from langchain_core.outputs import ChatGeneration, ChatResult
    from prompt_shields.integrations import langchain as lc_mod

    def reply():
        return AIMessage(
            content="ok",
            tool_calls=[{"name": "lookup", "args": {}, "id": "t1"}],
            usage_metadata={
                "input_tokens": 100, "output_tokens": 20, "total_tokens": 120,
                "input_token_details": {"cache_read": 40},
                "output_token_details": {"reasoning": 5},
            },
            response_metadata={"model_name": "gpt-4o-2024-08-06"},
        )

    class LocalModel(BaseChatModel):
        """Answers locally, or through `resource` (a patched provider SDK)."""

        resource: object = None
        fail: bool = False

        @property
        def _llm_type(self):
            return "local"

        @property
        def _identifying_params(self):
            return {"model": "gpt-4o"}

        def _get_ls_params(self, stop=None, **kwargs):
            return {"ls_provider": "openai", "ls_model_name": "gpt-4o",
                    "ls_model_type": "chat"}

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            if self.fail:
                raise RuntimeError("upstream down")
            if self.resource is not None:
                self.resource.create(model="gpt-4o", messages=MESSAGES)
            return ChatResult(generations=[ChatGeneration(message=reply())])

        async def _agenerate(self, messages, stop=None, run_manager=None,
                             **kwargs):
            if self.resource is not None and hasattr(self.resource, "create"):
                await self.resource.create(model="gpt-4o", messages=MESSAGES)
            return ChatResult(generations=[ChatGeneration(message=reply())])

    return NS(mod=lc_mod, Model=LocalModel,
              Handler=lc_mod.PromptShieldsCallbackHandler)


class NS:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _chain(lc, model, name="support_bot"):
    from langchain_core.prompts import ChatPromptTemplate
    prompt = ChatPromptTemplate.from_messages([("user", "{q}")])
    return (prompt | model).with_config(run_name=name)


def test_langchain_handler_emits_with_hint_and_usage(fakes, lc):  # noqa: F811
    prompt_shields.instrument(ps_api_key="ps_key", vendors=())
    _chain(lc, lc.Model()).invoke({"q": "mail jane@example.com"},
                                  config={"callbacks": [lc.Handler()]})
    (event,) = _events()
    assert event["framework"] == "langchain"
    assert event["use_case_name"] == "support_bot"
    assert event["vendor"] == "openai"
    assert event["model"] == "gpt-4o"
    assert event["served_model"] == "gpt-4o-2024-08-06"
    assert event["tokens_in"] == 100
    assert event["tokens_out"] == 20
    assert event["tokens_cached"] == 40
    assert event["tokens_reasoning"] == 5
    assert event["tool_calls_used"] == [{"name": "lookup", "type": "function"}]
    assert event["detected_pii_types"] == ["email"]
    assert event["cost"] is not None


def test_langchain_hint_precedence(fakes, lc):  # noqa: F811
    prompt_shields.instrument(ps_api_key="ps_key", vendors=())
    chain = _chain(lc, lc.Model())
    chain.invoke({"q": "x"}, config={"callbacks": [lc.Handler()],
                                     "metadata": {"ps_use_case": "from-meta"}})
    chain.invoke({"q": "x"}, config={"callbacks": [lc.Handler(use_case="fixed")],
                                     "metadata": {"ps_use_case": "from-meta"}})
    # Outermost chain wins for nested runs.
    from langchain_core.runnables import RunnableLambda
    outer = RunnableLambda(lambda x: chain.invoke(x)).with_config(run_name="agent")
    outer.invoke({"q": "x"}, config={"callbacks": [lc.Handler()]})
    # A bare model run has no chain name.
    lc.Model().invoke("x", config={"callbacks": [lc.Handler()]})
    hints = [e.get("use_case_name") for e in _events()]
    assert hints == ["from-meta", "fixed", "agent", None]


def test_langchain_no_double_count_when_instrumented(fakes, lc):  # noqa: F811
    prompt_shields.instrument(ps_api_key="ps_key")
    handler = lc.Handler()
    model = lc.Model(resource=fakes.chat.Completions())
    _chain(lc, model).invoke({"q": "x"}, config={"callbacks": [handler]})
    asyncio.run(_chain(lc, lc.Model(resource=fakes.chat.AsyncCompletions()))
                .ainvoke({"q": "x"}, config={"callbacks": [handler]}))
    events = _events()
    assert len(events) == 2  # one per run, from instrument(), not the handler
    for event in events:
        assert event["framework"] == "langchain"
        assert event["use_case_name"] == "support_bot"
        assert event["tokens_in"] == 11  # the provider response, not LangChain's
    assert handler._runs == {} and handler._roots == {}
    assert _FRAMEWORK_CALL.get() is None


def test_langchain_async_handler_path(fakes, lc):  # noqa: F811
    prompt_shields.instrument(ps_api_key="ps_key", vendors=())
    asyncio.run(_chain(lc, lc.Model()).ainvoke(
        {"q": "x"}, config={"callbacks": [lc.Handler()]}))
    (event,) = _events()
    assert event["framework"] == "langchain"
    assert event["tokens_in"] == 100


def test_langchain_error_emits_nothing_and_restores_context(fakes, lc):  # noqa: F811
    prompt_shields.instrument(ps_api_key="ps_key", vendors=())
    handler = lc.Handler()
    with pytest.raises(RuntimeError, match="upstream down"):
        _chain(lc, lc.Model(fail=True)).invoke({"q": "x"},
                                               config={"callbacks": [handler]})
    assert _events() == []
    assert handler._runs == {} and handler._roots == {}
    assert _FRAMEWORK_CALL.get() is None


def test_langchain_handler_never_raises(fakes, lc, monkeypatch):  # noqa: F811
    prompt_shields.instrument(ps_api_key="ps_key", vendors=())

    def boom(*args, **kwargs):
        raise RuntimeError("telemetry broke")

    monkeypatch.setattr(inst, "build_event", boom)
    handler = lc.Handler()
    result = _chain(lc, lc.Model()).invoke(
        {"q": "x"}, config={"callbacks": [handler]})
    assert result.content == "ok"
    # Direct calls with junk arguments are swallowed as well.
    handler.on_llm_end(object(), run_id=uuid.uuid4())
    handler.on_chat_model_start(None, None, run_id=uuid.uuid4(), metadata=5)


def test_langchain_without_instrument_is_noop(fakes, lc):  # noqa: F811
    result = _chain(lc, lc.Model()).invoke({"q": "x"},
                                           config={"callbacks": [lc.Handler()]})
    assert result.content == "ok"


def test_langchain_extract_falls_back_to_llm_output(lc):
    from langchain_core.outputs import Generation, LLMResult
    result = LLMResult(
        generations=[[Generation(text="hi")]],
        llm_output={"token_usage": {"prompt_tokens": 3, "completion_tokens": 4},
                    "model_name": "gpt-3.5-turbo-instruct"},
    )
    fields = lc.mod._extract(result)
    assert fields["tokens_in"] == 3
    assert fields["tokens_out"] == 4
    assert fields["served_model"] == "gpt-3.5-turbo-instruct"


# --- LiteLLM ---------------------------------------------------------------------


@pytest.fixture
def ll():
    litellm = pytest.importorskip("litellm")
    from prompt_shields.integrations import litellm as ll_mod
    saved = list(litellm.callbacks)
    yield NS(litellm=litellm, mod=ll_mod)
    ll_mod.unregister()
    litellm.callbacks[:] = saved


def _wait_for(n, timeout=5.0):
    import time
    deadline = time.monotonic() + timeout
    while len(_events()) < n and time.monotonic() < deadline:
        time.sleep(0.02)


def test_litellm_sync_and_async(fakes, ll):  # noqa: F811
    prompt_shields.instrument(ps_api_key="ps_key", vendors=())
    assert ll.mod.register() is ll.mod.register()  # idempotent
    ll.litellm.completion(
        model="gpt-4o-mini", mock_response="hi",
        messages=[{"role": "user", "content": "mail jane@example.com"}],
        metadata={"ps_use_case": "triage"},
    )
    _wait_for(1)

    async def run():
        await ll.litellm.acompletion(model="anthropic/claude-3-5-haiku-20241022",
                                     messages=[{"role": "user", "content": "x"}],
                                     mock_response="hey")
        for _ in range(100):
            if len(_events()) >= 2:
                break
            await asyncio.sleep(0.02)

    asyncio.run(run())
    first, second = _events()
    assert first["framework"] == "litellm"
    assert first["vendor"] == "openai"
    assert first["model"] == "gpt-4o-mini"
    assert first["use_case_name"] == "triage"
    assert first["detected_pii_types"] == ["email"]
    assert first["tokens_in"] is not None
    assert second["vendor"] == "anthropic"
    assert second["model"] == "claude-3-5-haiku-20241022"


def _kwargs(call_id="c1", **extra):
    return {"litellm_call_id": call_id, "model": "gpt-4o",
            "litellm_params": {"custom_llm_provider": "openai", "metadata": {}},
            "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
            **extra}


def _response():
    from types import SimpleNamespace as S
    return S(model="gpt-4o-2024-08-06", choices=[],
             usage=S(prompt_tokens=7, completion_tokens=3,
                     cache_creation_input_tokens=2))


_T0 = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
_T1 = _T0 + datetime.timedelta(milliseconds=250)


def test_litellm_reports_each_call_once(fakes, ll):  # noqa: F811
    prompt_shields.instrument(ps_api_key="ps_key", vendors=())
    logger = ll.mod.PromptShieldsLogger()
    logger.log_pre_api_call("gpt-4o", [], _kwargs())
    logger.log_success_event(_kwargs(), _response(), _T0, _T1)
    asyncio.run(logger.async_log_success_event(_kwargs(), _response(), _T0, _T1))
    (event,) = _events()
    assert event["latency_ms"] == 250
    assert event["tokens_in"] == 7
    assert event["tokens_cache_write"] == 2
    assert event["served_model"] == "gpt-4o-2024-08-06"


def test_litellm_skips_calls_captured_by_instrument(fakes, ll):  # noqa: F811
    prompt_shields.instrument(ps_api_key="ps_key")
    logger = ll.mod.PromptShieldsLogger(use_case="router")
    logger.log_pre_api_call("gpt-4o", [], _kwargs())
    fakes.chat.Completions().create(model="gpt-4o", messages=MESSAGES)
    logger.log_success_event(_kwargs(), _response(), _T0, _T1)
    (event,) = _events()  # from instrument(), tagged with the framework
    assert event["framework"] == "litellm"
    assert event["use_case_name"] == "router"
    assert event["tokens_in"] == 11
    # The finished call is not claimed by a later direct SDK call.
    fakes.chat.Completions().create(model="gpt-4o", messages=MESSAGES)
    assert "framework" not in _events()[1]


def test_litellm_failure_emits_nothing(fakes, ll):  # noqa: F811
    prompt_shields.instrument(ps_api_key="ps_key", vendors=())
    logger = ll.mod.PromptShieldsLogger()
    logger.log_pre_api_call("gpt-4o", [], _kwargs())
    logger.log_failure_event(_kwargs(), None, _T0, _T1)
    logger.log_success_event(_kwargs(), _response(), _T0, _T1)
    assert _events() == []


def test_litellm_cost_fallback_and_vendor_mapping(fakes, ll):  # noqa: F811
    prompt_shields.instrument(ps_api_key="ps_key", vendors=())
    logger = ll.mod.PromptShieldsLogger()
    kwargs = _kwargs(model="some-unpriced-model", response_cost=0.0042)
    kwargs["litellm_params"]["custom_llm_provider"] = "vertex_ai"
    logger.log_success_event(kwargs, _response(), _T0, _T1)
    (event,) = _events()
    assert event["vendor"] == "google"
    assert event["cost"] == 0.0042
    assert event["cost_source"] == "litellm"


def test_litellm_logger_never_raises(fakes, ll):  # noqa: F811
    prompt_shields.instrument(ps_api_key="ps_key", vendors=())
    logger = ll.mod.PromptShieldsLogger()
    logger.log_pre_api_call(None, None, {})
    logger.log_success_event({}, object(), None, None)
    logger.log_success_event(None, None, None, None)
    asyncio.run(logger.async_log_failure_event(None, None, None, None))
