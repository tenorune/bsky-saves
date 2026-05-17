"""Network safety helpers for outbound user-URL fetches.

Centralises SSRF defence: every user-supplied URL flows through
``assert_public_http_url`` before httpx ever sees it. ``safe_http_get`` wraps
``httpx.get`` to walk redirects manually with the guard re-applied per hop.

Also provides ``bsky_ssl_context()`` — a workaround for AWS WAF on
``bsky.social`` blocking the JA3 produced by Python stdlib ``ssl``'s default
cipher list under OpenSSL 3.0.x. See tenorune/bsky-saves#19. The cipher list
is from tenorune/bsky-saves-install v0.1.2 (@ebd55c0), verified against the
WAF (post-patch JA3 ``48b8472f8c6c7e3e91b544381d8b4d62`` is accepted).
"""
from __future__ import annotations

import ipaddress
import socket
import ssl
from collections.abc import Callable
from urllib.parse import urljoin, urlparse

import httpx


# AWS WAF on bsky.social blocks the JA3 produced by Python stdlib ssl's
# default cipher list under OpenSSL 3.0.x. Shipping this non-default order
# produces a different JA3 the WAF accepts. Exact ciphers don't matter much
# beyond "not OpenSSL 3.0.x's default"; this set is verified against the
# current WAF rules by the bsky-saves-install team. If a future WAF update
# blocks this JA3 too, swap the cipher list and reverify.
_BSKY_CIPHERS = (
    "TLS_AES_128_GCM_SHA256:TLS_CHACHA20_POLY1305_SHA256:TLS_AES_256_GCM_SHA384:"
    "ECDHE-RSA-CHACHA20-POLY1305:ECDHE-RSA-AES128-GCM-SHA256:ECDHE-RSA-AES256-GCM-SHA384:"
    "ECDHE-ECDSA-CHACHA20-POLY1305:ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-ECDSA-AES256-GCM-SHA384"
)


def bsky_ssl_context() -> ssl.SSLContext:
    """Return a default SSLContext with the bsky-saves cipher list set.

    Workaround for the AWS WAF rule on bsky.social that blocks the JA3
    produced by Python stdlib ssl's default cipher list under OpenSSL 3.0.x
    (tenorune/bsky-saves#19). Reordering ciphers produces a different JA3
    that the WAF accepts.

    Falls through to the unmodified default context if the runtime OpenSSL
    doesn't recognise one of the named ciphers — strictly better failure
    mode than crashing on startup, and the user just sees the original
    WAF 403 instead of an unrecoverable error.
    """
    ctx = ssl.create_default_context()
    try:
        ctx.set_ciphers(_BSKY_CIPHERS)
    except ssl.SSLError:
        # Hypothetical exotic OpenSSL build missing one of the named ciphers.
        # User on this build may still hit the WAF 403, but they don't suffer
        # an unrecoverable startup crash from this workaround itself.
        pass
    return ctx


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


class TooManyRedirectsError(Exception):
    """safe_http_get exceeded its redirect budget."""


def safe_http_get(
    url: str,
    *,
    allow_http: bool = False,
    max_redirects: int = 5,
    hop_check: Callable[[str], None] | None = None,
    **httpx_kwargs,
) -> httpx.Response:
    """Like httpx.get, but walks redirects manually with assert_public_http_url
    re-applied per hop. ``hop_check`` runs before the SSRF check on each hop
    (used to enforce per-endpoint allowlists in addition to the SSRF guard).
    Disables httpx's own redirect-following.

    Raises:
        UnsafeURLError: any hop fails ``hop_check`` or the SSRF guard.
        TooManyRedirectsError: more than ``max_redirects`` 3xx responses chained.
    """
    httpx_kwargs.pop("follow_redirects", None)  # we follow manually
    httpx_kwargs.setdefault("verify", bsky_ssl_context())
    current = url
    for _ in range(max_redirects + 1):
        if hop_check is not None:
            hop_check(current)
        assert_public_http_url(current, allow_http=allow_http)
        r = httpx.get(current, follow_redirects=False, **httpx_kwargs)
        if 300 <= r.status_code < 400 and "location" in (h.lower() for h in r.headers):
            location = r.headers.get("Location") or r.headers.get("location")
            if not location:
                return r
            current = urljoin(current, location)
            continue
        return r
    raise TooManyRedirectsError(f"exceeded {max_redirects} redirects starting from {url!r}")
