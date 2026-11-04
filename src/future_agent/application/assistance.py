"""Ordinary analysis orchestration, reusing the evidence, research and forecast services."""

import json
from collections.abc import Callable
from datetime import timedelta
from typing import TypeVar
from uuid import UUID, uuid4

from pydantic import ValidationError

from future_agent.application.assessment import AssessmentService
from future_agent.application.events import EventService
from future_agent.application.forecasts import ForecastService
from future_agent.application.model_calls import ModelCalls
from future_agent.application.passage_selection import select_excerpts
from future_agent.application.ports import AppError, Conflict, NotFound
from future_agent.application.reference import ReferenceService
from future_agent.application.research import ResearchService
from future_agent.domain.assessment import AssessmentReview, ReviewedEvidence
from future_agent.domain.assistance import (
    AnalysisPlan,
    AssistanceState,
    EvidenceReading,
    PlannedQuery,
)
from future_agent.domain.contracts import Contract, now
from future_agent.domain.events import AnalysisSession, EventDraft, ForecastQuestion, transition
from future_agent.domain.evidence import EvidenceSpan
from future_agent.domain.forecasts import Forecast, ForecastSpec, OutcomeDefinition
from future_agent.domain.research import PUBLIC_QUERY_RULES, SearchPlan, SearchQuery, public_terms
from future_agent.prompts.assistance import (
    CASE_PLAN_SYSTEM,
    PLAN_SYSTEM,
    PLAN_VERSION,
    READING_SYSTEM,
    READING_VERSION,
    REFERENCE_PLAN_SYSTEM,
)


