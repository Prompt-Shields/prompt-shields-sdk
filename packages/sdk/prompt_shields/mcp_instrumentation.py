"""Client-side MCP discovery: observe `mcp.ClientSession.call_tool`.

`instrument()` patches the official `mcp` package's `ClientSession` (when it
is installed and "mcp" is among the selected vendors) so every tool call the
app makes to an MCP server is reported to the collector's `/ingest/mcp`:

    {"server_name": "github", "tool_name": "create_issue",
     "outcome": "ok" | "error", "latency_ms": 41, "source": "sdk",
     "calling_service": ..., "environment": ...}

Names only. Tool arguments, results, tool descriptions, transport URLs,
commands, headers and environment are never read or sent. The server name is
the one the server reports about itself (`serverInfo.name`), so a URL-shaped
value is reduced to its host (never credentials, path or query). Every
string here comes from the MCP server or the app: it is capped and treated
as data.

Fail-open like the provider wrappers: telemetry errors are logged and
swallowed; the caller gets the tool's result or exception unchanged. Calls
made inside `suppress_instrumentation()` are not recorded.

Off the call path by default: in "batch" mode (`mcp_telemetry_mode`,
PS_MCP_TELEMETRY_MODE) the event is only buffered, with no I/O, and a
background thread posts batches to `/ingest/mcp`, so a slow or unreachable
collector does not delay `call_tool`. "sync" mode posts each event before
`call_tool` returns.
"""

from __future__ import annotations

import functools
import logging
import time
import weakref
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

logger = logging.getLogger("prompt_shields.instrumentation")

# Longest server or tool name sent; longer ones are cut (same cap as
# provider-hosted MCP calls in `providers.mcp_tool_call`).
MAX_NAME_LENGTH = 200
UNKNOWN_SERVER = "unknown"

# serverInfo.name captured from `initialize()` for mcp 1.x sessions, which
# have no `server_info` property. Weak, so a closed session is not kept.
_server_names: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


def server_identity(value: Any) -> str | None:
    """A server name safe to store: stripped, capped, no control characters,
    and a URL reduced to host[:port] (no credentials, path or query)."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if "://" in value:
        try:
            parts = urlsplit(value)
            host = parts.hostname
            port = parts.port
        except ValueError:
            return None
        if not host:
            return None
        value = f"{host}:{port}" if port else host
    return _label(value)


def _label(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = "".join(ch for ch in value.strip() if ch.isprintable())
    return value[:MAX_NAME_LENGTH] or None


def _name_of(info: Any) -> str | None:
    return server_identity(getattr(info, "name", None)) if info is not None else None


def _server_name(session: Any) -> str:
    name = None
    try:
        # mcp >= 2: a property, set by initialize(), discover() or adopt().
        name = _name_of(getattr(session, "server_info", None))
    except Exception:  # noqa: BLE001, S110 - a broken property: fall back
        pass
    if name is None:
        try:
            name = _server_names.get(session)
        except TypeError:  # not weak-referenceable / hashable
            name = None
    return name or UNKNOWN_SERVER


def _remember_server(session: Any, result: Any) -> None:
    """Keep serverInfo.name from an InitializeResult (1.x: `serverInfo`)."""
    info = getattr(result, "server_info", None) or getattr(result, "serverInfo", None)
    name = _name_of(info)
    if name is not None:
        try:
            _server_names[session] = name
        except TypeError:
            pass


def _is_error(result: Any) -> bool:
    for attr in ("is_error", "isError"):
        if getattr(result, attr, None) is True:
            return True
    return False


def make_wrapper(original: Any, api: str, get_state: Callable[[], Any]) -> Any:
    """Wrap `ClientSession.initialize` (api "mcp_initialize") or
    `ClientSession.call_tool` (api "mcp_call_tool"). `get_state` returns the
    instrumentation state, or None when uninstrumented or suppressed."""
    if api == "mcp_initialize":

        @functools.wraps(original)
        async def initialize(self, *args, **kwargs):
            result = await original(self, *args, **kwargs)
            try:
                _remember_server(self, result)
            except Exception as e:  # noqa: BLE001 - fail-open
                logger.debug(f"MCP instrumentation error: {e}")
            return result

        initialize.__ps_original__ = original
        return initialize

    @functools.wraps(original)
    async def call_tool(self, *args, **kwargs):
        state = get_state()
        if state is None:
            return await original(self, *args, **kwargs)
        start = time.monotonic()
        try:
            result = await original(self, *args, **kwargs)
        except Exception:
            # Recorded, then re-raised unchanged. Cancellation and other
            # BaseExceptions pass straight through.
            await _record(state, self, args, kwargs, "error", start)
            raise
        await _record(
            state, self, args, kwargs, "error" if _is_error(result) else "ok", start
        )
        return result

    call_tool.__ps_original__ = original
    return call_tool


async def _record(state: Any, session: Any, args: tuple, kwargs: dict,
                  outcome: str, start: float) -> None:
    try:
        tool = _label(args[0] if args else kwargs.get("name"))
        if tool is None:
            return
        event = {
            "server_name": _server_name(session),
            "tool_name": tool,
            "outcome": outcome,
            "latency_ms": int((time.monotonic() - start) * 1000),
            "source": "sdk",
        }
        for key in ("calling_service", "environment"):
            value = _label(state.metadata.get(key))
            if value is not None:
                event[key] = value
        sender = state.mcp_sender()
        sender.enqueue(event)
        if state.mcp_mode == "sync":
            await sender.flush()
    except Exception as e:  # noqa: BLE001 - fail-open
        logger.debug(f"MCP instrumentation error: {e}")
