"""Release experiment contracts and deterministic baseline; no I/O or model execution."""

import re
from datetime import timedelta
from typing import Literal, Self
from uuid import UUID, uuid4

from pydantic import Field, model_validator

from future_agent.domain.contracts import Contract, Text, UtcTime, now
from future_agent.domain.events import CallAttempt, SourceText
from future_agent.domain.forecasts import (
    ForecastDraft,
    ForecastSpec,
    OutcomeDefinition,
    Probability,
    validate_judgment,
)
from future_agent.domain.lifecycle import Resolution, fingerprint, validate_resolution_window
from future_agent.domain.protocols import EvaluationProtocol, release_question
from future_agent.domain.research import ResearchRun

BASELINE_VERSION = "release-frequency-1.0"
DIRECT_VERSION = "direct-1.0"


def release_spec() -> ForecastSpec:
    return ForecastSpec(
        outcomes=[
            OutcomeDefinition(key="yes", definition="窗口内至少一次符合协议的稳定发布"),
            OutcomeDefinition(key="no", definition="完整窗口无符合协议的稳定发布"),
        ],
        distribution="exclusive_exhaustive",
        definition_review="依照锁定协议的项目稳定版、通道、公开时间及去重规则",
    )


class ReleaseRecord(Contract):
    version: Text
    product_channel: Text
    published_at: UtcTime
    draft: bool = False
    prerelease: bool = False
    excluded_reason: Text | None = None
    # Exact source quote is mandatory; project-specific classification is a human review.
    quote: Text

    def stable(self, channel: str) -> bool:
        return (
            self.product_channel == channel
            and not self.draft
            and not self.prerelease
            and not self.excluded_reason
            and not re.search(
                r"alpha|beta|(?:^|[\W\d])rc\d*|nightly|preview|dev|draft", self.version, re.I
            )
        )


class ReleaseHistory(Contract):
    document_id: UUID
    coverage_start: UtcTime
    coverage_end: UtcTime
    complete: bool
    reviewer: Text
    coverage_quote: Text
    records: list[ReleaseRecord]


def baseline(history: ReleaseHistory, cutoff: UtcTime, channel: str) -> list[int]:
    if (
        not history.complete
        or history.coverage_start > cutoff - timedelta(days=360)
        or history.coverage_end != cutoff
    ):
        raise ValueError("历史记录不足12个完整连续30天窗口")
    events = {}
    for record in history.records:
        key = (record.product_channel, re.sub(r"^v(?=\d)", "", record.version.strip().casefold()))
        signature = (record.published_at, record.draft, record.prerelease, record.excluded_reason)
        if key in events and events[key][0] != signature:
            raise ValueError("同一版本的发布时间或资格冲突，须先人工核对")
        events[key] = (signature, record)
        if record.published_at > cutoff:
            raise ValueError("历史记录含截止后发布")
    return [
        int(
            any(
                r.stable(channel)
                and cutoff - timedelta(days=30 * j)
                < r.published_at
                <= cutoff - timedelta(days=30 * (j - 1))
                for _, r in events.values()
            )
        )
        for j in range(1, 13)
    ]


class SealInput(Contract):
    project_id: UUID
    session_id: UUID
    revision: int = Field(ge=1)
    assessment_id: UUID
    history: ReleaseHistory
    spec: ForecastSpec | None = Field(default=None, exclude_if=lambda value: value is None)


class ExperimentSnapshot(SealInput):
    sealed_at: UtcTime = Field(default_factory=now)
    sources: list[SourceText]
    preparation_attempts: list[CallAttempt]
    research_runs: list[ResearchRun]
    history_text: Text
    c_input_hash: Text
    input_hash: Text

    def content_hash(self) -> str:
        return fingerprint(self.model_dump(mode="json", exclude={"input_hash"}))

    @model_validator(mode="after")
    def valid_hash(self) -> Self:
        if self.input_hash != self.content_hash():
            raise ValueError("实验输入哈希不一致")
        return self


class EvaluationPrediction(Contract):
    id: UUID = Field(default_factory=uuid4)
    submitted_at: UtcTime = Field(default_factory=now)
    probability: Probability | None
    method: Literal["release_frequency_baseline", "llm_judgment", "scenario_only"]
    normalization_original_sum: float | None = None
    draft: ForecastDraft | None = None
    frozen_forecast_id: UUID | None = None
    windows: list[int] | None = None
    input_hash: Text
    method_version: Text
    model_id: Text | None = None
    prompt_version: Text | None = None
    calibration_status: Literal["uncalibrated", "not_applicable"] = "uncalibrated"


class EvaluationEntry(Contract):
    project_id: UUID
    arm: Literal["A", "B", "C"]
    status: Literal[
        "waiting", "running", "submitted", "missing", "refused", "scenario", "late", "cancelled"
    ] = "waiting"
    reason: Text | None = None
    prediction: EvaluationPrediction | None = None
    step_id: UUID = Field(default_factory=uuid4)
    attempt_ids: list[UUID] = Field(default_factory=list)
    prior_attempt_ids: list[UUID] = Field(default_factory=list)
    started_at: UtcTime | None = None
    prediction_hash: Text | None = None

    @model_validator(mode="after")
    def valid_entry(self) -> Self:
        if self.status in {"submitted", "refused", "scenario"} and self.prediction is None:
            raise ValueError("成功、拒绝或情景条目必须保留产物")
        if (
            self.status in {"missing", "late", "refused", "scenario", "cancelled"}
            and not self.reason
        ):
            raise ValueError("缺失、迟交及无概率条目须保存原因")
        if self.prediction is not None:
            if self.prediction_hash != fingerprint(self.prediction.model_dump(mode="json")):
                raise ValueError("实验预测产物哈希不一致")
            if (self.status == "submitted") != (
                self.prediction.probability is not None
            ) and self.status != "late":
                raise ValueError("仅提交数值产物可进入数值覆盖率")
        elif self.prediction_hash is not None:
            raise ValueError("无产物不可伪造哈希")
        return self


