"""Local immutable history import and as-of reference use cases."""

from typing import Protocol
from uuid import UUID

from future_agent.application.ports import AppError, Conflict
from future_agent.domain.assistance import AnalysisPlan
from future_agent.domain.case_reference import CaseDataset, CaseEstimate, CaseTarget, estimate_cases
from future_agent.domain.contracts import UtcTime, fingerprint
from future_agent.domain.reference import ReferenceBinding, ReferenceEstimate, ReferenceTarget
from future_agent.domain.release_reference import HistoricalMaterial as HistoricalMaterial
from future_agent.domain.release_reference import ReleaseDataset as ReleaseDataset
from future_agent.domain.release_reference import estimate_release_reference


class HistoricalReferenceRepository(Protocol):
    def import_dataset(self, dataset: ReleaseDataset) -> ReleaseDataset: ...
    def datasets(self, target: ReferenceTarget) -> list[ReleaseDataset]: ...
    def import_cases(self, dataset: CaseDataset) -> CaseDataset: ...
    def case_datasets(self, domain: str) -> list[CaseDataset]: ...


class ReferenceService:
    def __init__(self, repository: HistoricalReferenceRepository) -> None:
        self.repository = repository

    def import_dataset(self, value: ReleaseDataset) -> ReleaseDataset:
        # Never trust a file's claimed local import time; idempotency is handled by the repository.
        if value.review_origin == "human":
            from future_agent.domain.reference_quality import audit_release_dataset

            audit = audit_release_dataset(value)
            if audit.machine_status != "passed":
                raise Conflict("人工声明不能绕过来源审计：" + "; ".join(audit.issues))
        return self.repository.import_dataset(value)

    def import_cases(self, value: CaseDataset) -> CaseDataset:
        return self.repository.import_cases(
            CaseDataset.model_validate_json(value.model_dump_json())
        )

    def case_catalog(self, domain: str) -> list[dict]:
        return [
            dict(
                id=str(d.id),
                version=d.version,
                domain=d.domain,
                hash=d.manifest_hash,
                cases=[
                    dict(
                        key=c.key,
                        question=c.question,
                        visibility=c.visibility,
                        review_origin=c.review_origin,
                    )
                    for c in d.cases
                ],
            )
            for d in self.repository.case_datasets(domain)
        ]

    def replay_input(self, domain: str, key: str) -> dict:
        datasets = self.repository.case_datasets(domain)
        for dataset in sorted(datasets, key=lambda d: d.imported_at, reverse=True):
            case = next((c for c in dataset.cases if c.key == key), None)
            if case is not None:
                value = dataset.replay_input(key)
                value["dataset_hash"] = dataset.manifest_hash
                value["historical_reference"] = estimate_cases(
                    case.target,
                    datasets,
                    case.cutoff,
                    case.cutoff,
                    case.window_end,
                    retrospective=True,
                ).model_dump(mode="json")
                return value
        from future_agent.application.ports import NotFound

        raise NotFound("历史案例不存在")

    def check(
        self,
        plan: AnalysisPlan,
        cutoff: UtcTime,
        start: UtcTime,
        end: UtcTime,
    ) -> ReferenceEstimate | CaseEstimate:
        if isinstance(plan.reference_target, CaseTarget):
            target = plan.reference_target
            common = dict(target=target, cutoff=cutoff, window_start=start, window_end=end)
            if (
                plan.outcome_type != "binary"
                or plan.analysis_kind != "baseline"
                or plan.action_conditions
            ):
                return CaseEstimate(
                    status="not_applicable", reason="案例只匹配已登记的二元既定路径问题", **common
                )
            if not (start < end and start.year <= target.edition <= end.year):
                return CaseEstimate(
                    status="not_applicable", reason="案例届次与目标窗口不一致", **common
                )
            try:
                return estimate_cases(
                    target, self.repository.case_datasets(target.domain), cutoff, start, end
                )
            except (OSError, ValueError, AppError):
                return CaseEstimate(status="failed", reason="案例读取或完整性校验失败", **common)
        try:
            candidates = (
                self.repository.datasets(plan.reference_target) if plan.reference_target else []
            )
            return estimate_release_reference(plan, candidates, cutoff, start, end)
        except (OSError, ValueError, Conflict):
            return ReferenceEstimate(
                status="failed",
                reason="本地参照读取或完整性验证失败",
                cutoff=cutoff,
                window_start=start,
                window_end=end,
                target=plan.reference_target,
            )

    def bind(
        self, event_id: UUID, plan: AnalysisPlan, cutoff: UtcTime, start: UtcTime, end: UtcTime
    ) -> ReferenceBinding:
        estimate = self.check(plan, cutoff, start, end)
        payload = {
            "schema_version": "1.0",
            "event_revision_id": str(event_id),
            "estimate": estimate.model_dump(mode="json"),
        }
        return ReferenceBinding(
            event_revision_id=event_id, estimate=estimate, input_hash=fingerprint(payload)
        )


def validate_binding(binding: ReferenceBinding, event_id: UUID, cutoff: UtcTime) -> None:
    if (
        binding.event_revision_id != event_id
        or binding.estimate.cutoff != cutoff
        or binding.input_hash
        != fingerprint(binding.model_dump(mode="json", exclude={"input_hash"}))
    ):
        raise Conflict("参照绑定版本或哈希不一致")
