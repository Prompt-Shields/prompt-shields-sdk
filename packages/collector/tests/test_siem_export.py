"""SIEM connectors (#353) without a database: configuration checks, the CEF
and syslog encodings, and each connector against a recorded receiver
(Splunk HEC and the webhook through an httpx mock transport behind the
SSRF guard; syslog through a real local TLS server). No network."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import ssl
import uuid
from decimal import Decimal

import httpx
import pytest
from collector import siem_export as sx
from collector.connectors import ConnectorError
from collector.partner_scopes import LEGACY_REGISTRY_READ, SCOPES, expand_scopes
from collector.webhooks import verify_signature
from db.models import AIUsageEvent, GatewaySecurityEvent
from pydantic import ValidationError

from tests.test_sentinel_export import _event, _no_sleep, _private, _public

TENANT = uuid.UUID("11111111-1111-1111-1111-111111111111")
HEC_TOKEN = "hec-TOKEN-0f0f0f0f"
HOOK_SECRET = "whsec-SIGNING-secret"
HOOK_BEARER = "bearer-TOKEN-value"


def _security_row(**over) -> GatewaySecurityEvent:
    fields = {
        "id": uuid.uuid4(),
        "tenant_id": TENANT,
        "event_id": "8a1c2f9e-0000-4000-8000-000000000001",
        "occurred_at": dt.datetime(2026, 11, 6, 12, 0, 0, tzinfo=dt.UTC),
        "received_at": dt.datetime(2026, 11, 6, 12, 0, 1, tzinfo=dt.UTC),
        "name": "injection.detected",
        "kind": "alert",
        "outcome": "failure",
        "severity": "high",
        "action": "blocked",
        "signature": "azure.shieldPrompt",
        "request_id": "req-1",
        "route": "/v1/chat/completions",
        "method": "POST",
        "business_unit": "Retail",
        "environment": None,
        "provider": "openai",
        "model": "gpt-4o",
        "status_code": None,
        "user_hash": None,
        "schema_version": "1.0.0",
        "attributes": {
            "event.category": ["intrusion_detection"],
            "ps.guardrail.check_id": "azure.shieldPrompt",
            "ps.guardrail.stage": "input",
        },
    }
    fields.update(over)
    return GatewaySecurityEvent(**fields)


def _records() -> dict[str, list[dict]]:
    return {
        "calls": [sx.call_record(_event())],
        "security": [sx.security_record(_security_row())],
    }


# ──────────────────────────────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "url",
    [
        "http://splunk.example.com:8088",
        "https://10.0.0.5:8088",
        "https://user:pw@splunk.example.com",
        "https://splunk.example.com/services/collector",
        "https://splunk.example.com?token=x",
        "ftp://splunk.example.com",
    ],
)
def test_splunk_url_must_be_an_https_dns_origin(url):
    with pytest.raises(ValidationError):
        sx.SplunkHecConnection(url=url, token=HEC_TOKEN)


def test_connection_normalisation():
    hec = sx.SplunkHecConnection(url="https://Splunk.Example.com:8088/", token=HEC_TOKEN)
    assert hec.url == "https://splunk.example.com:8088"
    assert hec.include == ["calls", "audit", "security"]
    hook = sx.WebhookConnection(url="https://hooks.example.com:443/in/ps")
    assert hook.url == "https://hooks.example.com/in/ps" and hook.host == "hooks.example.com"
    with pytest.raises(ValidationError):
        sx.WebhookConnection(url="https://hooks.example.com/in?key=secret")
    with pytest.raises(ValidationError):
        sx.SyslogConnection(host="192.168.1.10")
    with pytest.raises(ValidationError):
        sx.SyslogConnection(host="siem.example.com", app_name="has space")
    with pytest.raises(ValidationError):
        sx.SplunkHecConnection(url="https://s.example.com", token="t", index="Bad Index")
    with pytest.raises(ValidationError):
        sx.SplunkHecConnection(url="https://s.example.com", token="t", include=[])


def test_bad_configuration_never_echoes_a_secret(monkeypatch):
    monkeypatch.setenv(
        "PS_SIEM_EXPORT",
        json.dumps({str(TENANT): {"splunk_hec": {"url": "http://x", "token": HEC_TOKEN}}}),
    )
    with pytest.raises(ConnectorError) as exc:
        sx.load_settings()
    assert HEC_TOKEN not in str(exc.value)


def test_public_config_has_no_credentials_or_webhook_path():
    hook = sx.WebhookConnection(
        url="https://hooks.example.com/t/SECRET-PATH",
        signing_secret=HOOK_SECRET,
        bearer_token=HOOK_BEARER,
    )
    hec = sx.SplunkHecConnection(url="https://s.example.com:8088", token=HEC_TOKEN)
    text = json.dumps([sx.public_config(hook), sx.public_config(hec)])
    for secret in (HOOK_SECRET, HOOK_BEARER, HEC_TOKEN, "SECRET-PATH"):
        assert secret not in text
    assert sx.public_config(hook)["signed"] is True
    assert sx.public_config(hec)["port"] == 8088


def test_security_events_scope_is_explicit():
    assert "security-events:read" in SCOPES
    assert "security-events:read" not in LEGACY_REGISTRY_READ
    assert "security-events:read" not in expand_scopes(["registry:read"])


# ──────────────────────────────────────────────────────────────────────
# Encodings
# ──────────────────────────────────────────────────────────────────────


def test_cef_escaping():
    assert sx.cef_header_escape("a|b\\c\nd") == "a\\|b\\\\c d"
    assert sx.cef_value_escape("k=v\\x\r\n") == "k\\=v\\\\x\\r\\n"
    assert sx.cef_value_escape(True) == "true"
    assert sx.cef_value_escape({"b": 1, "a": [1]}) == '{"a":[1],"b":1}'
    assert sx.cef_key("ps.guardrail.check_id") == "ps_guardrail_check_id"
    assert sx.cef_key("@timestamp") == "timestamp"


def test_cef_security_event():
    rec = sx.security_record(_security_row(signature="evil|sig=1"))
    line = sx.cef_message("security", rec)
    header, ext = line.split("|", 7)[:7], line.split("|", 7)[7]
    assert header[:5] == [
        "CEF:0", "Prompt Shields", "Prompt Shields Gateway", "1.0", "injection.detected"
    ]
    assert line.startswith(
        "CEF:0|Prompt Shields|Prompt Shields Gateway|1.0|injection.detected|"
        "injection.detected evil\\|sig=1|8|"
    )
    rt = int(dt.datetime(2026, 11, 6, 12, 0, tzinfo=dt.UTC).timestamp() * 1000)
    assert f"rt={rt} " in ext
    assert "externalId=8a1c2f9e-0000-4000-8000-000000000001" in ext
    assert "cat=SecurityEvent" in ext and "outcome=failure" in ext and "act=blocked" in ext
    assert "signature=evil|sig\\=1" in ext
    assert 'event_category=["intrusion_detection"]' in ext
    assert "ps_tenant_id=" + str(TENANT) in ext
    assert "\n" not in line


def test_cef_call_and_syslog_frame():
    rec = sx.call_record(_event(cost=Decimal("0.5")))
    line = sx.cef_message("calls", rec)
    assert line.startswith("CEF:0|Prompt Shields|Prompt Shields|1.0|ai.call|AI call|1|")
    assert "PsServedJurisdiction=EEA" in line and "PsCostUsd=0.5" in line
    assert "sha256-of-the-prompt" not in line and "user-free-text-session" not in line

    conn = sx.SyslogConnection(host="siem.example.com", facility=10)
    msg = sx.syslog_message(conn, "security", sx.security_record(_security_row()))
    # facility 10 (authpriv) * 8 + severity 3 (high -> error)
    assert msg.startswith(
        b"<83>1 2026-11-06T12:00:00.000000Z prompt-shields-collector prompt-shields - "
        b"SecurityEvent - CEF:0|"
    )
    assert sx.syslog_frame(b"abc") == b"3 abc"
    as_json = sx.syslog_message(
        sx.SyslogConnection(host="siem.example.com", format="json"), "calls", rec
    )
    assert json.loads(as_json.split(b" - AICall - ", 1)[1])["PsCallId"] == "call-abc"


def test_size_batches():
    items = [b"x" * 10] * 5
    assert [len(b) for b in sx.size_batches(items, 25)] == [2, 2, 1]
    with pytest.raises(ConnectorError):
        sx.size_batches([b"x" * 30], 25)


# ──────────────────────────────────────────────────────────────────────
# Splunk HEC against a recorded receiver
# ──────────────────────────────────────────────────────────────────────


class Recorder:
    def __init__(self, statuses: list[int] | None = None):
        self.requests: list[httpx.Request] = []
        self._statuses = list(statuses or [])

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        status = self._statuses.pop(0) if self._statuses else 200
        return httpx.Response(status, json={"text": "Success", "code": 0})


async def test_splunk_hec_request_shape():
    rec = Recorder()
    conn = sx.SplunkHecConnection(
        url="https://splunk.example.com:8088", token=HEC_TOKEN, index="ps", host="gw-eu"
    )
    async with sx.SplunkHecSink(
        conn, TENANT, transport=httpx.MockTransport(rec), resolver=_public, sleep=_no_sleep
    ) as sink:
        for kind, records in _records().items():
            assert await sink.send(kind, records) == 1
    calls_req, sec_req = rec.requests
    # Pinned to the checked address, Host header kept.
    assert calls_req.url.host == "93.184.216.34" and calls_req.url.port == 8088
    assert calls_req.headers["host"] == "splunk.example.com:8088"
    assert calls_req.url.path == "/services/collector/event"
    assert calls_req.headers["authorization"] == f"Splunk {HEC_TOKEN}"
    (call,) = [json.loads(x) for x in calls_req.content.splitlines()]
    assert call["sourcetype"] == "prompt_shields:call" and call["index"] == "ps"
    assert call["source"] == "prompt-shields" and call["host"] == "gw-eu"
    assert call["time"] == dt.datetime(2026, 11, 1, 9, 0, tzinfo=dt.UTC).timestamp()
    assert call["event"]["PsCallId"] == "call-abc"
    (sec,) = [json.loads(x) for x in sec_req.content.splitlines()]
    assert sec["sourcetype"] == "prompt_shields:security"
    assert sec["event"]["event.name"] == "injection.detected"
    assert sec["event"]["vendor_product"] == "Prompt Shields Gateway"


async def test_splunk_hec_batches_and_errors_without_secrets(monkeypatch):
    rec = Recorder([200, 403])
    conn = sx.SplunkHecConnection(url="https://splunk.example.com", token=HEC_TOKEN)
    records = [sx.call_record(_event(call_id=f"c{i}")) for i in range(3)]
    async with sx.SplunkHecSink(
        conn, TENANT, transport=httpx.MockTransport(rec), resolver=_public, sleep=_no_sleep
    ) as sink:
        one = len(sink._event("calls", records[0])) + 1
        monkeypatch.setattr(sx, "MAX_BODY_BYTES", 2 * one + 10)
        with pytest.raises(ConnectorError) as exc:
            await sink.send("calls", records)
    assert "HTTP 403" in str(exc.value) and HEC_TOKEN not in str(exc.value)
    assert [len(r.content.splitlines()) for r in rec.requests] == [2, 1]


async def test_splunk_hec_refuses_private_addresses():
    rec = Recorder()
    conn = sx.SplunkHecConnection(url="https://splunk.example.com", token=HEC_TOKEN)
    async with sx.SplunkHecSink(
        conn, TENANT, transport=httpx.MockTransport(rec), resolver=_private, sleep=_no_sleep
    ) as sink:
        with pytest.raises(ConnectorError, match="non-public"):
            await sink.send("calls", _records()["calls"])
    assert rec.requests == []
    # Opt-in for a SIEM on an internal network.
    private = sx.SplunkHecConnection(
        url="https://splunk.example.com", token=HEC_TOKEN, allow_private_network=True
    )
    async with sx.SplunkHecSink(
        private, TENANT, transport=httpx.MockTransport(rec), resolver=_private, sleep=_no_sleep
    ) as sink:
        await sink.send("calls", _records()["calls"])
    assert len(rec.requests) == 1


# ──────────────────────────────────────────────────────────────────────
# Webhook against a recorded receiver
# ──────────────────────────────────────────────────────────────────────


async def test_webhook_signed_delivery():
    rec = Recorder([202])
    conn = sx.WebhookConnection(
        url="https://hooks.example.com/in/ps",
        signing_secret=HOOK_SECRET,
        bearer_token=HOOK_BEARER,
    )
    async with sx.WebhookSink(
        conn,
        TENANT,
        transport=httpx.MockTransport(rec),
        resolver=_public,
        sleep=_no_sleep,
        wall_clock=lambda: 1_762_430_400.0,
    ) as sink:
        await sink.send("security", _records()["security"])
    (req,) = rec.requests
    assert req.url.path == "/in/ps" and req.headers["host"] == "hooks.example.com"
    assert req.headers["authorization"] == f"Bearer {HOOK_BEARER}"
    body = json.loads(req.content)
    assert body["kind"] == "security" and body["tenant_id"] == str(TENANT)
    assert body["schema_version"] == "1.0" and body["source"] == "prompt-shields"
    assert body["records"][0]["event.name"] == "injection.detected"
    assert verify_signature(
        HOOK_SECRET, req.content, req.headers["prompt-shields-signature"], now=1_762_430_400
    )


async def test_webhook_unsigned_and_refused():
    rec = Recorder([500, 500, 500, 500, 500])
    conn = sx.WebhookConnection(url="https://hooks.example.com")
    async with sx.WebhookSink(
        conn, TENANT, transport=httpx.MockTransport(rec), resolver=_public, sleep=_no_sleep
    ) as sink:
        with pytest.raises(ConnectorError, match="still failing"):
            await sink.send("calls", _records()["calls"])
    assert rec.requests[0].url.path == "/"
    assert "prompt-shields-signature" not in rec.requests[0].headers
    assert "authorization" not in rec.requests[0].headers


# ──────────────────────────────────────────────────────────────────────
# Syslog against a recorded TLS receiver
# ──────────────────────────────────────────────────────────────────────

SYSLOG_HOST = "siem.example.com"


def _self_signed(tmp_path) -> tuple[str, str]:
    x509 = pytest.importorskip("cryptography.x509")
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, SYSLOG_HOST)])
    now = dt.datetime.now(dt.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=1))
        .not_valid_after(now + dt.timedelta(hours=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(SYSLOG_HOST)]), False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), True)
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = tmp_path / "cert.pem", tmp_path / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return str(cert_path), str(key_path)


INTERNAL_IP = "10.20.30.40"


async def _to_loopback(address, port, context, server_hostname):
    """The checked internal address, connected to the test receiver on
    127.0.0.1 (the sink itself never admits loopback)."""
    assert address == INTERNAL_IP
    return await sx.default_opener("127.0.0.1", port, context, server_hostname)


class SyslogReceiver:
    """A TLS syslog receiver (RFC 5425 octet counting) on 127.0.0.1."""

    def __init__(self):
        self.messages: list[bytes] = []
        self.sni: list[str | None] = []
        self.done = asyncio.Event()

    def sni_callback(self, _sock, server_name, _ctx):
        self.sni.append(server_name)

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            while True:
                length = await reader.readuntil(b" ")
                self.messages.append(await reader.readexactly(int(length[:-1])))
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()
            self.done.set()


async def test_syslog_over_tls(tmp_path):
    cert, key = _self_signed(tmp_path)
    server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_ctx.load_cert_chain(cert, key)
    receiver = SyslogReceiver()
    server_ctx.sni_callback = receiver.sni_callback
    server = await asyncio.start_server(receiver.handle, "127.0.0.1", 0, ssl=server_ctx)
    port = server.sockets[0].getsockname()[1]

    async def internal(host: str, p: int) -> list[str]:
        assert host == SYSLOG_HOST
        return [INTERNAL_IP]

    conn = sx.SyslogConnection(
        host=SYSLOG_HOST, port=port, ca_file=cert, allow_private_network=True
    )
    async with server:
        async with sx.SyslogSink(
            conn, TENANT, resolver=internal, opener=_to_loopback, sleep=_no_sleep
        ) as sink:
            for kind, records in _records().items():
                assert await sink.send(kind, records) == 1
        await asyncio.wait_for(receiver.done.wait(), 5)
    assert receiver.sni == [SYSLOG_HOST]
    call, sec = receiver.messages
    assert call.startswith(b"<110>1 2026-11-01T09:00:00.000000Z ")  # 13*8 + 6
    assert b" AICall - CEF:0|Prompt Shields|Prompt Shields|1.0|ai.call|" in call
    assert b" SecurityEvent - CEF:0|Prompt Shields|Prompt Shields Gateway|" in sec


async def test_syslog_refuses_private_addresses_and_unverified_certs(tmp_path):
    conn = sx.SyslogConnection(host=SYSLOG_HOST)
    opened = []

    async def opener(*args):
        opened.append(args)
        raise AssertionError("must not connect")

    async with sx.SyslogSink(conn, TENANT, resolver=_private, opener=opener) as sink:
        with pytest.raises(ConnectorError, match="non-public"):
            await sink.send("calls", _records()["calls"])
    assert opened == []

    # A certificate the default trust store does not know: refused.
    cert, key = _self_signed(tmp_path)
    server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_ctx.load_cert_chain(cert, key)
    server = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0, ssl=server_ctx)
    port = server.sockets[0].getsockname()[1]

    async def internal(host: str, p: int) -> list[str]:
        return [INTERNAL_IP]

    strict = sx.SyslogConnection(host=SYSLOG_HOST, port=port, allow_private_network=True)
    async with (
        server,
        sx.SyslogSink(
            strict, TENANT, resolver=internal, opener=_to_loopback, sleep=_no_sleep
        ) as sink,
    ):
        with pytest.raises(ConnectorError, match="connection failed"):
            await sink.send("calls", _records()["calls"])


def test_ingest_model_has_no_content_columns():
    cols = set(GatewaySecurityEvent.__table__.columns.keys())
    assert not cols & {"prompt", "response", "client_ip", "email", "prompt_hash"}
    assert "user_id_raw" not in cols
    assert AIUsageEvent.__tablename__ != GatewaySecurityEvent.__tablename__


LOCAL_ADDRESSES = [
    "127.0.0.1",
    "::1",
    "169.254.169.254",
    "fe80::1",
    "fd00:ec2::254",
    "100.100.100.200",
    "::ffff:169.254.169.254",
    "64:ff9b::a9fe:a9fe",
]


@pytest.mark.parametrize("address", LOCAL_ADDRESSES)
async def test_private_network_opt_in_still_refuses_local_and_metadata(address):
    """allow_private_network admits an internal SIEM, never this host or a
    cloud metadata service (HTTP connectors and syslog)."""

    async def resolver(host: str, p: int) -> list[str]:
        return [address]

    rec = Recorder()
    conn = sx.SplunkHecConnection(
        url="https://splunk.example.com", token=HEC_TOKEN, allow_private_network=True
    )
    async with sx.SplunkHecSink(
        conn, TENANT, transport=httpx.MockTransport(rec), resolver=resolver, sleep=_no_sleep
    ) as sink:
        with pytest.raises(ConnectorError, match="non-public"):
            await sink.send("calls", _records()["calls"])
    hook = sx.WebhookConnection(url="https://hooks.example.com/ps", allow_private_network=True)
    async with sx.WebhookSink(
        hook, TENANT, transport=httpx.MockTransport(rec), resolver=resolver, sleep=_no_sleep
    ) as sink:
        with pytest.raises(ConnectorError, match="non-public"):
            await sink.send("calls", _records()["calls"])
    assert rec.requests == []

    async def opener(*args):
        raise AssertionError("must not connect")

    syslog = sx.SyslogConnection(host=SYSLOG_HOST, allow_private_network=True)
    async with sx.SyslogSink(syslog, TENANT, resolver=resolver, opener=opener) as sink:
        with pytest.raises(ConnectorError, match="non-public"):
            await sink.send("calls", _records()["calls"])
