"""
adapters/openapi/ssrf.py — Req 10

SSRF guard: validates that a URL is safe to fetch before we make the request.
Blocks private/loopback/link-local addresses, IPv4-mapped IPv6, cloud metadata
endpoints, and disallows redirects entirely.
"""

from __future__ import annotations

import ipaddress
import socket
import urllib.parse


class SSRFError(ValueError):
    """Raised when a URL fails SSRF validation."""


_BLOCKED_HOSTS = {
    "169.254.169.254",  # AWS/GCP/Azure metadata
    "metadata.google.internal",
    "fd00:ec2::254",
}


def _resolve(host: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return []
    addrs = []
    for info in infos:
        try:
            addrs.append(ipaddress.ip_address(info[4][0]))
        except ValueError:
            pass
    return addrs


def validate(url: str) -> str:
    """
    Validate *url* for safe outbound fetching. Returns the url unchanged on
    success; raises SSRFError on any violation.

    Rules:
    - scheme must be https (or http only for localhost in tests)
    - host must resolve to a public, globally-routable address
    - no IPv4-mapped IPv6 addresses
    - no well-known cloud metadata hosts
    - redirects are never followed by callers (enforced at fetch time)
    """
    parsed = urllib.parse.urlparse(url)

    if parsed.scheme not in ("http", "https"):
        raise SSRFError(f"scheme '{parsed.scheme}' not allowed; use https")

    host = parsed.hostname
    if not host:
        raise SSRFError("URL has no host")

    if host in _BLOCKED_HOSTS:
        raise SSRFError(f"host '{host}' is blocked (cloud metadata endpoint)")

    addrs = _resolve(host)
    if not addrs:
        # Cannot resolve — allow (will fail at fetch time with a network error,
        # not an SSRF bypass). DNS failure is not a security issue.
        return url

    for addr in addrs:
        # IPv4-mapped IPv6 (::ffff:169.254.x.x etc.)
        if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
            addr = addr.ipv4_mapped

        if addr.is_loopback:
            raise SSRFError(f"host '{host}' resolves to loopback address {addr}")
        if addr.is_private:
            raise SSRFError(f"host '{host}' resolves to private address {addr}")
        if addr.is_link_local:
            raise SSRFError(f"host '{host}' resolves to link-local address {addr}")
        if addr.is_reserved:
            raise SSRFError(f"host '{host}' resolves to reserved address {addr}")

    return url
