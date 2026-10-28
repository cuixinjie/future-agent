"""Pre-registered A/B/C execution over sealed existing workflow inputs."""

import json
from typing import Protocol
from uuid import UUID

from future_agent.application.forecasts import ForecastService
from future_agent.application.lifecycle import ResolutionService
from future_agent.application.model_calls import ModelCalls
from future_agent.application.ports import AppError, Conflict, NotFound
from future_agent.application.protocols import ProtocolRepository
from future_agent.domain.assessment import EvidenceAssessment
from future_agent.domain.contracts import now
from future_agent.domain.evaluation import (
    BASELINE_VERSION,
    DIRECT_VERSION,
    EvaluationEntry,
    EvaluationPrediction,
    EvaluationReview,
    EvaluationRun,
    ExperimentSnapshot,
    SealInput,
    baseline,
    release_spec,
)
from future_agent.domain.events import AnalysisSession, EventRevision, transition
from future_agent.domain.evidence import canonical_url
from future_agent.domain.forecasts import (
    FORECAST_METHOD_VERSION,
    ForecastDraft,
    forecast_schema,
    validate_judgment,
)
from future_agent.domain.lifecycle import (
    Resolution,
    ResolutionInput,
    fingerprint,
    validate_resolution_window,
)
from future_agent.domain.protocols import release_question
from future_agent.prompts.direct import DIRECT_SYSTEM
from future_agent.prompts.forecast import FORECAST_PROMPT_VERSION


class EvaluationRepository(Protocol):
    def evaluation(self, protocol_id: UUID) -> EvaluationRun: ...
    def save_evaluation(
        self, value: EvaluationRun, expected_version: int | None
    ) -> EvaluationRun: ...