def research_page_limit(session: AnalysisSession, round_index: int) -> int:
    used = sum(a.kind == "page" for run in session.research_runs for a in run.attempts)
    remaining = max(0, session.max_pages - used)
    return min(6, remaining // 2 if round_index == 0 and remaining > 1 else remaining)


T = TypeVar("T", bound=Contract)
MAX_BODY_CHARS = 24000


def plan_repair_hint(exc: Exception, contract: type[Contract] = AnalysisPlan) -> str:
    """Give only fixed rules and schema field names, never model-supplied values."""
    rules = {
        *PUBLIC_QUERY_RULES.values(),
        "绝对窗口必须同时提供起止时间",
        "绝对窗口顺序错误",
        "结果键重复",
        "二元问题必须有两个结果",
        "条件分析必须明确行动或适用条件",
        "查询不得重复",
        "整次分析的澄清机会已用完",
        "不得改写原文已明确的目标、窗口或规则",
        "无法定义目标须提供原因",
        "时间必须包含时区",
        "文本不能为空白",
    }
    if isinstance(exc, ValidationError):
        hints = []
        schema = contract.model_json_schema()
        for error in exc.errors(include_url=False, include_input=False):
            if error["type"] == "json_invalid":
                hints.append(
                    "输出不是有效 JSON：只返回一个 JSON 对象，"
                    "不要 Markdown 代码围栏、解释或思考文本"
                )
                continue
            # Resolve only schema-owned names; extra keys and dict keys are untrusted.
            node, path = schema, []
            for part in error["loc"]:
                if "$ref" in node:
                    node = schema["$defs"][node["$ref"].rsplit("/", 1)[-1]]
                if isinstance(part, int) and node.get("type") == "array":
                    path.append(f"[{part}]")
                    node = node["items"]
                elif isinstance(part, str) and part in node.get("properties", {}):
                    path.append(part)
                    node = node["properties"][part]
                else:
                    break
            location = ".".join(path) or "根对象"
            context = error.get("ctx", {}).get("error")
            if context is not None and str(context) in rules:
                hints.append(f"{location}：{context}")
            elif error["type"] == "extra_forbidden":
                hints.append(f"{location} 含 Schema 未定义的字段，请删除额外字段")
            elif error["type"] == "missing":
                hints.append(f"{location} 缺少必填字段，请按 Schema 补齐")
            elif error["type"] == "literal_error":
                hints.append(f"{location} 必须使用 Schema 列出的枚举值")
            else:
                hints.append(f"{location} 不符合 Schema，请检查类型、必填项、范围和数量限制")
        return "；".join(dict.fromkeys(hints[:5]))
    return str(exc) if str(exc) in rules else ""


def validate_public_queries(queries: list[PlannedQuery], field: str) -> None:
    errors = []
    for index, query in enumerate(queries):
        try:
            public_terms(query.query)
        except ValueError as exc:
            errors.append(
                {
                    "type": "value_error",
                    "loc": (field, index, "query"),
                    "ctx": {"error": exc},
                }
            )
    if errors:
        raise ValidationError.from_exception_data("PublicQueries", errors)


class AssistanceService:
    def __init__(
        self,
        events: EventService,
        research: ResearchService,
        assessments: AssessmentService,
        forecasts: ForecastService,
        references: ReferenceService | None = None,
    ) -> None:
        self.events, self.research, self.assessments, self.forecasts = (
            events,
            research,
            assessments,
            forecasts,
        )
        self.references = references
        self.sessions, self.evidence = events.repository, assessments.evidence

    def state(self, sid: UUID) -> AssistanceState:
        return self.sessions.load(sid).assistance or AssistanceState()

    def record_stop(self, sid: UUID) -> None:
        self._save(sid, stop_reason="用户已停止分析；已消耗预算保留")

    def _save(self, sid: UUID, **changes) -> AssistanceState:
        session = self.sessions.load(sid)
        state = AssistanceState.model_validate({**self.state(sid).model_dump(), **changes})
        self.sessions.save(session.model_copy(update={"assistance": state}), session.version)
        return state

    def _generate(
        self,
        sid: UUID,
        system: str,
        version: str,
        payload: dict,
        contract: type[T],
        validate=None,
        *,
        progress: Callable[[str], None] = lambda _: None,
    ) -> T:
        if self.events.provider is None:
            raise AppError("未配置模型服务；已保存原文，不生成模拟分析")
        session = self.sessions.load(sid)
        if session.status in {"READING", "FORECASTING"} or any(
            r.status == "running" for r in session.research_runs
        ):
            raise Conflict("存在执行中或中断步骤；请先恢复或取消")
        session = self.sessions.save(transition(session, "READING"), session.version)

        def check(text: str) -> T:
            result = contract.model_validate_json(text)
            if validate:
                validate(result)
            return result

        active, result = ModelCalls(self.sessions, self.events.provider).run(
            session,
            system,
            json.dumps({"prompt_version": version, **payload}, ensure_ascii=False),
            contract.model_json_schema(),
            check,
            progress=progress,
            repair_hint=lambda exc: plan_repair_hint(exc, contract),
        )
        self.sessions.save(transition(active, "NEEDS_INPUT"), active.version)
        progress("模型步骤已结束。")
        return result

    def prepare(
        self, sid: UUID, *, progress: Callable[[str], None] = lambda _: None
    ) -> AssistanceState:
        session, state = self.sessions.load(sid), self.state(sid)
        if state.stop_reason and state.stop_reason.startswith("用户已停止"):
            state = self._save(sid, stop_reason=None)
        if (
            state.plan
            and state.model_id
            and self.events.provider
            and state.model_id != self.events.provider.model_id
        ):
            raise Conflict("当前分析的模型已变化；请新建会话，保留原预测与成本记录")
        if session.status in {"READING", "FORECASTING"} or any(
            r.status == "running" for r in session.research_runs
        ):
            raise Conflict("存在执行中或中断步骤，请先 /retry 或 /cancel")
        if (
            state.plan
            and state.source_count == len(session.sources)
            and state.event_revision == session.current_revision
        ):
            return state
        # A fresh source invalidates dependent analysis, but never resets the clarification ledger.
        self._save(
            sid,
            stage="planning",
            plan=None,
            reference_binding=None,
            reference_preview=None,
            reading=None,
            reading_version=None,
            forecast_id=None,
            forecast_request_id=None,
            assessment_id=None,
            final_revision=None,
            research_revision=None,
            research_rounds=0,
            approved_queries=[],
            pending_queries=[],
            research_gaps=[],
            stop_reason=None,
        )
        if not session.current_revision or state.source_count != len(session.sources):
            progress("正在读取并整理原文。")
            self.events.read(sid, force=bool(session.current_revision), progress=progress)
        event, session = self.events.revision(sid), self.sessions.load(sid)
        self._save(
            sid,
            source_count=len(session.sources),
            event_revision=event.revision,
            model_id=self.events.provider.model_id if self.events.provider else None,
        )

        def validate(plan: AnalysisPlan) -> None:
            if state.clarification_used and plan.questions:
                raise ValueError("整次分析的澄清机会已用完")
            validate_public_queries(plan.queries, "queries")
            if event.question and event.semantic_review != "system_reviewed":
                for name in (
                    "question",
                    "outcome_type",
                    "window_start",
                    "window_end",
                    "resolution_rule",
                    "resolution_source_policy",
                ):
                    original = getattr(event.question, name)
                    if original is not None and getattr(plan, name) != original:
                        raise ValueError("不得改写原文已明确的目标、窗口或规则")
            if not plan.usable() and not plan.questions and not plan.unavailable_reason:
                raise ValueError("无法定义目标须提供原因")

        progress("正在拟定预测目标和研究计划。")
        plan = self._generate(
            sid,
            PLAN_SYSTEM + (REFERENCE_PLAN_SYSTEM + CASE_PLAN_SYSTEM if self.references else ""),
            "analysis-plan-reference-1.1" if self.references else PLAN_VERSION,
            {
                "sources": [s.model_dump(mode="json") for s in session.sources],
                "event": event.model_dump(mode="json"),
                "locked_target": event.question.model_dump(
                    mode="json",
                    exclude_none=True,
                    include={
                        "question",
                        "outcome_type",
                        "resolution_rule",
                        "resolution_source_policy",
                        "window_start",
                        "window_end",
                    },
                )
                if event.question and event.semantic_review != "system_reviewed"
                else {},
                "now": now().isoformat(),
                "clarification_available": not state.clarification_used,
                "previous_clarification_answer": state.clarification_answer,
                "previous_clarification_questions": [
                    q.model_dump(mode="json") for q in state.clarification_questions
                ],
            },
            AnalysisPlan,
            validate,
            progress=progress,
        )
        stage = "clarification" if plan.questions and not state.clarification_used else "planned"
        if stage == "planned" and not plan.usable():
            stage = "unanswerable"
        return self._save(
            sid,
            plan=plan,
            source_count=len(session.sources),
            event_revision=event.revision,
            stage=stage,
            clarification_used=state.clarification_used or stage == "clarification",
            clarification_questions=plan.questions
            if stage == "clarification"
            else state.clarification_questions,
            stop_reason=plan.unavailable_reason,
        )

    def answer(self, sid: UUID, answer: str) -> AssistanceState:
        state = self.state(sid)
        if state.stage != "clarification":
            raise Conflict("没有待回答的澄清轮次")
        text = answer.strip() or "用户明确不知道或跳过本轮问题；保留未知，不再追问。"
        session = self.sessions.load(sid)
        if (
            not answer.strip()
            and state.reading is not None
            and state.plan is not None
            and state.plan.usable()
            and state.research_revision == session.current_revision
            and state.event_revision == session.current_revision
            and state.source_count == len(session.sources)
            and state.clarification_questions
            and all(q.kind == "private_fact" for q in state.clarification_questions)
        ):
            # No new facts: keep the evidence and its authorization instead of replanning.
            # Persist each unresolved question so the final judgment sees the limitation.
            return self._save(
                sid,
                clarification_answer=text,
                stage="reviewing",
                research_gaps=[
                    *state.research_gaps,
                    *[
                        f"用户跳过，仍未知：{q.question}；对判断的影响：{q.changes_judgment}"
                        for q in state.clarification_questions
                    ],
                ],
            )
        self.events.answer(sid, session.current_revision, text)
        self._save(sid, clarification_answer=text, stage="planning")
        return self.prepare(sid)

    def _target(self, sid: UUID, *, final: bool) -> int:
        state, event = self.state(sid), self.events.revision(sid)
        plan = state.plan
        assert plan is not None
        cutoff = now()
        session = self.sessions.load(sid)
        # Predeclare a near-future window with enough room for remaining bounded generation.
        # This prevents a long model call from leaking an already-started outcome into a forecast.
        start = plan.window_start or cutoff + timedelta(
            seconds=max(1, session.max_run_seconds - session.consumed_seconds) + 1
        )
        if start <= cutoff:
            raise AppError("用户指定的绝对窗口已开始；不能悄悄移动窗口或把已发生结果当预测")
        question = ForecastQuestion(
            question=plan.question,
            forecast_at=cutoff,
            window_start=start,
            window_end=plan.window_end or start + timedelta(days=plan.duration_days),
            outcome_type=plan.outcome_type,
            resolution_rule=plan.resolution_rule,
            resolution_source_policy=plan.resolution_source_policy,
        )
        draft = EventDraft.model_validate(event.model_dump(include=EventDraft.model_fields.keys()))
        self.events.edit(
            sid,
            event.revision,
            draft.model_copy(update={"question": question}),
            confirmed=True,
            system_reviewed=True,
        )
        revision = self.sessions.load(sid).current_revision
        self._save(sid, event_revision=revision, **({"final_revision": revision} if final else {}))
        return revision

    def _research(
        self,
        sid: UUID,
        indexes: list[int],
        *,
        queries: list[PlannedQuery] | None = None,
        progress: Callable[[str], None] = lambda _: None,
    ) -> None:
        state, event, session = self.state(sid), self.events.revision(sid), self.sessions.load(sid)
        if state.research_rounds >= 2:
            return
        queries = queries if queries is not None else [state.plan.queries[i] for i in indexes]
        if any(q.query not in state.approved_queries for q in queries):
            raise Conflict("查询超出本次已授权公开范围")
        remaining = session.max_run_seconds - session.consumed_seconds
        # Preserve time and calls for evidence interpretation and the actual judgment.
        if remaining <= 120 or session.max_calls - len(session.attempts) < 2:
            self._save(
                sid, research_gaps=[*state.research_gaps, "研究停止：为证据分析与预测保留预算"]
            )
            return
        if research_page_limit(session, state.research_rounds) == 0:
            self._save(sid, research_gaps=[*state.research_gaps, "页面预算已耗尽，无法继续研究"])
            return
        self._save(sid, stage="researching", research_rounds=state.research_rounds + 1)
        run = self.research.start(
            sid,
            event.revision,
            "",
            "",
            planned=SearchPlan(
                event_revision_id=event.id,
                question_id=event.question.id,
                cutoff=event.question.forecast_at,
                planner_version=PLAN_VERSION,
                queries=[SearchQuery(query=q.query, purpose=q.purpose) for q in queries],
                skipped=[],
            ),
            page_limit=research_page_limit(session, state.research_rounds),
            progress=progress,
            seconds_limit=min(120, remaining - 120),
        )
        progress("本轮研究已结束。")
        self._save(
            sid, stage="reviewing", research_gaps=[*self.state(sid).research_gaps, *run.gaps]
        )

    def authorize_followup(
        self, sid: UUID, approved: bool, *, progress: Callable[[str], None] = lambda _: None
    ) -> None:
        state = self.state(sid)
        if state.stage != "search_authorization" or not state.pending_queries:
            raise Conflict("没有待授权的补充查询")
        queries = state.pending_queries
        if approved:
            self._save(sid, approved_queries=[*state.approved_queries, *(q.query for q in queries)])
            self._research(sid, [], queries=queries, progress=progress)
            current = self.state(sid)
            reading = (
                None
                if current.research_rounds > state.research_rounds
                else state.reading.model_copy(update={"followup_queries": []})
            )
            self._save(sid, reading=reading, pending_queries=[], stage="reviewing")
        else:
            reading = state.reading.model_copy(update={"followup_queries": []})
            self._save(
                sid,
                reading=reading,
                pending_queries=[],
                stage="reviewing",
                research_gaps=[*state.research_gaps, "用户未授权新增补充查询"],
            )

    def _reading(
        self,
        sid: UUID,
        allowed_followups: list[int],
        *,
        progress: Callable[[str], None] = lambda _: None,
    ) -> EvidenceReading:
        state, event = self.state(sid), self.events.revision(sid)
        research_gaps = list(state.research_gaps)
        for run in self.sessions.load(sid).research_runs:
            if run.plan.event_revision_id == event.id:
                if run.status != "completed":
                    research_gaps.append(f"research_{run.status}:{run.id}")
                research_gaps.extend(run.gaps)
        self._save(sid, research_gaps=list(dict.fromkeys(research_gaps)))
        state = self.state(sid)
        selected = self.evidence.select(sid, event.revision, now(), experiment=False)
        eligible = {s.document_id for s in selected if s.eligible}
        bodies, remaining, truncated = {}, MAX_BODY_CHARS, False
        documents = [
            doc
            for doc in sorted(
                self.evidence.list_documents(sid, event.revision),
                key=lambda d: (d.imported_at, str(d.id)),
            )
            if doc.id in eligible
        ]
        target = " ".join(
            [state.plan.question or "", *(q.decision_gap for q in state.plan.queries)]
        )
        for index, doc in enumerate(documents):
            text = self.evidence.snapshot(doc.id).normalized
            take = min(4000, remaining // (len(documents) - index))
            if take <= 0:
                truncated = True
                break
            passages = select_excerpts(text, target, take)
            bodies[doc.id] = {
                "document_id": str(doc.id),
                "url": doc.source.url,
                "passages": [{"body_start": start, "body": body} for start, body in passages],
                "truncated": len(text) > take,
            }
            truncated |= len(text) > take
            remaining -= sum(len(body) for _, body in passages)
        self._save(sid, stage="reviewing")

        def validate(value: EvidenceReading) -> None:
            if value.questions and (
                state.clarification_used or any(q.kind != "private_fact" for q in value.questions)
            ):
                raise ValueError("研究后仅允许使用剩余额度询问私人事实")
            for group in value.source_relationships:
                if len(set(group.document_ids)) < 2 or not set(group.document_ids) <= set(bodies):
                    raise ValueError("来源关系必须引用至少两份本次可见文档")
            seen = set()
            for citation in value.citations:
                document = bodies.get(citation.document_id)
                if (
                    document is None
                    or sum(p["body"].count(citation.quote) for p in document["passages"]) != 1
                ):
                    raise ValueError("引用必须来自本次可见正文且精确唯一")
                if (
                    self.evidence.snapshot(citation.document_id).normalized.count(citation.quote)
                    != 1
                ):
                    raise ValueError("引用在完整快照中不唯一，无法可靠定位")
                key = (citation.document_id, citation.quote, citation.purpose)
                if key in seen:
                    raise ValueError("引用用途重复")
                seen.add(key)
            if not set(value.followup_query_indexes) <= set(allowed_followups):
                raise ValueError("补充研究超出授权查询或轮次")
            if value.followup_queries:
                if (
                    state.research_rounds != 1
                    or not state.approved_queries
                    or value.followup_query_indexes
                ):
                    raise ValueError("新增查询仅能在首轮研究后单独提出")
                seen_queries = {q.query.casefold().strip() for q in state.plan.queries}
                validate_public_queries(value.followup_queries, "followup_queries")
                for query in value.followup_queries:
                    key = query.query.casefold().strip()
                    if key in seen_queries:
                        raise ValueError("新增查询不得重复已有查询")
                    seen_queries.add(key)
            if value.numeric_basis.status == "supported":
                if value.numeric_basis.source_kind == "external" and not value.citations:
                    raise ValueError("外部数值依据必须有正文引用")
                if value.numeric_basis.source_kind == "user_report" and not event.claims:
                    raise ValueError("用户依据必须有原文陈述")

        progress(f"已选取{len(bodies)}份正文；正在检查来源关系、目标覆盖和引用。")
        reading = self._generate(
            sid,
            READING_SYSTEM,
            READING_VERSION,
            {
                "event": event.model_dump(mode="json"),
                "plan": state.plan.model_dump(mode="json"),
                "documents": list(bodies.values()),
                "allowed_followup_indexes": allowed_followups,
                "allow_new_followup_queries": bool(
                    state.research_rounds == 1 and state.approved_queries
                ),
                "research_gaps": state.research_gaps,
                "historical_reference_preview": state.reference_preview.model_dump(mode="json")
                if state.reference_preview
                else None,
                "clarification_available": not state.clarification_used,
            },
            EvidenceReading,
            validate,
            progress=progress,
        )
        gaps = [*self.state(sid).research_gaps, *reading.coverage_gaps]
        gaps.extend(
            "模型识别的共同来源（非独立核实）：" + g.basis for g in reading.source_relationships
        )
        if truncated:
            gaps.append(
                "正文上下文按目标词项选择多个不连续原文片段（每页最多4000/总24000字符），未读部分不作依据；词项匹配不证明相关性"
            )
        self._save(
            sid,
            reading=reading,
            reading_version=READING_VERSION,
            research_gaps=gaps,
            stop_reason=reading.research_stop_reason,
        )
        return reading

    def execute(
        self,
        sid: UUID,
        *,
        allow_search: bool = False,
        progress: Callable[[str], None] = lambda _: None,
    ) -> Forecast:
        state = self.prepare(sid, progress=progress)
        if state.forecast_id:
            return self.forecasts.get(state.forecast_id)
        if state.final_revision is not None:
            completed = [
                f
                for f in self.forecasts.history(sid, state.final_revision)
                if f.method_version
                in {
                    "judgment-2.0",
                    "judgment-2.1",
                    "judgment-reference-1.0",
                    "judgment-reference-1.1",
                    "judgment-case-reference-1.0",
                }
                and f.assessment.id == state.assessment_id
            ]
            if completed:
                result = max(completed, key=lambda f: f.created_at)
                self._save(sid, stage="complete", forecast_id=result.id)
                return result
        if state.stage in {"clarification", "unanswerable"} or not state.plan.usable():
            raise AppError(
                state.stop_reason or "请回答或跳过唯一一轮澄清；无法定义目标时不强行预测"
            )
        if state.stage == "search_authorization":
            raise AppError("新增补充查询等待用户授权或跳过")
        if state.pending_queries and state.research_rounds >= 2:
            state = self._save(sid, pending_queries=[], reading=None, stage="reviewing")
        if state.stage == "planned":
            revision = self._target(sid, final=False)
            if self.references:
                q = self.events.revision(sid).question
                preview = self.references.check(
                    state.plan, q.forecast_at, q.window_start, q.window_end
                )
                self._save(sid, reference_preview=preview)
            self._save(sid, research_revision=revision)
            queries = state.plan.queries
            approved = [q.query for q in queries] if allow_search else []
            self._save(sid, approved_queries=approved, stage="reviewing")
            if allow_search and queries and self.research.search and self.research.pages:
                progress("正在搜索并抓取正文；单轮研究最多120秒。")
                self._research(sid, list(range(min(2, len(queries)))), progress=progress)
            else:
                self._save(sid, research_gaps=["本次未联网或未配置搜索；仅使用已有资料与明确假设"])
        state = self.state(sid)
        if state.stage == "ready" and now() >= self.events.revision(sid).question.window_start:
            if state.plan.window_start:
                raise AppError("固定窗口已开始，不能重试为事前预测")
            self._save(
                sid,
                stage="reviewing",
                final_revision=None,
                assessment_id=None,
                forecast_request_id=None,
            )
            state = self.state(sid)
        if state.stage != "ready":
            followups = (
                list(range(2, len(state.plan.queries)))
                if state.approved_queries and state.research_rounds == 1
                else []
            )
            progress("正在分析正文与引用。")
            reading = state.reading or self._reading(sid, followups, progress=progress)
            if reading.followup_queries and self.state(sid).research_rounds == 1:
                self._save(
                    sid, stage="search_authorization", pending_queries=reading.followup_queries
                )
                raise AppError("新增补充查询等待用户授权或跳过")
            if reading.followup_query_indexes and self.state(sid).research_rounds == 1:
                # A resumed run must not reuse the reading from before the follow-up.
                self._save(sid, reading=None)
                progress("正在补充搜索并抓取正文；单轮研究最多120秒。")
                self._research(sid, reading.followup_query_indexes, progress=progress)
                reading = self._reading(sid, [], progress=progress)
            current = self.state(sid)
            failed_network = any(
                attempt.status == "failed"
                for run in self.sessions.load(sid).research_runs
                if run.plan.event_revision_id == self.events.revision(sid).id
                for attempt in run.attempts
            )
            if (
                current.approved_queries
                and failed_network
                and not reading.citations
                and reading.numeric_basis.status != "supported"
                and reading.numeric_basis.source_kind != "user_report"
            ):
                reason = "研究服务失败且未取得可用判断依据；执行未完成，请修复通道或补充资料后继续"
                self._save(sid, stage="reviewing", stop_reason=reason)
                raise AppError(reason)
            if reading.questions and not self.state(sid).clarification_used:
                self._save(
                    sid,
                    stage="clarification",
                    clarification_used=True,
                    clarification_questions=reading.questions,
                )
                raise AppError("研究后需要一次关键澄清；可回答或跳过后继续")
            old_revision = self.state(sid).research_revision
            revision = self._target(sid, final=True)
            links = self.evidence.link_revision(sid, old_revision, revision)
            reviewed = []
            for citation in reading.citations:
                document_id = links[citation.document_id]
                text = self.evidence.snapshot(document_id).normalized
                start = text.find(citation.quote)
                span = self.evidence.cite(
                    sid,
                    revision,
                    EvidenceSpan(
                        document_id=document_id,
                        quote=citation.quote,
                        start=start,
                        end=start + len(citation.quote),
                        claim_ids=[],
                    ),
                )
                reviewed.append(
                    ReviewedEvidence(
                        span_id=span.id,
                        purpose=citation.purpose,
                        relevance_reason=citation.relevance_reason,
                    )
                )
            if self.references:
                event = self.events.revision(sid)
                q = event.question
                binding = self.references.bind(
                    event.id, self.state(sid).plan, q.forecast_at, q.window_start, q.window_end
                )
                self._save(sid, reference_binding=binding)
            assessment = self.assessments.assess(
                sid,
                revision,
                AssessmentReview(
                    reviewer=(
                        f"model:{self.events.provider.model_id}/"
                        f"{self.state(sid).reading_version or 'unknown-legacy'}"
                    ),
                    review_origin="model",
                    evidence=reviewed,
                    numeric_basis=reading.numeric_basis,
                    defer_delivery_choice=self.state(sid).reading_version == READING_VERSION,
                    allow_numeric=True,
                    limitations=[
                        *self.state(sid).research_gaps,
                        *reading.material_gaps,
                        *reading.numeric_basis.limitations,
                    ],
                ),
                experiment=False,
            )
            self._save(sid, stage="ready", assessment_id=assessment.id, final_revision=revision)
        state = self.state(sid)
        assessment = next(
            v.assessment
            for v in self.assessments.history(sid, state.final_revision)
            if v.assessment.id == state.assessment_id
        )
        plan = state.plan
        numeric = (
            "llm_judgment" in assessment.available_methods
            and plan.outcome_type != "continuous"
            and plan.analysis_kind != "exploration"
        )
        request_id = state.forecast_request_id
        if request_id is not None:
            try:
                saved_step = self.forecasts.step(request_id)
            except NotFound:
                pass  # The request ID may be durable before the forecast step is created.
            else:
                if saved_step.status == "cancelled":
                    request_id = None
        if request_id is None:
            request_id = uuid4()
            self._save(sid, forecast_request_id=request_id)
        progress("正在生成最终判断；模型单次请求最多60秒。")
        result = self.forecasts.run(
            sid,
            state.final_revision,
            state.assessment_id,
            ForecastSpec(
                schema_version="2.0",
                outcomes=[
                    OutcomeDefinition(key=o.key, definition=o.definition) for o in plan.outcomes
                ],
                distribution="exclusive_exhaustive",
                definition_review="系统拟定：" + plan.definition_reason,
                guidance=plan,
            ),
            numeric=numeric,
            request_id=request_id,
            progress=progress,
            reference_binding=state.reference_binding,
        )
        self._save(
            sid, stage="complete", forecast_id=result.id, event_revision=result.event.revision
        )
        return result
