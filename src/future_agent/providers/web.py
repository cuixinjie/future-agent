"""Bounded static page reader. Every connection is pinned to a validated public IP."""

import asyncio
import ipaddress
import json
import math
import os
import subprocess
import sys
import time
import zlib
from collections.abc import Awaitable, Callable
from urllib.parse import urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup
from bs4.exceptions import ParserRejectedMarkup

from future_agent.application.network import NETWORK_CALL_TIMEOUT, NetworkError
from future_agent.domain.events import now
from future_agent.domain.evidence import EvidenceImport, canonical_url
from future_agent.providers.compatible import retry_after

MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_REDIRECTS = 3
MAX_REQUEST_SECONDS = NETWORK_CALL_TIMEOUT
BODY_CHUNK_BYTES = 64 * 1024
REDIRECTS = {301, 302, 303, 307, 308}
Resolver = Callable[[str, int], Awaitable[list[str]]]


async def resolve(host: str, port: int) -> list[str]:
    # Numeric addresses require no DNS. OS getaddrinfo is not cancellable and a thread
    # would delay asyncio.run's executor shutdown beyond the request deadline. Isolate
    # it in a short-lived process that can be terminated on timeout/cancellation.
    try:
        return [str(ipaddress.ip_address(host))]
    except ValueError:
        pass
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-I",
        "-c",
        "import json,socket,sys; print(json.dumps(list(dict.fromkeys("
        "r[4][0] for r in socket.getaddrinfo(sys.argv[1],int(sys.argv[2]),"
        "type=socket.SOCK_STREAM)))))",
        host,
        str(port),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    try:
        output, _ = await process.communicate()
        if process.returncode != 0:
            raise NetworkError("network")
        return json.loads(output)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


def safe_url(value: str) -> str:
    try:
        url = canonical_url(value)
        parts = urlsplit(url)
        if parts.port == 0 or parts.hostname.lower().rstrip(".") == "localhost":
            raise ValueError
        httpx.URL(url)
        return url
    except (ValueError, UnicodeError, httpx.InvalidURL):
        raise NetworkError("unsafe_url") from None


def public_ip(value: str) -> bool:
    address = ipaddress.ip_address(value)
    # Reject transition/mapped addresses too: their routed destination can differ.
    return address.is_global and not (
        address.is_multicast
        or address.is_unspecified
        or address.is_reserved
        or (
            isinstance(address, ipaddress.IPv6Address)
            and (
                address.ipv4_mapped
                or address.sixtofour
                or address.teredo
                or address in ipaddress.ip_network("64:ff9b::/96")
                or address in ipaddress.ip_network("64:ff9b:1::/48")
            )
        )
    )