class EvaluationReview(Contract):
    project_id: UUID
    arm: Literal["A", "B", "C"]
    reviewer: Text
    citation_errors: int = Field(ge=0, strict=True)
    fact_omissions: int = Field(ge=0, strict=True)
    reason: Text
    recorded_at: UtcTime = Field(default_factory=now)


class EvaluationRun(Contract):
    id: UUID  # One immutable first-forecast experiment per protocol.
    version: int = Field(default=1, ge=1)
    protocol: EvaluationProtocol
    created_at: UtcTime = Field(default_factory=now)
    snapshots: list[ExperimentSnapshot] = Field(default_factory=list)
    entries: list[EvaluationEntry]
    resolutions: dict[str, list[Resolution]] = Field(default_factory=dict)
    reviews: list[EvaluationReview] = Field(default_factory=list)

    @model_validator(mode="after")
    def complete_registry(self) -> Self:
        expected = {(p.id, a) for p in self.protocol.projects for a in ("A", "B", "C")}
        if (
            self.id != self.protocol.id
            or self.protocol.status != "locked"
            or len(self.entries) != len(expected)
            or {(e.project_id, e.arm) for e in self.entries} != expected
            or len({s.project_id for s in self.snapshots}) != len(self.snapshots)
            or not {s.project_id for s in self.snapshots} <= {p.id for p in self.protocol.projects}
        ):
            raise ValueError("实验须保留锁定协议的全部且唯一A/B/C条目")
        if self.created_at > self.protocol.evidence_cutoff or any(
            s.sealed_at > self.protocol.evidence_cutoff for s in self.snapshots
        ):
            raise ValueError("实验登记和输入封存不得晚于Tc")
        if len({s.session_id for s in self.snapshots}) != len(self.snapshots):
            raise ValueError("不同项目不得复用实验会话")
        if any(
            (self.protocol.schema_version == "2.0") != (s.spec is not None)
            or (s.spec is not None and s.spec.schema_version != "2.0")
            for s in self.snapshots
        ):
            raise ValueError("实验协议与封存定义版本不一致")
        methods = {m.arm: m for m in self.protocol.methods}
        snapshots = {s.project_id: s for s in self.snapshots}
        projects = {p.id: p for p in self.protocol.projects}
        for entry in self.entries:
            prediction = entry.prediction
            if prediction is None:
                continue
            method = methods[entry.arm]
            snap = snapshots.get(entry.project_id)
            if (
                snap is None
                or prediction.input_hash != snap.input_hash
                or prediction.method_version != method.method_version
                or prediction.model_id != method.model_id
                or prediction.prompt_version != method.prompt_version
                or (prediction.submitted_at > self.protocol.forecast_at and entry.status != "late")
            ):
                raise ValueError("实验预测输入、版本或共同提交期限不匹配协议")
            if prediction.calibration_status != (
                "not_applicable" if prediction.method == "scenario_only" else "uncalibrated"
            ):
                raise ValueError("情景不适用校准，所有数值方法均未校准")
            if (entry.arm == "C") != (prediction.frozen_forecast_id is not None):
                raise ValueError("C须关联既有冻结预测；A/B独立封存实验产物")
            if entry.arm == "A":
                if prediction.method != "release_frequency_baseline":
                    raise ValueError("A必须使用发布频率基线")
                windows = baseline(
                    snap.history,
                    self.protocol.evidence_cutoff,
                    projects[entry.project_id].product_channel,
                )
                if prediction.windows != windows or prediction.probability != sum(windows) / 12:
                    raise ValueError("A概率必须来自封存历史的12个窗口")
            else:
                if prediction.method not in {"llm_judgment", "scenario_only"} or (
                    entry.arm == "B" and prediction.method != "llm_judgment"
                ):
                    raise ValueError("B必须为直接主观判断，C为主观判断或条件情景")
                if prediction.draft is None:
                    raise ValueError("B/C须保留同一结构的完整输出")
                allowed = (
                    {
                        s
                        for o in prediction.draft.outcomes
                        for s in [*o.supporting_evidence, *o.opposing_evidence]
                    }
                    if entry.arm == "C"
                    else set()
                )
                method_name = prediction.method
                probabilities, original = validate_judgment(
                    prediction.draft,
                    snap.spec if entry.arm == "C" and snap.spec else release_spec(),
                    method_name,
                    allowed,
                )
                if (
                    prediction.probability != probabilities[0]
                    or prediction.normalization_original_sum != original
                ):
                    raise ValueError("实验概率与完整输出不一致")
        for project_id, history in self.resolutions.items():
            project = projects.get(UUID(project_id))
            if project is None or project.id not in snapshots:
                raise ValueError("核验项目未登记或无封存事件")
            previous = None
            step = next(
                e.step_id for e in self.entries if e.project_id == project.id and e.arm == "C"
            )
            for index, value in enumerate(history, 1):
                if (
                    value.forecast_id != step
                    or value.sequence != index
                    or value.previous_resolution_id != previous
                    or value.resolved_outcome not in {None, "yes", "no"}
                    or value.recorded_at < self.protocol.forecast_at
                ):
                    raise ValueError("项目核验历史关联或时点不一致")
                validate_resolution_window(
                    release_question(project, self.protocol.forecast_at), ["yes"], value
                )
                previous = value.id
        if any((r.project_id, r.arm) not in expected for r in self.reviews):
            raise ValueError("人工审查对象未预登记")
        return self
