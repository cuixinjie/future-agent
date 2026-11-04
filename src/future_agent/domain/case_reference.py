"""Small, source-backed retrospective cases; no model or persistence dependencies."""

import re
from typing import Literal, Self
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from pydantic import Field, model_validator

from future_agent.domain.contracts import Contract, Text, UtcTime, fingerprint, now


class CaseTarget(Contract):
    definition: Literal["mens-world-cup-final-extra-time-1.0", "us-election-party-retention-1.0"]
    edition: int = Field(ge=1900, le=2199)

    @property
    def domain(self) -> str:
        return "sports" if self.definition.startswith("mens-") else "politics"

    @property
    def event_family(self) -> str:
        return f"{self.definition}:{self.edition}"


class CaseSource(Contract):
    url: Text
    retrieved_at: UtcTime
    text: Text
    content_hash: Text
    capture_provider: Text
    attribution: Text
    reuse: Literal["local_audit_only"] = "local_audit_only"

    @model_validator(mode="after")
    def integrity(self) -> Self:
        import hashlib

        host = urlsplit(self.url)
        if host.scheme != "https" or host.hostname not in {
            "www.archives.gov",
            "www.uefa.com",
            "hns.team",
            "www.thefa.com",
            "www.fff.fr",
        }:
            raise ValueError("案例来源必须为登记的官方HTTPS来源")
        if host.username or host.password or host.port not in {None, 443}:
            raise ValueError("案例来源地址不合格")
        if self.content_hash != hashlib.sha256(self.text.encode()).hexdigest():
            raise ValueError("案例来源哈希不一致")
        return self


class CaseFact(Contract):
    source_hash: Text
    quote: Text = Field(max_length=1200)
    # A conservative historical bound for the FACT, not the current page version.
    available_by: UtcTime
    availability_basis: Text


class HistoricalCase(Contract):
    key: Text
    target: CaseTarget
    question: Text
    rule: Text
    cutoff: UtcTime
    window_end: UtcTime
    prior_facts: list[CaseFact] = Field(min_length=1, max_length=5)
    outcome: Literal["yes", "no"]
    outcome_facts: list[CaseFact] = Field(min_length=1, max_length=5)
    outcome_available_by: UtcTime
    review_origin: Literal["program_source_checked"] = "program_source_checked"
    visibility: Literal["retrospective_fact_reconstruction"] = "retrospective_fact_reconstruction"

    @model_validator(mode="after")
    def chronology(self) -> Self:
        if not self.cutoff < self.window_end <= self.outcome_available_by:
            raise ValueError("案例截止、结果窗口和结果可知时间顺序错误")
        if any(f.available_by >= self.cutoff for f in self.prior_facts):
            raise ValueError("事前事实必须在案例截止前已知")
        if any(f.available_by > self.outcome_available_by for f in self.outcome_facts):
            raise ValueError("结果可知时间不能早于结果依据")
        return self


class CaseDataset(Contract):
    id: UUID = Field(default_factory=uuid4)
    version: Text
    domain: Literal["sports", "politics"]
    selection_rule: Text
    extraction_version: Literal["domain-case-extraction-1.0"] = "domain-case-extraction-1.0"
    sources: list[CaseSource] = Field(min_length=1, max_length=30)
    cases: list[HistoricalCase] = Field(min_length=1, max_length=20)
    imported_at: UtcTime = Field(default_factory=now)
    manifest_hash: Text

    @model_validator(mode="after")
    def integrity(self) -> Self:
        if self.manifest_hash != fingerprint(
            self.model_dump(mode="json", exclude={"manifest_hash"})
        ):
            raise ValueError("案例数据集清单哈希不一致")
        sources = {s.content_hash: s for s in self.sources}
        if len(sources) != len(self.sources) or any(
            s.retrieved_at > self.imported_at for s in self.sources
        ):
            raise ValueError("案例来源重复或获取晚于入库")
        if len({c.key for c in self.cases}) != len(self.cases) or len(
            {c.target.event_family for c in self.cases}
        ) != len(self.cases):
            raise ValueError("案例或事件族重复")
        for case in self.cases:
            if case.target.domain != self.domain:
                raise ValueError("案例领域不匹配")
            for fact in [*case.prior_facts, *case.outcome_facts]:
                source = sources.get(fact.source_hash)
                if source is None or source.text.count(fact.quote) != 1:
                    raise ValueError("案例事实须有唯一原文位置")
                if fact.available_by > source.retrieved_at:
                    raise ValueError("不能用现有资料证明尚未来临的可知时间")
            if case.target.domain == "politics":
                parties = []
                for facts, year in [
                    (case.prior_facts, case.target.edition - 4),
                    (case.outcome_facts, case.target.edition),
                ]:
                    matches = [
                        re.search(r"President \| [^|\n]+\[([RD])\]", f.quote)
                        for f in facts
                        if sources[f.source_hash].url
                        == f"https://www.archives.gov/electoral-college/{year}"
                    ]
                    matches = [m for m in matches if m]
                    if len(matches) != 1:
                        raise ValueError("总统选举标签须由对应届次官方胜者党派复算")
                    parties.append(matches[0].group(1))
                expected = "yes" if parties[0] == parties[1] else "no"
            else:
                quotes = "\n".join(f.quote for f in case.outcome_facts)
                if re.search(r"\d+[-–]\d+", quotes) and re.search(
                    r"\(aet\)|\d+[-–]\d+aet\b", quotes.lower()
                ):
                    expected = "yes"
                else:
                    score = re.search(r"\b(\d+):(\d+)\b", quotes)
                    minutes = re.findall(r"\((\d+)'", quotes)
                    if (
                        score is None
                        or not minutes
                        or max(map(int, minutes)) > 90
                        or len(minutes) != int(score[1]) + int(score[2])
                    ):
                        raise ValueError("无加时标签须有完整正常时间进球记录")
                    expected = "no"
            if case.outcome != expected:
                raise ValueError("案例标签与官方原文字段不一致")
        return self

    def replay_input(self, key: str) -> dict:
        case = next(c for c in self.cases if c.key == key)
        # Never supply raw source pages: a present-day page may disclose later events.
        return dict(
            key=case.key,
            target=case.target.model_dump(mode="json"),
            question=case.question,
            rule=case.rule,
            cutoff=case.cutoff.isoformat(),
            window_end=case.window_end.isoformat(),
            prior_facts=[f.model_dump(mode="json") for f in case.prior_facts],
            mode="retrospective_fact_reconstruction",
            live_search=False,
            limitation="今天重建的历史事实，不是当时网页快照；模型记忆和后见偏差仍存在",
        )


