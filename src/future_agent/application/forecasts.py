"""T09 prediction use case; no provider or database implementation dependencies."""

import json
from typing import Protocol
from uuid import UUID, uuid4

from future_agent.application.assessment import AssessmentService
from future_agent.application.model_calls import ModelCalls
from future_agent.application.ports import (
    AppError,
    Conflict,
    LLMProvider,
    NotFound,
    SessionRepository,
)
from future_agent.domain.events import transition
from future_agent.domain.forecasts import (
    FORECAST_METHOD_VERSION,
    Forecast,
    ForecastDraft,
    ForecastSpec,
    validate_judgment,
)
from future_agent.domain.lifecycle import ForecastStep
from future_agent.prompts.forecast import FORECAST_PROMPT_VERSION, FORECAST_SYSTEM


class ForecastRepository(Protocol):
    def save_forecast(
        self,
        forecast: Forecast,
        *,
        expected_session_version: int | None = None,
        step: ForecastStep | None = None,
    ) -> None: ...
    def forecast(self, forecast_id: UUID) -> Forecast: ...
    def forecasts(self, revision_id: UUID) -> list[Forecast]: ...
    def step(self, step_id: UUID) -> ForecastStep: ...
    def save_step(self, value: ForecastStep, expected_version: int | None) -> ForecastStep: ...


