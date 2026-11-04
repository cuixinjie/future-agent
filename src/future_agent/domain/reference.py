"""Versioned, prediction-safe reference projections. No labels or storage access."""

from typing import Literal, Self
from uuid import UUID

from pydantic import Field, model_validator

from future_agent.domain.case_reference import CaseEstimate
from future_agent.domain.contracts import Contract, Text, UtcTime, fingerprint, now

REFERENCE_VERSION = "release-reference-1.2"
MAX_ANCHOR_DELAY_SECONDS = 3601


class ReleasePosterior(Contract):
    """Fixed uniform prior; descriptive model, not independently calibrated."""

    method_version: Literal["beta-binomial-uniform-1.0"] = "beta-binomial-uniform-1.0"
    prior_alpha: float = Field(default=1.0, strict=True, ge=1, le=1)
    prior_beta: float = Field(default=1.0, strict=True, ge=1, le=1)
    successes: int = Field(strict=True, ge=0, le=12)
    trials: int = Field(default=12, strict=True, ge=12, le=12)
    posterior_alpha: float = Field(strict=True, gt=0)
    posterior_beta: float = Field(strict=True, gt=0)
    predictive_probability: float = Field(strict=True, gt=0, lt=1)

    @model_validator(mode="after")
    def reproducible(self) -> Self:
        alpha = self.prior_alpha + self.successes
        beta = self.prior_beta + self.trials - self.successes
        if (
            self.posterior_alpha != alpha
            or self.posterior_beta != beta
            or self.predictive_probability != alpha / (alpha + beta)
        ):
            raise ValueError("贝叶斯参照必须与先验、样本及后验计算一致")
        return self

    @classmethod
    def from_successes(cls, successes: int) -> Self:
        # Validate the observed count before deriving any numeric result.
        if type(successes) is not int or not 0 <= successes <= 12:
            raise ValueError("发布参照需要12个合格窗口的真实发生计数")
        alpha, beta = 1.0 + successes, 1.0 + 12 - successes
        return cls(
            successes=successes,
            posterior_alpha=alpha,
            posterior_beta=beta,
            predictive_probability=alpha / (alpha + beta),
        )


class ReferenceTarget(Contract):
    repository: Text
    product: Text
    channel: Text
    definition: Literal["github-stable-release-30d-1.0"]


class ReferenceEstimate(Contract):
    generated_at: UtcTime = Field(default_factory=now)
    source_audit_version: Text | None = Field(default=None, exclude_if=lambda v: v is None)
    method_version: Literal[
        "release-reference-1.0", "release-reference-1.1", "release-reference-1.2"
    ] = REFERENCE_VERSION
    status: Literal["computed", "qualitative_only", "not_applicable", "failed"]
    reason: Text
    target: ReferenceTarget | None = None
    dataset_id: UUID | None = None
    dataset_hash: Text | None = None
    cutoff: UtcTime
    history_anchor: UtcTime | None = None
    window_start: UtcTime
    window_end: UtcTime
    anchor_delay_seconds: float | None = None
    labels: list[Literal[0, 1]] = Field(default_factory=list)
    numerator: int | None = None
    denominator: int | None = None
    frequency: float | None = None
    bayesian_estimate: ReleasePosterior | None = Field(default=None, exclude_if=lambda v: v is None)
    limitations: list[Text] = Field(default_factory=list)
    sources: list[Text] = Field(default_factory=list)
    excluded: list[Text] = Field(default_factory=list)

    @model_validator(mode="after")
    def computed_values(self) -> Self:
        needs_posterior = (
            self.method_version == "release-reference-1.2" and self.status == "computed"
        )
        if needs_posterior != (self.bayesian_estimate is not None):
            raise ValueError("新版合格参照必须含贝叶斯估计；旧版、缺失及失败不得补入估计")
        if self.bayesian_estimate is not None and (
            self.bayesian_estimate.successes != self.numerator
            or self.bayesian_estimate.trials != self.denominator
        ):
            raise ValueError("贝叶斯样本必须对应原始历史窗口")
        if self.status == "computed":
            if (
                len(self.labels) != 12
                or self.numerator != sum(self.labels)
                or self.denominator != 12
                or self.frequency != sum(self.labels) / 12
                or not self.dataset_id
                or not self.dataset_hash
                or not self.target
                or self.history_anchor is None
                or self.history_anchor > self.cutoff
                or self.window_start <= self.cutoff
                or self.anchor_delay_seconds
                != (self.window_start - self.history_anchor).total_seconds()
                or not 0 < self.anchor_delay_seconds <= MAX_ANCHOR_DELAY_SECONDS
            ):
                raise ValueError("参照必须保留可复算的12窗口及锁定输入")
        elif self.labels or any(
            v is not None for v in (self.numerator, self.denominator, self.frequency)
        ):
            raise ValueError("缺失或失败不得填补统计值")
        return self


class ReferenceBinding(Contract):
    event_revision_id: UUID
    estimate: ReferenceEstimate | CaseEstimate
    input_hash: Text

    @model_validator(mode="after")
    def integrity(self) -> Self:
        if self.input_hash != fingerprint(self.model_dump(mode="json", exclude={"input_hash"})):
            raise ValueError("参照绑定哈希不一致")
        return self
