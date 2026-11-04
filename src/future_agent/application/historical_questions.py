"""Explicit diagnostic input projection and label-only scoring use case."""

from typing import Protocol
from uuid import UUID

from future_agent.domain.historical_questions import DiagnosticBundle


class DiagnosticRepository(Protocol):
    def save_diagnostic(self, bundle: DiagnosticBundle) -> DiagnosticBundle: ...
    def diagnostic(self, bundle_id: UUID) -> DiagnosticBundle: ...


class DiagnosticService:
    def __init__(self, repository: DiagnosticRepository) -> None:
        self.repository = repository

    def import_bundle(self, bundle: DiagnosticBundle) -> DiagnosticBundle:
        return self.repository.save_diagnostic(bundle)

    def inputs(self, bundle_id: UUID, split: str) -> list[dict]:
        return self.repository.diagnostic(bundle_id).prediction_inputs(split)
