"""URLGuard (SPEC 4.11): only http/https to public hosts, checked per hop.

Rejects loopback/private/link-local/reserved/benchmark/TEST-NET/multicast
targets, IPv4-mapped IPv6 equivalents, localhost-style names, and hosts that
resolve (via injectable resolver) to any blocked address.
"""
from __future__ import annotations

import ipaddress
import socket
from typing import Callable, Optional
from urllib.parse import urlsplit

from .errors import FatalError

_Resolver = Callable[[str], "list[str]"]

_EXTRA_V4_BLOCKED = [
    ipaddress.ip_network("100.64.0.0/10"),   # CGN shared address space
    ipaddress.ip_network("198.18.0.0/15"),   # benchmarking
    ipaddress.ip_network("0.0.0.0/8"),       # "this" network
]

_BLOCKED_HOSTNAMES = {"localhost", "local"}


def _default_resolver(host: str) -> list[str]:
    infos = socket.getaddrinfo(host, None)
    return list({info[4][0] for info in infos})


def _is_blocked_ip(ip: ipaddress._BaseAddress) -> bool:
    if isinstance(ip, ipaddress.IPv6Address):
        mapped = ip.ipv4_mapped
        if mapped is not None:
            ip = mapped
    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast \
            or ip.is_reserved or ip.is_unspecified:
        return True
    if isinstance(ip, ipaddress.IPv4Address):
        return any(ip in net for net in _EXTRA_V4_BLOCKED)
    return False


class URLGuard:
    def __init__(self, resolver: Optional[_Resolver] = None):
        self._resolver = resolver or _default_resolver

    def validate(self, url: str) -> None:
        try:
            parts = urlsplit(url)
        except ValueError as e:
            raise FatalError(f"invalid URL {url!r}: {e}") from e
        if parts.scheme not in ("http", "https"):
            raise FatalError(f"scheme not allowed: {parts.scheme!r} (only http/https)")
        host = parts.hostname
        if not host:
            raise FatalError(f"URL has no host: {url!r}")
        host = host.lower().rstrip(".")
        if host in _BLOCKED_HOSTNAMES or host.endswith((".local", ".localhost", ".internal")):
            raise FatalError(f"local hostname not allowed: {host}")

        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            literal = None
        if literal is not None:
            if _is_blocked_ip(literal):
                raise FatalError(f"non-public address not allowed: {host}")
            return

        # hostname: resolve and require every address to be public
        try:
            addrs = self._resolver(host)
        except Exception as e:
            raise FatalError(f"cannot resolve host {host!r}: {e}") from e
        if not addrs:
            raise FatalError(f"host {host!r} resolved to no addresses")
        for addr in addrs:
            try:
                ip = ipaddress.ip_address(addr.split("%")[0])
            except ValueError:
                raise FatalError(f"unparseable address {addr!r} for host {host!r}")
            if _is_blocked_ip(ip):
                raise FatalError(f"host {host!r} resolves to non-public {addr}")
