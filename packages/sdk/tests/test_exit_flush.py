"""Best-effort telemetry flush at interpreter exit (`register_exit_flush`).

Unit tests drive the registry and the `atexit` hook directly; the end-to-end
tests run a short script in a subprocess against a local HTTP collector.
"""

import http.server
import json
import os
import socket
import subprocess
import sys
import textwrap
import threading
import time
import weakref
from typing import ClassVar

import httpx
import prompt_shields
import pytest
from prompt_shields import ShieldsClient, telemetry
from prompt_shields.telemetry import TelemetrySender

SDK_DIR = os.path.dirname(os.path.dirname(os.path.abspath(telemetry.__file__)))


@pytest.fixture
def registry(monkeypatch):
    """A fresh exit-flush registry; `atexit` calls are recorded, not made."""
    calls = []
    monkeypatch.setattr(telemetry, "_exit_senders", weakref.WeakSet())
    monkeypatch.setattr(telemetry, "_exit_hook_registered", False)
    monkeypatch.setattr(telemetry, "_exit_timeout_override", None)
    monkeypatch.setattr(telemetry.atexit, "register",
                        lambda fn: calls.append(("register", fn)))
    monkeypatch.setattr(telemetry.atexit, "unregister",
                        lambda fn: calls.append(("unregister", fn)))
    for var in (telemetry.EXIT_FLUSH_DISABLE_ENV, telemetry.EXIT_FLUSH_TIMEOUT_ENV):
        monkeypatch.delenv(var, raising=False)
    return calls


class FakeSender:
    def __init__(self, cost=0.0, fail=False):
        self.timeouts = []
        self.cost = cost
        self.fail = fail

    def flush_at_exit(self, timeout):
        self.timeouts.append(timeout)
        if self.fail:
            raise RuntimeError("boom")
        time.sleep(self.cost)


def _sender(handler):
    sender = TelemetrySender("http://collector.test", "ps_key")
    sender._sync_client = httpx.Client(transport=httpx.MockTransport(handler))
    return sender


def test_hook_is_registered_once_and_removed_with_the_last_sender(registry):
    a, b = FakeSender(), FakeSender()
    telemetry.register_exit_flush(a, None)
    telemetry.register_exit_flush(b)
    assert [c[0] for c in registry] == ["register"]
    telemetry.unregister_exit_flush(a)
    assert [c[0] for c in registry] == ["register"]
    telemetry.unregister_exit_flush(b, None)
    assert [c[0] for c in registry] == ["register", "unregister"]
    telemetry.unregister_exit_flush(b)  # already gone: no second unregister
    assert len(registry) == 2


def test_hook_flushes_every_sender_within_one_budget(registry, monkeypatch):
    monkeypatch.setenv(telemetry.EXIT_FLUSH_TIMEOUT_ENV, "0.3")
    slow, failing, last = FakeSender(cost=0.4), FakeSender(fail=True), FakeSender()
    telemetry.register_exit_flush(slow, failing, last)
    started = time.monotonic()
    telemetry._flush_at_exit()  # never raises
    assert time.monotonic() - started < 1.0
    # Each gets what is left of the one budget; none after the slow one,
    # which spends it (set order is arbitrary, so it may come first).
    assert slow.timeouts
    assert all(0 < t <= 0.3 for s in (slow, failing, last) for t in s.timeouts)
    assert not any(t < slow.timeouts[0] - 0.01 for s in (failing, last)
                   for t in s.timeouts)


def test_hook_respects_opt_out_and_zero_timeout(registry, monkeypatch):
    sender = FakeSender()
    telemetry.register_exit_flush(sender)
    monkeypatch.setenv(telemetry.EXIT_FLUSH_DISABLE_ENV, "1")
    telemetry._flush_at_exit()
    monkeypatch.delenv(telemetry.EXIT_FLUSH_DISABLE_ENV)
    telemetry.set_exit_flush_timeout(0)
    telemetry._flush_at_exit()
    assert sender.timeouts == []
    telemetry.set_exit_flush_timeout(None)
    monkeypatch.setenv(telemetry.EXIT_FLUSH_TIMEOUT_ENV, "not a number")
    assert telemetry.exit_flush_timeout() == telemetry.EXIT_FLUSH_TIMEOUT
    telemetry._flush_at_exit()
    assert len(sender.timeouts) == 1


def test_flush_at_exit_sends_buffered_events():
    posted = []

    def handler(request):
        posted.append(json.loads(request.content)["events"])
        return httpx.Response(200, json={})

    sender = _sender(handler)
    sender.enqueue({"n": 1})
    sender.enqueue({"n": 2})
    sender.flush_at_exit(1.0)
    assert posted == [[{"n": 1}, {"n": 2}]]
    assert sender.stats()["sent"] == 2
    sender.flush_at_exit(1.0)  # nothing left: no request
    assert len(posted) == 1


def test_flush_at_exit_keeps_events_it_could_not_send():
    def handler(request):
        raise httpx.ConnectError("refused")

    sender = _sender(handler)
    sender.enqueue({"n": 1})
    sender.flush_at_exit(1.0)  # never raises
    assert sender.stats()["buffered"] == 1
    sender.flush_at_exit(0)  # no budget: no request, still buffered
    assert sender.stats()["buffered"] == 1

    # Rate limited: the back-off is respected; closed: nothing is sent.
    sender._retry_at = time.monotonic() + 60
    sender.flush_at_exit(1.0)
    sender._retry_at = 0.0
    sender.close_sync()
    sender.enqueue({"n": 2})
    sender.flush_at_exit(1.0)


