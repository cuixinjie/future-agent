"""Read-only projections for the local UI; every value comes from saved contracts."""

from datetime import datetime
from typing import Any
from uuid import UUID

from future_agent.application.ports import AppError, NotFound
from future_agent.domain.assistance import AnalysisPlan, AssistanceState, Clarification
from future_agent.domain.events import AnalysisSession, EventRevision
from future_agent.domain.forecasts import Forecast
from future_agent.domain.lifecycle import FrozenForecast, Resolution
from future_agent.web.services import WebServices

STAGE_LABELS = {
    "planning": "整理目标",
    "clarification": "待补充信息",
    "planned": "目标已就绪",
    "researching": "检索与读取",
    "reviewing": "形成判断",
    "search_authorization": "待授权补充检索",
    "ready": "判断已就绪",
    "complete": "已完成",
    "unanswerable": "无法判断",
}

DELIVERY_LABELS = {
    "probability": "概率预测（未校准）",
    "directional": "方向判断",
    "conditional": "条件分析（无总体预测）",
    "exploration": "开放探索",
    "unable": "无法判断",
}

PURPOSE_LABELS = {
    "current_state": "当前状态",
    "counterevidence": "反证",
    "reference": "参照",
    "mechanism": "机制",
}

QUERY_PURPOSE_LABELS = {
    "current_state": "当前状态",
    "counterevidence": "反证",
    "reference": "参照",
    "mechanism": "机制",
}

STATUS_LABELS = {
    "CREATED": "已保存",
    "READING": "读取中",
    "NEEDS_INPUT": "等待输入",
    "STRUCTURED": "判断已保存",
    "FORECASTING": "判断中",
    "FAILED": "失败",
}


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _preview(text: str, limit: int = 120) -> str:
    compact = " ".join(text.split())
    return compact if len(compact) <= limit else compact[: limit - 1] + "…"


def question_view(revision: EventRevision | None) -> dict[str, Any] | None:
    if revision is None or revision.question is None:
        return None
    question = revision.question
    return {
        "question": question.question,
        "outcome_type": question.outcome_type,
        "forecast_at": _iso(question.forecast_at),
        "window_start": _iso(question.window_start),
        "window_end": _iso(question.window_end),
        "resolution_rule": question.resolution_rule,
        "resolution_source_policy": question.resolution_source_policy,
        "confirmed": revision.confirmed,
    }


def clarification_view(item: Clarification) -> dict[str, Any]:
    return {
        "question": item.question,
        "kind": item.kind,
        "kind_label": "目标信息" if item.kind == "target" else "私人事实",
        "changes_judgment": item.changes_judgment,
        "cannot_research_reason": item.cannot_research_reason,
    }


def plan_view(plan: AnalysisPlan | None) -> dict[str, Any] | None:
    if plan is None:
        return None
    return {
        "question": plan.question,
        "analysis_kind": plan.analysis_kind,
        "outcome_type": plan.outcome_type,
        "duration_days": plan.duration_days,
        "window_start": _iso(plan.window_start),
        "window_end": _iso(plan.window_end),
        "resolution_rule": plan.resolution_rule,
        "resolution_source_policy": plan.resolution_source_policy,
        "assumptions": plan.assumptions,
        "action_conditions": plan.action_conditions,
        "outcomes": [
            {"key": outcome.key, "definition": outcome.definition} for outcome in plan.outcomes
        ],
        "definition_reason": plan.definition_reason,
        "queries": [
            {
                "query": query.query,
                "purpose": query.purpose,
                "purpose_label": QUERY_PURPOSE_LABELS.get(query.purpose, query.purpose),
                "decision_gap": query.decision_gap,
            }
            for query in plan.queries
        ],
        "unavailable_reason": plan.unavailable_reason,
        "usable": plan.usable(),
    }


def assistance_view(session: AnalysisSession, state: AssistanceState | None) -> dict[str, Any]:
    if state is None:
        return {
            "stage": None,
            "stage_label": "尚未开始",
            "plan": None,
            "questions": [],
            "pending_queries": [],
            "stop_reason": None,
            "research_gaps": [],
            "research_rounds": 0,
            "clarification_used": False,
            "forecast_id": None,
        }
    return {
        "stage": state.stage,
        "stage_label": STAGE_LABELS.get(state.stage, state.stage),
        "plan": plan_view(state.plan),
        "questions": [clarification_view(q) for q in state.clarification_questions],
        "pending_queries": [
            {
                "query": query.query,
                "purpose_label": QUERY_PURPOSE_LABELS.get(query.purpose, query.purpose),
                "decision_gap": query.decision_gap,
            }
            for query in state.pending_queries
        ],
        "stop_reason": state.stop_reason,
        "research_gaps": state.research_gaps,
        "research_rounds": state.research_rounds,
        "approved_queries": state.approved_queries,
        "clarification_used": state.clarification_used,
        "forecast_id": str(state.forecast_id) if state.forecast_id else None,
        "reading": reading_view(state),
        "budgets": {
            "calls_used": len(session.attempts),
            "max_calls": session.max_calls,
            "consumed_tokens": session.consumed_tokens,
            "max_total_tokens": session.max_total_tokens,
            "consumed_seconds": round(session.consumed_seconds, 1),
            "max_run_seconds": session.max_run_seconds,
        },
    }


