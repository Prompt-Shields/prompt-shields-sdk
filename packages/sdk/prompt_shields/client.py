"""ShieldsClient — drop-in wrappers around LLM provider SDKs.

Two surfaces:
  - ShieldsClient(vendor="openai" | "anthropic", ...)  — generic
  - ShieldsOpenAI(...) / ShieldsAnthropic(...)         — typed convenience

Both produce identical telemetry events. The generic form is useful when
the vendor is configured at runtime; the typed forms make IDE completion
nicer for app developers.

Telemetry is fail-open: every LLM call returns the upstream response even
if telemetry delivery fails. See `telemetry.py` for buffering details.
"""

import hashlib
import os
import time
from typing import Any

from prompt_shields.pii import scan_messages
from prompt_shields.pricing import estimate_cost
from prompt_shields.providers import get_adapter
from prompt_shields.telemetry import (
    AtlasTelemetrySender,
    TelemetrySender,
    build_atlas_event,
)
from prompt_shields.types import PSMetadata


def _fingerprint(api_key: str) -> str:
    """SHA-256 hash of an API key — never store the raw key in telemetry.

    Truncated to 16 hex chars for brevity. Collisions are not a concern at
    this scale; this is for grouping calls by issuer, not authentication.
    """
    return hashlib.sha256(api_key.encode()).hexdigest()[:16]


class ShieldsClient:
    """Generic Prompt Shields wrapper. Specify `vendor` to pick the provider.

    For most apps, prefer the typed convenience subclasses:
        - ShieldsOpenAI    — wraps openai.OpenAI
        - ShieldsAnthropic — wraps anthropic.Anthropic
    """

    def __init__(
        self,
        api_key: str,
        ps_api_key: str,
        vendor: str = "openai",
        ps_collector_url: str = "http://localhost:8000",
        # Discovery metadata (sent on every event)
        business_unit: str | None = None,
        use_case: str | None = None,
        owner: str | None = None,
        data_classification: str | None = None,
        environment: str | None = None,
        calling_service: str | None = None,
        # Behavior toggles
        scan_pii: bool = True,
        send_prompt_text: bool = False,
        pricing_table: dict | None = None,
        # Optional atlas.ai sink (second destination; collector unchanged).
        # Env fallback: PS_ATLAS_URL / PS_ATLAS_API_KEY. Inactive unless BOTH set.
        atlas_url: str | None = None,
        atlas_api_key: str | None = None,
        **provider_kwargs,
    ):
        self._vendor = vendor
        self._adapter = get_adapter(vendor)
        self._ps_api_key = ps_api_key
        self._telemetry = TelemetrySender(ps_collector_url, ps_api_key)
        atlas_url = atlas_url if atlas_url is not None else os.environ.get("PS_ATLAS_URL")
        atlas_api_key = (
            atlas_api_key if atlas_api_key is not None
            else os.environ.get("PS_ATLAS_API_KEY")
        )
        self._atlas: AtlasTelemetrySender | None = (
            AtlasTelemetrySender(atlas_url, atlas_api_key)
            if atlas_url and atlas_api_key
            else None
        )
        self._scan_pii = scan_pii
        self._send_prompt_text = send_prompt_text
        self._pricing_table = pricing_table
        self._api_key_fingerprint = _fingerprint(api_key)

        # Build immutable per-client metadata (None values are filtered out)
        self._metadata = {
            k: v for k, v in {
                "business_unit": business_unit,
                "use_case": use_case,
                "owner": owner,
                "data_classification": data_classification,
                "environment": environment,
                "calling_service": calling_service,
            }.items() if v is not None
        }

        # Lazy provider client construction — only import what we need
        self._upstream = self._make_upstream(api_key, **provider_kwargs)
        self.chat = _ChatNamespace(self)

    # --- provider construction --------------------------------------------

    def _make_upstream(self, api_key: str, **provider_kwargs) -> Any:
        if self._vendor == "openai":
            from openai import OpenAI
            return OpenAI(api_key=api_key, **provider_kwargs)
        if self._vendor == "anthropic":
            from anthropic import Anthropic
            return Anthropic(api_key=api_key, **provider_kwargs)
        raise ValueError(f"No upstream factory for vendor {self._vendor!r}")

    # --- event construction -----------------------------------------------

    def _build_event(
        self,
        model: str,
        messages: list,
        response: Any,
        latency_ms: int,
        ps_metadata: PSMetadata | None = None,
    ) -> dict:
        adapter_fields = self._adapter.extract(response)
        tokens_in = adapter_fields.get("tokens_in")
        tokens_out = adapter_fields.get("tokens_out")

        event = {
            "vendor": self._vendor,
            "model": model,
            "source": "sdk",
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "latency_ms": latency_ms,
            "tool_calls_used": adapter_fields.get("tool_calls_used"),
            "cost": estimate_cost(
                vendor=self._vendor,
                model=model,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                pricing_table=self._pricing_table,
            ),
            "api_key_fingerprint": self._api_key_fingerprint,
        }

        # Map SDK field names to collector field names
        field_mapping = {"use_case": "use_case_name", "owner": "owner_email"}
        for sdk_key, collector_key in field_mapping.items():
            if sdk_key in self._metadata:
                event[collector_key] = self._metadata[sdk_key]
        for k, v in self._metadata.items():
            if k not in field_mapping:
                event[k] = v

        # Per-request ps_metadata — wired through to event
        if ps_metadata:
            for key in ("data_sources", "output_destination", "risk_tags",
                        "session_id", "user_id"):
                if key in ps_metadata:
                    event[key] = ps_metadata[key]

        # Optional prompt content (default: never sent — only PII categories)
        if self._send_prompt_text:
            event["prompt_text"] = " ".join(
                m.get("content", "") if isinstance(m, dict) else ""
                for m in messages
            )

        # PII detection runs locally — only category labels leave the host
        if self._scan_pii:
            categories = scan_messages(messages)
            if categories:
                event["detected_pii_types"] = categories

        return event


