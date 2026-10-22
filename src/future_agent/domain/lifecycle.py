"""Append-only forecast seals, verification history and resumable step inputs."""

import hashlib
import json
from typing import Literal, Self
from uuid import UUID, uuid4

from pydantic import Field, model_validator

from future_agent.domain.contracts import Contract, Text, UtcTime, now
from future_agent.domain.evidence import EvidenceDocument, EvidenceSpan
from future_agent.domain.forecasts import PROBABILITY_TOLERANCE, Forecast, ForecastSpec


def fingerprint(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class FrozenForecast(Contract):
    forecast: Forecast
    frozen_at: UtcTime = Field(default_factory=now)
    supersedes_forecast_id: UUID | None = None
    revision_reason: Text | None = None
    early_resolution_outcomes: list[Text] = Field(default_factory=list)
    input_hash: Text
    calculation_parameters: dict[str, float] = Field(
        default_factory=lambda: {"probability_tolerance": PROBABILITY_TOLERANCE}
    )

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if self.input_hash != fingerprint(self.forecast.model_dump(mode="json")):
            raise ValueError("冻结内容哈希不一致")
        if self.frozen_at < self.forecast.created_at:
            raise ValueError("冻结时间不能早于预测创建时间")
        if (self.supersedes_forecast_id is None) != (self.revision_reason is None):
            raise ValueError("修订必须同时记录旧预测与理由")
        if self.supersedes_forecast_id == self.forecast.id:
            raise ValueError("预测不能修订自身")
        if len(set(self.early_resolution_outcomes)) != len(
            self.early_resolution_outcomes
        ) or not set(self.early_resolution_outcomes) <= {
            o.key for o in self.forecast.spec.outcomes
        }:
            raise ValueError("可提前核验的发生结果必须来自冻结结果定义且不得重复")
        if self.calculation_parameters != {"probability_tolerance": PROBABILITY_TOLERANCE}:
            raise ValueError("计算参数不匹配当前方法")
        return self


class ResolutionInput(Contract):
    status: Literal["pending", "resolved", "disputed", "void"]
    resolved_outcome: Text | None = None
    evidence_ids: list[UUID] = Field(default_factory=list)
    resolver: Text
    reason: Text
    observed_at: UtcTime | None = None

    @model_validator(mode="after")
    def outcome_status(self) -> Self:
        if (self.status == "resolved") != (self.resolved_outcome is not None):
            raise ValueError("只有 resolved 必须且可以填写结果；未决不得充当负例")
        if self.status == "resolved" and not self.evidence_ids:
            raise ValueError("核验结果必须附正文引用证据")
        if len(set(self.evidence_ids)) != len(self.evidence_ids):
            raise ValueError("结果证据不能重复")
        return self


class Resolution(ResolutionInput):
    id: UUID = Field(default_factory=uuid4)
    forecast_id: UUID
    previous_resolution_id: UUID | None = None
    sequence: int = Field(ge=1)
    recorded_at: UtcTime = Field(default_factory=now)
    resolved_at: UtcTime | None = None
    evidence_documents: list[EvidenceDocument]
    evidence_spans: list[EvidenceSpan]

    @model_validator(mode="after")
    def snapshot_consistency(self) -> Self:
        if self.resolved_at != (self.recorded_at if self.status == "resolved" else None):
            raise ValueError("只有已核验记录保存 resolved_at，且须等于本次登记时间")
        if (self.sequence == 1) != (self.previous_resolution_id is None):
            raise ValueError("核验序号与前驱不一致")
        documents = {d.id: d for d in self.evidence_documents}
        if (
            len(documents) != len(self.evidence_documents)
            or {s.id for s in self.evidence_spans} != set(self.evidence_ids)
            or len(self.evidence_spans) != len(self.evidence_ids)
            or set(documents) != {s.document_id for s in self.evidence_spans}
            or any(d.source.access_status != "read_success" for d in documents.values())
        ):
            raise ValueError("核验正文快照与引用不一致")
        if self.observed_at and self.observed_at > self.recorded_at:
            raise ValueError("不能核验未来发生的结果")
        return self


def validate_supersession(previous: FrozenForecast, updated: FrozenForecast) -> None:
    old, new = previous.forecast, updated.forecast
    fields = {
        "question",
        "window_start",
        "window_end",
        "outcome_type",
        "resolution_rule",
        "resolution_source_policy",
    }
    if (
        updated.supersedes_forecast_id != old.id
        or old.session_id != new.session_id
        or old.created_at >= new.created_at
        or previous.frozen_at > updated.frozen_at
        or old.event.question.model_dump(include=fields)
        != new.event.question.model_dump(include=fields)
        or old.spec != new.spec
        or previous.early_resolution_outcomes != updated.early_resolution_outcomes
    ):
        raise ValueError("修订必须来自同一会话且晚于旧预测；目标、窗口和规则改变须建立独立预测")


def validate_resolution(frozen: FrozenForecast, value: Resolution) -> None:
    forecast = frozen.forecast
    if (
        value.forecast_id != forecast.id
        or value.recorded_at < frozen.frozen_at
        or any(d.event_revision_id != forecast.event.id for d in value.evidence_documents)
    ):
        raise ValueError("核验必须绑定冻结预测及其事件版本")
    if value.status != "resolved":
        return
    if value.resolved_outcome not in {o.key for o in forecast.spec.outcomes}:
        raise ValueError("结果必须对应冻结的结果定义")
    question = forecast.event.question
    if (
        value.observed_at is not None
        and not question.window_start < value.observed_at <= question.window_end
    ):
        raise ValueError("发生时间必须在冻结的观察窗口内")
    if value.recorded_at < question.window_end:
        if (
            value.resolved_outcome not in frozen.early_resolution_outcomes
            or value.observed_at is None
            or not question.window_start
            < value.observed_at
            <= min(question.window_end, value.recorded_at)
        ):
            raise ValueError("窗口未结束；仅预先指定的发生结果可凭窗口内发生时间提前核验")


class ForecastStep(Contract):
    id: UUID = Field(default_factory=uuid4)
    session_id: UUID
    revision: int = Field(ge=1)
    assessment_id: UUID
    spec: ForecastSpec
    numeric: bool
    model_id: Text
    structured_mode: Text
    output_token_limit: int = Field(ge=1)
    prompt_version: Text
    method_version: Text
    status: Literal["ready", "running", "completed", "cancelled"] = "ready"
    version: int = Field(default=1, ge=1)
    forecast_id: UUID | None = None
    created_at: UtcTime = Field(default_factory=now)

    @model_validator(mode="after")
    def completion(self) -> Self:
        if (self.status == "completed") != (self.forecast_id is not None):
            raise ValueError("仅完成的步骤可以且必须关联预测产物")
        return self
