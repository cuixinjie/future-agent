"""Retrospective diagnostic inputs and labels have separate typed contracts."""

from typing import Literal, Self
from uuid import UUID, uuid4

from pydantic import Field, model_validator

from future_agent.domain.contracts import Contract, Text, UtcTime


class HistoricalQuestion(Contract):
    source_key: Text
    event_family: Text
    split: Literal["development", "selection", "sealed_test"]
    question: Text
    rule: Text
    rule_version: Text
    cutoff: UtcTime
    window_end: UtcTime
    # Only reviewed, pre-event material belongs here; live search is not supported.
    prior_evidence: list[Text] = Field(default_factory=list)
    visibility_basis: Text | None = None


class HistoricalLabel(Contract):
    source_key: Text
    status: Literal["yes", "no", "unresolved", "disputed", "void"]
    known_at: UtcTime | None = None
    evidence: Text | None = None

    @model_validator(mode="after")
    def resolved(self) -> Self:
        if self.status in {"yes", "no"} and (self.known_at is None or not self.evidence):
            raise ValueError("已解决标签须保留首次公开时间与结果依据")
        return self


class DiagnosticBundle(Contract):
    id: UUID = Field(default_factory=uuid4)
    purpose: Literal["retrospective_diagnostic"] = "retrospective_diagnostic"
    source_commit: Text
    license: Text
    attribution: Text
    selection_rule: Text
    review_origin: Literal["human", "fixture", "unreviewed"]
    questions: list[HistoricalQuestion] = Field(min_length=1, max_length=50)
    labels: list[HistoricalLabel]

    @model_validator(mode="after")
    def separation(self) -> Self:
        keys = {q.source_key for q in self.questions}
        if (
            len(keys) != len(self.questions)
            or {v.source_key for v in self.labels} != keys
            or len(self.labels) != len(keys)
        ):
            raise ValueError("问题与结果必须逐一对应，未知不能漏登记")
        families = {}
        for q in self.questions:
            if q.cutoff >= q.window_end:
                raise ValueError("预测时点必须早于窗口终点")
            if q.event_family in families and families[q.event_family] != q.split:
                raise ValueError("同事件族不能跨数据切分")
            families[q.event_family] = q.split
            if q.prior_evidence and not q.visibility_basis:
                raise ValueError("事前材料必须声明历史可见性依据")
        return self

    def prediction_inputs(self, split: str) -> list[dict]:
        # Construct from an allowlist, never serialize a row and remove its label.
        return [
            dict(
                source_key=q.source_key,
                event_family=q.event_family,
                question=q.question,
                rule=q.rule,
                rule_version=q.rule_version,
                cutoff=q.cutoff.isoformat(),
                window_end=q.window_end.isoformat(),
                prior_evidence=q.prior_evidence if self.review_origin == "human" else [],
                mode="retrospective_diagnostic",
                live_search=False,
                limitation="历史回放存在模型记忆及文本答案暗示风险；不是独立前瞻",
            )
            for q in self.questions
            if q.split == split
        ]
