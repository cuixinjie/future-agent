"""Reviewed outcome definitions and validated, explicitly uncalibrated forecasts."""

import math
from typing import Annotated, Literal, Self
from uuid import UUID, uuid4

from pydantic import Field, model_validator

from future_agent.domain.assessment import EvidenceAssessment
from future_agent.domain.contracts import Contract, Text, UtcTime, now
from future_agent.domain.events import EventRevision
from future_agent.domain.evidence import EvidenceDocument, EvidenceSpan

FORECAST_METHOD_VERSION = "judgment-1.0"
PROBABILITY_TOLERANCE = 1e-6
Probability = Annotated[float, Field(ge=0, le=1, strict=True)]


class OutcomeDefinition(Contract):
    key: Text
    definition: Text


class ForecastSpec(Contract):
    outcomes: list[OutcomeDefinition] = Field(min_length=2, max_length=12)
    distribution: Literal["independent", "exclusive_exhaustive"] = "independent"
    definition_review: Text

    @model_validator(mode="after")
    def unique(self) -> Self:
        if len({o.key for o in self.outcomes}) != len(self.outcomes):
            raise ValueError("结果键不得重复")
        if len({o.definition.strip() for o in self.outcomes}) != len(self.outcomes):
            raise ValueError("结果定义不得重复")
        return self


class OutcomeJudgment(Contract):
    key: Text
    probability: Probability | None = None
    reasoning: Text
    supporting_evidence: list[UUID]
    opposing_evidence: list[UUID]
    conditions: list[Text] = Field(min_length=1)
    watch_signals: list[Text] = Field(min_length=1)


class ForecastDraft(Contract):
    outcomes: list[OutcomeJudgment] = Field(min_length=2, max_length=12)
    assumptions: list[Text]
    refusal_reason: Text | None = None


class Forecast(Contract):
    id: UUID = Field(default_factory=uuid4)
    run_id: UUID = Field(default_factory=uuid4)
    session_id: UUID
    event: EventRevision
    assessment: EvidenceAssessment
    spec: ForecastSpec
    method: Literal["scenario_only", "llm_judgment"]
    method_version: Literal["judgment-1.0"] = FORECAST_METHOD_VERSION
    calibration_status: Literal["not_applicable", "uncalibrated"]
    model_id: Text
    prompt_version: Text
    structured_mode: Text
    created_at: UtcTime = Field(default_factory=now)
    evidence_cutoff: UtcTime
    evidence_documents: list[EvidenceDocument]
    evidence_spans: list[EvidenceSpan]
    attempt_ids: list[UUID] = Field(min_length=1)
    draft: ForecastDraft
    # Original values remain in draft; render/scoring consumers use probabilities.
    probabilities: list[Probability | None]
    normalization_original_sum: float | None = None

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if (
            self.event.session_id != self.session_id
            or self.assessment.session_id != self.session_id
            or self.assessment.event_revision_id != self.event.id
            or self.assessment.revision != self.event.revision
            or not self.event.confirmed
            or not self.event.question
            or self.event.question.missing()
            or self.assessment.question_id != self.event.question.id
            or self.evidence_cutoff != self.assessment.cutoff
            or self.evidence_cutoff != self.event.question.forecast_at
            or self.method not in self.assessment.available_methods
        ):
            raise ValueError("预测必须绑定事件、评估与截止版本")
        reviewed = {r.span_id for r in self.assessment.review.evidence}
        documents = {d.id: d for d in self.evidence_documents}
        eligible = {e.document_id for e in self.assessment.eligibility if e.eligible}
        if (
            {s.id for s in self.evidence_spans} != reviewed
            or len(reviewed) != len(self.evidence_spans)
            or len(documents) != len(self.evidence_documents)
            or set(documents) != {s.document_id for s in self.evidence_spans}
            or not set(documents) <= eligible
            or any(d.event_revision_id != self.event.id for d in documents.values())
        ):
            raise ValueError("预测证据快照必须完整对应已审查且合格的引用")
        if self.event.question.outcome_type == "binary" and (
            len(self.spec.outcomes) != 2 or self.spec.distribution != "exclusive_exhaustive"
        ):
            raise ValueError("二元问题必须有两个互斥穷尽结果")
        if self.method == "llm_judgment" and self.event.question.outcome_type == "continuous":
            raise ValueError("连续结果尚无数值方法")
        if self.method == "llm_judgment" and self.spec.distribution != "exclusive_exhaustive":
            raise ValueError("数值分类结果必须互斥穷尽，重叠事件须拆为独立二元问题")
        expected, original = validate_judgment(
            self.draft, self.spec, self.method, {s.id for s in self.evidence_spans}
        )
        if self.probabilities != expected or self.normalization_original_sum != original:
            raise ValueError("报告概率必须来自已校验预测，归一化须留痕")
        calibration = "uncalibrated" if self.method == "llm_judgment" else "not_applicable"
        if self.calibration_status != calibration:
            raise ValueError("主观概率必须标记未校准")
        return self


def validate_judgment(
    draft: ForecastDraft,
    spec: ForecastSpec,
    method: str,
    allowed_spans: set[UUID],
) -> tuple[list[float | None], float | None]:
    if [o.key for o in draft.outcomes] != [o.key for o in spec.outcomes]:
        raise ValueError("输出结果必须按审查定义逐项对应，不得新增、遗漏或重定义")
    for outcome in draft.outcomes:
        if not set(outcome.supporting_evidence + outcome.opposing_evidence) <= allowed_spans:
            raise ValueError("只能引用通过审查与截止资格的正文片段")
    values = [o.probability for o in draft.outcomes]
    if method == "scenario_only":
        if any(p is not None for p in values) or draft.refusal_reason is not None:
            raise ValueError("情景模式不接受数值或伪装成数值拒绝")
        return values, None
    if all(p is None for p in values):
        if not draft.refusal_reason:
            raise ValueError("拒绝数值估计必须说明原因")
        return values, None
    if any(p is None for p in values) or draft.refusal_reason is not None:
        raise ValueError("数值估计须完整，拒绝时须全部为空")
    numbers = [p for p in values if p is not None]
    if spec.distribution == "exclusive_exhaustive":
        total = math.fsum(numbers)
        if abs(total - 1) > PROBABILITY_TOLERANCE:
            raise ValueError("互斥穷尽概率总和不为1")
        if total != 1:
            return [p / total for p in numbers], total
    return values, None
