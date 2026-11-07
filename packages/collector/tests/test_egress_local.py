"""Private-network opt-ins never reach loopback, link-local or cloud
metadata addresses (#370). One section per outbound path that honours a
"private network allowed" opt-in. No database needed."""

from __future__ import annotations

import ipaddress
import uuid

import httpx
import pytest
from collector import connectors as cx
from collector import egress, webhooks
from collector.config import settings

# Loopback, link-local, cloud metadata, and IPv6 forms embedding them.
LOCAL = [
    "127.0.0.1",
    "127.1.2.3",
    "::1",
    "169.254.169.254",  # cloud metadata (AWS, GCP, Azure, ...)
    "169.254.0.1",
    "fe80::1",
    "fd00:ec2::254",  # AWS IPv6 metadata, inside private fd00::/8
    "100.100.100.200",  # Alibaba Cloud metadata, inside CGNAT
    "::ffff:127.0.0.1",  # IPv4-mapped
    "::ffff:169.254.169.254",
    "::ffff:100.100.100.200",
    "64:ff9b::a9fe:a9fe",  # NAT64 of 169.254.169.254
    "64:ff9b::7f00:1",  # NAT64 of 127.0.0.1
    "2002:a9fe:a9fe::1",  # 6to4 of 169.254.169.254
    "2001:0:4136:e378:8000:63bf:80ff:fffe",  # Teredo, client 127.0.0.1
    "::a9fe:a9fe",  # IPv4-compatible
    "64:ff9b:1::a9fe:a9fe",  # local-use NAT64 (RFC 8215) of 169.254.169.254
    "64:ff9b:1:ffff::7f00:1",  # local-use NAT64 of 127.0.0.1
    "::ffff:0:a9fe:a9fe",  # IPv4-translated (SIIT) of 169.254.169.254
]
# What the private opt-ins are for: internal networks.
PRIVATE = ["10.0.0.7", "172.16.0.1", "192.168.1.10", "fd12:3456::1", "100.64.0.1"]
# Never allowed, opt-in or not.
NEVER = ["0.0.0.0", "::", "224.0.0.1", "ff02::1"]
LOOPBACK = ["127.0.0.1", "127.1.2.3", "::1", "::ffff:127.0.0.1"]


def _ip(addr: str):
    return ipaddress.ip_address(addr)


def _resolver(*addresses):
    async def resolve(host, port):
        return list(addresses)

    return resolve


# ── egress primitive ──────────────────────────────────────────────────


@pytest.mark.parametrize("addr", LOCAL + NEVER)
def test_egress_allow_private_without_local_refuses(addr):
    assert not egress.ip_is_public(_ip(addr), allow_private=True, allow_local=False)
    with pytest.raises(egress.AddressNotAllowed):
        egress.check_addresses(
            ["10.0.0.7", addr], allow_private=True, allow_local=False
        )


@pytest.mark.parametrize("addr", PRIVATE)
def test_egress_allow_private_without_local_admits_internal(addr):
    assert egress.ip_is_public(_ip(addr), allow_private=True, allow_local=False)


# ── Partner API source (LeanIX and ServiceNow `partner_api`) ──────────

TENANT = uuid.uuid4()


def _partner(resolver, transport, *, allow_private=True):
    conn = cx.PartnerApiConnection(
        base_url="https://collector.internal",
        api_key="ps_partner_x",
        allow_private_network=allow_private,
    )
    return cx.PartnerApiClient(
        conn, tenant_id=TENANT, transport=transport, resolver=resolver
    )


def _ok(seen):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"meta": {"tenant_id": str(TENANT)}})

    return httpx.MockTransport(handler)


@pytest.mark.parametrize("addr", LOCAL + NEVER)
async def test_partner_api_private_opt_in_refuses_local(addr):
    seen: list = []
    # A DNS name resolving to the address, alone or next to an internal one.
    for resolver in (_resolver(addr), _resolver("10.0.0.7", addr)):
        async with _partner(resolver, _ok(seen)) as client:
            with pytest.raises(cx.ConnectorError, match="non-public"):
                await client._get("/partner/v1/export", "export")
    assert seen == []


@pytest.mark.parametrize("addr", PRIVATE)
async def test_partner_api_private_opt_in_admits_internal(addr):
    seen: list = []
    async with _partner(_resolver(addr), _ok(seen)) as client:
        await client._get("/partner/v1/export", "export")
    assert seen[0].url.host == addr
    assert seen[0].headers["host"] == "collector.internal"


