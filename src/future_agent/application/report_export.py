"""Export a minimal result projection, never session sources or cached page bodies."""

import re
from pathlib import Path
from typing import Protocol
from uuid import UUID

from pydantic import JsonValue

from future_agent.application.forecasts import ForecastService
from future_agent.application.lifecycle import ResolutionService


class ReportStore(Protocol):
    def save(self, markdown: str, result: dict[str, JsonValue]) -> tuple[Path, Path]: ...


class ReportExportService:
    def __init__(
        self, forecasts: ForecastService, lifecycle: ResolutionService, store: ReportStore
    ) -> None:
        self.forecasts, self.lifecycle, self.store = forecasts, lifecycle, store

    def save(self, forecast_id: UUID) -> tuple[Path, Path]:
        forecast = self.forecasts.get(forecast_id)
        frozen = self.lifecycle.frozen(forecast_id)
        # This is an export contract, deliberately not a restorable Forecast dump.
        result = {
            "schema_version": "1.1" if forecast.draft.judgment else "1.0",
            "kind": "forecast_report",
            "forecast_id": str(forecast.id),
            "session_id": str(forecast.session_id),
            "event_revision": forecast.event.revision,
            "status": "frozen" if frozen else "draft",
            "frozen_at": frozen.frozen_at.isoformat() if frozen else None,
            "input_hash": frozen.input_hash if frozen else None,
            "supersedes_forecast_id": str(frozen.supersedes_forecast_id)
            if frozen and frozen.supersedes_forecast_id
            else None,
            "revision_reason": frozen.revision_reason if frozen else None,
            "question": forecast.event.question.model_dump(mode="json"),
            "spec": forecast.spec.model_dump(mode="json"),
            "method": forecast.method,
            "method_version": forecast.method_version,
            "model_id": forecast.model_id,
            "prompt_version": forecast.prompt_version,
            "calibration_status": forecast.calibration_status,
            "created_at": forecast.created_at.isoformat(),
            "evidence_cutoff": forecast.evidence_cutoff.isoformat(),
            "draft": forecast.draft.model_dump(mode="json"),
            "probabilities": forecast.probabilities,
            "normalization_original_sum": forecast.normalization_original_sum,
            "unknowns": [
                u.description for u in forecast.event.unknowns if u.resolution_status != "answered"
            ],
            "limitations": forecast.assessment.limitations,
            "gaps": forecast.assessment.gaps,
            "evidence": [
                {
                    "span": span.model_dump(mode="json"),
                    "url": document.source.url,
                    "retrieved_at": document.source.retrieved_at.isoformat()
                    if document.source.retrieved_at
                    else None,
                    "content_hash": document.content_hash,
                }
                for span in forecast.evidence_spans
                for document in forecast.evidence_documents
                if document.id == span.document_id
            ],
        }
        report = self.lifecycle.report(forecast_id)
        # Preserve the existing report's layout and treat all its external text as literal.
        fence = "`" * max(3, 1 + max((len(s) for s in re.findall(r"`+", report)), default=0))
        markdown = f"# 预测报告\n\n{fence}text\n{report}{fence}\n"
        return self.store.save(markdown, result)
