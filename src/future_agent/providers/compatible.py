"""Bounded synchronous chat-completions adapter with sanitized failures."""

import asyncio
import json
import math
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import httpx
from pydantic import BaseModel, Field, JsonValue, ValidationError

from future_agent.application.ports import ModelError, ModelReply
from future_agent.config import Settings

MAX_RESPONSE_BYTES = 2 * 1024 * 1024


class Message(BaseModel):
    content: str | None = None


class Choice(BaseModel):
    message: Message
    finish_reason: str | None = None


class Usage(BaseModel):
    prompt_tokens: int | None = Field(default=None, ge=0)
    completion_tokens: int | None = Field(default=None, ge=0)


class Completion(BaseModel):
    choices: list[Choice] = Field(min_length=1)
    usage: Usage | None = None


def retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            timestamp = parsedate_to_datetime(value)
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=UTC)
            seconds = (timestamp - datetime.now(UTC)).total_seconds()
        except (ValueError, TypeError, OverflowError):
            return None
    return max(0, seconds) if math.isfinite(seconds) else None


class CompatibleProvider:
    def __init__(
        self, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        if not settings.model_ready():
            raise ModelError("not_configured")
        self.settings = settings
        self.model_id = settings.llm_model
        self.structured_mode = settings.llm_structured_mode
        self.output_token_limit = settings.llm_output_tokens
        self.transport = transport
        # Prepare local certificate configuration before timing network I/O. Reuse
        # it across calls without sharing sockets between asyncio.run event loops.
        self._verify = httpx.create_ssl_context() if transport is None else True

    def close(self) -> None:
        # Each synchronous generate call closes its own client and event loop.
        pass

    def generate(
        self,
        system: str,
        user: str,
        schema: dict[str, JsonValue],
        timeout: float,
    ) -> ModelReply:
        if not math.isfinite(timeout) or timeout <= 0:
            raise ModelError("timeout")
        return asyncio.run(self._generate(system, user, schema, min(timeout, 60.0)))

    async def _generate(
        self, system: str, user: str, schema: dict[str, JsonValue], timeout: float
    ) -> ModelReply:
        schema_text = json.dumps(schema, ensure_ascii=False) if schema else ""
        if len(system) + len(user) + len(schema_text) > self.settings.max_context_chars:
            raise ModelError("context_limit")
        payload: dict[str, JsonValue] = {
            "model": self.model_id,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "max_tokens": self.settings.llm_output_tokens,
        }
        if schema and self.structured_mode == "json_schema":
            # Local validation remains required; strict subsets may reject optional/default fields.
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "reader_result",
                    "strict": False,
                    "schema": schema,
                },
            }
        elif schema:
            payload["messages"][0]["content"] = system + "\nJSON Schema:\n" + schema_text
        headers = {"Content-Type": "application/json"}
        if self.settings.llm_api_key.get_secret_value():
            headers["Authorization"] = "Bearer " + self.settings.llm_api_key.get_secret_value()
        url = str(self.settings.llm_base_url).rstrip("/") + "/chat/completions"
        try:
            # Cancellation covers DNS/connect, response headers and every body chunk.
            async with asyncio.timeout(timeout):
                async with httpx.AsyncClient(
                    transport=self.transport, verify=self._verify, follow_redirects=False
                ) as client:
                    async with client.stream(
                        "POST",
                        url,
                        json=payload,
                        headers=headers,
                        follow_redirects=False,
                        timeout=httpx.Timeout(
                            timeout, connect=min(timeout, self.settings.llm_connect_timeout)
                        ),
                    ) as response:
                        if response.status_code != 200:
                            code = {
                                401: "authentication",
                                403: "authentication",
                                429: "rate_limited",
                                400: "invalid_request",
                                404: "invalid_request",
                                422: "invalid_request",
                            }.get(
                                response.status_code,
                                "temporary" if response.status_code >= 500 else "http_error",
                            )
                            raise ModelError(
                                code, retry_after=retry_after(response.headers.get("Retry-After"))
                            )
                        data = bytearray()
                        async for chunk in response.aiter_bytes():
                            data.extend(chunk)
                            if len(data) > MAX_RESPONSE_BYTES:
                                raise ModelError("response_too_large")
            obj = Completion.model_validate_json(data)
            choice = obj.choices[0]
            content = choice.message.content
            if choice.finish_reason not in (None, "stop"):
                raise ModelError("incomplete_output")
            if not isinstance(content, str) or not content.strip():
                raise ModelError("empty_output")
            usage = obj.usage or Usage()
            return ModelReply(
                content=content,
                input_tokens=usage.prompt_tokens,
                output_tokens=usage.completion_tokens,
            )
        except (TimeoutError, httpx.TimeoutException):
            raise ModelError("timeout") from None
        except httpx.HTTPError:
            raise ModelError("network") from None
        except (ValueError, KeyError, IndexError, TypeError, ValidationError):
            raise ModelError("invalid_response") from None
