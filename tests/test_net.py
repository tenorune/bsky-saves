"""Unit tests for bsky_saves._net.assert_public_http_url."""
from __future__ import annotations

from unittest.mock import patch

import pytest

from bsky_saves._net import UnsafeURLError, assert_public_http_url


# Happy path

def test_public_https_url_passes():
    # example.com resolves to public IPs; no exception.
    assert_public_http_url("https://example.com/path")


def test_public_https_url_with_port_passes():
    assert_public_http_url("https://example.com:8443/x")


# IP-literal rejections — IPv4

def test_ipv4_loopback_rejected():
    with pytest.raises(UnsafeURLError):
        assert_public_http_url("https://127.0.0.1/x")


def test_ipv4_private_10_rejected():
    with pytest.raises(UnsafeURLError):
        assert_public_http_url("https://10.0.0.1/x")


def test_ipv4_private_192_168_rejected():
    with pytest.raises(UnsafeURLError):
        assert_public_http_url("https://192.168.0.1/x")


def test_ipv4_private_172_16_rejected():
    with pytest.raises(UnsafeURLError):
        assert_public_http_url("https://172.16.0.1/x")


def test_ipv4_link_local_metadata_rejected():
    """169.254.169.254 is the AWS/GCP/Azure metadata IP."""
    with pytest.raises(UnsafeURLError):
        assert_public_http_url("https://169.254.169.254/latest/meta-data/")


def test_ipv4_cgnat_rejected():
    """100.64.0.0/10 is RFC 6598 CGNAT; not is_private in older Python."""
    with pytest.raises(UnsafeURLError):
        assert_public_http_url("https://100.64.0.1/x")


def test_ipv4_unspecified_rejected():
    with pytest.raises(UnsafeURLError):
        assert_public_http_url("https://0.0.0.0/x")


# IP-literal rejections — IPv6

def test_ipv6_loopback_rejected():
    with pytest.raises(UnsafeURLError):
        assert_public_http_url("https://[::1]/x")


def test_ipv6_link_local_rejected():
    with pytest.raises(UnsafeURLError):
        assert_public_http_url("https://[fe80::1]/x")


def test_ipv6_mapped_ipv4_loopback_rejected():
    """::ffff:127.0.0.1 wraps an IPv4 loopback in IPv6; must reject."""
    with pytest.raises(UnsafeURLError):
        assert_public_http_url("https://[::ffff:127.0.0.1]/x")


# Hostname rejections

def test_localhost_rejected():
    with pytest.raises(UnsafeURLError):
        assert_public_http_url("https://localhost/x")


def test_localhost_trailing_dot_rejected():
    with pytest.raises(UnsafeURLError):
        assert_public_http_url("https://localhost./x")


def test_dns_alias_resolving_to_loopback_rejected():
    """A hostname that getaddrinfo says points at 127.0.0.1 must reject."""
    with patch("bsky_saves._net.socket.getaddrinfo") as gai:
        # getaddrinfo returns list of 5-tuples; we only care about index 4 (sockaddr).
        gai.return_value = [
            (0, 0, 0, "", ("127.0.0.1", 0)),
        ]
        with pytest.raises(UnsafeURLError):
            assert_public_http_url("https://malicious.example/x")


def test_dns_alias_resolving_to_metadata_rejected():
    """metadata.google.internal-style alias rejection."""
    with patch("bsky_saves._net.socket.getaddrinfo") as gai:
        gai.return_value = [
            (0, 0, 0, "", ("169.254.169.254", 0)),
        ]
        with pytest.raises(UnsafeURLError):
            assert_public_http_url("https://metadata.google.internal/x")


# Scheme + format rejections

def test_http_rejected_when_allow_http_false():
    with pytest.raises(UnsafeURLError):
        assert_public_http_url("http://example.com/x", allow_http=False)


def test_http_allowed_when_allow_http_true():
    assert_public_http_url("http://example.com/x", allow_http=True)


def test_ftp_always_rejected():
    with pytest.raises(UnsafeURLError):
        assert_public_http_url("ftp://example.com/x", allow_http=True)


def test_javascript_scheme_rejected():
    with pytest.raises(UnsafeURLError):
        assert_public_http_url("javascript:alert(1)", allow_http=True)


def test_empty_url_rejected():
    with pytest.raises(UnsafeURLError):
        assert_public_http_url("", allow_http=True)


def test_url_without_hostname_rejected():
    with pytest.raises(UnsafeURLError):
        assert_public_http_url("https:///path", allow_http=True)


import httpx
import respx

from bsky_saves._net import TooManyRedirectsError, safe_http_get


@respx.mock
def test_safe_http_get_happy_path_returns_response():
    route = respx.get("https://example.com/x").mock(
        return_value=httpx.Response(200, content=b"ok")
    )
    r = safe_http_get("https://example.com/x")
    assert r.status_code == 200
    assert r.content == b"ok"
    assert route.called


def test_safe_http_get_rejects_unsafe_initial_url():
    with pytest.raises(UnsafeURLError):
        safe_http_get("https://127.0.0.1/x")


@respx.mock
def test_safe_http_get_follows_safe_redirect():
    respx.get("https://example.com/a").mock(
        return_value=httpx.Response(302, headers={"Location": "https://example.com/b"})
    )
    respx.get("https://example.com/b").mock(
        return_value=httpx.Response(200, content=b"final")
    )
    r = safe_http_get("https://example.com/a")
    assert r.status_code == 200
    assert r.content == b"final"


@respx.mock
def test_safe_http_get_rejects_redirect_to_private_ip():
    respx.get("https://example.com/a").mock(
        return_value=httpx.Response(302, headers={"Location": "https://127.0.0.1/b"})
    )
    with pytest.raises(UnsafeURLError):
        safe_http_get("https://example.com/a")