class EvaluationService:
    def __init__(
        self,
        repository: EvaluationRepository,
        protocols: ProtocolRepository,
        forecasts: ForecastService,
        lifecycle: ResolutionService,
    ) -> None:
        self.repository, self.protocols = repository, protocols
        self.forecasts, self.lifecycle = forecasts, lifecycle
        self.sessions = forecasts.sessions
        self.assessments = forecasts.assessments
        self.evidence = self.assessments.evidence

    def get(self, protocol_id: UUID) -> EvaluationRun:
        return self.repository.evaluation(protocol_id)

    def start(self, protocol_id: UUID) -> EvaluationRun:
        protocol = self.protocols.protocol(protocol_id)
        if protocol.status != "locked" or now() > protocol.evidence_cutoff:
            raise Conflict("须在Tc之前从已锁定协议登记全部实验条目")
        methods = {m.arm: m for m in protocol.methods}
        if (
            methods["A"].method_version != BASELINE_VERSION
            or methods["B"].method_version != DIRECT_VERSION
            or methods["B"].prompt_version != DIRECT_VERSION
            or methods["C"].method_version
            != ("judgment-2.0" if protocol.schema_version == "2.0" else FORECAST_METHOD_VERSION)
            or methods["C"].prompt_version
            != ("forecast-2.0" if protocol.schema_version == "2.0" else FORECAST_PROMPT_VERSION)
        ):
            raise Conflict("协议方法或提示词版本与当前实现不匹配")
        if protocol.budget.max_cost is not None:
            raise Conflict("当前无价格配置，不能执行金额上限；请预登记调用/token/时间预算")
        value = EvaluationRun(
            id=protocol.id,
            protocol=protocol,
            entries=[
                EvaluationEntry(project_id=p.id, arm=a)
                for p in protocol.projects
                for a in ("A", "B", "C")
            ],
        )
        return self.repository.save_evaluation(value, None)

    def _save(self, run: EvaluationRun) -> EvaluationRun:
        return self.repository.save_evaluation(run, run.version)

    @staticmethod
    def _entry(run: EvaluationRun, project_id: UUID, arm: str) -> EvaluationEntry:
        item = next((e for e in run.entries if e.project_id == project_id and e.arm == arm), None)
        if item is None:
            raise AppError("项目或实验组未预登记")
        return item

    def _set(self, run: EvaluationRun, entry: EvaluationEntry) -> EvaluationRun:
        return self._save(
            run.model_copy(
                update={
                    "entries": [
                        entry if (e.project_id, e.arm) == (entry.project_id, entry.arm) else e
                        for e in run.entries
                    ]
                }
            )
        )

    def _context(
        self, value: SealInput
    ) -> tuple[AnalysisSession, EventRevision, EvidenceAssessment, str]:
        session = self.sessions.load(value.session_id)
        event = self.sessions.revision(value.session_id, value.revision)
        view = next(
            (
                v
                for v in self.assessments.history(value.session_id, value.revision)
                if v.assessment.id == value.assessment_id
            ),
            None,
        )
        if (
            view is None
            or view.stale
            or not event.confirmed
            or session.current_revision != value.revision
        ):
            raise Conflict("封存的事件/评估不存在、未确认或已失效")
        if session.status not in {"STRUCTURED", "FAILED"} or any(
            r.status == "running" for r in session.research_runs
        ):
            raise Conflict("封存与运行需要无执行中任务的已确认事件")
        payload = {
            "event": event.model_dump(mode="json"),
            "assessment": view.assessment.model_dump(mode="json"),
            "budget": session.model_dump(
                include={
                    "max_calls",
                    "max_run_seconds",
                    "max_total_tokens",
                    "max_search_queries",
                    "max_pages",
                }
            ),
            "sources": [s.model_dump(mode="json") for s in session.sources],
        }
        return session, event, view.assessment, fingerprint(payload)

    def seal(self, protocol_id: UUID, value: SealInput) -> EvaluationRun:
        run = self.get(protocol_id)
        project = next((p for p in run.protocol.projects if p.id == value.project_id), None)
        if project is None:
            raise AppError("项目未预登记")
        if now() > run.protocol.evidence_cutoff:
            raise Conflict("已过Tc，不能补交或替换输入快照")
        if any(
            s.project_id == value.project_id or s.session_id == value.session_id
            for s in run.snapshots
        ):
            raise Conflict("项目快照不可覆盖，实验项目不能共享执行会话")
        session, event, assessment, digest = self._context(value)
        modern = run.protocol.schema_version == "2.0"
        if modern:
            spec = value.spec
            if (
                spec is None
                or spec.schema_version != "2.0"
                or assessment.assessor_version != "evidence-assessor-2.0"
                or assessment.experiment
                or [(o.key, o.definition) for o in spec.outcomes]
                != [(o.key, o.definition) for o in release_spec().outcomes]
                or spec.guidance.question != event.question.question
                or spec.guidance.window_start != event.question.window_start
                or spec.guidance.window_end != event.question.window_end
                or spec.guidance.resolution_rule != event.question.resolution_rule
                or spec.guidance.resolution_source_policy != event.question.resolution_source_policy
                or spec.guidance.analysis_kind != "baseline"
                or spec.guidance.questions
            ):
                raise Conflict("新版实验须封存与协议目标一致的2.0定义及新版依据评估")
        elif value.spec is not None or assessment.assessor_version == "evidence-assessor-2.0":
            raise Conflict("旧实验不能封存新版定义或评估")
        expected = release_question(project, run.protocol.forecast_at)
        fields = {
            "question",
            "window_start",
            "window_end",
            "outcome_type",
            "resolution_rule",
            "resolution_source_policy",
        }
        if (
            event.question.model_dump(include=fields) != expected.model_dump(include=fields)
            or event.question.forecast_at != run.protocol.evidence_cutoff
            or assessment.cutoff != run.protocol.evidence_cutoff
            or any(s.created_at > run.protocol.evidence_cutoff for s in session.sources)
        ):
            raise Conflict("C目标、规则、Tc/T0或共享答案时点与协议不一致")
        if run.protocol.purpose == "prospective":
            revisions = self.sessions.history(session.id)
            reader_model = next(m.model_id for m in run.protocol.methods if m.arm == "C")
            if (
                any(r.origin == "fixture" for r in revisions)
                or not any(
                    r.origin == "model"
                    and r.model_id == reader_model
                    and r.prompt_version == "reader-1.0"
                    for r in revisions
                )
                or any(r.origin == "model" and r.model_id != reader_model for r in revisions)
                or (not modern and not assessment.experiment)
                or any(r.origin == "fixture" for r in session.research_runs)
            ):
                raise Conflict("真实C组需要同模型Reader及实验资格评估，不接受人工/夹具替代")
            if modern:
                eligible = {
                    item.document_id
                    for item in self.evidence.select(
                        session.id, value.revision, run.protocol.evidence_cutoff, experiment=True
                    )
                    if item.eligible
                }
                if any(
                    item.eligible and item.document_id not in eligible
                    for item in assessment.eligibility
                ):
                    raise Conflict("新版真实实验不得使用夹具或不合格截止证据")
        document = self.evidence.repository.document(value.history.document_id)
        text = self.evidence.snapshot(document.id).normalized
        if (
            document.event_revision_id != event.id
            or canonical_url(document.source.url) != canonical_url(str(project.history_source))
            or not any(e.document_id == document.id and e.eligible for e in assessment.eligibility)
        ):
            raise Conflict("历史正文必须属于当前事件、官方历史来源且通过截止资格")
        reviewed = {r.span_id for r in assessment.review.evidence}
        spans = self.evidence.spans(document.id)
        if not any(s.id in reviewed and s.start == 0 and s.end == len(text) for s in spans):
            raise Conflict("B/C必须共享完整历史正文；C评估须审查全文引用")
        if value.history.coverage_quote not in text or any(
            r.quote not in text for r in value.history.records
        ):
            raise Conflict("历史完整性声明与发布记录必须引用已封存正文")
        if not assessment.review.allow_numeric:
            raise Conflict(
                "实验协议统一请求B/C数值估计；C应按资料资格拒绝或输出情景，不能单独关闭请求"
            )
        budget = run.protocol.budget
        if (
            session.max_calls > budget.max_llm_calls_per_project
            or session.max_run_seconds > budget.max_seconds_per_project
            or session.max_total_tokens > budget.max_tokens_per_project
            or session.max_search_queries > budget.max_search_queries_per_project
            or session.max_pages > budget.max_pages_per_project
        ):
            raise Conflict("C会话预算须在创建时受协议约束，不能先消费后放宽")
        data = {
            **{name: getattr(value, name) for name in SealInput.model_fields},
            "sources": session.sources,
            "preparation_attempts": session.attempts,
            "research_runs": session.research_runs,
            "history_text": text,
            "c_input_hash": digest,
            "sealed_at": now(),
        }
        snapshot = ExperimentSnapshot(
            **data,
            input_hash=fingerprint(
                # Hash the contract's canonical UTC representation.
                json.loads(
                    ExperimentSnapshot.model_construct(
                        **data, input_hash="pending"
                    ).model_dump_json(exclude={"input_hash"})
                )
            ),
        )
        if snapshot.sealed_at > run.protocol.evidence_cutoff:
            raise Conflict("封存写入前已过Tc")
        return self._save(run.model_copy(update={"snapshots": [*run.snapshots, snapshot]}))

    def mark_missing(
        self, protocol_id: UUID, project_id: UUID, arm: str, reason: str, *, cancelled: bool = False
    ) -> EvaluationRun:
        run = self.get(protocol_id)
        entry = self._entry(run, project_id, arm)
        if entry.status not in {"waiting", "running"}:
            raise Conflict("已提交或已终止的首次实验条目不可覆盖")
        result = self._set(
            run,
            entry.model_copy(
                update={"status": "cancelled" if cancelled else "missing", "reason": reason}
            ),
        )
        if entry.status == "running" and arm == "C":
            try:
                self.forecasts.cancel(entry.step_id)
            except NotFound:
                pass  # Cancellation may arrive before the workflow step has been created.
        return result

    def run_arm(self, protocol_id: UUID, project_id: UUID, arm: str) -> EvaluationRun:
        run = self.get(protocol_id)
        entry = self._entry(run, project_id, arm)
        if entry.status not in {"waiting"}:
            if entry.status == "running":
                raise Conflict("中断条目保留费用未知；先显式登记缺失，不自动重试首次实验")
            return run
        if now() > run.protocol.forecast_at:
            return self._set(
                run, entry.model_copy(update={"status": "late", "reason": "共同T0期限已过"})
            )
        snap = next((s for s in run.snapshots if s.project_id == project_id), None)
        if snap is None:
            raise Conflict("项目尚无截止快照；可显式登记缺失")
        if arm == "C" and self._entry(run, project_id, "B").status in {"waiting", "running"}:
            raise Conflict("先运行或登记B组，确保B获得全部共享答案且不接收C结论")
        session, event, assessment, digest = self._context(snap)
        if digest != snap.c_input_hash:
            raise Conflict("封存后C输入已变化，禁止读入新材料")
        provider = self.forecasts.provider
        method = next(m for m in run.protocol.methods if m.arm == arm)
        if arm != "A" and (
            provider is None
            or provider.model_id != method.model_id
            or provider.output_token_limit != method.final_output_tokens
        ):
            raise Conflict("实际模型或最终输出额度与协议不匹配")
        entry = entry.model_copy(
            update={
                "status": "running",
                "prior_attempt_ids": [a.id for a in session.attempts],
                "started_at": now(),
            }
        )
        run = self._set(run, entry)  # Durable before any external I/O, even abrupt process exit.
        before = {a.id for a in session.attempts}
        try:
            prediction = None
            status, reason = "submitted", None
            if arm == "A":
                project = next(p for p in run.protocol.projects if p.id == project_id)
                windows = baseline(
                    snap.history, run.protocol.evidence_cutoff, project.product_channel
                )
                prediction = EvaluationPrediction(
                    probability=sum(windows) / 12,
                    method="release_frequency_baseline",
                    windows=windows,
                    input_hash=snap.input_hash,
                    method_version=method.method_version,
                )
            elif arm == "B":
                active = self.sessions.save(
                    transition(session, "FORECASTING").model_copy(
                        update={"active_forecast_step_id": entry.step_id}
                    ),
                    session.version,
                )
                payload = {
                    "method": "llm_judgment",
                    "spec": release_spec().model_dump(mode="json"),
                    "question": release_question(
                        next(p for p in run.protocol.projects if p.id == project_id),
                        run.protocol.forecast_at,
                    ).model_dump(mode="json"),
                    "project": next(
                        p for p in run.protocol.projects if p.id == project_id
                    ).model_dump(mode="json"),
                    "sources": [s.model_dump(mode="json") for s in snap.sources],
                    "history": snap.history.model_dump(mode="json"),
                    "history_text": snap.history_text,
                }

                def validate(content: str) -> ForecastDraft:
                    draft = ForecastDraft.model_validate_json(content)
                    validate_judgment(draft, release_spec(), "llm_judgment", set())
                    return draft

                active, draft = ModelCalls(self.sessions, provider).run(
                    active,
                    DIRECT_SYSTEM,
                    json.dumps(payload, ensure_ascii=False),
                    forecast_schema(),
                    validate,
                    cancelled=lambda: (
                        self._entry(self.get(protocol_id), project_id, arm).status != "running"
                    ),
                    max_repairs=0,
                    max_retries=0,
                )
                self.sessions.save(
                    transition(active, "STRUCTURED").model_copy(
                        update={"active_forecast_step_id": None}
                    ),
                    active.version,
                )
                probabilities, original = validate_judgment(
                    draft, release_spec(), "llm_judgment", set()
                )
                prediction = EvaluationPrediction(
                    probability=probabilities[0],
                    method="llm_judgment",
                    normalization_original_sum=original,
                    draft=draft,
                    input_hash=snap.input_hash,
                    method_version=method.method_version,
                    model_id=provider.model_id,
                    prompt_version=DIRECT_VERSION,
                )
                if prediction.probability is None:
                    status, reason = "refused", draft.refusal_reason
            else:
                numeric = "llm_judgment" in assessment.available_methods
                forecast = self.forecasts.run(
                    snap.session_id,
                    snap.revision,
                    snap.assessment_id,
                    snap.spec or release_spec(),
                    numeric=numeric,
                    request_id=entry.step_id,
                )
                frozen = self.lifecycle.freeze(forecast.id, early_outcomes=["yes"])
                prediction = EvaluationPrediction(
                    submitted_at=frozen.frozen_at,
                    probability=forecast.probabilities[0],
                    method=forecast.method,
                    calibration_status=forecast.calibration_status,
                    normalization_original_sum=forecast.normalization_original_sum,
                    draft=forecast.draft,
                    frozen_forecast_id=forecast.id,
                    input_hash=snap.input_hash,
                    method_version=forecast.method_version,
                    model_id=forecast.model_id,
                    prompt_version=forecast.prompt_version,
                )
                if prediction.probability is None:
                    status = "refused" if forecast.draft.refusal_reason else "scenario"
                    reason = forecast.draft.refusal_reason or "C数据资格不足，保留条件情景"
            if (
                prediction.submitted_at > run.protocol.forecast_at
                or now() > run.protocol.forecast_at
            ):
                status, reason = "late", "提交/持久化时间晚于共同T0，保留产物但不纳入首次评分"
        except (AppError, ValueError) as exc:
            status, reason = (
                "missing",
                "执行失败："
                + (str(exc) if isinstance(exc, AppError) else "输入或输出规则校验失败"),
            )
        if arm == "B":
            current_session = self.sessions.load(snap.session_id)
            if (
                current_session.status == "FAILED"
                and current_session.active_forecast_step_id == entry.step_id
            ):
                self.sessions.save(
                    current_session.model_copy(update={"active_forecast_step_id": None}),
                    current_session.version,
                )
        # Cancellation wins over late external responses; never resurrect a cancelled entry.
        latest = self.get(protocol_id)
        current = self._entry(latest, project_id, arm)
        if current.status != "running":
            return latest
        attempts = [
            a.id for a in self.sessions.load(snap.session_id).attempts if a.id not in before
        ]
        return self._set(
            latest,
            current.model_copy(
                update={
                    "status": status,
                    "reason": reason,
                    "prediction": prediction,
                    "prediction_hash": fingerprint(prediction.model_dump(mode="json"))
                    if prediction
                    else None,
                    "attempt_ids": attempts,
                }
            ),
        )

    def resolve(
        self,
        protocol_id: UUID,
        project_id: UUID,
        value: ResolutionInput,
        expected_previous: UUID | None,
    ) -> EvaluationRun:
        run = self.get(protocol_id)
        self._entry(run, project_id, "A")
        snap = next((s for s in run.snapshots if s.project_id == project_id), None)
        if snap is None:
            raise Conflict("项目无封存事件，不能关联结果证据")
        history = run.resolutions.get(str(project_id), [])
        if expected_previous != (history[-1].id if history else None):
            raise Conflict("核验历史已变化")
        project = next(p for p in run.protocol.projects if p.id == project_id)
        documents = self.evidence.list_documents(snap.session_id, snap.revision)
        spans = [
            s for d in documents for s in self.evidence.spans(d.id) if s.id in value.evidence_ids
        ]
        documents = [d for d in documents if d.id in {s.document_id for s in spans}]
        if any(
            canonical_url(d.source.url) != canonical_url(str(project.official_resolution_source))
            for d in documents
        ):
            raise Conflict("结果核验须引用预登记官方来源")
        timestamp = now()
        result = Resolution(
            **value.model_dump(),
            forecast_id=self._entry(run, project_id, "C").step_id,
            previous_resolution_id=expected_previous,
            sequence=len(history) + 1,
            recorded_at=timestamp,
            resolved_at=timestamp if value.status == "resolved" else None,
            evidence_documents=documents,
            evidence_spans=spans,
        )
        if value.resolved_outcome is not None and value.resolved_outcome not in {"yes", "no"}:
            raise ValueError("发布实验结果须为yes/no")
        if timestamp < run.protocol.forecast_at:
            raise Conflict("尚未到共同预测时点")
        validate_resolution_window(
            release_question(project, run.protocol.forecast_at), ["yes"], result
        )
        if run.protocol.purpose == "prospective" and any(
            d.source.origin == "fixture" for d in documents
        ):
            raise Conflict("真实核验不接受夹具证据")
        return self._save(
            run.model_copy(
                update={"resolutions": {**run.resolutions, str(project_id): [*history, result]}}
            )
        )

    def review(self, protocol_id: UUID, value: EvaluationReview) -> EvaluationRun:
        run = self.get(protocol_id)
        self._entry(run, value.project_id, value.arm)
        value = EvaluationReview.model_validate_json(value.model_dump_json())
        value = value.model_copy(update={"recorded_at": now()})
        return self._save(run.model_copy(update={"reviews": [*run.reviews, value]}))

    def finalize(self, protocol_id: UUID) -> EvaluationRun:
        run = self.get(protocol_id)
        if now() <= run.protocol.forecast_at:
            raise Conflict("共同提交期限尚未结束")
        for entry in run.entries:
            if entry.status in {"waiting", "running"}:
                self.mark_missing(
                    protocol_id,
                    entry.project_id,
                    entry.arm,
                    "共同期限内未提交；中断调用费用可能未知"
                    if entry.status == "running"
                    else "共同期限内未执行或未封存输入",
                )
        return self.get(protocol_id)
