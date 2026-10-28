"""Synthetic HTTP and DNS responses test the real adapters; no external services."""

import asyncio
import gzip
import os
import time

import httpx
import pytest

from future_agent.application.network import NetworkError
from future_agent.config import Settings
from future_agent.domain.research import SearchQuery, public_terms
from future_agent.providers import web
from future_agent.providers.searxng import SearXNGProvider
from future_agent.providers.web import (
    MAX_RESPONSE_BYTES,
    StaticPageReader,
    public_ip,
    resolve,
)


async def public_dns(host, port):
    return ["8.8.8.8"]


def reader(handler, resolver=public_dns):
    return StaticPageReader(resolver=resolver, transport=httpx.MockTransport(handler))


def test_html_text_and_dns_pinning_with_tls_hostname():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(
            200,
            headers={"Content-Type": "text/html; charset=utf-8"},
            content="<title>标题</title><nav>噪声</nav><main>正文😀<script>bad</script></main>".encode(),
        )

    result = reader(handler).read("https://news.example/page", 2)
    assert result.raw_text == "正文😀" and result.title == "标题"
    assert result.url == result.final_url == "https://news.example/page"
    assert result.published_at is None and result.retrieved_at is not None
    assert seen[0].url.host == "8.8.8.8"
    assert seen[0].headers["host"] == "news.example"
    assert seen[0].extensions["sni_hostname"] == "news.example"
    result = reader(
        lambda r: httpx.Response(
            200, headers={"Content-Type": "text/plain"}, content="正文\r\n保留".encode()
        )
    ).read("https://x.org", 2)
    assert result.raw_text == "正文\r\n保留"


def test_gzip_page_ignoring_identity_request_is_read():
    raw = "<title>发布</title><main>正式版本😀</main>".encode()

    def handler(request):
        assert request.headers["accept-encoding"] == "identity"
        return httpx.Response(
            200,
            headers={"Content-Type": "text/html; charset=utf-8", "Content-Encoding": "gzip"},
            stream=httpx.ByteStream(gzip.compress(raw)),
        )

    result = reader(handler).read("https://x.org/releases", 2)
    assert result.raw_text == "正式版本😀" and result.title == "发布"


