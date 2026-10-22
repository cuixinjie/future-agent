"""Protocol, safety and error classification using HTTPX fixed transports."""

import json
from contextlib import contextmanager

import httpx
import pytest
from pydantic import ValidationError

from future_agent.application.ports import AppError, ModelError
from future_agent.config import Settings, load_settings
from future_agent.providers.compatible import CompatibleProvider, retry_after


@contextmanager
def fixture_transport(*, transport):
    """Explicit fixed transport; the provider owns each request client."""
    try:
        yield transport
    finally:
        transport.close()


def settings(**updates):
    return Settings(
        llm_base_url="http://127.0.0.1:1234/v1",
        llm_model="test-model",
        llm_api_key="test-secret",
        **updates,
    )


def response(content='{"ok":true}', **updates):
    return {
        "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 8, "completion_tokens": 3},
        **updates,
    }


@pytest.mark.parametrize("mode", ["text_json", "json_schema"])
def test_request_protocol_and_usage(mode):
    def handle(request):
        assert str(request.url) == "http://127.0.0.1:1234/v1/chat/completions"
        assert request.headers["Authorization"] == "Bearer test-secret"
        assert request.extensions["timeout"]["connect"] <= 5
        body = json.loads(request.content)
        assert body["max_tokens"] == 4000
        if mode == "json_schema":
            assert body["response_format"]["json_schema"]["schema"] == {"type": "object"}
        else:
            assert "response_format" not in body
            assert "JSON Schema" in body["messages"][0]["content"]
        return httpx.Response(200, json=response())

    with fixture_transport(transport=httpx.MockTransport(handle)) as client:
        provider = CompatibleProvider(settings(llm_structured_mode=mode), transport=client)
        reply = provider.generate("system", "user", {"type": "object"}, 5)
        assert reply.input_tokens == 8 and reply.output_tokens == 3


@pytest.mark.parametrize(
    "status,code",
    [
        (401, "authentication"),
        (403, "authentication"),
        (400, "invalid_request"),
        (404, "invalid_request"),
        (429, "rate_limited"),
        (503, "temporary"),
        (302, "http_error"),
    ],
)
def test_http_errors_are_sanitized_and_redirects_not_followed(status, code):
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(
            status,
            text="test-secret should never be logged",
            headers={
                "Retry-After": "2",
                "Location": "https://unexpected.example/",
            },
        )

    with fixture_transport(transport=httpx.MockTransport(handle)) as client:
        provider = CompatibleProvider(settings(), transport=client)
        with pytest.raises(ModelError) as captured:
            provider.generate("system", "user", {}, 5)
        assert captured.value.code == code
        assert captured.value.retry_after == 2
        assert "test-secret" not in str(captured.value)
        assert len(calls) == 1


@pytest.mark.parametrize(
    "failure,code",
    [
        (httpx.ReadTimeout("secret"), "timeout"),
        (httpx.ConnectError("secret"), "network"),
    ],
)
def test_network_errors_sanitized(failure, code):
    def handle(request):
        raise failure

    with fixture_transport(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(ModelError, match=code) as captured:
            CompatibleProvider(settings(), transport=client).generate("s", "u", {}, 5)
        assert "secret" not in str(captured.value)


@pytest.mark.parametrize(
    "payload,code",
    [
        ({}, "invalid_response"),
        (response(content=""), "empty_output"),
        (response(usage={"prompt_tokens": -1}), "invalid_response"),
        (
            {"choices": [{"message": {"content": "{}"}, "finish_reason": "length"}]},
            "incomplete_output",
        ),
    ],
)
def test_bad_response_not_empty_success(payload, code):
    with fixture_transport(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload))
    ) as client:
        with pytest.raises(ModelError, match=code):
            CompatibleProvider(settings(), transport=client).generate("s", "u", {}, 5)


def test_unknown_usage_remains_null():
    with fixture_transport(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=response(usage=None)))
    ) as client:
        reply = CompatibleProvider(settings(), transport=client).generate("s", "u", {}, 5)
        assert reply.input_tokens is None and reply.output_tokens is None


def test_malformed_nested_usage_is_classified():
    with fixture_transport(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json=response(usage="not-an-object"))
        )
    ) as client:
        with pytest.raises(ModelError, match="invalid_response"):
            CompatibleProvider(settings(), transport=client).generate("s", "u", {}, 5)


def test_model_response_size_is_bounded():
    with fixture_transport(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, content=b"x" * (2 * 1024 * 1024 + 1))
        )
    ) as client:
        with pytest.raises(ModelError, match="response_too_large"):
            CompatibleProvider(settings(), transport=client).generate("s", "u", {}, 5)


def test_context_limit_prevents_network_call():
    def handle(request):
        pytest.fail("context-limited requests must not reach network")

    with fixture_transport(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(ModelError, match="context_limit"):
            CompatibleProvider(settings(max_context_chars=100), transport=client).generate(
                "s", "x" * 101, {}, 5
            )


@pytest.mark.parametrize("value,expected", [("2", 2), ("-1", 0), ("NaN", None), ("invalid", None)])
def test_retry_after_numeric_invalid_and_date(value, expected):
    assert retry_after(value) == expected
    assert retry_after("Wed, 01 Jan 2020 00:00:00 GMT") == 0


@pytest.mark.parametrize(
    "url",
    [
        "https://user:secret@example.com/v1",
        "https://example.com/v1?key=secret",
        "ftp://example.com",
    ],
)
def test_endpoint_cannot_embed_credentials(url):
    with pytest.raises(ValidationError):
        Settings(llm_base_url=url)


def test_env_file_is_explicit_and_environment_wins(tmp_path, monkeypatch):
    file = tmp_path / "local.env"
    file.write_text('LLM_MODEL=file-model\nLLM_API_KEY="${literal-secret}"\n', encoding="utf-8")
    monkeypatch.setenv("LLM_MODEL", "env-model")
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    loaded = load_settings(file)
    assert loaded.llm_model == "env-model"
    assert loaded.llm_api_key.get_secret_value() == "${literal-secret}"
    assert "literal-secret" not in repr(loaded)


def test_database_path_cannot_escape_approved_data_dir(tmp_path):
    with pytest.raises(AppError, match="DATA_DIR"):
        Settings(
            data_dir=tmp_path / "approved", database_url="sqlite:///./outside.db"
        ).database_path()
