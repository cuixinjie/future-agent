"""Assess immutable evidence inputs without network calls or invented semantic judgments."""

import hashlib
import json
from typing import Protocol
from uuid import UUID

from future_agent.application.evidence import EvidenceService
from future_agent.application.ports import AppError, Conflict, SessionRepository
from future_agent.domain.assessment import (
    ASSESSOR_VERSION,
    AssessmentReview,
    AssessmentView,
    EvidenceAssessment,
    MethodQualification,
    ResearchCondition,
    qualify_methods,
)
from future_agent.domain.evidence import EvidenceSpan


class AssessmentRepository(Protocol):
    def save_assessment(self, assessment: EvidenceAssessment) -> None: ...
    def assessments(self, revision_id: UUID) -> list[EvidenceAssessment]: ...


class AssessmentService:
    def __init__(
        self,
        sessions: SessionRepository,
        repository: AssessmentRepository,
        evidence: EvidenceService,
    ) -> None:
        self.sessions, self.repository, self.evidence = sessions, repository, evidence

    def _inputs(
        self, sid: UUID, revision: int
    ) -> tuple[str, dict[UUID, EvidenceSpan], list[ResearchCondition]]:
        event = self.sessions.revision(sid, revision)
        conditions = [
            ResearchCondition(run_id=run.id, status=run.status, gaps=run.gaps)
            for run in self.sessions.load(sid).research_runs
            if run.plan.event_revision_id == event.id
        ]
        documents = sorted(self.evidence.list_documents(sid, revision), key=lambda d: str(d.id))
        spans = {
            span.id: span for document in documents for span in self.evidence.spans(document.id)
        }
        # Citation reads also check the persisted snapshot. Uncited bodies are checked by select.
        payload = {
            "research_conditions": [item.model_dump(mode="json") for item in conditions],
            "documents": [d.model_dump(mode="json") for d in documents],
            "spans": [spans[key].model_dump(mode="json") for key in sorted(spans, key=str)],
        }
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        return digest, spans, conditions

    def assess(
        self, sid: UUID, revision: int, review: AssessmentReview, *, experiment: bool = True
    ) -> EvidenceAssessment:
        session = self.sessions.load(sid)
        if session.current_revision != revision:
            raise Conflict("只能评估当前事件版本；旧评估保留用于追溯")
        if session.status in {"READING", "FORECASTING"} or any(
            r.status == "running" for r in session.research_runs
        ):
            raise Conflict("读取或检索仍在执行，请完成或显式终止后评估")
        event = self.sessions.revision(sid, revision)
        if not event.confirmed or not event.question or event.question.missing():
            raise AppError("评估需要已确认且完整的预测问题")
        cutoff = event.question.forecast_at
        selected = self.evidence.select(sid, revision, cutoff, experiment=experiment)
        digest, spans, conditions = self._inputs(sid, revision)
        eligible = {item.document_id for item in selected if item.eligible}
        covered = set()
        for item in review.evidence:
            span = spans.get(item.span_id)
            if span is None or span.document_id not in eligible:
                raise AppError("评估引用必须属于当前版本且通过截止资格和去重检查")
            if item.purpose == "current_state":
                covered.update(span.claim_ids)
        current = any(item.purpose == "current_state" for item in review.evidence)
        methods = qualify_methods(
            target_complete=True,
            current_evidence=current,
            critical_unknowns=bool(review.critical_questions),
            allow_numeric=review.allow_numeric,
        )
        if review.numeric_basis is not None:
            if experiment:
                raise AppError("自动主观判断资格仅用于普通分析，不能改变旧实验规则")
            basis = review.numeric_basis
            supported = basis.status == "supported" and review.allow_numeric
            if basis.source_kind == "external" and not review.evidence:
                supported = False
            if basis.source_kind == "user_report" and not event.claims:
                supported = False
            methods[1] = MethodQualification(
                method="llm_judgment",
                data_eligible=supported,
                implemented=True,
                reasons=[] if supported else ["judgment_basis_insufficient_or_conditional"],
            )
        gaps = []
        for condition in conditions:
            if condition.status != "completed":
                gaps.append(f"research_{condition.status}:{condition.run_id}")
            gaps.extend(f"research:{condition.run_id}:{gap}" for gap in condition.gaps)
        if not current:
            gaps.append("no_reviewed_current_evidence")
        if not any(item.purpose == "counterevidence" for item in review.evidence):
            gaps.append("counterevidence_not_reviewed")
        gaps.extend(review.critical_questions)
        unknowns = [item.id for item in event.unknowns if item.resolution_status != "answered"]
        if unknowns:
            gaps.append("current_unknowns_preserved")
        assessment = EvidenceAssessment(
            assessor_version="evidence-assessor-2.0" if review.numeric_basis else ASSESSOR_VERSION,
            session_id=sid,
            event_revision_id=event.id,
            revision=revision,
            question_id=event.question.id,
            cutoff=cutoff,
            experiment=experiment,
            input_hash=digest,
            review=review,
            research_conditions=conditions,
            eligibility=selected,
            covered_claim_ids=[c.id for c in event.claims if c.id in covered],
            uncovered_claim_ids=[c.id for c in event.claims if c.id not in covered],
            unresolved_unknown_ids=unknowns,
            methods=methods,
            recommended_method="llm_judgment" if methods[1].data_eligible else "scenario_only",
            available_methods=[m.method for m in methods if m.data_eligible and m.implemented],
            status="needs_input"
            if review.critical_questions and not review.numeric_basis
            else "assessed",
            gaps=gaps,
            limitations=[
                "引用相关性由模型分析并经程序校验引用；不等于事实已核实。"
                if review.review_origin == "model"
                else "引用相关性由显式人工审查提供；可引用不等于事实已核实。",
                "不推断资料统计口径或样本完整性；未接入合格数据集和时间序列。",
                "情景及主观判断可执行；统计估计、校准和预测效果尚未验证。",
                *review.limitations,
            ],
        )
        if self.sessions.load(sid).version != session.version:
            raise Conflict("评估期间会话已变化，请重新评估")
        if self._inputs(sid, revision)[0] != digest:
            raise Conflict("评估期间证据已变化，请重新评估")
        if self.evidence.select(sid, revision, cutoff, experiment=experiment) != selected:
            raise Conflict("评估期间证据资格已变化，请重新评估")
        self.repository.save_assessment(assessment)
        return assessment

    def history(self, sid: UUID, revision: int) -> list[AssessmentView]:
        session = self.sessions.load(sid)
        event = self.sessions.revision(sid, revision)
        digest, _, _ = self._inputs(sid, revision)
        views = []
        for assessment in self.repository.assessments(event.id):
            reasons = []
            expected_version = (
                "evidence-assessor-2.0" if assessment.review.numeric_basis else ASSESSOR_VERSION
            )
            if assessment.assessor_version != expected_version:
                reasons.append("assessor_version_changed")
            if revision != session.current_revision:
                reasons.append("event_revision_changed")
            if digest != assessment.input_hash:
                reasons.append("evidence_inputs_changed")
            # Revalidate uncited eligible snapshots too; corruption must surface as an error.
            self.evidence.select(sid, revision, assessment.cutoff, experiment=assessment.experiment)
            views.append(
                AssessmentView(
                    assessment=assessment, stale=bool(reasons), invalidation_reasons=reasons
                )
            )
        return views
