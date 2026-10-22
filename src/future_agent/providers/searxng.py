"""Explicitly configured SearXNG JSON endpoint; independent of the public page channel."""

import asyncio
import math

import httpx
from pydantic import BaseModel, Field, ValidationError

from future_agent.application.network import NetworkError
from future_agent.config import Settings
from future_agent.domain.evidence import canonical_url
from future_agent.domain.research import SearchCandidate, SearchQuery, SearchReply
from future_agent.providers.web import (
    MAX_REQUEST_SECONDS,
    PublicTransport,
    bounded_body,
    check_status,
    safe_url,
)


class ConfiguredSearchTransport(PublicTransport):
    """Only the explicitly configured search endpoint may resolve to a local address."""

    def validate_addresses(self, addresses: list[str]) -> None:
        if not addresses:
            raise NetworkError("network")

    def validate_url(self, value: str) -> None:
        canonical_url(value)


class Result(BaseModel):
    url: str
    title: str | None = None
    content: str | None = None
    publishedDate: str | None = None


class Results(BaseModel):
    results: list[Result]
    unresponsive_engines: list = Field(default_factory=list)


class SearXNGProvider:
    def __init__(
        self, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        if settings.search_base_url is None:
            raise NetworkError("search_not_configured")
        self.url = str(settings.search_base_url).rstrip("/") + "/search"
        self.transport = transport
        self.limit = settings.max_pages

    def search(self, query: SearchQuery, timeout: float) -> SearchReply:
        if not math.isfinite(timeout) or timeout <= 0:
            raise NetworkError("timeout")
        return asyncio.run(self._search(query, min(timeout, MAX_REQUEST_SECONDS)))

    async def _search(self, query: SearchQuery, timeout: float) -> SearchReply:
        params = {"q": query.query, "format": "json", "language": query.language}
        if query.time_filter:
            params["time_range"] = query.time_filter
        try:
            async with asyncio.timeout(timeout):
                async with httpx.AsyncClient(
                    transport=ConfiguredSearchTransport(inner=self.transport),
                    trust_env=False,
                    follow_redirects=False,
                    headers={"Accept-Encoding": "identity"},
                ) as client:
                    async with client.stream("GET", self.url, params=params, timeout=timeout) as r:
                        check_status(r)
                        if r.headers.get("Content-Type", "").split(";")[0] != "application/json":
                            raise NetworkError("invalid_response")
                        parsed = Results.model_validate_json(await bounded_body(r))
            candidates = []
            warnings = ["search_engines_unavailable"] if parsed.unresponsive_engines else []
            for result in parsed.results[: self.limit]:
                try:
                    url = safe_url(result.url)
                except NetworkError:
                    warnings.append("invalid_candidate_url")
                    continue
                candidates.append(
                    SearchCandidate(
                        url=url,
                        title=result.title or None,
                        snippet=result.content or None,
                        date_hint=result.publishedDate or None,
                    )
                )
            return SearchReply(candidates=candidates, warnings=warnings)
        except (TimeoutError, httpx.TimeoutException):
            raise NetworkError("timeout") from None
        except httpx.HTTPError:
            raise NetworkError("network") from None
        except (ValidationError, ValueError):
            raise NetworkError("invalid_response") from None
