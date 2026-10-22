"""Domain boundaries: source offsets, unknowns, times, versions and state."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from conftest import NARRATIVE, complete_question, make_draft
from pydantic import ValidationError

from future_agent.application.events import questions
from future_agent.domain.events import (
    AnalysisSession,
    Claim,
    EventDraft,
    ForecastQuestion,
    SourceText,
    transition,
)


def test_normalization_and_roundtrip_preserve_negative_intention_and_unicode():
    source = SourceText.from_raw(NARRATIVE)
    assert source.raw == NARRATIVE
    assert "\r" not in source.normalized
    draft = make_draft(source)
    draft.validate_sources([source], reader=True)
    restored = EventDraft.model_validate_json(draft.model_dump_json())
    assert restored == draft
    assert restored.claims[0].text == "我没有发布稳定版。"
    assert restored.claims[1].assertion_type == "intention"
    assert restored.question is None
    assert restored.unknowns[0].resolution_status == "open"
    assert len(questions(restored)) == 3


@pytest.mark.parametrize(
    "change",
    [
        {"start": 1},
        {"end": 999},
        {"source_id": uuid4()},
        {"verification_status": "externally_supported"},
        {"assertion_type": "intention", "text": "发布了"},
    ],
)
def test_invalid_claims_are_not_accepted(change):
    source = SourceText.from_raw(NARRATIVE)
    data = make_draft(source).model_dump()
    data["claims"][0].update(change)
    with pytest.raises(ValueError):
        EventDraft.model_validate(data).validate_sources([source], reader=True)


def test_valid_substring_cannot_drop_negation():
    source = SourceText.from_raw("我没有发布稳定版。")
    quote = "发布稳定版。"
    claim = Claim(
        text=quote,
        quote=quote,
        source_id=source.id,
        start=3,
        end=len(source.normalized),
        assertion_type="observation",
    )
    with pytest.raises(ValueError, match="完整句子"):
        EventDraft(claims=[claim]).validate_sources([source], reader=True)


def test_plan_cannot_be_promoted_to_observation():
    source = SourceText.from_raw(NARRATIVE)
    draft = make_draft(source)
    draft.claims[1] = draft.claims[1].model_copy(update={"assertion_type": "observation"})
    with pytest.raises(ValueError, match="意图"):
        draft.validate_sources([source], reader=True)


@pytest.mark.parametrize(
    "change",
    [
        {"schema_version": "0.9"},
        {"probability": 0.5},
        {"question": {"outcome_type": "invalid"}},
        {"unknowns": [{"description": "未来", "impact_reason": "未知", "kind": "future"}]},
    ],
)
def test_unknown_schema_fields_and_enums_are_rejected(change):
    with pytest.raises(ValidationError):
        EventDraft.model_validate(change)


def test_naive_time_rejected_and_timezone_normalized():
    with pytest.raises(ValidationError):
        ForecastQuestion(forecast_at=datetime(2026, 1, 1))
    question = ForecastQuestion(forecast_at="2026-10-20T08:00:00+08:00")
    assert question.forecast_at == datetime(2026, 10, 20, tzinfo=UTC)
    assert question.missing()


@pytest.mark.parametrize(
    "field,value",
    [
        ("window_end", "2098-01-01T00:00:00Z"),
        ("forecast_at", "2099-01-02T00:00:00Z"),
        ("window_end", "2099-01-01T00:00:00Z"),
    ],
)
def test_time_contradictions_rejected(field, value):
    data = complete_question().model_dump()
    data[field] = value
    with pytest.raises(ValidationError):
        ForecastQuestion.model_validate(data)


def test_programmatic_state_transitions():
    session = AnalysisSession(sources=[SourceText.from_raw("叙述")], request_fingerprint="test")
    with pytest.raises(ValueError, match="非法状态"):
        transition(session, "FAILED")
    assert transition(session, "READING").status == "READING"