def reading_view(state: AssistanceState) -> dict[str, Any] | None:
    if state.reading is None:
        return None
    reading = state.reading
    return {
        "numeric_status": reading.numeric_basis.status,
        "source_relationships": [g.model_dump(mode="json") for g in reading.source_relationships],
        "coverage_gaps": reading.coverage_gaps,
        "source_kind": reading.numeric_basis.source_kind,
        "reference": reading.numeric_basis.reference,
        "limitations": reading.numeric_basis.limitations,
        "material_gaps": reading.material_gaps,
        "stop_reason": reading.research_stop_reason,
        "citations": len(reading.citations),
    }


def _evidence_view(forecast: Forecast) -> list[dict[str, Any]]:
    spans = {span.id: span for span in forecast.evidence_spans}
    documents = {document.id: document for document in forecast.evidence_documents}
    items = []
    for reviewed in forecast.assessment.review.evidence:
        span = spans.get(reviewed.span_id)
        if span is None:
            continue
        document = documents.get(span.document_id)
        source = document.source if document else None
        items.append(
            {
                "span_id": str(span.id),
                "document_id": str(span.document_id),
                "quote": span.quote,
                "purpose": reviewed.purpose,
                "purpose_label": PURPOSE_LABELS.get(reviewed.purpose, reviewed.purpose),
                "relevance_reason": reviewed.relevance_reason,
                "title": (source.title if source else None) or "未命名来源",
                "url": document.canonical_url if document else None,
                "publisher": source.publisher if source else None,
                "retrieved_at": _iso(source.retrieved_at) if source else None,
            }
        )
    return items


def forecast_view(forecast: Forecast, frozen: FrozenForecast | None) -> dict[str, Any]:
    judgment = forecast.draft.judgment
    definitions = {item.key: item.definition for item in forecast.spec.outcomes}
    outcomes = []
    for index, definition in enumerate(forecast.spec.outcomes):
        outcome = forecast.draft.outcomes[index]
        probability = forecast.probabilities[index]
        outcomes.append(
            {
                "key": definition.key,
                "definition": definition.definition,
                "probability": probability,
                "percent": None if probability is None else round(probability * 100, 1),
                "reasoning": outcome.reasoning,
                "conditions": outcome.conditions,
                "watch_signals": outcome.watch_signals,
                "supporting_evidence": len(outcome.supporting_evidence),
                "opposing_evidence": len(outcome.opposing_evidence),
            }
        )
    limitations = list(dict.fromkeys([*forecast.assessment.limitations, *forecast.assessment.gaps]))
    guidance = forecast.spec.guidance
    return {
        "id": str(forecast.id),
        "created_at": _iso(forecast.created_at),
        "method": forecast.method,
        "method_version": forecast.method_version,
        "calibration_status": forecast.calibration_status,
        "model_id": forecast.model_id,
        "prompt_version": forecast.prompt_version,
        "structured_mode": forecast.structured_mode,
        "evidence_cutoff": _iso(forecast.evidence_cutoff),
        "delivery": judgment.delivery if judgment else ("scenario_only"),
        "delivery_label": DELIVERY_LABELS.get(
            judgment.delivery if judgment else "scenario_only", "情景分析"
        ),
        "decision_review": forecast.draft.decision_review.model_dump(mode="json")
        if forecast.draft.decision_review
        else None,
        "conclusion": judgment.conclusion if judgment else None,
        "primary_outcome": definitions.get(judgment.primary_outcome)
        if judgment and judgment.primary_outcome
        else None,
        "ranking": [definitions[key] for key in judgment.ranking] if judgment else [],
        "historical_reference": forecast.reference_binding.estimate.model_dump(mode="json")
        if forecast.reference_binding
        else None,
        "reference_basis": judgment.reference_basis if judgment else None,
        "reference_kind": judgment.reference_kind if judgment else None,
        "differences_and_adjustment": judgment.differences_and_adjustment if judgment else None,
        "key_drivers": judgment.key_drivers if judgment else [],
        "unknown_effects": judgment.unknown_effects if judgment else [],
        "change_conditions": judgment.change_conditions if judgment else [],
        "no_numeric_reason": judgment.no_numeric_reason if judgment else None,
        "refusal_reason": forecast.draft.refusal_reason,
        "outcomes": outcomes,
        "assumptions": forecast.draft.assumptions,
        "analysis_kind": guidance.analysis_kind if guidance else None,
        "action_conditions": guidance.action_conditions if guidance else [],
        "limitations": limitations,
        "definition_review": forecast.spec.definition_review,
        "origin": forecast.event.origin,
        "evidence": _evidence_view(forecast),
        "frozen": _frozen_view(frozen),
        "revision": forecast.event.revision,
        "normalization_original_sum": forecast.normalization_original_sum,
    }


