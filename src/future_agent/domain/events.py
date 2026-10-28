"""M1 contracts. Offsets refer to newline-normalized Python strings."""

import re
from typing import Literal, Self
from uuid import UUID, uuid4

from pydantic import Field, model_validator

from future_agent.domain.assistance import AssistanceState
from future_agent.domain.contracts import Contract as Contract
from future_agent.domain.contracts import Text as Text
from future_agent.domain.contracts import UtcTime as UtcTime
from future_agent.domain.contracts import now as now
from future_agent.domain.contracts import utc as utc
from future_agent.domain.research import DEFAULT_MAX_PAGES, DEFAULT_MAX_SEARCH_QUERIES, ResearchRun


class SourceText(Contract):
    id: UUID = Field(default_factory=uuid4)
    kind: Literal["narrative", "answer"] = "narrative"
    raw: Text
    normalized: Text
    created_at: UtcTime = Field(default_factory=now)

    @model_validator(mode="after")
    def check_normalization(self) -> Self:
        if self.normalized != self.raw.replace("\r\n", "\n").replace("\r", "\n"):
            raise ValueError("规范化仅转换 CRLF/CR 为 LF；不改变 Unicode、空格或标点")
        return self

    @classmethod
    def from_raw(cls, raw: str, kind: Literal["narrative", "answer"] = "narrative") -> Self:
        return cls(raw=raw, normalized=raw.replace("\r\n", "\n").replace("\r", "\n"), kind=kind)


class TimeRange(Contract):
    start: UtcTime | None = None
    end: UtcTime | None = None

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if self.start and self.end and self.start > self.end:
            raise ValueError("时间范围起点晚于终点")
        return self


class Claim(Contract):
    id: UUID = Field(default_factory=uuid4)
    text: Text
    source_id: UUID
    quote: Text
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    time_range: TimeRange | None = None
    assertion_type: Literal["observation", "opinion", "intention", "hypothesis"]
    verification_status: Literal[
        "user_reported", "externally_supported", "disputed", "unverified"
    ] = "user_reported"

    @model_validator(mode="after")
    def verbatim(self) -> Self:
        if self.start >= self.end or self.text != self.quote:
            raise ValueError("M1 陈述 text 必须保留原句 quote，且 start < end")
        return self


class Ambiguity(Contract):
    id: UUID = Field(default_factory=uuid4)
    claim_ids: list[UUID] = Field(min_length=1)
    missing_fields: list[Text] = Field(min_length=1)
    candidate_interpretations: list[Text] = Field(default_factory=list)
    question: Text
    priority_reason: Text


class Unknown(Contract):
    id: UUID = Field(default_factory=uuid4)
    description: Text
    kind: Literal["current_state"] = "current_state"
    impact_reason: Text
    resolution_status: Literal["open", "kept_unknown", "answered"] = "open"


class ForecastQuestion(Contract):
    id: UUID = Field(default_factory=uuid4)
    event_revision_id: UUID | None = None
    question: Text | None = None
    forecast_at: UtcTime | None = None
    window_start: UtcTime | None = None
    window_end: UtcTime | None = None
    outcome_type: Literal["binary", "categorical", "continuous"] | None = None
    resolution_rule: Text | None = None
    resolution_source_policy: Text | None = None

    def missing(self) -> list[str]:
        fields = (
            "question",
            "forecast_at",
            "window_start",
            "window_end",
            "outcome_type",
            "resolution_rule",
            "resolution_source_policy",
        )
        return [field for field in fields if getattr(self, field) is None]

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if self.window_start and self.window_end and self.window_start >= self.window_end:
            raise ValueError("预测窗口起点必须早于终点")
        if self.forecast_at and self.window_start and self.forecast_at > self.window_start:
            raise ValueError("预测时点不得晚于窗口起点")
        return self


class EventDraft(Contract):
    claims: list[Claim] = Field(default_factory=list)
    ambiguities: list[Ambiguity] = Field(default_factory=list)
    unknowns: list[Unknown] = Field(default_factory=list)
    actors: list[Text] = Field(default_factory=list)
    timeline: list[Text] = Field(default_factory=list)
    constraints: list[Text] = Field(default_factory=list)
    planned_actions: list[Text] = Field(default_factory=list)
    question: ForecastQuestion | None = None

    @model_validator(mode="after")
    def unique_item_ids(self) -> Self:
        ids = [item.id for item in [*self.claims, *self.ambiguities, *self.unknowns]]
        if len(set(ids)) != len(ids):
            raise ValueError("档案项目 ID 不得重复")
        return self

    def validate_sources(self, sources: list[SourceText], *, reader: bool = False) -> None:
        source_map = {source.id: source.normalized for source in sources}
        ids = [claim.id for claim in self.claims]
        if len(set(ids)) != len(ids):
            raise ValueError("陈述 ID 重复")
        for claim in self.claims:
            text = source_map.get(claim.source_id)
            if (
                text is None
                or claim.end > len(text)
                or text[claim.start : claim.end] != claim.quote
            ):
                raise ValueError("引用与对应来源切片不一致")
            if reader:
                # Keep context: a valid substring must not turn “没有发布” into “发布”.
                before = re.split(r"[。！？!?；;\n]", text[: claim.start])[-1]
                after = (
                    ""
                    if claim.quote[-1] in "。！？!?；;\n"
                    else re.split(r"[。！？!?；;\n]", text[claim.end :])[0]
                )
                if before.strip() or after.strip():
                    raise ValueError("Reader 必须引用完整句子，不能截掉否定、计划或条件上下文")
            if claim.verification_status == "externally_supported":
                raise ValueError("M1 尚无外部证据，不能升级为外部核实")
            if reader and claim.verification_status != "user_reported":
                raise ValueError("Reader 的陈述必须标记 user_reported")
            if reader and claim.assertion_type == "observation":
                if any(word in claim.quote for word in ("准备", "计划", "打算", "希望")):
                    raise ValueError("计划/意图不能标记为 observation，请保留意图类别")
                if any(word in claim.quote for word in ("我觉得", "我认为", "我猜", "如果")):
                    raise ValueError("观点/假设不能标记为 observation")
        for ambiguity in self.ambiguities:
            if not set(ambiguity.claim_ids) <= set(ids):
                raise ValueError("模糊项引用了不存在的陈述")


