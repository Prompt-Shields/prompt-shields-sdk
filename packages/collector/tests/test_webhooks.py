"""Unit tests for partner change webhooks: signing, secret encryption and
destination (SSRF) checks. No database needed."""

from __future__ import annotations

import ipaddress
import time
import uuid

import httpx
import pytest
from collector import webhooks
from collector.config import settings

KEY = "k" * 40


@pytest.fixture(autouse=True)
def _defaults(monkeypatch):
    monkeypatch.setattr(settings, "webhook_secret_key", KEY)
    monkeypatch.setattr(settings, "webhook_allow_http", False)
    monkeypatch.setattr(settings, "webhook_allow_private_destinations", False)


def _resolver(*addresses):
    async def resolve(host, port):
        return list(addresses)

    return resolve


# ── signatures ────────────────────────────────────────────────────────


def test_signature_round_trip():
    body = b'{"id":"e1"}'
    ts = int(time.time())
    header = webhooks.sign_payload("whsec_x", body, ts)
    assert header.startswith(f"t={ts},v1=")
    assert webhooks.verify_signature("whsec_x", body, header)


@pytest.mark.parametrize(
    "secret, body, header_ts_offset",
    [
        ("whsec_other", b'{"id":"e1"}', 0),  # wrong secret
        ("whsec_x", b'{"id":"e2"}', 0),  # tampered body
        ("whsec_x", b'{"id":"e1"}', -301),  # replay: too old
    ],
)
def test_signature_rejects(secret, body, header_ts_offset):
    ts = int(time.time()) + header_ts_offset
    header = webhooks.sign_payload("whsec_x", b'{"id":"e1"}', ts)
    assert not webhooks.verify_signature(secret, body, header)


@pytest.mark.parametrize("header", ["", "garbage", "t=abc,v1=00", "v1=00"])
def test_signature_rejects_malformed_header(header):
    assert not webhooks.verify_signature("whsec_x", b"{}", header)


# ── secret encryption ─────────────────────────────────────────────────


def test_secret_encryption_round_trip_and_binding():
    sid = uuid.uuid4()
    secret = webhooks.generate_signing_secret()
    token = webhooks.encrypt_secret(secret, sid)
    assert secret not in token and token.startswith("v1.")
    assert webhooks.decrypt_secret(token, sid) == secret
    # Bound to the subscription id: a ciphertext copied to another row fails.
    with pytest.raises(ValueError):
        webhooks.decrypt_secret(token, uuid.uuid4())


def test_secret_unreadable_after_key_change(monkeypatch):
    sid = uuid.uuid4()
    token = webhooks.encrypt_secret("whsec_x", sid)
    monkeypatch.setattr(settings, "webhook_secret_key", "z" * 40)
    with pytest.raises(ValueError):
        webhooks.decrypt_secret(token, sid)


@pytest.mark.parametrize("key", ["", "short"])
def test_missing_or_short_key_is_not_configured(monkeypatch, key):
    monkeypatch.setattr(settings, "webhook_secret_key", key)
    with pytest.raises(webhooks.WebhooksNotConfigured):
        webhooks.encrypt_secret("whsec_x", uuid.uuid4())


# ── destination checks ────────────────────────────────────────────────


@pytest.mark.parametrize(
    "url",
    [
        "http://hooks.example.com/x",
        "ftp://hooks.example.com/x",
        "https://user:pw@hooks.example.com/x",
        "https://hooks.example.com/x#frag",
        "https:///nohost",
        "https://hooks.example.com:99999/x",
        "https://hooks.example.com/" + "a" * 3000,
    ],
)
def test_check_url_rejects(url):
    with pytest.raises(webhooks.DestinationNotAllowed):
        webhooks.check_url(url)


def test_http_allowed_only_when_configured(monkeypatch):
    monkeypatch.setattr(settings, "webhook_allow_http", True)
    assert webhooks.check_url("http://hooks.example.com/x").hostname == "hooks.example.com"


@pytest.mark.parametrize(
    "addr",
    [
        "127.0.0.1",
        "10.1.2.3",
        "172.16.0.1",
        "192.168.1.1",
        "169.254.169.254",  # cloud metadata
        "100.64.0.1",  # carrier-grade NAT
        "0.0.0.0",
        "224.0.0.1",
        "::1",
        "fe80::1",
        "fc00::1",
        "::ffff:127.0.0.1",
        "::ffff:169.254.169.254",
        "2002:a00:1::",  # 6to4 wrapping 10.0.0.1
    ],
)
def test_non_public_addresses_blocked(addr):
    assert not webhooks.ip_allowed(ipaddress.ip_address(addr))


@pytest.mark.parametrize("addr", ["93.184.216.34", "2606:4700::1111"])
def test_public_addresses_allowed(addr):
    assert webhooks.ip_allowed(ipaddress.ip_address(addr))


def test_private_allowed_only_when_configured(monkeypatch):
    monkeypatch.setattr(settings, "webhook_allow_private_destinations", True)
    assert webhooks.ip_allowed(ipaddress.ip_address("10.0.0.1"))
    assert not webhooks.ip_allowed(ipaddress.ip_address("0.0.0.0"))


async def test_resolve_refuses_any_private_record(monkeypatch):
    monkeypatch.setattr(webhooks, "_resolve_host", _resolver("93.184.216.34", "10.0.0.5"))
    with pytest.raises(webhooks.DestinationNotAllowed):
        await webhooks.resolve_destination("https://hooks.example.com/x")


async def test_resolve_failure_is_refused_with_same_message(monkeypatch):
    async def fail(host, port):
        raise OSError("nxdomain")

    monkeypatch.setattr(webhooks, "_resolve_host", fail)
    with pytest.raises(webhooks.DestinationNotAllowed, match="public addresses"):
        await webhooks.resolve_destination("https://nope.example.com/x")


async def test_post_signed_pins_checked_address_and_does_not_follow_redirects(monkeypatch):
    monkeypatch.setattr(webhooks, "_resolve_host", _resolver("93.184.216.34"))
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(302, headers={"Location": "http://169.254.169.254/"})

    status = await webhooks.post_signed(
        "https://hooks.example.com:8443/in?x=1",
        b"{}",
        {"X-Test": "1"},
        transport=httpx.MockTransport(handler),
    )
    assert status == 302
    assert len(seen) == 1  # redirect not followed
    req = seen[0]
    assert req.url.host == "93.184.216.34" and req.url.port == 8443
    assert req.url.path == "/in" and req.url.query == b"x=1"
    assert req.headers["Host"] == "hooks.example.com:8443"
    assert req.extensions["sni_hostname"] == "hooks.example.com"


async def test_post_signed_ipv6_target(monkeypatch):
    monkeypatch.setattr(webhooks, "_resolve_host", _resolver("2606:4700::1111"))
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200)

    await webhooks.post_signed(
        "https://hooks.example.com/", b"{}", {}, transport=httpx.MockTransport(handler)
    )
    assert seen[0].url.host == "2606:4700::1111"


def test_retry_schedule_is_bounded():
    assert webhooks.retry_delay(1).total_seconds() == 60
    last = webhooks.RETRY_SCHEDULE_SECONDS[-1]
    assert webhooks.retry_delay(50).total_seconds() == last


def test_payload_excludes_owner_email():
    from db.models import AIAsset

    asset = AIAsset(
        id=uuid.uuid4(), vendor="openai", owner_email="a@b.c", discovery_source=["sdk"]
    )
    payload = webhooks.asset_payload(asset)
    assert "owner_email" not in payload
    assert payload["vendor"] == "openai"