@respx.mock
def test_safe_http_get_hop_check_runs_on_each_target():
    """hop_check raises -> safe_http_get propagates the exception."""
    respx.get("https://example.com/a").mock(
        return_value=httpx.Response(302, headers={"Location": "https://other.com/b"})
    )

    def reject_other(url: str) -> None:
        if "other.com" in url:
            raise UnsafeURLError("not allowed by hop_check")

    with pytest.raises(UnsafeURLError):
        safe_http_get("https://example.com/a", hop_check=reject_other)


@respx.mock
def test_safe_http_get_too_many_redirects():
    respx.get("https://example.com/a").mock(
        return_value=httpx.Response(302, headers={"Location": "https://example.com/b"})
    )
    respx.get("https://example.com/b").mock(
        return_value=httpx.Response(302, headers={"Location": "https://example.com/c"})
    )
    respx.get("https://example.com/c").mock(
        return_value=httpx.Response(302, headers={"Location": "https://example.com/d"})
    )
    with pytest.raises(TooManyRedirectsError):
        safe_http_get("https://example.com/a", max_redirects=2)


# --- v0.6.5: bsky_ssl_context cipher-reorder workaround for the WAF block ---


def test_bsky_ssl_context_returns_sslcontext_with_custom_ciphers():
    """bsky_ssl_context returns a valid SSLContext whose cipher list reflects
    the documented workaround set (not the OpenSSL 3.0.x default that the
    bsky.social WAF blocks)."""
    import ssl
    from bsky_saves._net import bsky_ssl_context, _BSKY_CIPHERS

    ctx = bsky_ssl_context()
    assert isinstance(ctx, ssl.SSLContext)
    # The configured cipher list should be a strict subset of _BSKY_CIPHERS
    # (the runtime OpenSSL may not support every named cipher; that's fine
    # as long as it supports some). Spot-check at least one cipher is present.
    configured_names = {c["name"] for c in ctx.get_ciphers()}
    requested_names = set(_BSKY_CIPHERS.split(":"))
    assert configured_names & requested_names, (
        f"none of the requested ciphers were configured. configured={configured_names!r}"
    )


def test_bsky_ssl_context_handles_unknown_cipher_gracefully(monkeypatch):
    """If the runtime OpenSSL doesn't recognise the cipher list (hypothetical
    exotic build), bsky_ssl_context falls through to the unmodified default
    context rather than crashing. The user on that build would still hit the
    WAF 403, but doesn't get an unrecoverable startup error."""
    import ssl
    from bsky_saves import _net

    monkeypatch.setattr(_net, "_BSKY_CIPHERS", "NOT_A_REAL_CIPHER_THAT_OPENSSL_KNOWS")
    ctx = _net.bsky_ssl_context()
    assert isinstance(ctx, ssl.SSLContext)


def test_bsky_ssl_context_loads_ca_bundle():
    """Regression guard for tenorune/bsky-saves#19 follow-up: bsky_ssl_context
    must load certifi's CA bundle explicitly. Without it, ssl.create_default_context()
    finds nothing in environments where the runtime ssl module has no
    OS-level CA path configured (Briefcase bundles, Alpine containers,
    PyInstaller-frozen apps, slim Docker images) — and httpx, when passed
    verify=<SSLContext>, does NOT auto-load certifi the way it does with
    verify=True. The context must be self-sufficient. Assert the cert store
    contains many certs (certifi ships ~150)."""
    from bsky_saves._net import bsky_ssl_context

    ctx = bsky_ssl_context()
    stats = ctx.cert_store_stats()
    # certifi ships ~150 root CAs; assert we've got at least a handful so a
    # future "oops we dropped certifi" regression fires loudly.
    assert stats["x509"] > 10, f"expected populated CA store, got {stats}"


def test_safe_http_get_defaults_verify_to_bsky_ssl_context(monkeypatch):
    """Regression guard against silently dropping the cipher-context override:
    safe_http_get should default the `verify` kwarg to a bsky_ssl_context()
    instance, not to True. If a future refactor changes this, the WAF
    workaround stops protecting calls that go through _net (articles, images
    via safe_http_get + redirects)."""
    import ssl
    from bsky_saves._net import safe_http_get

    captured = {}

    def fake_get(url, **kwargs):
        captured["kwargs"] = kwargs
        return httpx.Response(200, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", fake_get)
    # Bypass the SSRF assertion (we don't care for this test).
    monkeypatch.setattr("bsky_saves._net.assert_public_http_url", lambda *a, **k: None)

    safe_http_get("https://example.com/")
    assert isinstance(captured["kwargs"].get("verify"), ssl.SSLContext), (
        f"expected SSLContext, got {captured['kwargs'].get('verify')!r}"
    )


def test_auth_create_session_passes_bsky_ssl_context(monkeypatch):
    """Regression guard: auth.create_session must thread an SSLContext into
    httpx.post. Without it, the WAF blocks the sign-in call on OpenSSL-3.0.x
    Pythons (see tenorune/bsky-saves#19)."""
    import ssl
    from bsky_saves import auth

    captured = {}

    def fake_post(url, **kwargs):
        captured["kwargs"] = kwargs
        return httpx.Response(
            200,
            json={"accessJwt": "x", "refreshJwt": "y", "did": "did:plc:z", "handle": "x.example"},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx, "post", fake_post)
    auth.create_session("https://bsky.social", "alice.bsky.social", "xxxx-xxxx-xxxx-xxxx")
    assert isinstance(captured["kwargs"].get("verify"), ssl.SSLContext)
