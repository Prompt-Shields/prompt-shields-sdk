"""Async ShieldsClient — non-blocking variant for asyncio apps.

Mirrors the sync API surface but uses async upstream clients and `await
telemetry.flush()` instead of the threaded fast path. Use this when your
app is already async (FastAPI handlers, asyncio agents, etc.) — the sync
`ShieldsClient` works fine in async code via the threaded flush, but a
native async client avoids the thread overhead.
"""

import os
import time
from collections.abc import Callable
from typing import Any

from prompt_shields.client import _fingerprint
from prompt_shields.instrumentation import suppress_instrumentation
from prompt_shields.pii import scan_messages
from prompt_shields.pricing import (
    add_usage_detail_fields,
    resolve_pricing_model,
    usage_cost,
)
from prompt_shields.prompt_features import PromptFeatures, notify_prompt_features
from prompt_shields.providers import get_adapter
from prompt_shields.streaming import (
    AsyncShieldsStream,
    StreamAccumulator,
    request_openai_stream_usage,
    upstream_accepts_stream_options,
)
from prompt_shields.telemetry import (
    AtlasTelemetrySender,
    TelemetrySender,
    build_atlas_event,
    register_exit_flush,
    unregister_exit_flush,
)
from prompt_shields.types import PSMetadata, RouteHint


