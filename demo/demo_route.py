"""Offline demo of ShieldsRouter: sensitivity routing, failover, savings.

    pip install -e "packages/sdk[all]"
    python demo/demo_route.py

No API keys and no network. Every provider is a stub: the real OpenAI and
Anthropic SDKs are given a ``MockTransport`` that answers in-process.
Telemetry goes to a stub collector on 127.0.0.1, which prints what it gets
(if you set HTTP_PROXY, also set NO_PROXY=127.0.0.1).

The policy is ``schemas/route-policy/examples/nordic-sovereign.json``:
US models for public and internal data, EEA + zero data retention + no US
CLOUD Act exposure for confidential data (also the default for a call with
no classification), and two named EEA providers for restricted data.
"""

from __future__ import annotations

import importlib
import json
import math
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import openai
from prompt_shields import PolicyViolation, RouteHint, ShieldsRouter
from prompt_shields.route_policy import estimate_tier_cost

ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = ROOT / "schemas" / "route-policy" / "examples" / "nordic-sovereign.json"
POLICY = json.loads(POLICY_PATH.read_text())

#: What a team without routing would hardcode: the one model the policy
#: allows at every sensitivity level. Savings are measured against it.
BASELINE_MODEL = "mistral-large-eu"

COMPLETION_TOKENS = 60

# --- stub providers ------------------------------------------------------------

calls: dict[str, int] = {}  # provider -> requests that reached its stub
outage: set[str] = set()     # providers whose stub answers 503


def _prompt_tokens(messages) -> int:
    chars = 0
    for m in messages:
        content = m.get("content")
        if isinstance(content, str):
            chars += len(content)
        elif isinstance(content, list):
            chars += sum(len(p.get("text", "")) for p in content)
    return math.ceil(chars / 4)


def _stub(provider: str, vendor: str):
    """The vendor SDK's own HTTP client, answering from ``handle`` in-process.

    Built from the SDK's ``DefaultHttpxClient`` and the HTTP library it uses
    (``httpx``, or ``httpx2`` in newer SDKs), so no request leaves the process.
    """
    sdk = importlib.import_module(vendor)
    base = next(
        c for c in sdk.DefaultHttpxClient.__mro__
        if c.__name__ == "Client" and c.__module__.startswith("httpx")
    )
    http = importlib.import_module(base.__module__.split(".")[0])

    def handle(request):
        calls[provider] = calls.get(provider, 0) + 1
        if provider in outage:
            return http.Response(503, json={"error": {"message": "stub outage"}})
        body = json.loads(request.content)
        messages = list(body["messages"])
        if body.get("system"):
            messages.append({"content": body["system"]})
        tokens_in = _prompt_tokens(messages)
        text = f"[{provider} stub answer]"
        if vendor == "anthropic":
            return http.Response(200, json={
                "id": "msg_stub", "type": "message", "role": "assistant",
                "model": body["model"],
                "content": [{"type": "text", "text": text}],
                "stop_reason": "end_turn", "stop_sequence": None,
                "usage": {"input_tokens": tokens_in,
                          "output_tokens": COMPLETION_TOKENS},
            })
        return http.Response(200, json={
            "id": "chatcmpl-stub", "object": "chat.completion",
            "created": 1700000000, "model": body["model"],
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": text}}],
            "usage": {"prompt_tokens": tokens_in,
                      "completion_tokens": COMPLETION_TOKENS,
                      "total_tokens": tokens_in + COMPLETION_TOKENS},
        })

    return sdk.DefaultHttpxClient(transport=http.MockTransport(handle))


def _provider(name: str, vendor: str = "openai") -> dict:
    config = {
        "api_key": "stub-key",
        "vendor": vendor,
        "http_client": _stub(name, vendor),
        "max_retries": 0,  # the SDK's own retries would hide the outage
    }
    if name not in ("openai", "anthropic"):
        config["base_url"] = f"https://{name}.invalid/v1"  # never resolved
    return config


PROVIDERS = {
    "openai": _provider("openai"),
    "anthropic": _provider("anthropic", vendor="anthropic"),
    "scaleway": _provider("scaleway"),
    "mistral-eu": _provider("mistral-eu"),
    "berget": _provider("berget"),
}

# --- stub collector --------------------------------------------------------------

events: list[dict] = []


class _Collector(BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        events.extend(json.loads(body).get("events", []))
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"accepted": true}')

    def log_message(self, *args):
        pass


def _start_collector() -> HTTPServer:
    server = HTTPServer(("127.0.0.1", 0), _Collector)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


# --- the demo ----------------------------------------------------------------------

