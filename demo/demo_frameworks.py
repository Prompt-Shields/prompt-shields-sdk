"""Framework integrations demo: LangChain, LiteLLM and LlamaIndex calls ->
registry assets.

Runs offline: the LangChain and LlamaIndex models are local fakes and LiteLLM
uses `mock_response`, so no provider API key is needed. Needs the collector
(docker compose up) with the demo tenant seeded (demo/seed_data.py), plus
`pip install langchain-core litellm llama-index-core`.
"""
import asyncio
import time

import httpx
import litellm
import prompt_shields
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from langchain_core.prompts import ChatPromptTemplate
from llama_index.core.llms import CompletionResponse, CustomLLM, LLMMetadata
from llama_index.core.llms.callbacks import llm_completion_callback
from llama_index.core.workflow import StartEvent, StopEvent, Workflow, step
from prompt_shields.integrations import llamaindex
from prompt_shields.integrations.langchain import PromptShieldsCallbackHandler
from prompt_shields.integrations.litellm import register

COLLECTOR_URL = "http://localhost:8000"
API_KEY = "ps-demo-key-acme"
HEADERS = {"Authorization": f"Bearer {API_KEY}"}


class DemoChatModel(GenericFakeChatModel):
    """A local chat model that reports itself as OpenAI gpt-4o-mini."""

    def _get_ls_params(self, stop=None, **kwargs):
        return {"ls_provider": "openai", "ls_model_name": "gpt-4o-mini",
                "ls_model_type": "chat"}


class DemoLlamaLLM(CustomLLM):
    """A local LlamaIndex LLM that reports itself as OpenAI gpt-4o."""

    @classmethod
    def class_name(cls):
        return "openai_llm"

    @property
    def metadata(self):
        return LLMMetadata(model_name="gpt-4o")

    @llm_completion_callback()
    def complete(self, prompt, formatted=False, **kwargs):
        return CompletionResponse(
            text="Claim C-7 is covered.",
            additional_kwargs={"prompt_tokens": 210, "completion_tokens": 9})

    @llm_completion_callback()
    def stream_complete(self, prompt, formatted=False, **kwargs):
        raise NotImplementedError


class CoverageCheck(Workflow):
    """A LlamaIndex workflow: its class name becomes the use case."""

    @step
    async def check(self, ev: StartEvent) -> StopEvent:
        answer = await DemoLlamaLLM().acomplete(ev.question)
        return StopEvent(result=answer.text)


def main():
    print("=== Prompt Shields framework integrations demo ===\n")
    # vendors=() sets up telemetry without patching provider SDKs; the
    # integrations below report the framework calls.
    prompt_shields.instrument(ps_api_key=API_KEY, ps_collector_url=COLLECTOR_URL,
                              business_unit="Claims", vendors=())

    print("1. LangChain chain 'claims_triage' (callback handler)...")
    reply = AIMessage(
        content="Route to fraud review.",
        tool_calls=[{"name": "lookup_policy", "args": {"id": "P-1"}, "id": "t1"}],
        usage_metadata={"input_tokens": 180, "output_tokens": 12,
                        "total_tokens": 192},
    )
    chain = (
        ChatPromptTemplate.from_messages([("user", "Triage this claim: {claim}")])
        | DemoChatModel(messages=iter([reply]))
    ).with_config(run_name="claims_triage")
    chain.invoke({"claim": "Water damage, contact jane@example.com"},
                 config={"callbacks": [PromptShieldsCallbackHandler()]})

    print("2. LiteLLM completion tagged ps_use_case='policy_summaries'...")
    register()
    litellm.completion(
        model="anthropic/claude-3-5-haiku-20241022",
        messages=[{"role": "user", "content": "Summarise policy P-1"}],
        mock_response="Policy P-1 covers water damage.",
        metadata={"ps_use_case": "policy_summaries"},
    )

    print("3. LlamaIndex workflow 'CoverageCheck' (instrumentation handlers)...")
    llamaindex.register()

    async def check_coverage():
        return await CoverageCheck().run(question="Is claim C-7 covered?")

    asyncio.run(check_coverage())

    print("\n4. Registry assets discovered from the framework calls:")
    # LiteLLM logs sync calls from a background thread; give it a moment.
    for _ in range(50):
        resp = httpx.get(
            f"{COLLECTOR_URL}/api/v1/registry/assets?business_unit=Claims",
            headers=HEADERS)
        if resp.json()["meta"]["total"] >= 3:
            break
        time.sleep(0.1)
    for a in resp.json()["data"]:
        print(f"   - {a['vendor']}/{a['model']} | {a['use_case_name']} | "
              f"{a['business_unit']}")
    print("\n=== Demo complete ===")


if __name__ == "__main__":
    main()