class AsyncShieldsClient:
    """Async-native Prompt Shields wrapper."""

    def __init__(
        self,
        api_key: str,
        ps_api_key: str,
        vendor: str = "openai",
        ps_collector_url: str = "http://localhost:8000",
        business_unit: str | None = None,
        use_case: str | None = None,
        owner: str | None = None,
        data_classification: str | None = None,
        environment: str | None = None,
        calling_service: str | None = None,
        scan_pii: bool = True,
        send_prompt_text: bool = False,
        pricing_table: dict | None = None,
        # Local prompt features (#45): when set, called once per call with
        # PromptFeatures (incl. a local embedding) before the provider call.
        # Nothing from it is added to telemetry.
        on_prompt_features: Callable[[PromptFeatures], Any] | None = None,
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
        # Best-effort flush of whatever is still buffered (an abandoned
        # stream's event, a failed send) when the interpreter exits.
        register_exit_flush(self._telemetry, self._atlas)
        self._scan_pii = scan_pii
        self._send_prompt_text = send_prompt_text
        self._pricing_table = pricing_table
        self._on_prompt_features = on_prompt_features
        self._api_key_fingerprint = _fingerprint(api_key)

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

        self._upstream = self._make_upstream(api_key, **provider_kwargs)
        self.chat = _AsyncChatNamespace(self)

    def _make_upstream(self, api_key: str, **provider_kwargs) -> Any:
        if self._vendor == "openai":
            from openai import AsyncOpenAI
            return AsyncOpenAI(api_key=api_key, **provider_kwargs)
        if self._vendor == "anthropic":
            from anthropic import AsyncAnthropic
            return AsyncAnthropic(api_key=api_key, **provider_kwargs)
        raise ValueError(f"No async upstream factory for vendor {self._vendor!r}")

    def _build_event(
        self,
        model: str,
        messages: list,
        response: Any,
        latency_ms: int,
        ps_metadata: PSMetadata | None = None,
        adapter_fields: dict | None = None,
    ) -> dict:
        # Streams pass fields accumulated from their chunks instead.
        if adapter_fields is None:
            adapter_fields = self._adapter.extract(response)
        tokens_in = adapter_fields.get("tokens_in")
        tokens_out = adapter_fields.get("tokens_out")

        served_model = adapter_fields.get("served_model") or model

        event = {
            "vendor": self._vendor,
            "model": model,
            "requested_model": model,
            "served_model": served_model,
            "source": "sdk",
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "latency_ms": latency_ms,
            "tool_calls_used": adapter_fields.get("tool_calls_used"),
            # Priced at the served model (#6), the requested one if the
            # served id has no price.
            "cost": usage_cost(
                self._vendor,
                resolve_pricing_model(
                    self._vendor,
                    self._adapter.pricing_candidates(model, served_model),
                    self._pricing_table,
                ),
                adapter_fields,
                pricing_table=self._pricing_table,
            ),
            "api_key_fingerprint": self._api_key_fingerprint,
        }
        add_usage_detail_fields(event, adapter_fields)

        field_mapping = {"use_case": "use_case_name", "owner": "owner_email"}
        for sdk_key, collector_key in field_mapping.items():
            if sdk_key in self._metadata:
                event[collector_key] = self._metadata[sdk_key]
        for k, v in self._metadata.items():
            if k not in field_mapping:
                event[k] = v

        if ps_metadata:
            for key in ("data_sources", "output_destination", "risk_tags",
                        "session_id", "user_id"):
                if key in ps_metadata:
                    event[key] = ps_metadata[key]

        if self._send_prompt_text:
            event["prompt_text"] = " ".join(
                m.get("content", "") if isinstance(m, dict) else ""
                for m in messages
            )

        if self._scan_pii:
            categories = scan_messages(messages)
            if categories:
                event["detected_pii_types"] = categories

        return event

    def telemetry_stats(self) -> dict:
        """Delivery counters per destination: enqueued, sent, buffered and
        dropped events by reason. "atlas" is None when that sink is off."""
        return {
            "collector": self._telemetry.stats(),
            "atlas": self._atlas.stats() if self._atlas is not None else None,
        }

    async def aclose(self):
        unregister_exit_flush(self._telemetry, self._atlas)
        await self._telemetry.close()
        if self._atlas is not None:
            await self._atlas.close()


class _AsyncChatNamespace:
    def __init__(self, parent: AsyncShieldsClient):
        self._parent = parent
        self.completions = _AsyncCompletionsNamespace(parent)


class _AsyncCompletionsNamespace:
    def __init__(self, parent: AsyncShieldsClient):
        self._parent = parent

    async def create(
        self,
        *,
        model: str,
        messages: list,
        ps_metadata: PSMetadata | None = None,
        route: RouteHint | None = None,
        max_tokens: int | None = None,
        **kwargs,
    ):
        if route is not None:
            kwargs["extra_headers"] = {
                **kwargs.get("extra_headers", {}),
                **route.to_headers(),
            }

        streaming = kwargs.get("stream") is True
        hide_usage_chunk = False
        if (
            streaming
            and self._parent._vendor == "openai"
            and upstream_accepts_stream_options(self._parent._upstream)
        ):
            hide_usage_chunk = request_openai_stream_usage(kwargs)

        notify_prompt_features(
            self._parent._on_prompt_features,
            messages,
            self._parent._metadata.get("use_case"),
        )

        start = time.monotonic()
        # This client emits its own event; keep instrument() from adding one.
        with suppress_instrumentation():
            response = await self._call_upstream(
                model=model, messages=messages, max_tokens=max_tokens, **kwargs
            )

        if streaming:
            return self._wrap_stream(
                response, model=model, messages=messages,
                ps_metadata=ps_metadata, start=start,
                hide_usage_chunk=hide_usage_chunk,
            )

        latency_ms = int((time.monotonic() - start) * 1000)

        event = self._parent._build_event(
            model=model, messages=messages, response=response,
            latency_ms=latency_ms, ps_metadata=ps_metadata,
        )
        await self._send(event, messages)
        return response

    async def _send(self, event: dict, messages: list,
                    flush: bool = True) -> None:
        self._enqueue(event, messages)
        if flush:
            await self._parent._telemetry.flush()
            if self._parent._atlas is not None:
                await self._parent._atlas.flush()

    def _enqueue(self, event: dict, messages: list) -> None:
        self._parent._telemetry.enqueue(event)
        if self._parent._atlas is not None:
            self._parent._atlas.enqueue(build_atlas_event(event, messages))

    def _wrap_stream(self, response: Any, *, model: str, messages: list,
                     ps_metadata: PSMetadata | None, start: float,
                     hide_usage_chunk: bool) -> Any:
        """Wrap a stream so one event is emitted when it ends. Fail-open."""

        def stream_event(adapter_fields: dict, stream_fields: dict,
                         latency_ms: int) -> dict:
            event = self._parent._build_event(
                model=model, messages=messages, response=None,
                latency_ms=latency_ms, ps_metadata=ps_metadata,
                adapter_fields=adapter_fields,
            )
            event.update(stream_fields)
            if adapter_fields.get("tokens_estimated"):
                event["tokens_estimated"] = True
            return event

        async def on_finish(*args, flush: bool = True) -> None:
            await self._send(stream_event(*args), messages, flush=flush)

        try:
            return AsyncShieldsStream(
                response,
                StreamAccumulator(self._parent._vendor, messages),
                on_finish,
                start,
                hide_usage_chunk=hide_usage_chunk,
                # Garbage-collected unfinished: enqueued, sent by a later
                # flush (no I/O inside the garbage collector).
                on_abandon=lambda *args: self._enqueue(stream_event(*args),
                                                       messages),
            )
        except Exception:  # noqa: BLE001 - fail-open: return the raw stream
            return response

    async def _call_upstream(self, *, model: str, messages: list,
                             max_tokens: int | None = None, **kwargs):
        upstream = self._parent._upstream
        vendor = self._parent._vendor

        if vendor == "openai":
            return await upstream.chat.completions.create(
                model=model, messages=messages, **kwargs
            )
        if vendor == "anthropic":
            return await upstream.messages.create(
                model=model, messages=messages,
                max_tokens=max_tokens if max_tokens is not None else 1024,
                **kwargs,
            )
        raise ValueError(f"No async upstream dispatch for vendor {vendor!r}")


class AsyncShieldsOpenAI(AsyncShieldsClient):
    def __init__(self, **kwargs):
        kwargs.setdefault("vendor", "openai")
        super().__init__(**kwargs)


class AsyncShieldsAnthropic(AsyncShieldsClient):
    def __init__(self, **kwargs):
        kwargs.setdefault("vendor", "anthropic")
        super().__init__(**kwargs)
