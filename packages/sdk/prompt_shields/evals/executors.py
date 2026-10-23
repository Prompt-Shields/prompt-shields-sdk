"""Executors: how a sampled prompt is re-run on a candidate model.

- :class:`ProviderExecutor` calls the provider with the customer's own keys,
  through the vendor SDK (``openai`` / ``anthropic``). Point ``base_url`` at
  the Prompt Shields gateway to re-run through it. Usage is read with the
  SDK's provider adapters and priced with ``prompt_shields.pricing``, the same
  code that prices production events. Calls run inside
  ``suppress_instrumentation()``, so re-execution never shows up as
  production telemetry. It sends prompts off the machine
  (``sends_prompts = True``), so the harness only runs it with an explicit
  opt-in and a spend cap.
- :class:`FakeExecutor` answers from a Python function, for tests and
  dry runs. No network.
- :class:`RecordedExecutor` replays answers recorded earlier (JSONL). No
  network.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from prompt_shields.instrumentation import suppress_instrumentation
from prompt_shields.pricing import estimate_cost, usage_cost
from prompt_shields.providers import get_adapter
from prompt_shields.route_policy import estimate_input_tokens


@dataclass(frozen=True, order=True)
class Candidate:
    """A model to re-run on: ``vendor:model`` (e.g. ``openai:gpt-4o-mini``)."""

    vendor: str
    model: str

    @classmethod
    def parse(cls, text: str) -> Candidate:
        vendor, sep, model = text.strip().partition(":")
        if not sep or not vendor or not model:
            raise ValueError(f"candidate must be 'vendor:model', got {text!r}")
        return cls(vendor=vendor, model=model)

    @property
    def label(self) -> str:
        return f"{self.vendor}:{self.model}"

    def __str__(self) -> str:
        return self.label


@dataclass(frozen=True)
class Completion:
    """The outcome of one re-executed call."""

    text: str | None
    tokens_in: int | None = None
    tokens_out: int | None = None
    cost: float | None = None
    latency_ms: int | None = None
    served_model: str | None = None
    error: str | None = None


class Executor(Protocol):
    #: True when prompts leave this process (a real provider or gateway).
    sends_prompts: bool

    def complete(
        self,
        candidate: Candidate,
        messages: list[dict],
        *,
        max_tokens: int,
        temperature: float | None,
    ) -> Completion: ...


def prompt_key(messages: list[dict]) -> str:
    """Stable key for a prompt, used to match recorded answers. Not reported."""
    canonical = json.dumps(messages, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _approx_tokens(text: str) -> int:
    return estimate_input_tokens([{"content": text}])


# --- offline executors -------------------------------------------------------


class FakeExecutor:
    """Answers from ``responder(candidate, messages) -> str``. No network.

    Token counts are approximated (~4 characters per token) and priced at the
    candidate's list price, so cost deltas and the spend cap behave as they
    would on a real run. ``latency_ms`` maps candidate labels to a fixed
    latency (default 100 ms).
    """

    sends_prompts = False

    def __init__(
        self,
        responder: Callable[[Candidate, list[dict]], str],
        *,
        latency_ms: Mapping[str, int] | None = None,
        pricing_table: dict | None = None,
    ):
        self._responder = responder
        self._latency = dict(latency_ms or {})
        self._pricing_table = pricing_table
        self.calls: list[tuple[Candidate, list[dict]]] = []

    def complete(self, candidate, messages, *, max_tokens, temperature):
        self.calls.append((candidate, messages))
        try:
            text = self._responder(candidate, messages)
        except Exception as exc:  # noqa: BLE001 - reported as a failed trial
            return Completion(text=None, error=f"{type(exc).__name__}: {exc}")
        tokens_in = estimate_input_tokens(messages)
        tokens_out = min(_approx_tokens(text), max_tokens)
        return Completion(
            text=text,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost=estimate_cost(candidate.vendor, candidate.model, tokens_in,
                               tokens_out, self._pricing_table),
            latency_ms=self._latency.get(candidate.label, 100),
            served_model=candidate.model,
        )


class RecordedExecutor:
    """Replays recorded completions. No network.

    Each recording is ``{"candidate": "vendor:model", "prompt_key": ...,
    "text": ..., "tokens_in": ..., "tokens_out": ..., "cost": ...,
    "latency_ms": ...}``; ``prompt_key`` is :func:`prompt_key` of the
    messages. A prompt with no recording comes back as an error.
    """

    sends_prompts = False

    def __init__(self, recordings: Iterable[Mapping[str, Any]]):
        self._by_key: dict[tuple[str, str], Completion] = {}
        for item in recordings:
            self._by_key[(item["candidate"], item["prompt_key"])] = Completion(
                text=item.get("text"),
                tokens_in=item.get("tokens_in"),
                tokens_out=item.get("tokens_out"),
                cost=item.get("cost"),
                latency_ms=item.get("latency_ms"),
                served_model=item.get("served_model"),
                error=item.get("error"),
            )

    @classmethod
    def from_jsonl(cls, path: str | Path) -> RecordedExecutor:
        with open(path, encoding="utf-8") as handle:
            return cls(json.loads(line) for line in handle if line.strip())

    def complete(self, candidate, messages, *, max_tokens, temperature):
        found = self._by_key.get((candidate.label, prompt_key(messages)))
        if found is None:
            return Completion(text=None, error="no recording for this prompt")
        return found


def recording(candidate: Candidate, messages: list[dict], completion: Completion) -> dict:
    """One line for :class:`RecordedExecutor`, from a completed call."""
    return {
        "candidate": candidate.label,
        "prompt_key": prompt_key(messages),
        "text": completion.text,
        "tokens_in": completion.tokens_in,
        "tokens_out": completion.tokens_out,
        "cost": completion.cost,
        "latency_ms": completion.latency_ms,
        "served_model": completion.served_model,
        "error": completion.error,
    }


# --- live executor -----------------------------------------------------------


def response_text(vendor: str, response: Any) -> str | None:
    """The text of a non-streamed chat response (OpenAI-like or Anthropic)."""

    def get(obj: Any, name: str) -> Any:
        return obj.get(name) if isinstance(obj, Mapping) else getattr(obj, name, None)

    if vendor == "anthropic":
        parts = [
            get(block, "text")
            for block in get(response, "content") or []
            if get(block, "type") == "text"
        ]
        texts = [p for p in parts if isinstance(p, str)]
        return "".join(texts) if texts else None
    choices = get(response, "choices") or []
    if not choices:
        return None
    content = get(get(choices[0], "message"), "content")
    return content if isinstance(content, str) else None


def _default_client(vendor: str, api_key: str | None, base_url: str | None) -> Any:
    kwargs = {k: v for k, v in {"api_key": api_key, "base_url": base_url}.items() if v}
    if vendor not in ("openai", "anthropic") and not base_url:
        raise ValueError(
            f"vendor {vendor!r} needs a base_url (its OpenAI-compatible endpoint "
            "or the Prompt Shields gateway)"
        )
    if vendor == "anthropic":
        from anthropic import Anthropic

        return Anthropic(**kwargs)
    # Every other vendor is reached through an OpenAI-compatible endpoint
    # (OpenAI itself, the Prompt Shields gateway, or a router).
    from openai import OpenAI

    return OpenAI(**kwargs)


class ProviderExecutor:
    """Re-runs prompts on real models with the customer's keys.

    ``clients`` maps a vendor to an SDK client object (``openai.OpenAI`` or
    ``anthropic.Anthropic``, or anything with the same ``create`` method).
    Vendors without a client get one from :meth:`for_vendors` defaults.
    """

    sends_prompts = True

    def __init__(
        self,
        clients: Mapping[str, Any],
        *,
        pricing_table: dict | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._clients = dict(clients)
        self._pricing_table = pricing_table
        self._clock = clock

    @classmethod
    def for_vendors(
        cls,
        vendors: Iterable[str],
        *,
        api_keys: Mapping[str, str] | None = None,
        base_urls: Mapping[str, str] | None = None,
        pricing_table: dict | None = None,
    ) -> ProviderExecutor:
        """Build vendor SDK clients. Keys default to the SDKs' own environment
        variables (``OPENAI_API_KEY``, ``ANTHROPIC_API_KEY``)."""
        api_keys = api_keys or {}
        base_urls = base_urls or {}
        return cls(
            {v: _default_client(v, api_keys.get(v), base_urls.get(v)) for v in set(vendors)},
            pricing_table=pricing_table,
        )

    def complete(self, candidate, messages, *, max_tokens, temperature):
        client = self._clients.get(candidate.vendor)
        if client is None:
            return Completion(text=None, error=f"no client for vendor {candidate.vendor!r}")
        kwargs: dict[str, Any] = {"model": candidate.model, "max_tokens": max_tokens}
        if temperature is not None:
            kwargs["temperature"] = temperature
        start = self._clock()
        try:
            with suppress_instrumentation():
                if candidate.vendor == "anthropic":
                    system = [m for m in messages if m.get("role") == "system"]
                    rest = [m for m in messages if m.get("role") != "system"]
                    if system:
                        kwargs["system"] = "\n\n".join(
                            str(m.get("content") or "") for m in system
                        )
                    response = client.messages.create(messages=rest, **kwargs)
                else:
                    response = client.chat.completions.create(messages=messages, **kwargs)
        except Exception as exc:  # noqa: BLE001 - reported as a failed trial
            return Completion(
                text=None,
                latency_ms=int((self._clock() - start) * 1000),
                error=f"{type(exc).__name__}: {exc}",
            )
        latency_ms = int((self._clock() - start) * 1000)
        try:
            adapter = get_adapter(candidate.vendor)
            fields = adapter.extract(response)
            served = fields.get("served_model")
            cost = usage_cost(
                candidate.vendor,
                adapter.pricing_model(candidate.model, served),
                fields,
                self._pricing_table,
            )
        except ValueError:  # vendor without an adapter: no usage, no cost
            fields, served, cost = {}, None, None
        return Completion(
            text=response_text(candidate.vendor, response),
            tokens_in=fields.get("tokens_in"),
            tokens_out=fields.get("tokens_out"),
            cost=cost,
            latency_ms=latency_ms,
            served_model=served or candidate.model,
        )
