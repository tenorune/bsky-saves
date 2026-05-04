"""Characterization / regression tests for bsky_saves.articles.fetch_article.

Pins the v0.2 public contract before the v0.3 refactor introduces
_extract_article. These tests must pass before AND after the refactor.
"""
from __future__ import annotations

import httpx
import respx

from bsky_saves.articles import fetch_article


HAPPY_HTML = (
    "<html><head><title>Hello</title></head><body><article>"
    + ("This is the article body. " * 30)
    + "</article></body></html>"
)
SHORT_HTML = "<html><body><article>too short</article></body></html>"


@respx.mock
def test_fetch_article_returns_text_and_date_on_success():
    respx.get("https://example.com/a").respond(200, html=HAPPY_HTML)
    result, error = fetch_article("https://example.com/a")
    assert error is None
    assert isinstance(result, dict)
    assert isinstance(result["text"], str) and len(result["text"]) >= 100
    assert "date" in result  # may be None when not extractable


@respx.mock
def test_fetch_article_http_error_returns_http_code_string():
    respx.get("https://example.com/a").respond(404)
    result, error = fetch_article("https://example.com/a")
    assert result is None
    assert error == "http_404"


@respx.mock
def test_fetch_article_network_error_returns_fetch_error_string():
    respx.get("https://example.com/a").mock(side_effect=httpx.ConnectError("nope"))
    result, error = fetch_article("https://example.com/a")
    assert result is None
    assert error is not None
    assert error.startswith("fetch_error:")
    assert "ConnectError" in error


@respx.mock
def test_fetch_article_short_extraction_returns_too_short_error():
    respx.get("https://example.com/a").respond(200, html=SHORT_HTML)
    result, error = fetch_article("https://example.com/a")
    assert result is None
    assert error == "extraction_too_short_or_empty"
