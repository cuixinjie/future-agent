"""Explicit offline fixtures; external model behavior is never a production fallback."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from future_agent.application.ports import ModelError, ModelReply
from future_agent.domain.events import (
    Ambiguity,
    Claim,
    EventDraft,
    ForecastQuestion,
    SourceText,
    Unknown,
)
from future_agent.domain.forecasts import ForecastSpec, OutcomeDefinition
from future_agent.domain.protocols import EvaluationProtocol
from future_agent.storage.sqlite import SQLiteRepository

NARRATIVE = "我没有发布稳定版。\r\n我准备收费。最近增长很快。😀我觉得还需观察。"


def make_draft(source: SourceText) -> EventDraft:
    claims = []
    for quote, kind in [
        ("我没有发布稳定版。", "observation"),
        ("我准备收费。", "intention"),
        ("最近增长很快。", "observation"),
        ("😀我觉得还需观察。", "opinion"),
    ]:
        start = source.normalized.index(quote)
        claims.append(
            Claim(
                text=quote,
                quote=quote,
                source_id=source.id,
                start=start,
                end=start + len(quote),
                assertion_type=kind,
            )
        )
    return EventDraft(
        claims=claims,
        ambiguities=[
            Ambiguity(
                claim_ids=[claims[2].id],
                missing_fields=["时间与增长口径"],
                question="最近和增长快的具体含义？",
                priority_reason="影响目标",
            )
        ],
        unknowns=[Unknown(description="留存率未知", impact_reason="影响当前状态理解")],
    )


def complete_question() -> ForecastQuestion:
    start = datetime(2099, 1, 1, tzinfo=UTC)
    return ForecastQuestion(
        question="示例项目未来30天是否发布稳定版？",
        forecast_at=start,
        window_start=start,
        window_end=start + timedelta(days=30),
        outcome_type="binary",
        resolution_rule="官方稳定版本列表出现新的稳定版本记为1；完整窗口无发布记为0。",
        resolution_source_policy="示例官方发布页（离线夹具）",
    )


class FixtureProvider:
    """Tests only. Captures input and returns the explicitly supplied sequence."""

    model_id = "offline-fixture-not-a-real-model"
    structured_mode = "text_json"
    output_token_limit = 4000

    def __init__(self, responses: list[str | ModelError | BaseException]) -> None:
        self.responses = iter(responses)
        self.users: list[dict] = []

    def generate(self, system, user, schema, timeout) -> ModelReply:
        self.users.append(json.loads(user.split("\n上次输出")[0]))
        value = next(self.responses)
        if isinstance(value, BaseException):
            raise value
        return ModelReply(content=value, input_tokens=100, output_tokens=40)


@pytest.fixture
def repo(tmp_path):
    repository = SQLiteRepository(tmp_path / "app.db")
    repository.migrate()
    yield repository
    repository.close()


@pytest.fixture
def protocol_fixture():
    path = Path(__file__).parents[1] / "examples" / "protocol.fixture.json"
    return EvaluationProtocol.model_validate_json(path.read_text(encoding="utf-8"))


def forecast_spec(distribution="exclusive_exhaustive"):
    return ForecastSpec(
        outcomes=[
            OutcomeDefinition(key="yes", definition="窗口内按规则发布稳定版本"),
            OutcomeDefinition(key="no", definition="完整窗口内无符合规则的发布"),
        ],
        distribution=distribution,
        definition_review="人工确认：同一发布目标的发生与未发生，互斥且穷尽有效结果",
    )


def forecast_response(probabilities=(None, None), span=None, refusal=None):
    return {
        "outcomes": [
            {
                "key": key,
                "probability": p,
                "reasoning": "条件性推断，已有资料不足以证明未来结果",
                "supporting_evidence": [str(span)] if span else [],
                "opposing_evidence": [],
                "conditions": ["发布通道与核验规则保持不变"],
                "watch_signals": ["官方候选版与稳定版记录变化"],
            }
            for key, p in zip(("yes", "no"), probabilities, strict=True)
        ],
        "assumptions": ["窗口内无未披露的重大计划变更"],
        "refusal_reason": refusal,
    }
