"""MCP telemetry off the call path: `BackgroundTelemetrySender` and the
"batch" (default) / "sync" `mcp_telemetry_mode`.

Sender tests drive the sender against an `httpx.MockTransport`; the
instrumentation tests run real senders against a local HTTP collector that
can be made slow, with a fake `mcp.client.session` module, and the last one
uses the real `mcp` package when it is installed.
"""

import asyncio
import http.server
import json
import os
import subprocess
import sys
import textwrap
import threading
import time
from types import ModuleType
from types import SimpleNamespace as NS
from typing import ClassVar

import httpx
import prompt_shields
import pytest
from prompt_shields import instrumentation as inst
from prompt_shields import telemetry
from prompt_shields.telemetry import (
    MAX_BUFFER_SIZE,
    BackgroundTelemetrySender,
    TelemetrySender,
)

SDK_DIR = os.path.dirname(os.path.dirname(os.path.abspath(telemetry.__file__)))


def _wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class _Transport:
    """Records each POST's events; `delay` slows every response."""

    def __init__(self, delay=0.0, status=200):
        self.batches: list[list[dict]] = []
        self.delay = delay
        self.status = status
        self.lock = threading.Lock()

    def __call__(self, request):
        if self.delay:
            time.sleep(self.delay)
        events = json.loads(request.content)["events"]
        with self.lock:
            self.batches.append(events)
        return httpx.Response(self.status, json={"ingested": len(events),
                                                 "skipped": 0})

    @property
    def events(self):
        with self.lock:
            return [e for b in self.batches for e in b]


def _sender(transport, **kwargs):
    kwargs.setdefault("path", "/ingest/mcp")
    kwargs.setdefault("report_loss", False)
    sender = BackgroundTelemetrySender("http://collector.test", "ps_key", **kwargs)
    sender._sync_client = httpx.Client(transport=httpx.MockTransport(transport))
    return sender


@pytest.fixture
def senders():
    made = []
    yield made
    for s in made:
        s.stop(final_flush=False)


# --- BackgroundTelemetrySender -----------------------------------------------


def test_enqueue_never_waits_for_a_slow_collector(senders):
    transport = _Transport(delay=1.0)
    s = _sender(transport, flush_size=1, flush_interval=60)
    senders.append(s)
    started = time.monotonic()
    for i in range(50):
        s.enqueue({"i": i})
    assert time.monotonic() - started < 0.2
    assert _wait_for(lambda: transport.batches, timeout=3)


def test_flushes_when_flush_size_events_are_buffered(senders):
    transport = _Transport()
    s = _sender(transport, flush_size=5, flush_interval=60)
    senders.append(s)
    for i in range(4):
        s.enqueue({"i": i})
    time.sleep(0.2)
    assert transport.batches == []  # neither size nor interval reached
    s.enqueue({"i": 4})
    assert _wait_for(lambda: transport.batches)
    assert transport.batches == [[{"i": i} for i in range(5)]]  # one request


def test_flushes_on_the_interval(senders):
    transport = _Transport()
    s = _sender(transport, flush_size=100, flush_interval=0.1)
    senders.append(s)
    for i in range(3):
        s.enqueue({"i": i})
    assert _wait_for(lambda: transport.batches, timeout=2)
    assert transport.batches == [[{"i": 0}, {"i": 1}, {"i": 2}]]
    s.enqueue({"i": 3})
    assert _wait_for(lambda: len(transport.batches) == 2, timeout=2)


def test_large_backlog_goes_out_in_max_batch_requests(senders):
    transport = _Transport()
    s = _sender(transport, flush_size=10_000, flush_interval=60, max_batch=500)
    senders.append(s)
    for i in range(1000):
        s.enqueue({"i": i})
    s.flush_sync()
    assert [len(b) for b in transport.batches] == [500, 500]


def test_backlog_is_chunked_under_the_body_size_limit(senders):
    """500 events with long non-ASCII names are over /ingest/mcp's 1 MiB:
    the MCP sender splits by size as well as by count."""
    transport = _Transport()
    s = _sender(transport, flush_size=10_000, flush_interval=60, max_batch=500,
                max_batch_bytes=inst._MCP_MAX_BATCH_BYTES)
    senders.append(s)
    name = "\U0001F600" * 200  # the SDK's cap on a name, 4 bytes per char
    for i in range(1000):
        s.enqueue({"server_name": name, "tool_name": name, "calling_service": name,
                   "environment": name, "outcome": "ok", "latency_ms": i})
    s.flush_sync()
    assert len(transport.events) == 1000
    assert len(transport.batches) > 2
    for batch in transport.batches:
        body = json.dumps({"events": batch}, ensure_ascii=False,
                          separators=(",", ":")).encode()
        assert len(body) <= 1024 * 1024
        assert len(batch) <= 500


