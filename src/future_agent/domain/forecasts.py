"""Reviewed outcome definitions and validated, explicitly uncalibrated forecasts."""

import math
import re
from typing import Annotated, Literal, Self
from uuid import UUID, uuid4

from pydantic import Field, model_validator

from future_agent.domain.assessment import EvidenceAssessment
from future_agent.domain.assistance import AnalysisPlan, JudgmentSummary
from future_agent.domain.case_reference import CaseEstimate
from future_agent.domain.contracts import Contract, Text, UtcTime, now
from future_agent.domain.events import EventRevision
from future_agent.domain.evidence import EvidenceDocument, EvidenceSpan
from future_agent.domain.reference import ReferenceBinding

FORECAST_METHOD_VERSION = "judgment-1.0"
PROBABILITY_TOLERANCE = 1e-6
Probability = Annotated[float, Field(ge=0, le=1, strict=True)]


class OutcomeDefinition(Contract):
    key: Text
    definition: Text


class ForecastSpec(Contract):
    schema_version: Literal["1.0", "2.0"] = "1.0"
    outcomes: list[OutcomeDefinition] = Field(min_length=2, max_length=12)
    distribution: Literal["independent", "exclusive_exhaustive"] = "independent"
    definition_review: Text
    guidance: AnalysisPlan | None = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def unique(self) -> Self:
        if (self.schema_version == "2.0") != (self.guidance is not None):
            raise ValueError("结果定义版本与自动分析上下文不一致")
        if self.guidance and (
            not self.guidance.usable()
            or [(o.key, o.definition) for o in self.guidance.outcomes]
            != [(o.key, o.definition) for o in self.outcomes]
        ):
            raise ValueError("结果定义必须与本次目标建议一致")
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


class DecisionReview(Contract):
    strongest_counterargument: Text
    synthesis_reason: Text
    conditional_factors: list[Text] = Field(default_factory=list, max_length=5)


class ForecastDraft(Contract):
    schema_version: Literal["1.0", "2.0"] = "1.0"
    outcomes: list[OutcomeJudgment] = Field(min_length=2, max_length=12)
    assumptions: list[Text]
    decision_review: DecisionReview | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    refusal_reason: Text | None = None
    judgment: JudgmentSummary | None = Field(default=None, exclude_if=lambda value: value is None)


def forecast_schema(*, assisted: bool = False, useful: bool = False) -> dict:
    schema = ForecastDraft.model_json_schema()
    version = "2.0" if assisted else "1.0"
    schema["properties"]["schema_version"] = {
        "const": version,
        "default": version,
        "title": "Schema Version",
        "type": "string",
    }
    if useful:
        schema.setdefault("required", []).append("decision_review")
    else:
        schema["properties"].pop("decision_review", None)
        schema.get("$defs", {}).pop("DecisionReview", None)
    if not assisted:
        schema["properties"].pop("judgment", None)
        schema.get("$defs", {}).pop("JudgmentSummary", None)
    return schema