def test_instrument_and_clients_register_their_senders(registry, monkeypatch):
    monkeypatch.delenv("PS_ATLAS_URL", raising=False)
    prompt_shields.instrument(ps_api_key="ps_key", vendors=(),
                              exit_flush_timeout=0.5)
    try:
        assert telemetry.exit_flush_timeout() == 0.5
        assert len(telemetry._exit_senders) == 1
    finally:
        prompt_shields.uninstrument()
    assert len(telemetry._exit_senders) == 0
    assert telemetry.exit_flush_timeout() == telemetry.EXIT_FLUSH_TIMEOUT

    pytest.importorskip("openai")
    client = ShieldsClient(api_key="sk-test", ps_api_key="ps-test")
    assert client._telemetry in telemetry._exit_senders
    client.close()
    assert client._telemetry not in telemetry._exit_senders


# --- end to end: a short script that exits right after a stream -------------


class _Collector(http.server.BaseHTTPRequestHandler):
    received: ClassVar[list] = []

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        _Collector.received.extend(json.loads(body)["events"])
        payload = b"{}"
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


@pytest.fixture
def collector():
    _Collector.received = []
    server = http.server.HTTPServer(("127.0.0.1", 0), _Collector)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


SCRIPT = textwrap.dedent("""
    import sys

    import httpx
    import openai
    import prompt_shields

    prompt_shields.instrument(ps_api_key="ps_key", ps_collector_url=sys.argv[1])
    http = getattr(openai._base_client, "httpx2", None) or httpx
    chunk = ('data: {"id": "c", "object": "chat.completion.chunk", "created": 0, '
             '"model": "gpt-4o", "choices": [{"index": 0, "delta": '
             '{"content": "yo"}}]}\\n\\n')
    transport = http.MockTransport(lambda r: http.Response(
        200, text=chunk * 3 + "data: [DONE]\\n\\n",
        headers={"content-type": "text/event-stream"}))
    client = openai.OpenAI(api_key="sk", http_client=http.Client(transport=transport))

    def first():
        for c in client.chat.completions.create(
                model="gpt-4o", messages=[{"role": "user", "content": "hi"}],
                stream=True):
            return c  # abandoned: its event is only enqueued

    first()
""")


def _run(url, **env):
    pytest.importorskip("openai")
    child_env = {k: v for k, v in os.environ.items()
                 if not k.lower().endswith("_proxy")
                 and not k.startswith("PS_")}
    child_env.update(PYTHONPATH=SDK_DIR, NO_PROXY="*", **env)
    started = time.monotonic()
    result = subprocess.run([sys.executable, "-c", SCRIPT, url], check=False,
                            env=child_env,
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    return result, time.monotonic() - started


def test_short_script_sends_the_abandoned_stream_event_at_exit(collector):
    _run(collector)
    assert len(_Collector.received) == 1
    event = _Collector.received[0]
    assert event["stream_abandoned"] is True
    assert event["vendor"] == "openai"


def test_exit_flush_opt_out(collector):
    result, _ = _run(collector, PS_DISABLE_EXIT_FLUSH="1")
    assert _Collector.received == []
    assert "never sent" in result.stderr  # the existing unsent-events warning


def test_exit_flush_is_bounded_when_the_collector_hangs():
    # Accepts connections (backlog) but never answers.
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(8)
    try:
        url = f"http://127.0.0.1:{sock.getsockname()[1]}"
        _, quick = _run(url, PS_DISABLE_EXIT_FLUSH="1")
        _, hanging = _run(url, PS_EXIT_FLUSH_TIMEOUT="0.5")
    finally:
        sock.close()
    assert hanging - quick < 2.5


FORK_SCRIPT = textwrap.dedent("""
    import json
    import os
    import sys

    import httpx
    from prompt_shields.telemetry import TelemetrySender, register_exit_flush

    def post(url, json=None, headers=None, timeout=None):
        with open(sys.argv[1], "a") as f:
            f.write(dumps([e["id"] for e in json["events"]]) + "\\n")
        return httpx.Response(200, json={}, request=httpx.Request("POST", url))

    dumps = json.dumps
    sender = TelemetrySender("http://collector.test", "ps_key")
    sender._sync_client.post = post
    register_exit_flush(sender)
    sender.enqueue({"id": "parent"})
    pid = os.fork()
    if pid == 0:
        sender.enqueue({"id": "child"})
        assert sender.stats()["enqueued"] == 1
        sys.exit(0)
    os.waitpid(pid, 0)
""")


@pytest.mark.skipif(not hasattr(os, "fork"), reason="needs os.fork")
def test_forked_child_does_not_send_the_parents_buffered_events(tmp_path):
    # A worker forked with events still buffered (gunicorn --preload): only
    # the parent sends them; the child's flush (here, at exit) sends its own.
    log = tmp_path / "posts"
    env = {k: v for k, v in os.environ.items() if not k.startswith("PS_")}
    env["PYTHONPATH"] = SDK_DIR
    result = subprocess.run([sys.executable, "-c", FORK_SCRIPT, str(log)],
                            check=False, env=env, capture_output=True,
                            text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    batches = sorted(json.loads(line) for line in log.read_text().splitlines())
    assert batches == [["child"], ["parent"]]
