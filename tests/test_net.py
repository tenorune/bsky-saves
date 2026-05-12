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
