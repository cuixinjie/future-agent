"""Immutable historical source data, independent of reference eligibility audits."""

from typing import Literal, Self
from uuid import UUID, uuid4

from pydantic import Field, model_validator

from future_agent.domain.contracts import Contract, Text, UtcTime, fingerprint, now
from future_agent.domain.evaluation import ReleaseHistory
from future_agent.domain.reference import ReferenceTarget


class HistoricalMaterial(Contract):
    url: Text
    raw: Text
    retrieved_at: UtcTime


class ReleaseDataset(Contract):
    id: UUID = Field(default_factory=uuid4)
    version: Text
    target: ReferenceTarget
    source_revision: Text
    license: Text
    selection_rule: Text
    review_origin: Literal["human", "fixture", "unreviewed"]
    reviewer: Text
    history: ReleaseHistory
    materials: list[HistoricalMaterial] = Field(min_length=1)
    imported_at: UtcTime = Field(default_factory=now)
    manifest_hash: Text

    def content_hash(self) -> str:
        return fingerprint(self.model_dump(mode="json", exclude={"manifest_hash"}))

    @model_validator(mode="after")
    def integrity(self) -> Self:
        if self.manifest_hash != self.content_hash():
            raise ValueError("数据集清单哈希不一致")
        if any(m.retrieved_at > self.imported_at for m in self.materials):
            raise ValueError("获取时间不能晚于入库时间")
        if self.history.coverage_end > min(m.retrieved_at for m in self.materials):
            raise ValueError("覆盖终点不能晚于资料获取")
        if self.history.coverage_start >= self.history.coverage_end:
            raise ValueError("历史覆盖时间无效")
        # Exact extraction provenance, including excluded records, remains auditable.
        for quote in [self.history.coverage_quote, *(r.quote for r in self.history.records)]:
            if not any(quote in m.raw for m in self.materials):
                raise ValueError("历史提取或覆盖审查缺少原文位置依据")
        return self
