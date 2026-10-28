"""Bounded ordinary-analysis contracts; independent of persistence and model SDKs."""

from typing import Literal, Self
from uuid import UUID

from pydantic import Field, model_validator

from future_agent.domain.contracts import Contract, Text, UtcTime


class ProposedOutcome(Contract):
    key: Text
    definition: Text


class Clarification(Contract):
    question: Text
    kind: Literal["target", "private_fact"]
    changes_judgment: Text
    cannot_research_reason: Text


class PlannedQuery(Contract):
    query: Text
    purpose: Literal["current_state", "counterevidence", "reference", "mechanism"]
    decision_gap: Text


class AnalysisPlan(Contract):
    question: Text | None
    outcome_type: Literal["binary", "categorical", "continuous"] = "binary"
    duration_days: int = Field(default=30, ge=1, le=3660)
    window_start: UtcTime | None = None
    window_end: UtcTime | None = None
    resolution_rule: Text | None
    resolution_source_policy: Text | None
    analysis_kind: Literal["baseline", "conditional", "exploration"] = "baseline"
    action_conditions: list[Text] = Field(default_factory=list)
    assumptions: list[Text] = Field(default_factory=list)
    outcomes: list[ProposedOutcome] = Field(default_factory=list, max_length=6)
    definition_reason: Text
    questions: list[Clarification] = Field(default_factory=list, max_length=3)
    queries: list[PlannedQuery] = Field(default_factory=list, max_length=4)
    unavailable_reason: Text | None = None

    def usable(self) -> bool:
        return bool(
            self.question
            and self.resolution_rule
            and self.resolution_source_policy
            and len(self.outcomes) >= 2
            and not self.unavailable_reason
        )

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if (self.window_start is None) != (self.window_end is None):
            raise ValueError("绝对窗口必须同时提供起止时间")
        if self.window_start and self.window_start >= self.window_end:
            raise ValueError("绝对窗口顺序错误")
        if len({o.key for o in self.outcomes}) != len(self.outcomes):
            raise ValueError("结果键重复")
        if self.outcome_type == "binary" and self.outcomes and len(self.outcomes) != 2:
            raise ValueError("二元问题必须有两个结果")
        if self.analysis_kind == "conditional" and not self.action_conditions:
            raise ValueError("条件分析必须明确行动或适用条件")
        if len({q.query.casefold().strip() for q in self.queries}) != len(self.queries):
            raise ValueError("查询不得重复")
        return self


class NumericBasis(Contract):
    status: Literal["supported", "conditional_only", "insufficient"]
    source_kind: Literal["external", "user_report", "judgment", "none"]
    reference: Text
    relevance: Text
    unknown_effect: Text
    limitations: list[Text] = Field(min_length=1)

    @model_validator(mode="after")
    def support(self) -> Self:
        if self.status == "supported" and self.source_kind == "none":
            raise ValueError("数值判断必须声明依据")
        return self


class ExtractedCitation(Contract):
    document_id: UUID
    quote: Text
    purpose: Literal["current_state", "counterevidence", "mechanism"]
    relevance_reason: Text


class EvidenceReading(Contract):
    questions: list[Clarification] = Field(default_factory=list, max_length=3)
    citations: list[ExtractedCitation] = Field(default_factory=list, max_length=16)
    numeric_basis: NumericBasis
    material_gaps: list[Text] = Field(default_factory=list, max_length=5)
    followup_query_indexes: list[int] = Field(default_factory=list, max_length=2)
    followup_queries: list[PlannedQuery] = Field(default_factory=list, max_length=2)
    research_stop_reason: Text


class JudgmentSummary(Contract):
    delivery: Literal["probability", "directional", "conditional", "exploration", "unable"]
    conclusion: Text
    primary_outcome: Text | None
    ranking: list[Text] = Field(default_factory=list)
    reference_kind: Literal[
        "history", "reference_class", "timetable", "persistence", "judgment", "none"
    ]
    reference_basis: Text
    differences_and_adjustment: Text
    key_drivers: list[Text] = Field(min_length=1, max_length=5)
    unknown_effects: list[Text] = Field(default_factory=list)
    change_conditions: list[Text] = Field(min_length=1)
    no_numeric_reason: Text | None


class AssistanceState(Contract):
    """Progress and clarification allowance survive restarts and event revisions."""

    source_count: int = Field(default=0, ge=0)
    event_revision: int = Field(default=0, ge=0)
    stage: Literal[
        "planning",
        "clarification",
        "planned",
        "researching",
        "reviewing",
        "search_authorization",
        "ready",
        "complete",
        "unanswerable",
    ] = "planning"
    clarification_used: bool = False
    clarification_answer: Text | None = None
    clarification_questions: list[Clarification] = Field(default_factory=list, max_length=3)
    model_id: Text | None = None
    plan: AnalysisPlan | None = None
    reading: EvidenceReading | None = None
    reading_version: Text | None = None
    research_rounds: int = Field(default=0, ge=0, le=2)
    approved_queries: list[Text] = Field(default_factory=list)
    pending_queries: list[PlannedQuery] = Field(default_factory=list, max_length=2)
    research_gaps: list[Text] = Field(default_factory=list)
    stop_reason: Text | None = None
    forecast_id: UUID | None = None
    forecast_request_id: UUID | None = None
    assessment_id: UUID | None = None
    final_revision: int | None = None
    research_revision: int | None = None