async def test_partner_api_private_opt_in_does_not_follow_redirect_to_metadata():
    seen: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            302, headers={"location": "http://169.254.169.254/latest/meta-data/"}
        )

    async with _partner(_resolver("10.0.0.7"), httpx.MockTransport(handler)) as c:
        with pytest.raises(cx.ConnectorError, match="HTTP 302"):
            await c._get("/partner/v1/export", "export")
    assert len(seen) == 1


def test_leanix_and_servicenow_read_through_the_partner_api_client():
    """Both connectors' `partner_api.allow_private_network` goes through
    `PartnerApiClient`, which is what the tests above cover."""
    from collector import leanix, servicenow

    for module in (leanix, servicenow):
        assert module.PartnerApiClient is cx.PartnerApiClient
        assert module.PartnerApiConnection is cx.PartnerApiConnection


# ── partner webhooks (PS_WEBHOOK_ALLOW_PRIVATE_DESTINATIONS) ──────────


@pytest.fixture
def private_webhooks(monkeypatch):
    monkeypatch.setattr(settings, "webhook_allow_http", True)
    monkeypatch.setattr(settings, "webhook_allow_private_destinations", True)
    monkeypatch.setattr(settings, "webhook_allow_loopback_destinations", False)


@pytest.mark.parametrize("addr", LOCAL + NEVER)
def test_webhook_private_opt_in_refuses_local(private_webhooks, addr):
    assert not webhooks.ip_allowed(_ip(addr))


@pytest.mark.parametrize("addr", PRIVATE)
def test_webhook_private_opt_in_admits_internal(private_webhooks, addr):
    assert webhooks.ip_allowed(_ip(addr))


@pytest.mark.parametrize("addr", LOCAL)
async def test_webhook_private_opt_in_refuses_dns_name_resolving_local(
    private_webhooks, monkeypatch, addr
):
    for resolved in ((addr,), ("10.0.0.7", addr)):
        monkeypatch.setattr(webhooks, "_resolve_host", _resolver(*resolved))
        with pytest.raises(webhooks.DestinationNotAllowed):
            await webhooks.resolve_destination("http://hooks.internal/x")


async def test_webhook_private_opt_in_does_not_follow_redirect_to_metadata(
    private_webhooks, monkeypatch
):
    monkeypatch.setattr(webhooks, "_resolve_host", _resolver("10.0.0.7"))
    seen: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(302, headers={"Location": "http://169.254.169.254/"})

    status = await webhooks.post_signed(
        "http://hooks.internal/in", b"{}", {}, transport=httpx.MockTransport(handler)
    )
    assert status == 302 and len(seen) == 1
    assert seen[0].url.host == "10.0.0.7"


@pytest.mark.parametrize("private", [False, True])
@pytest.mark.parametrize("addr", LOOPBACK)
def test_webhook_loopback_opt_in_admits_loopback_only(monkeypatch, private, addr):
    monkeypatch.setattr(settings, "webhook_allow_private_destinations", private)
    monkeypatch.setattr(settings, "webhook_allow_loopback_destinations", False)
    assert not webhooks.ip_allowed(_ip(addr))
    monkeypatch.setattr(settings, "webhook_allow_loopback_destinations", True)
    assert webhooks.ip_allowed(_ip(addr))


@pytest.mark.parametrize("private", [False, True])
@pytest.mark.parametrize("addr", sorted(set(LOCAL) - set(LOOPBACK)) + NEVER)
def test_webhook_loopback_opt_in_still_refuses_link_local_and_metadata(
    monkeypatch, private, addr
):
    monkeypatch.setattr(settings, "webhook_allow_private_destinations", private)
    monkeypatch.setattr(settings, "webhook_allow_loopback_destinations", True)
    assert not webhooks.ip_allowed(_ip(addr))


def test_webhook_loopback_opt_in_is_off_by_default():
    from collector.config import Settings

    assert Settings.model_fields["webhook_allow_loopback_destinations"].default is False


@pytest.mark.parametrize("addr", ["::ffff:0:a9fe:a9fe", "::ffff:0:7f00:1", "::ffff:0:a00:7"])
def test_egress_default_refuses_ipv4_translated_non_public(addr):
    assert not egress.ip_is_public(_ip(addr))


def test_egress_default_admits_ipv4_translated_public():
    assert egress.ip_is_public(_ip("::ffff:0:808:808"))
