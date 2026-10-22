"""Freeze and manual resolution use cases; result evidence never alters forecast inputs."""

from typing import Protocol
from uuid import UUID

from future_agent.application.evidence import EvidenceService
from future_agent.application.forecasts import ForecastRepository
from future_agent.application.ports import Conflict
from future_agent.domain.contracts import now
from future_agent.domain.lifecycle import (
    FrozenForecast,
    Resolution,
    ResolutionInput,
    fingerprint,
    validate_resolution,
    validate_supersession,
)


class LifecycleRepository(Protocol):
    def seal(self, value: FrozenForecast) -> FrozenForecast: ...
    def frozen(self, forecast_id: UUID) -> FrozenForecast: ...
    def resolutions(self, forecast_id: UUID) -> list[Resolution]: ...
    def append_resolution(self, value: Resolution) -> None: ...


class ResolutionService:
    def __init__(
        self,
        forecasts: ForecastRepository,
        repository: LifecycleRepository,
        evidence: EvidenceService,
    ) -> None:
        self.forecasts, self.repository, self.evidence = forecasts, repository, evidence

    def freeze(
        self,
        forecast_id: UUID,
        *,
        supersedes: UUID | None = None,
        reason: str | None = None,
        early_outcomes: list[str] | None = None,
    ) -> FrozenForecast:
        forecast = self.forecasts.forecast(forecast_id)
        # Check retained files before sealing; only the forecast's own snapshot is used.
        for document in forecast.evidence_documents:
            self.evidence.snapshots.read(document.snapshot_id, document.content_hash)
        value = FrozenForecast(
            forecast=forecast,
            supersedes_forecast_id=supersedes,
            revision_reason=reason,
            input_hash=fingerprint(forecast.model_dump(mode="json")),
            early_resolution_outcomes=early_outcomes or [],
        )
        if supersedes:
            try:
                validate_supersession(self.repository.frozen(supersedes), value)
            except ValueError as exc:
                raise Conflict(str(exc)) from exc
        return self.repository.seal(value)

    def history(self, forecast_id: UUID) -> list[Resolution]:
        self.repository.frozen(forecast_id)
        return self.repository.resolutions(forecast_id)

    def record(
        self, forecast_id: UUID, value: ResolutionInput, *, expected_previous: UUID | None
    ) -> Resolution:
        value = ResolutionInput.model_validate_json(value.model_dump_json())
        frozen = self.repository.frozen(forecast_id)
        forecast = frozen.forecast
        history = self.history(forecast_id)
        previous = history[-1].id if history else None
        if previous != expected_previous:
            raise Conflict("核验历史已变化，请重读后提交")
        recorded_at = now()
        documents = self.evidence.list_documents(forecast.session_id, forecast.event.revision)
        spans = [
            span
            for document in documents
            for span in self.evidence.spans(document.id)
            if span.id in value.evidence_ids
        ]
        if {span.id for span in spans} != set(value.evidence_ids):
            raise Conflict("核验证据必须属于预测事件版本且可定位正文")
        documents = [d for d in documents if d.id in {s.document_id for s in spans}]
        result = Resolution(
            **value.model_dump(),
            forecast_id=forecast_id,
            previous_resolution_id=previous,
            sequence=len(history) + 1,
            recorded_at=recorded_at,
            resolved_at=recorded_at if value.status == "resolved" else None,
            evidence_documents=documents,
            evidence_spans=spans,
        )
        validate_resolution(frozen, result)
        self.repository.append_resolution(result)
        return result