class ShieldsOpenAI(ShieldsClient):
    """Typed convenience: ShieldsOpenAI(api_key=..., ps_api_key=...)."""

    def __init__(self, **kwargs):
        kwargs.setdefault("vendor", "openai")
        super().__init__(**kwargs)


class ShieldsAnthropic(ShieldsClient):
    """Typed convenience: ShieldsAnthropic(api_key=..., ps_api_key=...).

    Anthropic does not have a `chat.completions.create` surface — it uses
    `messages.create`. We keep the same `client.chat.completions.create(...)`
    call shape on the wrapper for cross-vendor consistency, internally
    dispatching to the right upstream method.
    """

    def __init__(self, **kwargs):
        kwargs.setdefault("vendor", "anthropic")
        super().__init__(**kwargs)


# --- chat namespace --------------------------------------------------------


class _ChatNamespace:
    def __init__(self, parent: ShieldsClient):
        self._parent = parent
        self.completions = _CompletionsNamespace(parent)


class _CompletionsNamespace:
    def __init__(self, parent: ShieldsClient):
        self._parent = parent

    def create(
        self,
        *,
        model: str,
        messages: list,
        ps_metadata: PSMetadata | None = None,
        max_tokens: int | None = None,
        **kwargs,
    ):
        start = time.monotonic()
        response = self._call_upstream(model=model, messages=messages,
                                        max_tokens=max_tokens, **kwargs)
        latency_ms = int((time.monotonic() - start) * 1000)

        event = self._parent._build_event(
            model=model,
            messages=messages,
            response=response,
            latency_ms=latency_ms,
            ps_metadata=ps_metadata,
        )
        self._parent._telemetry.enqueue(event)
        self._parent._telemetry.flush_sync()

        if self._parent._atlas is not None:
            self._parent._atlas.enqueue(build_atlas_event(event, messages))
            self._parent._atlas.flush_sync()

        return response

    def _call_upstream(self, *, model: str, messages: list,
                       max_tokens: int | None = None, **kwargs):
        upstream = self._parent._upstream
        vendor = self._parent._vendor

        if vendor == "openai":
            return upstream.chat.completions.create(
                model=model, messages=messages, **kwargs
            )
        if vendor == "anthropic":
            # Anthropic requires max_tokens; default to 1024 if not provided
            return upstream.messages.create(
                model=model,
                messages=messages,
                max_tokens=max_tokens if max_tokens is not None else 1024,
                **kwargs,
            )
        raise ValueError(f"No upstream dispatch for vendor {vendor!r}")