class EventRevision(EventDraft):
    id: UUID = Field(default_factory=uuid4)
    session_id: UUID
    revision: int = Field(ge=1)
    created_at: UtcTime = Field(default_factory=now)
    origin: Literal["model", "manual", "fixture"]
    model_id: str | None = None
    prompt_version: str | None = None
    confirmed: bool = False
    semantic_review: Literal["pending_user_review", "user_confirmed", "system_reviewed"] = (
        "pending_user_review"
    )

    @model_validator(mode="after")
    def consistent_confirmation(self) -> Self:
        if self.question and self.question.event_revision_id != self.id:
            raise ValueError("预测问题必须绑定当前事件版本")
        if self.confirmed and (not self.question or self.question.missing()):
            raise ValueError("预测问题不完整，不能确认")
        if self.confirmed != (self.semantic_review != "pending_user_review"):
            raise ValueError("确认状态与人工语义审查状态不一致")
        return self


class CallAttempt(Contract):
    schema_version: Literal["1.1"] = "1.1"

    @model_validator(mode="before")
    @classmethod
    def upgrade_v1(cls, value: object) -> object:
        if isinstance(value, dict) and value.get("schema_version") == "1.0":
            return {**value, "schema_version": "1.1"}
        return value

    id: UUID = Field(default_factory=uuid4)
    forecast_step_id: UUID | None = None
    started_at: UtcTime = Field(default_factory=now)
    status: Literal["started", "succeeded", "failed"] = "started"
    error_code: str | None = None
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    reserved_tokens: int = Field(default=0, ge=0)
    charged_tokens: int = Field(default=0, ge=0)
    elapsed_seconds: float | None = Field(default=None, ge=0)
    cost: float | None = Field(default=None, ge=0)


SessionStatus = Literal["CREATED", "READING", "FORECASTING", "NEEDS_INPUT", "STRUCTURED", "FAILED"]


class AnalysisSession(Contract):
    schema_version: Literal["1.5"] = "1.5"

    @model_validator(mode="before")
    @classmethod
    def upgrade_v1(cls, value: object) -> object:
        if isinstance(value, dict) and value.get("schema_version") in {
            "1.0",
            "1.1",
            "1.2",
            "1.3",
            "1.4",
        }:
            return {**value, "schema_version": "1.5"}
        return value

    id: UUID = Field(default_factory=uuid4)
    version: int = Field(default=1, ge=1)
    created_at: UtcTime = Field(default_factory=now)
    status: SessionStatus = "CREATED"
    sources: list[SourceText]
    current_revision: int = Field(default=0, ge=0)
    active_forecast_step_id: UUID | None = None
    pending_questions: list[Text] = Field(default_factory=list, max_length=3)
    attempts: list[CallAttempt] = Field(default_factory=list)
    max_calls: int = Field(default=12, ge=1, le=100)
    max_run_seconds: float = Field(default=600, gt=0)
    max_total_tokens: int = Field(default=1000000, ge=1)
    consumed_tokens: int = Field(default=0, ge=0)
    consumed_seconds: float = Field(default=0, ge=0)
    max_search_queries: int = Field(default=DEFAULT_MAX_SEARCH_QUERIES, ge=1, le=100)
    max_pages: int = Field(default=DEFAULT_MAX_PAGES, ge=1, le=100)
    research_runs: list[ResearchRun] = Field(default_factory=list)
    error_code: str | None = None
    request_key: str | None = None
    request_fingerprint: str
    assistance: AssistanceState | None = Field(default=None, exclude_if=lambda value: value is None)


ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "CREATED": {"READING", "NEEDS_INPUT", "STRUCTURED"},
    "READING": {"NEEDS_INPUT", "FAILED"},
    "NEEDS_INPUT": {"READING", "NEEDS_INPUT", "STRUCTURED"},
    "STRUCTURED": {"READING", "FORECASTING", "NEEDS_INPUT", "STRUCTURED"},
    "FAILED": {"READING", "FORECASTING", "NEEDS_INPUT", "STRUCTURED"},
    "FORECASTING": {"STRUCTURED", "FAILED"},
}


def transition(session: AnalysisSession, status: SessionStatus) -> AnalysisSession:
    if status not in ALLOWED_TRANSITIONS[session.status]:
        raise ValueError(f"非法状态转换：{session.status} → {status}")
    return session.model_copy(update={"status": status})
