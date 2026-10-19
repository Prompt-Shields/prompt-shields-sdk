"""Client-side MCP discovery: `instrument()` patches `mcp.ClientSession`.

Most tests use a fake `mcp.client.session` module, so they run whether or
not the `mcp` package is installed. The last test drives the real package
(skipped when it is missing) with an in-memory server.
"""

import asyncio
import sys
from types import ModuleType
from types import SimpleNamespace as NS

import prompt_shields
import pytest
from prompt_shields import instrumentation as inst
from prompt_shields import mcp_instrumentation as mi

from .test_instrumentation import RecordingSender, fakes  # noqa: F401 - fixture

SECRET_ARG = "token-in-an-argument"
SECRET_RESULT = "result-with-customer-data"


class FakeSession:
    """mcp 1.x shape: `initialize()` returns a result with `serverInfo`,
    `call_tool` results carry `isError`; no `server_info` property."""

    server_name = "github"

    async def initialize(self):
        return NS(serverInfo=NS(name=self.server_name, version="1.0"))

    async def call_tool(self, name, arguments=None, read_timeout_seconds=None):
        if name == "explode":
            raise RuntimeError("transport closed")
        return NS(isError=name == "fails", content=[NS(text=SECRET_RESULT)])


@pytest.fixture
def mcp_session(fakes, monkeypatch):  # noqa: F811 - pytest fixture
    session_mod = ModuleType("mcp.client.session")

    # A fresh class per test, with the methods in its own __dict__ (what
    # instrument() patches).
    session_mod.ClientSession = type(
        "ClientSession", (), {k: v for k, v in vars(FakeSession).items()
                              if not k.startswith("__")})
    monkeypatch.setitem(sys.modules, "mcp.client.session", session_mod)
    return session_mod


def _mcp_sender():
    mcp = [s for s in RecordingSender.instances
           if s.kwargs.get("path") == "/ingest/mcp"]
    assert len(mcp) == 1
    return mcp[0]


@pytest.fixture(autouse=True)
def _record_kwargs(monkeypatch):
    original = RecordingSender.__init__

    def init(self, *args, **kwargs):
        original(self, *args, **kwargs)
        self.kwargs = kwargs

    monkeypatch.setattr(RecordingSender, "__init__", init)


async def _session_calls(cls, *calls):
    session = cls()
    await session.initialize()
    out = []
    for name, args in calls:
        out.append(await session.call_tool(name, args))
    return out


def test_records_names_outcome_and_latency_only(mcp_session):
    prompt_shields.instrument(
        ps_api_key="ps_key", calling_service="billing-agent", environment="prod"
    )
    results = asyncio.run(_session_calls(
        mcp_session.ClientSession,
        ("create_issue", {"token": SECRET_ARG}),
        ("fails", {}),
    ))
    assert results[0].content[0].text == SECRET_RESULT  # unchanged

    sender = _mcp_sender()
    assert sender.args == ("http://localhost:8000", "ps_key")
    assert sender.kwargs["report_loss"] is False
    ok, failed = sender.events
    assert set(ok) == {"server_name", "tool_name", "outcome", "latency_ms", "source",
                       "calling_service", "environment"}
    assert ok["server_name"] == "github"
    assert ok["tool_name"] == "create_issue"
    assert ok["outcome"] == "ok" and ok["source"] == "sdk"
    assert isinstance(ok["latency_ms"], int) and ok["latency_ms"] >= 0
    assert (ok["calling_service"], ok["environment"]) == ("billing-agent", "prod")
    assert failed["outcome"] == "error"
    assert SECRET_ARG not in repr(sender.events)
    assert SECRET_RESULT not in repr(sender.events)
    # Usage events are untouched by MCP calls.
    assert RecordingSender.instances[0].events == []


def test_positional_and_keyword_tool_names(mcp_session):
    prompt_shields.instrument(ps_api_key="ps_key")

    async def run():
        s = mcp_session.ClientSession()
        await s.initialize()
        await s.call_tool(name="by_keyword", arguments={})

    asyncio.run(run())
    assert _mcp_sender().events[0]["tool_name"] == "by_keyword"