class ForecastService:
    def __init__(
        self,
        sessions: SessionRepository,
        repository: ForecastRepository,
        assessments: AssessmentService,
        provider: LLMProvider | None = None,
    ) -> None:
        self.sessions, self.repository = sessions, repository
        self.assessments, self.provider = assessments, provider

    def get(self, forecast_id: UUID) -> Forecast:
        return self.repository.forecast(forecast_id)

    def history(self, sid: UUID, revision: int) -> list[Forecast]:
        return self.repository.forecasts(self.sessions.revision(sid, revision).id)

    def run(
        self,
        sid: UUID,
        revision: int,
        assessment_id: UUID,
        spec: ForecastSpec,
        *,
        numeric: bool = False,
        request_id: UUID | None = None,
        _step: ForecastStep | None = None,
    ) -> Forecast:
        if request_id is not None and _step is None:
            try:
                saved_step = self.repository.step(request_id)
            except NotFound:
                pass
            else:
                if (
                    saved_step.session_id != sid
                    or saved_step.revision != revision
                    or saved_step.assessment_id != assessment_id
                    or saved_step.spec != spec
                    or saved_step.numeric != numeric
                ):
                    raise Conflict("步骤幂等键已用于不同输入")
                return self.resume(request_id)
        session = self.sessions.load(sid)
        if session.status not in {"STRUCTURED", "FAILED"} or any(
            r.status == "running" for r in session.research_runs
        ):
            raise Conflict("预测需要已确认且无执行中任务的事件；中断预测先显式 abandon")
        if session.current_revision != revision:
            raise Conflict("只能预测当前事件版本")
        event = self.sessions.revision(sid, revision)
        if not event.confirmed or not event.question or event.question.missing():
            raise AppError("预测需要已确认且完整的预测问题")
        views = self.assessments.history(sid, revision)
        view = next((v for v in views if v.assessment.id == assessment_id), None)
        if view is None:
            raise NotFound("当前事件版本中不存在该评估")
        if view.stale:
            raise Conflict("评估已失效，请重新评估后预测")
        assessment = view.assessment
        method = "llm_judgment" if numeric else "scenario_only"
        if method not in assessment.available_methods:
            raise AppError("方法资格不满足；需重新评估并显式开启数值估计，不能自动降级")
        if numeric and event.question.outcome_type == "continuous":
            raise AppError("连续结果的数值预测尚未实现，请使用条件情景")
        if numeric and spec.distribution != "exclusive_exhaustive":
            raise AppError("数值分类需要经审查的互斥穷尽结果；重叠目标请拆为独立二元问题")
        if event.question.outcome_type == "binary" and (
            len(spec.outcomes) != 2 or spec.distribution != "exclusive_exhaustive"
        ):
            raise AppError("二元问题须明确两个互斥穷尽的结果定义")
        if self.provider is None:
            raise AppError("未配置模型服务；预测不会降级为模拟输出")
        evidence = self.assessments.evidence
        reviewed_ids = {r.span_id for r in assessment.review.evidence}
        documents = evidence.list_documents(sid, revision)
        spans = [s for d in documents for s in evidence.spans(d.id) if s.id in reviewed_ids]
        document_ids = {s.document_id for s in spans}
        documents = [d for d in documents if d.id in document_ids]
        payload = {
            "method": method,
            "event": event.model_dump(mode="json"),
            "assessment": assessment.model_dump(mode="json"),
            "spec": spec.model_dump(mode="json"),
            "reviewed_evidence": [s.model_dump(mode="json") for s in spans],
            "sources": [d.model_dump(mode="json") for d in documents],
            "cases": [],
        }
        step = _step or self.repository.save_step(
            ForecastStep(
                id=request_id or uuid4(),
                session_id=sid,
                revision=revision,
                assessment_id=assessment_id,
                spec=spec,
                numeric=numeric,
                model_id=self.provider.model_id,
                structured_mode=self.provider.structured_mode,
                output_token_limit=self.provider.output_token_limit,
                prompt_version=FORECAST_PROMPT_VERSION,
                method_version=FORECAST_METHOD_VERSION,
            ),
            None,
        )
        step = self.repository.save_step(
            step.model_copy(update={"status": "running"}), step.version
        )
        # Persist the lock and share the Reader/search session quotas, including repairs.
        active = self.sessions.save(
            transition(session, "FORECASTING").model_copy(
                update={"active_forecast_step_id": step.id}
            ),
            session.version,
        )

        def validate(content: str) -> ForecastDraft:
            draft = ForecastDraft.model_validate_json(content)
            validate_judgment(draft, spec, method, reviewed_ids)
            return draft

        active, draft = ModelCalls(self.sessions, self.provider).run(
            active,
            FORECAST_SYSTEM,
            json.dumps(payload, ensure_ascii=False),
            ForecastDraft.model_json_schema(),
            validate,
            cancelled=lambda: self.repository.step(step.id).status == "cancelled",
        )
        # Recheck snapshot integrity and version dependencies after an external call.
        try:
            latest = next(
                v
                for v in self.assessments.history(sid, revision)
                if v.assessment.id == assessment_id
            )
            if latest.stale or self.sessions.load(sid).version != active.version:
                raise Conflict("预测期间输入已变化，拒绝保存结果")
            probabilities, original_sum = validate_judgment(draft, spec, method, reviewed_ids)
            result = Forecast(
                session_id=sid,
                event=event,
                assessment=assessment,
                spec=spec,
                method=method,
                calibration_status="uncalibrated" if numeric else "not_applicable",
                model_id=self.provider.model_id,
                prompt_version=FORECAST_PROMPT_VERSION,
                structured_mode=self.provider.structured_mode,
                evidence_cutoff=assessment.cutoff,
                evidence_documents=documents,
                evidence_spans=spans,
                attempt_ids=[a.id for a in active.attempts if a.forecast_step_id == step.id],
                draft=draft,
                probabilities=probabilities,
                normalization_original_sum=original_sum,
            )
            self.repository.save_forecast(
                result, expected_session_version=active.version, step=step
            )
        except (AppError, ValueError):
            current = self.sessions.load(sid)
            if current.version == active.version:
                self.sessions.save(
                    transition(current, "FAILED").model_copy(
                        update={"error_code": "forecast_rejected"}
                    ),
                    current.version,
                )
            raise
        return result

    def resume(self, step_id: UUID, *, retry_interrupted: bool = False) -> Forecast:
        step = self.repository.step(step_id)
        if step.status == "cancelled":
            raise Conflict("预测步骤已取消，不能恢复")
        if step.status == "completed":
            return self.get(step.forecast_id)
        if self.provider is None:
            raise AppError("未配置模型服务")
        if (
            step.model_id != self.provider.model_id
            or step.structured_mode != self.provider.structured_mode
            or step.output_token_limit != self.provider.output_token_limit
            or step.prompt_version != FORECAST_PROMPT_VERSION
            or step.method_version != FORECAST_METHOD_VERSION
        ):
            raise Conflict("模型、提示词、方法或计算配置已变化，不能恢复旧步骤")
        session = self.sessions.load(step.session_id)
        if session.current_revision != step.revision:
            raise Conflict("事件版本已变化，不能恢复旧步骤")
        if step.status == "running":
            if not retry_interrupted:
                raise Conflict(
                    "中断调用结果及费用可能未知；显式 retry-interrupted 后重试可能重复计费"
                )
            if session.status == "FORECASTING":
                if session.active_forecast_step_id != step.id:
                    raise Conflict("另一个预测正在执行")
                self.abandon(step.session_id)
        return self.run(
            step.session_id,
            step.revision,
            step.assessment_id,
            step.spec,
            numeric=step.numeric,
            _step=step,
        )

    def cancel(self, step_id: UUID) -> ForecastStep:
        step = self.repository.step(step_id)
        if step.status == "completed":
            raise Conflict("已完成预测不能取消；历史不会删除")
        if step.status == "cancelled":
            return step
        return self.repository.save_step(
            step.model_copy(update={"status": "cancelled"}), step.version
        )

    def abandon(self, sid: UUID) -> None:
        session = self.sessions.load(sid)
        if session.status != "FORECASTING":
            raise Conflict("没有执行中的预测")
        attempts = [
            a.model_copy(update={"status": "failed", "error_code": "interrupted_cost_unknown"})
            if a.status == "started"
            else a
            for a in session.attempts
        ]
        self.sessions.save(
            transition(session, "FAILED").model_copy(
                update={"attempts": attempts, "error_code": "interrupted_cost_unknown"}
            ),
            session.version,
        )