class ChunkedBody(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk


def gzip_reader(body, *, headers=None):
    return reader(
        lambda r: httpx.Response(
            200,
            headers={
                "Content-Type": "text/plain; charset=utf-8",
                "Content-Encoding": "gzip",
                **(headers or {}),
            },
            stream=ChunkedBody([body[i : i + 7] for i in range(0, len(body), 7)]),
        )
    )


def test_gzip_split_header_trailer_and_exact_decoded_limit(monkeypatch):
    monkeypatch.setattr(web, "MAX_RESPONSE_BYTES", 8192)
    text = "a" * 8192
    assert gzip_reader(gzip.compress(text.encode())).read("https://x.org", 2).raw_text == text


@pytest.mark.parametrize("kind", ["decoded", "wire", "declared-wire"])
def test_gzip_bounds_wire_and_decoded_sizes(monkeypatch, kind):
    monkeypatch.setattr(web, "MAX_RESPONSE_BYTES", 8192)
    raw = os.urandom(8192) if kind == "wire" else b"a" * 8193
    headers = {"Content-Length": "8193"} if kind == "declared-wire" else None
    with pytest.raises(NetworkError, match="response_too_large"):
        gzip_reader(gzip.compress(raw), headers=headers).read("https://x.org", 2)


@pytest.mark.parametrize("kind", ["truncated", "crc", "garbage", "trailing", "members", "empty"])
def test_invalid_gzip_is_explicit_failure(kind):
    compressed = gzip.compress(b"body")
    invalid = {
        "truncated": compressed[:-1],
        "crc": compressed[:-8] + bytes([compressed[-8] ^ 1]) + compressed[-7:],
        "garbage": b"not a gzip stream",
        "trailing": compressed + b"unexpected payload",
        "members": compressed + gzip.compress(b"second member"),
        "empty": b"",
    }[kind]
    with pytest.raises(NetworkError, match="invalid_response"):
        gzip_reader(invalid).read("https://x.org", 2)


def test_gzip_final_chunk_still_obeys_deadline():
    class SlowFinalBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            # Synchronous work cannot be cancelled until control is yielded.
            time.sleep(0.06)
            yield gzip.compress(b"late response")

    page = reader(
        lambda r: httpx.Response(
            200,
            headers={"Content-Type": "text/plain", "Content-Encoding": "gzip"},
            stream=SlowFinalBody(),
        )
    )
    with pytest.raises(NetworkError, match="timeout"):
        page.read("https://x.org", 0.03)


def test_search_does_not_implicitly_enable_gzip():
    search = SearXNGProvider(
        Settings(search_base_url="http://127.0.0.1:8080"),
        transport=httpx.MockTransport(
            lambda r: httpx.Response(
                200,
                headers={"Content-Type": "application/json", "Content-Encoding": "gzip"},
                stream=ChunkedBody([gzip.compress(b'{"results":[]}')]),
            )
        ),
    )
    with pytest.raises(NetworkError, match="unsupported_encoding"):
        search.search(SearchQuery(query="public", purpose="current_state"), 2)


@pytest.mark.parametrize(
    "url",
    [
        "file:///a",
        "http://user:secret@x.org",
        "http://localhost/a",
        "http://x.org:0/",
        "http://x.org/\\a",
        "https://x.org/a\nb",
        "gopher://x.org",
    ],
)
def test_unsafe_url_rejected_without_request(url):
    def handler(r):
        pytest.fail("unsafe URL reached HTTP transport")

    with pytest.raises(NetworkError, match="unsafe_url"):
        reader(handler).read(url, 2)


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.0.0.1",
        "169.254.169.254",
        "192.168.1.1",
        "100.64.0.1",
        "198.18.1.99",
        "0.0.0.0",
        "224.0.0.1",
        "::1",
        "fe80::1",
        "fc00::1",
        "::ffff:8.8.8.8",
        "64:ff9b::a00:1",
        "2002:0808:0808::1",
        "2001:db8::1",
    ],
)
def test_nonpublic_or_transition_dns_rejected(address):
    assert not public_ip(address)

    async def dns(host, port):
        return ["8.8.8.8", address]

    def handler(r):
        pytest.fail("mixed public/private DNS reached transport")

    with pytest.raises(NetworkError, match="unsafe_url"):
        reader(handler, dns).read("https://x.org/", 2)


def test_redirect_rechecks_dns_and_blocks_rebinding():
    resolutions, requests = [], []

    async def dns(host, port):
        resolutions.append(host)
        return ["8.8.8.8"] if len(resolutions) == 1 else ["127.0.0.1"]

    def handler(r):
        requests.append(r)
        return httpx.Response(302, headers={"Location": "/second"})

    with pytest.raises(NetworkError, match="unsafe_url"):
        reader(handler, dns).read("https://x.org/", 2)
    assert resolutions == ["x.org", "x.org"] and len(requests) == 1


def test_redirect_limits_and_final_url():
    with pytest.raises(NetworkError, match="redirect_limit"):
        reader(lambda r: httpx.Response(302, headers={"Location": "/loop"})).read(
            "https://x.org", 2
        )

    def handler(r):
        if r.url.path == "/":
            return httpx.Response(302, headers={"Location": "/final"})
        return httpx.Response(200, headers={"Content-Type": "text/plain"}, content=b"body")

    result = reader(handler).read("https://x.org", 2)
    assert result.final_url == "https://x.org/final"