class Forecast(Contract):
    reference_binding: ReferenceBinding | None = Field(default=None, exclude_if=lambda v: v is None)
    schema_version: Literal["1.0", "2.0", "2.1", "2.2"] = "1.0"
    id: UUID = Field(default_factory=uuid4)
    run_id: UUID = Field(default_factory=uuid4)
    session_id: UUID
    event: EventRevision
    assessment: EvidenceAssessment
    spec: ForecastSpec
    method: Literal["scenario_only", "llm_judgment"]
    method_version: Literal[
        "judgment-1.0",
        "judgment-2.0",
        "judgment-2.1",
        "judgment-reference-1.0",
        "judgment-reference-1.1",
        "judgment-case-reference-1.0",
    ] = FORECAST_METHOD_VERSION
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
        if (
            self.method_version
            in {"judgment-reference-1.0", "judgment-reference-1.1", "judgment-case-reference-1.0"}
        ) != (self.reference_binding is not None):
            raise ValueError("参照方法必须绑定封存输入")
        if self.reference_binding is not None:
            b = self.reference_binding
            if (self.method_version == "judgment-case-reference-1.0") != isinstance(
                b.estimate, CaseEstimate
            ):
                raise ValueError("案例参照与发布参照不得混用方法版本")
            if (self.method_version == "judgment-reference-1.1") != (
                b.estimate.method_version == "release-reference-1.2"
            ):
                raise ValueError("贝叶斯参照必须绑定对应预测方法版本")
            if (
                self.method_version == "judgment-reference-1.1"
                and self.prompt_version != "forecast-reference-1.1"
            ):
                raise ValueError("贝叶斯参照必须绑定对应提示词版本")
            if (
                b.event_revision_id != self.event.id
                or b.estimate.cutoff != self.evidence_cutoff
                or b.estimate.window_start != self.event.question.window_start
                or b.estimate.window_end != self.event.question.window_end
                or self.spec.guidance is None
                or b.estimate.target != self.spec.guidance.reference_target
            ):
                raise ValueError("参照绑定必须属于当前事件与截止")
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
        if (
            self.method_version
            in {
                "judgment-2.0",
                "judgment-2.1",
                "judgment-reference-1.0",
                "judgment-reference-1.1",
                "judgment-case-reference-1.0",
            }
        ) != (self.spec.guidance is not None):
            raise ValueError("预测方法版本与交付契约不一致")
        if self.schema_version != (
            "2.2"
            if self.method_version == "judgment-case-reference-1.0"
            else "2.1"
            if self.reference_binding
            else self.spec.schema_version
        ):
            raise ValueError("预测版本与结果定义版本不一致")
        if self.spec.guidance:
            if self.assessment.experiment or self.assessment.assessor_version not in {
                "evidence-assessor-2.0",
                "evidence-assessor-2.1",
            }:
                raise ValueError("普通新版判断必须使用新版普通评估")
            for name in ("question", "outcome_type", "resolution_rule", "resolution_source_policy"):
                if getattr(self.spec.guidance, name) != getattr(self.event.question, name):
                    raise ValueError("目标建议与实际冻结问题不一致")
        if (
            self.method_version
            in {
                "judgment-2.1",
                "judgment-reference-1.0",
                "judgment-reference-1.1",
                "judgment-case-reference-1.0",
            }
        ) != (self.assessment.assessor_version == "evidence-assessor-2.1"):
            raise ValueError("最终交付审查必须绑定对应评估版本")
        if self.method_version in {
            "judgment-2.1",
            "judgment-reference-1.0",
            "judgment-reference-1.1",
            "judgment-case-reference-1.0",
        }:
            validate_decision_review(self.draft, self.spec)
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
    if draft.schema_version != spec.schema_version:
        raise ValueError("输出合同版本与定义不一致")
    if spec.guidance is None and draft.judgment is not None:
        raise ValueError("旧方法不能混入新版判断字段")
    if [o.key for o in draft.outcomes] != [o.key for o in spec.outcomes]:
        raise ValueError("输出结果必须按审查定义逐项对应，不得新增、遗漏或重定义")
    for outcome in draft.outcomes:
        if not set(outcome.supporting_evidence + outcome.opposing_evidence) <= allowed_spans:
            raise ValueError("只能引用通过审查与截止资格的正文片段")
    values = [o.probability for o in draft.outcomes]
    if spec.guidance is not None:
        summary = draft.judgment
        if summary is None:
            raise ValueError("新版预测必须交付主判断或具体无法判断原因")
        keys = {o.key for o in spec.outcomes}
        if len(set(summary.ranking)) != len(summary.ranking) or not set(summary.ranking) <= keys:
            raise ValueError("排序只能引用有效且不重复的结果")
        decisive = summary.delivery in {"probability", "directional"}
        if decisive:
            if set(summary.ranking) != keys or summary.primary_outcome != summary.ranking[0]:
                raise ValueError("明确判断须包含完整排序与首选结果")
        elif summary.ranking or summary.primary_outcome is not None:
            raise ValueError("条件分析、探索或无法判断不得冒充总体排序")
        numeric = all(p is not None for p in values)
        if numeric != (summary.delivery == "probability"):
            raise ValueError("交付类型必须与实际概率一致")
        if numeric:
            ordered = {o.key: o.probability for o in draft.outcomes}
            if any(
                ordered[a] < ordered[b]
                for a, b in zip(summary.ranking, summary.ranking[1:], strict=False)
            ):
                raise ValueError("排序与概率矛盾")
        elif not summary.no_numeric_reason:
            raise ValueError("无数字必须说明原因")
        if spec.guidance.analysis_kind == "exploration" and summary.delivery != "exploration":
            raise ValueError("开放探索不得冒充预测")
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


def validate_decision_review(draft: ForecastDraft, spec: ForecastSpec) -> None:
    """Validate new generation only; old frozen forecasts retain their original rules.

    These checks enforce structure and explicit contradictions, not semantic truth.
    """
    review, summary = draft.decision_review, draft.judgment
    if review is None or summary is None:
        raise ValueError("最终判断必须包含反面解释与交付依据审查")
    if summary.delivery == "conditional":
        factors = [f.strip() for f in review.conditional_factors]
        if len(set(factors)) < 2:
            raise ValueError("条件分析须列出至少两个不同的驱动分支条件")
        definitions = {o.definition.strip() for o in spec.outcomes}
        if any(f.strip() in definitions for f in factors):
            raise ValueError("分支条件不能重复结果定义")
        if re.search(r"(?:默认(?:分支|选择)|首选结果)(?:为|是)", summary.conclusion):
            raise ValueError("存在总体首选时应交付方向判断，不能冒充条件分析")
    elif review.conditional_factors:
        raise ValueError("非条件分析不得混入条件分支清单")
    texts = [
        summary.conclusion,
        summary.reference_basis,
        summary.differences_and_adjustment,
        summary.no_numeric_reason or "",
        draft.refusal_reason or "",
        *summary.key_drivers,
        *summary.unknown_effects,
        *summary.change_conditions,
        review.strongest_counterargument,
        review.synthesis_reason,
        *review.conditional_factors,
        *(o.reasoning for o in draft.outcomes),
        *(t for o in draft.outcomes for t in [*o.conditions, *o.watch_signals]),
    ]
    if any(
        re.search(
            r"(?<![A-Za-z0-9_])(?:scenario_only|reviewed_evidence|UUID|conditional_only)(?![A-Za-z0-9_])",
            t,
        )
        for t in texts
    ):
        raise ValueError("用户判断须用自然语言解释依据，不得暴露内部资格字段")