def test_exception_is_recorded_and_reraised_unchanged(mcp_session):
    prompt_shields.instrument(ps_api_key="ps_key")

    with pytest.raises(RuntimeError, match="transport closed"):
        asyncio.run(_session_calls(mcp_session.ClientSession, ("explode", {})))
    assert _mcp_sender().events[0]["outcome"] == "error"


def test_telemetry_failure_never_reaches_the_caller(mcp_session, monkeypatch):
    prompt_shields.instrument(ps_api_key="ps_key")

    def broken(self):
        raise RuntimeError("sender broken")

    monkeypatch.setattr(inst._State, "mcp_sender", broken)
    result = asyncio.run(_session_calls(mcp_session.ClientSession, ("ok_tool", {})))
    assert result[0].isError is False


def test_unknown_server_without_initialize(mcp_session):
    prompt_shields.instrument(ps_api_key="ps_key")

    async def run():
        await mcp_session.ClientSession().call_tool("t")

    asyncio.run(run())
    assert _mcp_sender().events[0]["server_name"] == "unknown"


def test_server_info_property_is_preferred(mcp_session):
    """mcp >= 2 exposes `server_info` (also set by discover()/adopt())."""

    class Modern(mcp_session.ClientSession):
        server_info = NS(name="linear")

    prompt_shields.instrument(ps_api_key="ps_key")

    async def run():
        await Modern().call_tool("search", {})

    asyncio.run(run())
    assert _mcp_sender().events[0]["server_name"] == "linear"


@pytest.mark.parametrize(("reported", "stored"), [
    ("https://user:pa55@mcp.example.com:8443/v1?key=abc", "mcp.example.com:8443"),
    ("http://mcp.internal/sse", "mcp.internal"),
    ("  jira\x00\n ", "jira"),
    ("x" * 500, "x" * mi.MAX_NAME_LENGTH),
    ("https:///nohost", None),
    (42, None),
])
def test_server_identity_is_names_only(reported, stored):
    assert mi.server_identity(reported) == stored


def test_vendors_filter_skips_mcp(mcp_session):
    original = mcp_session.ClientSession.__dict__["call_tool"]
    prompt_shields.instrument(ps_api_key="ps_key", vendors=("openai",))
    assert mcp_session.ClientSession.__dict__["call_tool"] is original


def test_suppressed_calls_are_not_recorded(mcp_session):
    prompt_shields.instrument(ps_api_key="ps_key")

    async def run():
        s = mcp_session.ClientSession()
        with inst.suppress_instrumentation():
            await s.call_tool("quiet", {})

    asyncio.run(run())
    assert not [s for s in RecordingSender.instances
                if s.kwargs.get("path") == "/ingest/mcp"]


def test_uninstrument_restores_client_session(mcp_session):
    cls = mcp_session.ClientSession
    originals = {m: cls.__dict__[m] for m in ("initialize", "call_tool")}
    prompt_shields.instrument(ps_api_key="ps_key")
    assert cls.__dict__["call_tool"] is not originals["call_tool"]
    prompt_shields.uninstrument()
    assert {m: cls.__dict__[m] for m in originals} == originals


def test_real_mcp_package_in_memory(fakes, monkeypatch):  # noqa: F811
    pytest.importorskip("mcp")
    try:
        from mcp import Client
        from mcp.server.mcpserver import MCPServer
    except ImportError:
        pytest.skip("needs mcp >= 2 (in-memory Client)")

    server = MCPServer("weather-srv")

    @server.tool()
    def forecast(city: str) -> str:
        return f"sunny in {city}"

    @server.tool()
    def broken() -> str:
        raise ValueError("nope")

    prompt_shields.instrument(ps_api_key="ps_key", vendors=("mcp",))

    async def run():
        async with Client(server) as client:
            ok = await client.call_tool("forecast", {"city": SECRET_ARG})
            bad = await client.call_tool("broken", {})
            return ok, bad

    ok, bad = asyncio.run(run())
    assert ok.is_error is False and bad.is_error is True
    events = _mcp_sender().events
    assert [(e["server_name"], e["tool_name"], e["outcome"]) for e in events] == [
        ("weather-srv", "forecast", "ok"),
        ("weather-srv", "broken", "error"),
    ]
    assert SECRET_ARG not in repr(events)
