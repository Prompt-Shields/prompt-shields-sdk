"""Outbound HTTP SSRF guard shared by webhooks and EA-tool connectors.

  - `ip_is_public`: is an address publicly routable unicast? Catches the
    IPv6 forms that embed or stand for an IPv4 address (IPv4-mapped,
    6to4, Teredo, NAT64 64:ff9b::/96, IPv4-compatible ::/96) and
    site-local fec0::/10, which `is_global` does not catch on every
    Python version. Private, loopback, link-local (cloud metadata
    169.254.169.254), CGNAT 100.64.0.0/10 and reserved ranges are not
    global.
  - `resolve_checked`: resolve a host and require every address to pass.
  - `pin_request`: rewrite a URL to connect to the checked address while
    keeping the original ``Host`` header and TLS SNI / certificate
    hostname, so a second DNS lookup (DNS rebinding) cannot redirect the
    connection.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket

import httpx

# NAT64 well-known prefix (64:ff9b::/96 reaches the embedded IPv4 address
# through a NAT64 gateway, e.g. 64:ff9b::a9fe:a9fe -> 169.254.169.254),
# IPv4-compatible ::/96 and site-local fec0::/10.
_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_IPV4_COMPATIBLE = ipaddress.ip_network("::/96")
_SITE_LOCAL = ipaddress.ip_network("fec0::/10")
# Cloud instance metadata services outside link-local: AWS IPv6 IMDS (in
# the private fd00::/8 range) and Alibaba Cloud (in CGNAT 100.64.0.0/10).
_METADATA = frozenset(
    {ipaddress.ip_address("fd00:ec2::254"), ipaddress.ip_address("100.100.100.200")}
)


class AddressNotAllowed(ValueError):
    """The host did not resolve, or resolved to a refused address."""


def ip_is_public(
    ip: ipaddress.IPv4Address | ipaddress.IPv6Address,
    *,
    allow_private: bool = False,
    allow_local: bool = True,
) -> bool:
    """True for publicly routable unicast addresses only. With
    ``allow_private`` only unspecified and multicast addresses (and IPv6
    forms embedding them) are refused; with ``allow_local=False`` as well,
    loopback, link-local (cloud metadata 169.254.169.254) and the other
    cloud metadata addresses stay refused, so the opt-in admits an internal
    network but not this host or its metadata service."""
    if ip.version == 6:
        if ip in _IPV4_COMPATIBLE or ip in _SITE_LOCAL:
            return False
        embedded = ip.ipv4_mapped or ip.sixtofour or (ip.teredo[1] if ip.teredo else None)
        if embedded is None and ip in _NAT64:
            embedded = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
        if embedded is not None and not ip_is_public(
            embedded, allow_private=allow_private, allow_local=allow_local
        ):
            return False
    if ip.is_unspecified or ip.is_multicast:
        return False
    if not allow_local and (ip.is_loopback or ip.is_link_local or ip in _METADATA):
        return False
    if allow_private:
        return True
    return ip.is_global


async def resolve_host(host: str, port: int) -> list[str]:
    """All addresses ``host`` resolves to, deduplicated, in resolver order."""
    infos = await asyncio.get_running_loop().getaddrinfo(
        host, port, type=socket.SOCK_STREAM
    )
    return list(dict.fromkeys(info[4][0] for info in infos))


def check_addresses(
    addresses: list[str], *, allow_private: bool = False, allow_local: bool = True
) -> str:
    """The address to connect to. Every address must be allowed, so a host
    with one public and one private record is refused."""
    if not addresses:
        raise AddressNotAllowed("no address")
    for addr in addresses:
        try:
            ip = ipaddress.ip_address(addr.split("%", 1)[0])
        except ValueError as exc:
            raise AddressNotAllowed("not an IP address") from exc
        if not ip_is_public(ip, allow_private=allow_private, allow_local=allow_local):
            raise AddressNotAllowed("non-public address")
    return addresses[0].split("%", 1)[0]


def pin_request(url: httpx.URL, address: str) -> tuple[httpx.URL, dict, dict]:
    """``(url to connect to, headers, extensions)`` that send a request for
    ``url`` to ``address``: the URL carries the IP, the ``Host`` header the
    original authority, and ``sni_hostname`` makes httpcore use the original
    host name for TLS SNI and certificate verification."""
    return (
        url.copy_with(host=address),
        {"Host": url.netloc.decode("ascii")},
        {"sni_hostname": url.host},
    )
