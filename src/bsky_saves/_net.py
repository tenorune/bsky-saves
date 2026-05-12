"""Network safety helpers for outbound user-URL fetches.

Centralises SSRF defence: every user-supplied URL flows through
``assert_public_http_url`` before httpx ever sees it. ``safe_http_get`` wraps
``httpx.get`` to walk redirects manually with the guard re-applied per hop.
"""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse


class UnsafeURLError(ValueError):
    """Raised when a URL targets a non-public IP or is otherwise unsafe."""


# RFC 6598 CGNAT — Python's IPv4Address.is_private doesn't include this in
# 3.11 and earlier. Explicit check below.
_CGNAT = ipaddress.ip_network("100.64.0.0/10")


def _is_unsafe_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Return True if the IP is in any range we refuse to fetch from."""
    # Unwrap IPv4-mapped IPv6 first so the checks below see the IPv4 form.
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if ip.is_private:
        return True
    if ip.is_loopback:
        return True
    if ip.is_link_local:
        return True
    if ip.is_multicast:
        return True
    if ip.is_reserved:
        return True
    if ip.is_unspecified:
        return True
    if isinstance(ip, ipaddress.IPv4Address) and ip in _CGNAT:
        return True
    return False


def assert_public_http_url(url: str, *, allow_http: bool = False) -> None:
    """Raise UnsafeURLError if url is malformed, uses a disallowed scheme,
    or resolves to a private/loopback/link-local/multicast/reserved IP.

    Args:
        url: The URL to validate.
        allow_http: If True, permit ``http://`` URLs (used by /extract-article
            which targets the open web). If False, require ``https://``.
    """
    if not url or not isinstance(url, str):
        raise UnsafeURLError("empty or non-string URL")

    try:
        parsed = urlparse(url)
    except ValueError as e:
        raise UnsafeURLError(f"unparseable URL: {e}")

    scheme = (parsed.scheme or "").lower()
    allowed = ("https",) if not allow_http else ("http", "https")
    if scheme not in allowed:
        raise UnsafeURLError(f"scheme not allowed: {scheme!r}")

    host = (parsed.hostname or "").lower().rstrip(".")
    if not host:
        raise UnsafeURLError("URL has no hostname")

    if host == "localhost":
        raise UnsafeURLError("hostname 'localhost' not allowed")

    # IP literal? Check directly. Otherwise: DNS-resolve and check every address.
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None

    if ip is not None:
        if _is_unsafe_ip(ip):
            raise UnsafeURLError(f"unsafe IP literal: {host}")
        return

    # DNS lookup. getaddrinfo returns a list of 5-tuples; index 4 is the
    # sockaddr (ip-string, port) for IPv4 or (ip-string, port, flow, scope) for IPv6.
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as e:
        raise UnsafeURLError(f"DNS resolution failed: {e}")

    for info in infos:
        sockaddr = info[4]
        try:
            resolved = ipaddress.ip_address(sockaddr[0])
        except ValueError:
            continue
        if _is_unsafe_ip(resolved):
            raise UnsafeURLError(
                f"hostname {host!r} resolves to unsafe address {resolved}"
            )
