"""LangGraph demo (#388): a two-node graph (draft -> review) whose model calls
show up in the collector per node.

Each node calls a chat model; `PromptShieldsCallbackHandler` sends one event
per call, tagged `langgraph_node=<node>`. With a partner token
(`PS_PARTNER_TOKEN`, scope usage:read) the demo then reads requests, tokens
and cost per node back from the collector's usage series.

Runs offline by default: the model is a local stand-in that reports itself
as Claude on Vertex AI, so the events are priced like real ones. Set
`PS_DEMO_VERTEX_PROJECT` (and Google credentials) to call Claude on Vertex
AI through `langchain-google-vertexai`'s `ChatAnthropicVertex` instead.

Needs the collector (docker compose up) with the demo tenant seeded
(demo/seed_data.py), plus `pip install langchain-core langgraph`.

Raw events keep the tag. The usage series and showback group by it
(`group_by=tag:langgraph_node`) once `langgraph_node` is in the tenant's
`settings.cost_tag_keys` and the rollups have been refreshed.
"""
import os
import time
from typing import TypedDict

import httpx
import prompt_shields
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.graph import END, START, StateGraph
from prompt_shields.integrations.langchain import PromptShieldsCallbackHandler

COLLECTOR_URL = os.environ.get("PS_COLLECTOR_URL", "http://localhost:8000")
API_KEY = os.environ.get("PS_API_KEY", "ps-demo-key-acme")
VERTEX_MODEL = "claude-sonnet-4-5@20250929"


class LocalClaudeOnVertex(BaseChatModel):
    """Answers locally; reports itself as Claude on Vertex AI, with usage."""

    output_tokens: int = 200

    @property
    def _llm_type(self):
        return "local-demo"

    def _get_ls_params(self, stop=None, **kwargs):
        return {"ls_provider": "google_vertexai", "ls_model_name": VERTEX_MODEL,
                "ls_model_type": "chat"}

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        prompt = " ".join(str(m.content) for m in messages)
        message = AIMessage(
            content=f"[{self.output_tokens} tokens about: {prompt[:40]}]",
            usage_metadata={"input_tokens": 1200,
                            "output_tokens": self.output_tokens,
                            "total_tokens": 1200 + self.output_tokens},
            response_metadata={"model_name": "claude-sonnet-4-5-20250929"},
        )
        return ChatResult(generations=[ChatGeneration(message=message)])


def models():
    project = os.environ.get("PS_DEMO_VERTEX_PROJECT")
    if project:
        from langchain_google_vertexai.model_garden import ChatAnthropicVertex

        def real():
            return ChatAnthropicVertex(model_name=VERTEX_MODEL, project=project,
                                       location="global")
        return real(), real()
    return LocalClaudeOnVertex(output_tokens=800), LocalClaudeOnVertex(output_tokens=150)


class State(TypedDict):
    topic: str
    draft: str
    review: str


def build_graph():
    drafter, reviewer = models()

    def draft(state: State):
        return {"draft": drafter.invoke(f"Draft a release note: {state['topic']}").content}

    def review(state: State):
        return {"review": reviewer.invoke(f"Review this draft: {state['draft']}").content}

    graph = StateGraph(State)
    graph.add_node("draft", draft)
    graph.add_node("review", review)
    graph.add_edge(START, "draft")
    graph.add_edge("draft", "review")
    graph.add_edge("review", END)
    return graph.compile()


def per_node_usage(token: str) -> list:
    """Requests, tokens and cost per node from the collector's usage series
    (`group_by=tag:langgraph_node`; needs the key in the tenant's
    cost_tag_keys and a rollup refresh, see the module doc)."""
    resp = httpx.get(f"{COLLECTOR_URL}/partner/v1/usage/series",
                     headers={"Authorization": f"Bearer {token}"},
                     params={"group_by": "tag:langgraph_node",
                             "granularity": "day"}, timeout=10)
    resp.raise_for_status()
    return resp.json()["data"]


def main():
    prompt_shields.instrument(ps_api_key=API_KEY, ps_collector_url=COLLECTOR_URL,
                              use_case="release-notes")
    handler = PromptShieldsCallbackHandler()
    result = build_graph().invoke({"topic": "per-node usage in LangGraph"},
                                  config={"callbacks": [handler]})
    print("review:", result["review"])

    token = os.environ.get("PS_PARTNER_TOKEN")  # a usage:read partner token
    if not token:
        print("Events sent, one per node, tagged langgraph_node=draft|review.\n"
              "To read them back per node: add langgraph_node to the tenant's "
              "cost_tag_keys, run `python -m collector.usage_rollups refresh "
              "--all-tenants`, then\n"
              f"  curl -H 'Authorization: Bearer $PS_PARTNER_TOKEN' "
              f"'{COLLECTOR_URL}/partner/v1/usage/series?group_by=tag:langgraph_node'")
        return
    time.sleep(1)
    for point in per_node_usage(token):
        print(f"{point['key'] or '(no node)':>10}: {point['requests']} call(s), "
              f"{point['tokens_in']} in / {point['tokens_out']} out, "
              f"${float(point['cost_usd'] or 0):.6f}")


if __name__ == "__main__":
    main()