def _frozen_view(frozen: FrozenForecast | None) -> dict[str, Any] | None:
    if frozen is None:
        return None
    return {
        "frozen_at": _iso(frozen.frozen_at),
        "input_hash": frozen.input_hash,
        "supersedes_forecast_id": str(frozen.supersedes_forecast_id)
        if frozen.supersedes_forecast_id
        else None,
        "revision_reason": frozen.revision_reason,
        "early_resolution_outcomes": frozen.early_resolution_outcomes,
    }


def resolution_view(item: Resolution) -> dict[str, Any]:
    return {
        "id": str(item.id),
        "status": item.status,
        "resolved_outcome": item.resolved_outcome,
        "resolver": item.resolver,
        "reason": item.reason,
        "recorded_at": _iso(item.recorded_at),
        "observed_at": _iso(item.observed_at),
        "sequence": item.sequence,
        "evidence_ids": [str(evidence_id) for evidence_id in item.evidence_ids],
    }


def session_summary(session: AnalysisSession) -> dict[str, Any]:
    state = session.assistance
    plan = state.plan if state else None
    question = plan.question if plan and plan.question else None
    return {
        "id": str(session.id),
        "created_at": _iso(session.created_at),
        "status": session.status,
        "status_label": STATUS_LABELS.get(session.status, session.status),
        "stage": state.stage if state else None,
        "stage_label": STAGE_LABELS.get(state.stage, state.stage) if state else "尚未开始",
        "question": question,
        "preview": _preview(session.sources[0].normalized if session.sources else ""),
        "has_forecast": bool(state and state.forecast_id),
        "source_count": len(session.sources),
    }


def _load_forecast(services: WebServices, forecast_id: UUID) -> Forecast | None:
    try:
        return services.forecasts.get(forecast_id)
    except NotFound:
        return None


def _load_frozen(services: WebServices, forecast_id: UUID) -> FrozenForecast | None:
    return services.lifecycle.frozen(forecast_id)


def _load_resolutions(services: WebServices, forecast_id: UUID) -> list[Resolution]:
    try:
        return services.lifecycle.history(forecast_id)
    except AppError:
        return []


def session_detail(services: WebServices, session_id: UUID) -> dict[str, Any]:
    session = services.events.get(session_id)
    state = session.assistance
    try:
        revision: EventRevision | None = services.events.revision(session_id)
    except AppError:
        revision = None
    forecast = None
    frozen = None
    resolutions: list[Resolution] = []
    if state and state.forecast_id:
        forecast = _load_forecast(services, state.forecast_id)
        if forecast:
            frozen = _load_frozen(services, forecast.id)
            if frozen:
                resolutions = _load_resolutions(services, forecast.id)
    return {
        "id": str(session.id),
        "created_at": _iso(session.created_at),
        "status": session.status,
        "status_label": STATUS_LABELS.get(session.status, session.status),
        "current_revision": session.current_revision,
        "source_text": "\n\n".join(
            source.normalized for source in session.sources if source.kind == "narrative"
        ),
        "answers": [
            {"text": source.normalized, "created_at": _iso(source.created_at)}
            for source in session.sources
            if source.kind == "answer"
        ],
        "question": question_view(revision),
        "assistance": assistance_view(session, state),
        "forecast": forecast_view(forecast, frozen) if forecast else None,
        "resolutions": [resolution_view(item) for item in resolutions],
        "last_resolution_id": str(resolutions[-1].id) if resolutions else None,
        "error_code": session.error_code,
    }


def overview(services: WebServices) -> dict[str, Any]:
    settings = services.settings
    return {
        "model_ready": services.provider is not None,
        "model": settings.llm_model,
        "search_ready": bool(settings.search_base_url),
        "data_dir": str(settings.data_dir.resolve()),
        "defaults": services.create_options(),
    }


def verification_queue(
    services: WebServices, sessions: list[AnalysisSession]
) -> list[dict[str, Any]]:
    items = []
    for session in sessions:
        state = session.assistance
        if not state or not state.forecast_id:
            continue
        frozen = services.lifecycle.frozen(state.forecast_id)
        if frozen is None:
            continue
        question = frozen.forecast.event.question
        history = _load_resolutions(services, state.forecast_id)
        if not services.lifecycle.needs_verification(state.forecast_id):
            continue
        items.append(
            {
                "session_id": str(session.id),
                "question": question.question,
                "window_end": _iso(question.window_end),
                "status": history[-1].status if history else "pending",
            }
        )
    return sorted(items, key=lambda item: item["window_end"])
