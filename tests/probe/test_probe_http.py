"""HttpxFetcher over httpx.MockTransport, and the page rules in check_page."""

from __future__ import annotations

import hashlib

import httpx
import pytest
from probe_helpers import make_spec

from keyhold.probe.owner import HttpFetch, HttpxFetcher, ProbeConfigError, check_page


def fetcher_for(handler, **kwargs) -> HttpxFetcher:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return HttpxFetcher(client=client, **kwargs)


def test_get_returns_status_and_body_with_plain_headers() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=b"<p>keyhold-ok</p>")

    page = fetcher_for(handler).get("http://srv.test:8080/health")

    assert page == HttpFetch(200, b"<p>keyhold-ok</p>", False, None)
    assert seen[0].method == "GET"
    assert seen[0].headers["accept-encoding"] == "identity"
    assert seen[0].headers["user-agent"].startswith("keyhold-probe/")


def test_redirects_are_not_followed() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(302, headers={"location": "http://elsewhere.test/"})

    page = fetcher_for(handler).get("http://srv.test/")
    assert page.status == 302 and page.location == "http://elsewhere.test/"
    assert calls == ["http://srv.test/"]


def test_body_is_cut_at_the_size_limit() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"a" * 5000)

    page = fetcher_for(handler, max_bytes=1000).get("http://srv.test/")
    assert page.truncated and len(page.body) == 1000


def test_transport_errors_propagate_for_run_probe_to_explain() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(httpx.ConnectError):
        fetcher_for(handler).get("http://srv.test/")


def test_default_client_ignores_proxy_settings_and_redirects() -> None:
    client = HttpxFetcher(timeout_s=3).make_client()
    try:
        assert client.trust_env is False
        assert client.follow_redirects is False
        assert client.timeout.connect == 3
    finally:
        client.close()
    with pytest.raises(ProbeConfigError):
        HttpxFetcher(timeout_s=0)


# ---------------------------------------------------------------------------------------------
# check_page
# ---------------------------------------------------------------------------------------------

BODY = b"<html>status: keyhold-ok</html>"
DIGEST = hashlib.sha256(BODY).hexdigest()


@pytest.mark.parametrize(
    ("spec_kwargs", "page", "ok", "detail"),
    [
        ({}, HttpFetch(200, BODY), True, "HTTP 200, expected text found"),
        ({}, HttpFetch(204, b""), False, "HTTP 204, expected text not found"),
        ({}, HttpFetch(200, b"keyhold-OK"), False, "HTTP 200, expected text not found"),
        ({}, HttpFetch(500, BODY), False, "HTTP 500"),
        ({}, HttpFetch(301, BODY, location="https://srv.test/"), False, "HTTP 301 redirect"),
        ({"expect_substring": "café"}, HttpFetch(200, "menu: café".encode()), True, "expected text found"),
        ({"expect_sha256": DIGEST}, HttpFetch(200, BODY), True, "HTTP 200, sha256 matches"),
        ({"expect_sha256": DIGEST.upper()}, HttpFetch(200, BODY), True, "sha256 matches"),
        ({"expect_sha256": DIGEST}, HttpFetch(200, BODY + b"\n"), False, "does not match"),
        ({"expect_sha256": DIGEST}, HttpFetch(200, BODY, truncated=True), False, "size limit"),
        ({}, HttpFetch(200, BODY, truncated=True), True, "expected text found"),
        ({"expect_substring": ""}, HttpFetch(200, BODY), False, "no expected text"),
    ],
)
def test_check_page(spec_kwargs, page: HttpFetch, ok: bool, detail: str) -> None:
    got_ok, got_detail = check_page(make_spec(**spec_kwargs), page)
    assert got_ok is ok
    assert detail in got_detail
