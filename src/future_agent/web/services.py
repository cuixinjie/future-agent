"""Composition root for the local web entry point; no business rules live here."""

from dataclasses import dataclass

from future_agent.application.assessment import AssessmentService
from future_agent.application.assistance import AssistanceService
from future_agent.application.events import EventService
from future_agent.application.evidence import EvidenceService
from future_agent.application.forecasts import ForecastService
from future_agent.application.lifecycle import ResolutionService
from future_agent.application.ports import LLMProvider
from future_agent.application.report_export import ReportExportService
from future_agent.application.research import ResearchService
from future_agent.config import Settings
from future_agent.providers.compatible import CompatibleProvider
from future_agent.providers.searxng import SearXNGProvider
from future_agent.providers.web import StaticPageReader
from future_agent.storage.reports import FileReportStore
from future_agent.storage.snapshots import FileSnapshotStore
from future_agent.storage.sqlite import SQLiteRepository


@dataclass
class WebServices:
    settings: Settings
    repository: SQLiteRepository
    provider: LLMProvider | None
    events: EventService
    evidence: EvidenceService
    assessments: AssessmentService
    forecasts: ForecastService
    lifecycle: ResolutionService
    research: ResearchService
    assistance: AssistanceService
    exports: ReportExportService

    def create_options(self) -> dict[str, float | int]:
        settings = self.settings
        return {
            "max_calls": settings.max_llm_calls,
            "max_run_seconds": settings.max_run_seconds,
            "max_total_tokens": settings.max_total_tokens,
            "max_search_queries": settings.max_search_queries,
            "max_pages": settings.max_pages,
        }


def build_services(settings: Settings) -> WebServices:
    repo = SQLiteRepository(settings.database_path())
    repo.migrate()
    provider = CompatibleProvider(settings) if settings.model_ready() else None
    events = EventService(repo, provider)
    evidence = EvidenceService(repo, repo, FileSnapshotStore(settings.data_dir))
    assessments = AssessmentService(repo, repo, evidence)
    forecasts = ForecastService(repo, repo, assessments, provider)
    lifecycle = ResolutionService(repo, repo, evidence)
    research = ResearchService(
        repo,
        evidence,
        SearXNGProvider(settings) if settings.search_base_url else None,
        StaticPageReader() if settings.search_base_url else None,
    )
    return WebServices(
        settings=settings,
        repository=repo,
        provider=provider,
        events=events,
        evidence=evidence,
        assessments=assessments,
        forecasts=forecasts,
        lifecycle=lifecycle,
        research=research,
        assistance=AssistanceService(events, research, assessments, forecasts),
        exports=ReportExportService(forecasts, lifecycle, FileReportStore(settings.data_dir)),
    )