SCENARIOS = [
    # (title, data_classification, route hint, prompt)
    ("Public FAQ, no hint", "public", None,
     "Classify the sentiment of: 'Great service, thanks!'"),
    ("Public code review (heuristic: complex)", "public", None,
     "Review this:\n```python\ndef add(a, b): return a - b\n```"),
    ("Internal, quality=balanced", "internal", RouteHint(quality="balanced"),
     "Draft a status update for the platform team."),
    ("No classification (counts as confidential)", None, None,
     "Summarise the attached board minutes."),
    ("Declared internal, prompt mentions a diagnosis", "internal", None,
     "Summarise: the claimant's diagnosis delays their return to work."),
]


def _tier(model: str) -> dict:
    for tiers in POLICY["groups"].values():
        for tier in tiers:
            if tier["model"] == model:
                return tier
    raise KeyError(model)


def main() -> int:
    server = _start_collector()
    router = ShieldsRouter(
        providers=PROVIDERS,
        policy=POLICY,
        ps_api_key="ps-demo",
        ps_collector_url=f"http://127.0.0.1:{server.server_port}",
        business_unit="Claims",
        calling_service="demo_route",
    )
    baseline = _tier(BASELINE_MODEL)
    rows = []

    print("== 1. Sensitivity-aware routing ==\n")
    for title, classification, hint, prompt in SCENARIOS:
        resp = router.chat.completions.create(
            messages=[{"role": "user", "content": prompt}],
            route=hint, max_tokens=200, data_classification=classification,
        )
        r = resp.ps.route
        print(f"{title}")
        print(f"  -> {r.model} via {r.provider} ({r.jurisdiction}), group "
              f"{r.group}, reason {r.reason}")
        print(f"     sensitivity {r.sensitivity_level} ({r.sensitivity_source})")
        usage = resp.usage
        base = estimate_tier_cost(
            baseline, usage.prompt_tokens, usage.completion_tokens
        )
        rows.append((title, r.model, resp.ps.cost.usd, resp.ps.cost.source, base))

    print("\nRestricted data pinned to the US-hosted group:")
    before = dict(calls)
    try:
        router.chat.completions.create(
            messages=[{"role": "user", "content": "Diagnosis notes for claim 7"}],
            route=RouteHint(model_group="us-hosted"),
        )
        print("  !! sent (unexpected)")
        return 1
    except PolicyViolation as exc:
        sent = sum(calls.values()) - sum(before.values())
        print(f"  -> PolicyViolation: {exc}")
        print(f"     requests sent to any provider: {sent}")

    print("\n== 2. Failover ==\n")
    outage.add("scaleway")
    before = dict(calls)
    try:
        router.chat.completions.create(
            messages=[{"role": "user", "content": "Summarise the Q3 incident log."}],
            data_classification="confidential",
        )
        print("  !! succeeded (unexpected: scaleway is down)")
        return 1
    except openai.APIStatusError as exc:  # the provider's error, passed through
        tried = {p: calls.get(p, 0) - before.get(p, 0) for p in calls}
        tried = {p: n for p, n in tried.items() if n}
        print(f"  scaleway answered 503 -> {type(exc).__name__}")
        print(f"  requests per provider: {tried}")
        print("  Not wired in yet (#21): prompt_shields.failover has the engine and")
        print("  circuit breaker, but ShieldsRouter makes one attempt per call. It")
        print("  does not move on to mistral-small-eu, the next compliant model.")
    finally:
        outage.clear()

    print("\n== 3. Savings ==\n")
    print(f"Baseline: every call on {BASELINE_MODEL}, the one model this policy")
    print("allows at every sensitivity level. Same tokens, policy-tier rates.\n")
    print(f"{'call':<48} {'served':<26} {'USD':>10} {'baseline':>10}")
    total, total_base = 0.0, 0.0
    for title, model, usd, source, base in rows:
        total += usd
        total_base += base
        print(f"{title[:47]:<48} {model:<26} {usd:>10.6f} {base:>10.6f}")
    print(f"{'total':<75} {total:>10.6f} {total_base:>10.6f}")
    print(f"\nSaved {1 - total / total_base:.0%} against the baseline on these "
          f"{len(rows)} calls.")
    print("Cost sources:", sorted({source for *_, source, _ in rows}))

    router.close()
    print(f"\n== 4. Telemetry (stub collector received {len(events)} events) ==\n")
    for e in events:
        print(f"  requested {e.get('requested_model')!s:<5} served "
              f"{e.get('served_model')!s:<25} {e.get('served_provider')!s:<11} "
              f"{e.get('sensitivity_level')!s:<13} {e.get('route_reason')}")
    server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
