"""Live-diagnostic logic tested with explicit fixed replies, never a paid service."""

import json

import pytest

from future_agent.application.ports import AppError, ModelReply
from future_agent.cli import doctor
from future_agent.config import Settings


class ProbeFixture:
    model_id = "offline-probe-fixture"
    structured_mode = "text_json"
    output_token_limit = 100

    def __init__(self):
        self.schemas = []

    def generate(self, system, user, schema, timeout):
        self.schemas.append(schema)
        return ModelReply(content='{"ok":true}', input_tokens=5, output_tokens=3)


def test_diagnostic_runs_two_bounded_synthetic_probes(capsys):
    provider = ProbeFixture()
    doctor(Settings(llm_model="fixture", llm_base_url="http://127.0.0.1:1234/v1"), True, provider)
    result = json.loads(capsys.readouterr().out)
    assert result["calls"] == 2 and result["cost"] is None
    assert provider.schemas[0] == {} and provider.schemas[1]["type"] == "object"


def test_diagnostic_token_budget_prevents_calls():
    provider = ProbeFixture()
    with pytest.raises(AppError, match="预算"):
        doctor(Settings(max_total_tokens=1), True, provider)
    assert provider.schemas == []


@pytest.mark.parametrize("probe_index", [0, 1])
@pytest.mark.parametrize(
    "content",
    [
        '{"ok":1}',
        '{"ok":1.0}',
        '{"ok":"true"}',
        '{"ok":false}',
        '{"ok":true,"extra":1}',
        "{}",
        "true",
        "[]",
        "null",
    ],
)
def test_diagnostic_rejects_invalid_probe_contract(content, probe_index, capsys):
    class InvalidProbe(ProbeFixture):
        def generate(self, *args):
            reply = super().generate(*args)
            if len(self.schemas) == probe_index + 1:
                return ModelReply(content=content)
            return reply

    provider = InvalidProbe()
    with pytest.raises(AppError, match="返回格式"):
        doctor(Settings(), True, provider)
    assert len(provider.schemas) == probe_index + 1
    assert capsys.readouterr().out == ""