class CaseView(Contract):
    key: Text
    event_family: Text
    question: Text
    prior_facts: list[Text]
    outcome: Literal["yes", "no"]
    outcome_available_by: UtcTime
    source_urls: list[Text]


class CaseEstimate(Contract):
    method_version: Literal["case-reference-1.0"] = "case-reference-1.0"
    generated_at: UtcTime = Field(default_factory=now)
    status: Literal["qualitative_only", "not_applicable", "failed"]
    reason: Text
    target: CaseTarget
    cutoff: UtcTime
    window_start: UtcTime
    window_end: UtcTime
    dataset_id: UUID | None = None
    dataset_hash: Text | None = None
    cases: list[CaseView] = Field(default_factory=list, max_length=20)
    sources: list[Text] = Field(default_factory=list)
    excluded: list[Text] = Field(default_factory=list)
    limitations: list[Text] = Field(
        default_factory=lambda: [
            "小样本定性参照，不计算基础概率，不授予数值资格",
            "程序核对官方来源事实，未经人工独立批准；当时页面版本未恢复",
            "案例按固定连续届次选取，不能代表整个体育或政治领域",
            "同届、相邻届与共享主体可能相关；模型映射仍需语义审查",
        ]
    )

    @model_validator(mode="after")
    def safe_projection(self) -> Self:
        if (self.status == "qualitative_only") != bool(self.cases):
            raise ValueError("定性参照必须含合格案例；空结果不得冒充成功")
        if self.status != "qualitative_only" and self.cases:
            raise ValueError("失败或不适用不能携带案例")
        if self.cases and (not self.dataset_id or not self.dataset_hash):
            raise ValueError("案例参照须绑定数据集")
        if any(
            c.event_family == self.target.event_family or c.outcome_available_by >= self.cutoff
            for c in self.cases
        ):
            raise ValueError("目标事件或截止后结果不能进入参照")
        return self


def estimate_cases(
    target: CaseTarget,
    datasets: list[CaseDataset],
    cutoff: UtcTime,
    start: UtcTime,
    end: UtcTime,
    *,
    retrospective: bool = False,
) -> CaseEstimate:
    excluded = []
    for dataset in sorted(datasets, key=lambda d: (d.imported_at, str(d.id)), reverse=True):
        if dataset.domain != target.domain:
            continue
        if not retrospective and (
            dataset.imported_at > cutoff or any(s.retrieved_at > cutoff for s in dataset.sources)
        ):
            excluded.append(f"{dataset.id}: 截止后获取或入库")
            continue
        sources = {s.content_hash: s for s in dataset.sources}
        views = []
        for case in dataset.cases:
            if case.target.definition != target.definition:
                continue
            if (
                case.target.event_family == target.event_family
                or case.outcome_available_by >= cutoff
            ):
                excluded.append(f"{case.key}: 同一事件或结果尚不可知")
                continue
            views.append(
                CaseView(
                    key=case.key,
                    event_family=case.target.event_family,
                    question=case.question,
                    prior_facts=[f.quote for f in case.prior_facts],
                    outcome=case.outcome,
                    outcome_available_by=case.outcome_available_by,
                    source_urls=sorted(
                        {
                            sources[f.source_hash].url
                            for f in [*case.prior_facts, *case.outcome_facts]
                        }
                    ),
                )
            )
        if views:
            return CaseEstimate(
                status="qualitative_only",
                reason="官方来源历史案例（定性参照）",
                target=target,
                cutoff=cutoff,
                window_start=start,
                window_end=end,
                dataset_id=dataset.id,
                dataset_hash=dataset.manifest_hash,
                cases=views,
                sources=sorted({u for c in views for u in c.source_urls}),
                excluded=excluded,
            )
    return CaseEstimate(
        status="not_applicable",
        reason="没有截止前可用且排除同事件的历史案例",
        target=target,
        cutoff=cutoff,
        window_start=start,
        window_end=end,
        excluded=excluded,
    )