def test_failing_sends_retry_on_the_interval_not_per_event(senders):
    """A failing collector is retried every flush_interval, not on each new
    event once flush_size are buffered (that would resend the backlog per
    tool call)."""
    transport = _Transport(status=503)
    s = _sender(transport, flush_size=1, flush_interval=60)
    senders.append(s)
    s.enqueue({"i": 0})
    assert _wait_for(lambda: s._backlog_reason == "send_failed")
    posts = len(transport.batches)
    for i in range(1, 50):
        s.enqueue({"i": i})
    time.sleep(0.3)
    assert len(transport.batches) == posts
    assert s.stats()["buffered"] == 50


def test_rate_limited_sends_back_off_without_a_hot_loop(senders):
    calls = []

    def limited(request):
        calls.append(time.monotonic())
        return httpx.Response(429, headers={"Retry-After": "30"})

    s = _sender(limited, flush_size=1, flush_interval=0.05)
    senders.append(s)
    s.enqueue({"i": 0})
    assert _wait_for(lambda: calls)
    for i in range(1, 20):
        s.enqueue({"i": i})
    time.sleep(0.5)  # ~10 intervals: all inside the 30 s Retry-After
    assert len(calls) == 1
    assert s.stats()["rate_limited_responses"] == 1
    assert s.stats()["buffered"] == 20


@pytest.mark.skipif(not hasattr(os, "fork"), reason="needs os.fork")
@pytest.mark.filterwarnings("ignore:This process .* is multi-threaded")
def test_forked_child_starts_its_own_thread_and_drops_the_parents_events(
        senders, collector):
    s = BackgroundTelemetrySender(collector, "ps_key", path="/ingest/mcp",
                                  report_loss=False, flush_size=1000,
                                  flush_interval=60)
    senders.append(s)
    s.enqueue({"tool_name": "parent"})
    parent_thread = s._thread
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:  # child: report, then exit without running test teardown
        ok = False
        try:
            fresh = s._thread is None and s.stats()["buffered"] == 0
            s.enqueue({"tool_name": "child"})
            s.flush_sync()
            ok = fresh and s._thread is not None and s._thread.is_alive()
        finally:
            os.write(w, b"1" if ok else b"0")
            os._exit(0)
    os.close(w)
    with os.fdopen(r, "rb") as f:
        assert f.read() == b"1"
    os.waitpid(pid, 0)
    assert parent_thread.is_alive()
    s.flush_sync()
    assert sorted(e["tool_name"] for _, b in _Collector.received for e in b) == [
        "child", "parent"]


def test_full_buffer_drops_the_oldest_and_counts_it(senders):
    transport = _Transport()
    s = _sender(transport, flush_size=10_000, flush_interval=60)
    senders.append(s)
    s._retry_at = time.monotonic() + 60  # backing off: nothing is sent
    for i in range(MAX_BUFFER_SIZE + 25):
        s.enqueue({"i": i})
    stats = s.stats()
    assert stats["buffered"] == MAX_BUFFER_SIZE
    assert stats["dropped"]["buffer_overflow"] == 25
    assert stats["enqueued"] == MAX_BUFFER_SIZE + 25
    s._retry_at = 0.0
    s.flush_sync()
    assert transport.events[0] == {"i": 25}  # the oldest 25 were dropped
    assert len(transport.events) == MAX_BUFFER_SIZE