class PublicTransport(httpx.AsyncBaseTransport):
    def __init__(
        self, *, resolver: Resolver = resolve, inner: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self.resolver = resolver
        self.inner = inner or httpx.AsyncHTTPTransport(retries=0, trust_env=False)

    def validate_addresses(self, addresses: list[str]) -> None:
        if not addresses or any(not public_ip(address) for address in addresses):
            raise NetworkError("unsafe_url")

    def validate_url(self, value: str) -> None:
        safe_url(value)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.validate_url(str(request.url))
        host = request.url.host
        port = request.url.port or (443 if request.url.scheme == "https" else 80)
        try:
            addresses = await self.resolver(host, port)
            self.validate_addresses(addresses)
        except ValueError:
            raise NetworkError("unsafe_url") from None
        # Connect to the checked address, while TLS validates the original host.
        pinned = httpx.Request(
            request.method,
            request.url.copy_with(host=addresses[0]),
            headers=request.headers,
            stream=request.stream,
            extensions={**request.extensions, "sni_hostname": host},
        )
        return await self.inner.handle_async_request(pinned)

    async def aclose(self) -> None:
        await self.inner.aclose()


def check_status(response: httpx.Response) -> None:
    if response.status_code == 429 or response.status_code >= 500:
        raise NetworkError(
            "rate_limited" if response.status_code == 429 else "temporary",
            retry_after(response.headers.get("Retry-After")),
        )
    if response.status_code != 200:
        raise NetworkError("http_error")


async def bounded_body(
    response: httpx.Response, *, allow_gzip: bool = False, deadline: float | None = None
) -> bytes:
    encoding = response.headers.get("Content-Encoding", "identity").strip().lower()
    if encoding not in {"", "identity"} and not (allow_gzip and encoding == "gzip"):
        raise NetworkError("unsupported_encoding")
    length = response.headers.get("Content-Length")
    if length:
        try:
            if int(length) < 0 or int(length) > MAX_RESPONSE_BYTES:
                raise NetworkError("response_too_large")
        except ValueError:
            raise NetworkError("invalid_response") from None
    if response.is_stream_consumed:
        # HTTPX may already have decoded a buffered compressed response. Its wire
        # size and gzip integrity cannot be checked here; production uses streams.
        if encoding == "gzip":
            raise NetworkError("invalid_response")
        if len(response.content) > MAX_RESPONSE_BYTES:
            raise NetworkError("response_too_large")
        return response.content
    decoder = zlib.decompressobj(16 + zlib.MAX_WBITS) if encoding == "gzip" else None
    wire_size = 0
    body = bytearray()
    async for chunk in response.aiter_raw(chunk_size=BODY_CHUNK_BYTES):
        # Yield even for immediately-ready streams and enforce the same deadline
        # around bounded synchronous decompression, including its final chunk.
        await asyncio.sleep(0)
        if deadline is not None and time.monotonic() >= deadline:
            raise NetworkError("timeout")
        wire_size += len(chunk)
        if wire_size > MAX_RESPONSE_BYTES:
            raise NetworkError("response_too_large")
        try:
            decoded = (
                decoder.decompress(chunk, MAX_RESPONSE_BYTES - len(body) + 1) if decoder else chunk
            )
        except zlib.error:
            raise NetworkError("invalid_response") from None
        body.extend(decoded)
        if len(body) > MAX_RESPONSE_BYTES:
            raise NetworkError("response_too_large")
        # Accept one complete gzip member only; do not silently discard trailing
        # bytes or additional members. zlib checks the gzip CRC and size trailer.
        if decoder and decoder.unused_data:
            raise NetworkError("invalid_response")
        if deadline is not None and time.monotonic() >= deadline:
            raise NetworkError("timeout")
    if decoder and not decoder.eof:
        raise NetworkError("invalid_response")
    return bytes(body)


class StaticPageReader:
    def __init__(
        self, *, resolver: Resolver = resolve, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self.resolver = resolver
        self.transport = transport

    def read(self, url: str, timeout: float) -> EvidenceImport:
        if not math.isfinite(timeout) or timeout <= 0:
            raise NetworkError("timeout")
        return asyncio.run(self._read(url, min(timeout, MAX_REQUEST_SECONDS)))

    async def _read(self, url: str, timeout: float) -> EvidenceImport:
        original = safe_url(url)
        current = original
        try:
            async with asyncio.timeout(timeout):
                deadline = time.monotonic() + timeout
                async with httpx.AsyncClient(
                    transport=PublicTransport(resolver=self.resolver, inner=self.transport),
                    trust_env=False,
                    follow_redirects=False,
                    headers={"Accept-Encoding": "identity", "User-Agent": "future-agent/0.1"},
                ) as client:
                    for hop in range(MAX_REDIRECTS + 1):
                        async with client.stream("GET", current, timeout=timeout) as response:
                            if response.status_code in REDIRECTS:
                                location = response.headers.get("Location")
                                if hop == MAX_REDIRECTS or not location:
                                    raise NetworkError("redirect_limit")
                                try:
                                    current = safe_url(urljoin(current, location))
                                except ValueError:
                                    raise NetworkError("unsafe_url") from None
                                continue
                            check_status(response)
                            mime = response.headers.get("Content-Type", "").split(";")[0].lower()
                            if mime not in {"text/html", "text/plain"}:
                                raise NetworkError("unsupported")
                            raw = await bounded_body(response, allow_gzip=True, deadline=deadline)
                            encoding = response.charset_encoding or "utf-8"
                            try:
                                text = raw.decode(encoding, errors="strict")
                            except (UnicodeError, LookupError):
                                raise NetworkError("invalid_encoding") from None
                            title = None
                            if mime == "text/html":
                                try:
                                    soup = BeautifulSoup(text, "html.parser")
                                except ParserRejectedMarkup:
                                    raise NetworkError("unsupported") from None
                                title = soup.title.get_text(" ", strip=True) if soup.title else None
                                for tag in soup(
                                    [
                                        "script",
                                        "style",
                                        "nav",
                                        "header",
                                        "footer",
                                        "noscript",
                                        "template",
                                    ]
                                ):
                                    tag.decompose()
                                text = (soup.find("main") or soup.find("article") or soup).get_text(
                                    "\n", strip=True
                                )
                            if not text.strip():
                                raise NetworkError("empty_body")
                            if time.monotonic() > deadline:
                                raise NetworkError("timeout")
                            return EvidenceImport(
                                url=original,
                                final_url=current,
                                title=title or None,
                                raw_text=text,
                                retrieved_at=now(),
                                access_status="read_success",
                                origin="network",
                            )
        except (TimeoutError, httpx.TimeoutException):
            raise NetworkError("timeout") from None
        except (httpx.HTTPError, OSError):
            raise NetworkError("network") from None
        raise NetworkError("redirect_limit")
