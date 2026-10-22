"""Evidence conditions and method gates; relevance is explicitly reviewed by a human."""

from typing import Literal, Self
from uuid import UUID, uuid4

from pydantic import Field, model_validator

from future_agent.domain.contracts import Contract, Text, UtcTime, now
from future_agent.domain.evidence import EvidenceEligibility

ASSESSOR_VERSION = "evidence-assessor-1.1"
Method = Literal[
    "scenario_only", "llm_judgment", "reference_class", "time_series", "release_frequency_baseline"
]
# Register only executable engines; statistical methods remain unavailable.
IMPLEMENTED_METHODS: frozenset[Method] = frozenset({"scenario_only", "llm_judgment"})


class ReviewedEvidence(Contract):
    span_id: UUID
    purpose: Literal["current_state", "counterevidence", "mechanism"]
    relevance_reason: Text


class AssessmentReview(Contract):
    reviewer: Text
    evidence: list[ReviewedEvidence] = Field(default_factory=list)
    critical_questions: list[Text] = Field(default_factory=list, max_length=3)
    limitations: list[Text] = Field(default_factory=list)
    allow_numeric: bool = False

    @model_validator(mode="after")
    def unique_evidence(self) -> Self:
        keys = [(item.span_id, item.purpose) for item in self.evidence]
        if len(keys) != len(set(keys)):
            raise ValueError("同一引用用途不能重复评估")
        return self


class MethodQualification(Contract):
    method: Method
    data_eligible: bool
    implemented: bool
    reasons: list[Text]


class ResearchCondition(Contract):
    run_id: UUID
    status: Literal["running", "completed", "partial", "failed", "cancelled"]
    gaps: list[Text]


def qualify_methods(
    *, target_complete: bool, current_evidence: bool, critical_unknowns: bool, allow_numeric: bool
) -> list[MethodQualification]:
    conditions: dict[Method, tuple[bool, list[str]]] = {
        "scenario_only": (target_complete, [] if target_complete else ["target_incomplete"]),
        "llm_judgment": (
            target_complete and current_evidence and not critical_unknowns and allow_numeric,
            [
                reason
                for condition, reason in (
                    (not target_complete, "target_incomplete"),
                    (not current_evidence, "no_reviewed_current_evidence"),
                    (critical_unknowns, "critical_conditions_unknown"),
                    (not allow_numeric, "numeric_estimation_not_enabled"),
                )
                if condition
            ],
        ),
        "reference_class": (False, ["qualified_reference_dataset_not_available"]),
        "time_series": (False, ["consistent_time_series_not_available"]),
        "release_frequency_baseline": (False, ["release_history_not_validated"]),
    }
    return [
        MethodQualification(
            method=method,
            data_eligible=eligible,
            implemented=method in IMPLEMENTED_METHODS,
            reasons=[*reasons, *([] if method in IMPLEMENTED_METHODS else ["not_implemented"])],
        )
        for method, (eligible, reasons) in conditions.items()
    ]


class EvidenceAssessment(Contract):
    id: UUID = Field(default_factory=uuid4)
    session_id: UUID
    event_revision_id: UUID
    revision: int = Field(ge=1)
    question_id: UUID
    created_at: UtcTime = Field(default_factory=now)
    cutoff: UtcTime
    experiment: bool
    assessor_version: Literal["evidence-assessor-1.0", "evidence-assessor-1.1"] = ASSESSOR_VERSION
    input_hash: Text
    review: AssessmentReview
    research_conditions: list[ResearchCondition]
    eligibility: list[EvidenceEligibility]
    covered_claim_ids: list[UUID]
    uncovered_claim_ids: list[UUID]
    unresolved_unknown_ids: list[UUID]
    methods: list[MethodQualification]
    recommended_method: Literal["scenario_only", "llm_judgment"]
    available_methods: list[Method]
    status: Literal["assessed", "needs_input"]
    gaps: list[Text]
    limitations: list[Text]
    case_branch: Literal["skipped"] = "skipped"
    case_skip_reason: Text = "案例抽取和比较尚未实现；案例不是情景或判断预测的前置条件。"

    @model_validator(mode="after")
    def valid_methods(self) -> Self:
        expected = [m.method for m in self.methods if m.data_eligible and m.implemented]
        if self.available_methods != expected:
            raise ValueError("可用方法必须是数据资格与已实现能力的交集")
        return self


class AssessmentView(Contract):
    assessment: EvidenceAssessment
    stale: bool
    invalidation_reasons: list[Text]