@pytest.mark.parametrize(
    "headers,body,code",
    [
        ({"Content-Type": "application/pdf"}, b"pdf", "unsupported"),
        (
            {"Content-Type": "text/plain", "Content-Length": str(MAX_RESPONSE_BYTES + 1)},
            b"x",
            "response_too_large",
        ),
        ({"Content-Type": "text/plain"}, b"x" * (MAX_RESPONSE_BYTES + 1), "response_too_large"),
        ({"Content-Type": "text/plain", "Content-Encoding": "br"}, b"", "unsupported_encoding"),
        ({"Content-Type": "text/plain"}, b"\xff", "invalid_encoding"),
        ({"Content-Type": "text/html"}, b"<script>only script</script>", "empty_body"),
        ({"Content-Type": "text/html"}, b"<![foo]><main>body</main>", "unsupported"),
    ],
    ids=[
        "mime",
        "declared-size",
        "actual-size",
        "compression",
        "encoding",
        "empty-html",
        "bad-html",
    ],
)
def test_body_failure_classification(headers, body, code):
    with pytest.raises(NetworkError, match=code):
        reader(lambda r: httpx.Response(200, headers=headers, stream=httpx.ByteStream(body))).read(
            "https://x.org", 2
        )


def test_total_timeout_covers_headers_body_and_dns():
    class SlowBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            while True:
                await asyncio.sleep(0.02)
                yield b"x"

    async def headers(r):
        await asyncio.sleep(0.2)
        return httpx.Response(200)

    async def dns(host, port):
        await asyncio.sleep(0.2)
        return ["8.8.8.8"]

    for page in [
        reader(headers),
        reader(
            lambda r: httpx.Response(200, headers={"Content-Type": "text/plain"}, stream=SlowBody())
        ),
        reader(lambda r: pytest.fail("DNS timed out"), dns),
    ]:
        started = time.monotonic()
        with pytest.raises(NetworkError, match="timeout"):
            page.read("https://x.org", 0.06)
        assert time.monotonic() - started < 1


def test_numeric_dns_uses_no_subprocess():
    assert asyncio.run(resolve("127.0.0.1", 80)) == ["127.0.0.1"]


def test_dns_child_is_terminated_on_timeout(monkeypatch):
    create = asyncio.create_subprocess_exec
    children = []

    async def slow_dns(*args, **kwargs):
        child = await create(args[0], "-I", "-c", "import time; time.sleep(10)", **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(asyncio, "create_subprocess_exec", slow_dns)

    async def scenario():
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(resolve("public.example", 443), 0.2)
        assert len(children) == 1 and children[0].returncode is not None

    started = time.monotonic()
    asyncio.run(scenario())
    assert time.monotonic() - started < 2


def test_search_json_protocol_and_date_clues():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "url": "https://x.org/a",
                        "title": "A",
                        "content": "snippet",
                        "publishedDate": "yesterday",
                    },
                    {"url": "file:///secret"},
                ],
                "unresponsive_engines": [["example", "timeout"]],
            },
        )

    search = SearXNGProvider(
        Settings(search_base_url="http://127.0.0.1:8080/base"),
        transport=httpx.MockTransport(handler),
    )
    reply = search.search(
        SearchQuery(query="public release", purpose="current_state", time_filter="month"), 2
    )
    assert seen[0].url.host == "127.0.0.1" and seen[0].url.path == "/base/search"
    assert seen[0].url.params["format"] == "json" and seen[0].url.params["time_range"] == "month"
    assert reply.candidates[0].date_hint == "yesterday" and len(reply.candidates) == 1
    assert set(reply.warnings) == {"invalid_candidate_url", "search_engines_unavailable"}


@pytest.mark.parametrize(
    "status,code", [(403, "http_error"), (429, "rate_limited"), (500, "temporary")]
)
def test_search_http_errors_redacted(status, code):
    search = SearXNGProvider(
        Settings(search_base_url="http://127.0.0.1:8080"),
        transport=httpx.MockTransport(
            lambda r: httpx.Response(
                status, headers={"Retry-After": "2"}, text="secret-provider-payload"
            )
        ),
    )
    with pytest.raises(NetworkError) as error:
        search.search(SearchQuery(query="public", purpose="current_state"), 2)
    assert error.value.code == code and "secret" not in str(error.value)


@pytest.mark.parametrize("value", ["me@example.org", "13800138000", "https://x.org", "a\nb", " "])
def test_queries_reject_obvious_personal_identifiers(value):
    with pytest.raises(ValueError):
        public_terms(value)