def test_concurrent_enqueues_lose_nothing_uncounted(senders):
    transport = _Transport(delay=0.002)
    s = _sender(transport, flush_size=50, flush_interval=0.01)
    senders.append(s)
    n_threads, per_thread = 8, 500

    def produce(t):
        for i in range(per_thread):
            s.enqueue({"t": t, "i": i})

    threads = [threading.Thread(target=produce, args=(t,)) for t in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    s.stop(final_flush=False)
    s.flush_sync()
    sent = [(e["t"], e["i"]) for e in transport.events]
    stats = s.stats()
    assert len(sent) == len(set(sent))  # nothing sent twice
    assert stats["enqueued"] == n_threads * per_thread
    assert len(sent) + stats["dropped_total"] == n_threads * per_thread
    assert stats["buffered"] == 0


def test_errors_are_never_raised_to_the_caller(senders):
    def broken(request):
        raise httpx.ConnectError("down")

    s = _sender(broken, flush_size=1, flush_interval=0.05)
    senders.append(s)
    s.enqueue({"i": 0})
    assert _wait_for(lambda: s.stats()["buffered"] == 1
                     and s._backlog_reason == "send_failed")
    s.enqueue({"i": 1})  # still fine; the thread is alive
    assert s._thread.is_alive()


def test_exit_flush_stops_the_thread_and_sends_the_rest(senders):
    transport = _Transport()
    s = _sender(transport, flush_size=100, flush_interval=60)
    senders.append(s)
    s.enqueue({"i": 0})
    thread = s._thread
    s.flush_at_exit(1.0)
    assert transport.batches == [[{"i": 0}]]
    thread.join(1)
    assert not thread.is_alive()


def test_exit_flush_is_bounded_by_a_send_in_flight(senders):
    transport = _Transport(delay=2.0)
    s = _sender(transport, flush_size=1, flush_interval=60)
    senders.append(s)
    s.enqueue({"i": 0})
    assert _wait_for(lambda: s._send_lock.locked())  # thread is sending
    s.enqueue({"i": 1})
    started = time.monotonic()
    s.flush_at_exit(0.2)
    assert time.monotonic() - started < 0.6


def test_fork_reset_gives_the_child_a_fresh_thread_state(senders):
    s = _sender(_Transport(), flush_size=100, flush_interval=60)
    senders.append(s)
    s.enqueue({"i": 0})
    old_lock = s._lock
    s._reset_after_fork()
    assert s._thread is None and s._lock is not old_lock
    assert s.stats()["buffered"] == 0


# --- exit flush in a real interpreter ------------------------------------------


class _Collector(http.server.BaseHTTPRequestHandler):
    received: ClassVar[list] = []
    delay = 0.0

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        events = json.loads(body)["events"]
        if _Collector.delay:
            time.sleep(_Collector.delay)
        _Collector.received.append((self.path, events))
        payload = json.dumps({"ingested": len(events), "skipped": 0}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


@pytest.fixture
def collector(monkeypatch):
    _Collector.received = []
    _Collector.delay = 0.0
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Collector)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy",
                "https_proxy", "all_proxy"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("NO_PROXY", "*")
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


EXIT_SCRIPT = textwrap.dedent("""
    import sys
    from prompt_shields.telemetry import BackgroundTelemetrySender, register_exit_flush

    s = BackgroundTelemetrySender(sys.argv[1], "ps_key", path="/ingest/mcp",
                                  report_loss=False, flush_size=100,
                                  flush_interval=60)
    register_exit_flush(s)
    for i in range(3):
        s.enqueue({"tool_name": f"t{i}"})
""")


def test_buffered_events_are_sent_at_interpreter_exit(collector):
    env = {k: v for k, v in os.environ.items()
           if not k.lower().endswith("_proxy") and not k.startswith("PS_")}
    env.update(PYTHONPATH=SDK_DIR, NO_PROXY="*")
    result = subprocess.run([sys.executable, "-c", EXIT_SCRIPT, collector],
                            check=False, env=env, capture_output=True,
                            text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert _Collector.received == [
        ("/ingest/mcp", [{"tool_name": f"t{i}"} for i in range(3)])]


# --- instrument(): mode, wiring, suppression --------------------------------


class FakeSession:
    async def initialize(self):
        return NS(serverInfo=NS(name="github", version="1.0"))

    async def call_tool(self, name, arguments=None):
        await asyncio.sleep(0.002)
        return NS(isError=False, content=[])


@pytest.fixture
def instrumented(monkeypatch, collector):
    """A clean PS_* environment and a local collector; uninstruments after."""
    for var in ("PS_API_KEY", "PS_COLLECTOR_URL", "PS_ATLAS_URL",
                "PS_ATLAS_API_KEY", inst.MCP_TELEMETRY_MODE_ENV,
                *inst._METADATA_ENV.values()):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(telemetry, "register_exit_flush", lambda *a: None)
    monkeypatch.setattr(inst, "register_exit_flush", lambda *a: None)
    yield collector
    state = inst._state
    prompt_shields.uninstrument()
    if state is not None and isinstance(state.mcp_telemetry,
                                        BackgroundTelemetrySender):
        state.mcp_telemetry.stop(final_flush=False)


@pytest.fixture
def mcp_session(monkeypatch, instrumented):
    session_mod = ModuleType("mcp.client.session")
    session_mod.ClientSession = type(
        "ClientSession", (), {k: v for k, v in vars(FakeSession).items()
                              if not k.startswith("__")})
    monkeypatch.setitem(sys.modules, "mcp.client.session", session_mod)
    return NS(cls=session_mod.ClientSession, url=instrumented)


async def _timed_calls(cls, n):
    session = cls()
    await session.initialize()
    started = time.monotonic()
    for i in range(n):
        await session.call_tool(f"tool{i}", {})
    return time.monotonic() - started


def _mcp_events():
    return [e for path, batch in _Collector.received if path == "/ingest/mcp"
            for e in batch]


def test_batch_mode_call_latency_ignores_a_slow_collector(mcp_session):
    _Collector.delay = 1.0
    prompt_shields.instrument(ps_api_key="ps_key", ps_collector_url=mcp_session.url)
    elapsed = asyncio.run(_timed_calls(mcp_session.cls, 5))
    assert elapsed < 0.5  # five ~2 ms calls; the collector takes 1 s per POST
    sender = inst._state.mcp_telemetry
    assert isinstance(sender, BackgroundTelemetrySender)
    assert sender._max_batch_bytes == inst._MCP_MAX_BATCH_BYTES
    sender.stop(final_flush=True)  # send now instead of in 2 s
    assert _wait_for(lambda: len(_mcp_events()) == 5, timeout=5)
    assert [e["tool_name"] for e in _mcp_events()] == [f"tool{i}" for i in range(5)]
    assert len(_Collector.received) == 1  # one request for the batch


def test_sync_mode_sends_before_call_tool_returns(mcp_session, monkeypatch):
    monkeypatch.setenv(inst.MCP_TELEMETRY_MODE_ENV, "sync")
    _Collector.delay = 0.3
    prompt_shields.instrument(ps_api_key="ps_key", ps_collector_url=mcp_session.url)
    elapsed = asyncio.run(_timed_calls(mcp_session.cls, 2))
    assert elapsed >= 0.6
    assert type(inst._state.mcp_telemetry) is TelemetrySender
    assert len(_mcp_events()) == 2 and len(_Collector.received) == 2


def test_argument_overrides_the_environment(mcp_session, monkeypatch):
    monkeypatch.setenv(inst.MCP_TELEMETRY_MODE_ENV, "sync")
    prompt_shields.instrument(ps_api_key="ps_key", ps_collector_url=mcp_session.url,
                              mcp_telemetry_mode="batch")
    assert inst._state.mcp_mode == "batch"


def test_unknown_modes(mcp_session, monkeypatch, caplog):
    with pytest.raises(ValueError, match="mcp_telemetry_mode"):
        prompt_shields.instrument(ps_api_key="ps_key", mcp_telemetry_mode="later")
    assert not inst.is_instrumented()
    monkeypatch.setenv(inst.MCP_TELEMETRY_MODE_ENV, "bogus")
    with caplog.at_level("WARNING", logger="prompt_shields.instrumentation"):
        prompt_shields.instrument(ps_api_key="ps_key", ps_collector_url=mcp_session.url)
    assert inst._state.mcp_mode == "batch"
    assert "PS_MCP_TELEMETRY_MODE" in caplog.text


def test_suppressed_calls_are_not_buffered(mcp_session):
    prompt_shields.instrument(ps_api_key="ps_key", ps_collector_url=mcp_session.url)

    async def run():
        with inst.suppress_instrumentation():
            await mcp_session.cls().call_tool("quiet", {})

    asyncio.run(run())
    assert inst._state.mcp_telemetry is None  # no sender, no thread


def test_vendor_opt_out_leaves_mcp_alone(mcp_session):
    original = mcp_session.cls.__dict__["call_tool"]
    prompt_shields.instrument(ps_api_key="ps_key", ps_collector_url=mcp_session.url,
                              vendors=("openai",))
    assert mcp_session.cls.__dict__["call_tool"] is original


def test_uninstrument_sends_what_is_buffered(mcp_session):
    prompt_shields.instrument(ps_api_key="ps_key", ps_collector_url=mcp_session.url)
    asyncio.run(_timed_calls(mcp_session.cls, 3))
    sender = inst._state.mcp_telemetry
    prompt_shields.uninstrument()
    assert _wait_for(lambda: len(_mcp_events()) == 3)
    sender._thread.join(2)
    assert not sender._thread.is_alive()


def test_real_mcp_package_round_trip(instrumented):
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

    _Collector.delay = 1.0
    prompt_shields.instrument(ps_api_key="ps_key", ps_collector_url=instrumented,
                              vendors=("mcp",))

    async def run():
        async with Client(server) as client:
            started = time.monotonic()
            result = await client.call_tool("forecast", {"city": "Paris"})
            return result, time.monotonic() - started

    result, elapsed = asyncio.run(run())
    assert result.is_error is False
    assert elapsed < 0.5  # not held up by the 1 s collector
    inst._state.mcp_telemetry.stop(final_flush=True)
    assert _wait_for(lambda: _mcp_events(), timeout=5)
    (event,) = _mcp_events()
    assert (event["server_name"], event["tool_name"], event["outcome"]) == (
        "weather-srv", "forecast", "ok")
    assert "Paris" not in repr(_Collector.received)
