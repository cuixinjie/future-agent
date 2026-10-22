"""Create, read, clarify, edit and confirm persistent event dossiers."""

import hashlib
import json
import time
from collections.abc import Callable
from uuid import UUID, uuid4

from future_agent.application.model_calls import ModelCalls
from future_agent.application.ports import (
    AppError,
    Conflict,
    LLMProvider,
    SessionRepository,
)
from future_agent.domain.events import (
    AnalysisSession,
    EventDraft,
    EventRevision,
    SourceText,
    transition,
)
from future_agent.domain.research import DEFAULT_MAX_PAGES, DEFAULT_MAX_SEARCH_QUERIES
from future_agent.prompts.reader import READER_SYSTEM, READER_VERSION


def questions(draft: EventDraft) -> list[str]:
    target = draft.question
    missing = target.missing() if target else ["question", "window", "resolution"]
    prompts: list[str] = []
    if "question" in missing or "outcome_type" in missing:
        prompts.append("希望预测什么对象和可观察结果？")
    if any(field in missing for field in ("forecast_at", "window_start", "window_end", "window")):
        prompts.append("预测时点和观察窗口的具体起止时间是什么（请含时区）？")
    if any(
        field in missing for field in ("resolution_rule", "resolution_source_policy", "resolution")
    ):
        prompts.append("什么情况算结果发生，届时依据什么来源核验？")
    prompts.extend(item.question for item in draft.ambiguities)
    prompts.extend(item.description for item in draft.unknowns if item.resolution_status == "open")
    return (prompts or ["请核对原文分类、整理字段和目标，编辑后运行 confirm 确认。"])[0:3]


class EventService:
    def __init__(
        self,
        repository: SessionRepository,
        provider: LLMProvider | None = None,
        *,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.repository = repository
        self.provider = provider
        self.sleep = sleep

    def get(self, session_id: UUID) -> AnalysisSession:
        return self.repository.load(session_id)

    def revision(self, session_id: UUID, number: int | None = None) -> EventRevision:
        session = self.get(session_id)
        selected = session.current_revision if number is None else number
        if selected < 1:
            raise AppError("尚无事件档案，请先读取或导入人工档案")
        return self.repository.revision(session_id, selected)

    def history(self, session_id: UUID) -> list[EventRevision]:
        self.get(session_id)
        return self.repository.history(session_id)

    def create(
        self,
        raw: str,
        *,
        request_key: str | None = None,
        max_calls: int = 12,
        max_run_seconds: float = 600,
        max_total_tokens: int = 1000000,
        max_search_queries: int = DEFAULT_MAX_SEARCH_QUERIES,
        max_pages: int = DEFAULT_MAX_PAGES,
    ) -> AnalysisSession:
        # Retain M1 idempotency keys when the newly introduced quotas use defaults.
        fingerprint_input = [raw, max_calls, max_run_seconds, max_total_tokens]
        if (max_search_queries, max_pages) != (DEFAULT_MAX_SEARCH_QUERIES, DEFAULT_MAX_PAGES):
            fingerprint_input.extend([max_search_queries, max_pages])
        fingerprint = hashlib.sha256(
            json.dumps(fingerprint_input, ensure_ascii=False).encode()
        ).hexdigest()
        return self.repository.create(
            AnalysisSession(
                sources=[SourceText.from_raw(raw)],
                request_key=request_key,
                request_fingerprint=fingerprint,
                max_calls=max_calls,
                max_run_seconds=max_run_seconds,
                max_total_tokens=max_total_tokens,
                max_search_queries=max_search_queries,
                max_pages=max_pages,
            )
        )

    def _save(
        self, session: AnalysisSession, revision: EventRevision | None = None
    ) -> AnalysisSession:
        return self.repository.save(session, session.version, revision)

    def edit(
        self,
        session_id: UUID,
        expected_revision: int,
        draft: EventDraft,
        *,
        origin: str = "manual",
        confirmed: bool = False,
    ) -> AnalysisSession:
        session = self.repository.load(session_id)
        if session.status in {"READING", "FORECASTING"} or any(
            r.status == "running" for r in session.research_runs
        ):
            raise Conflict("读取执行中，不能编辑；中断后先显式恢复")
        return self._append(session, expected_revision, draft, origin=origin, confirmed=confirmed)

    def _append(
        self,
        session: AnalysisSession,
        expected_revision: int,
        draft: EventDraft,
        *,
        origin: str,
        confirmed: bool = False,
    ) -> AnalysisSession:
        if session.current_revision != expected_revision:
            raise Conflict("事件版本冲突，请重新读取当前档案")
        if origin == "model" and session.current_revision:
            previous = self.repository.revision(session.id, session.current_revision)
            kept = [item for item in previous.unknowns if item.resolution_status == "kept_unknown"]
            if kept:
                ids, descriptions = {item.id for item in kept}, {item.description for item in kept}
                draft = draft.model_copy(
                    update={
                        "unknowns": [
                            *[
                                item
                                for item in draft.unknowns
                                if item.id not in ids and item.description not in descriptions
                            ],
                            *kept,
                        ]
                    }
                )
        draft.validate_sources(session.sources, reader=origin == "model")
        if origin == "model" and not draft.claims:
            raise ValueError("Reader 未提取任何原文陈述，拒绝空成功结果")
        revision_id = uuid4()
        data = draft.model_dump(mode="json")
        if draft.question:
            data["question"] = draft.question.model_copy(
                update={"event_revision_id": revision_id}
            ).model_dump(mode="json")
        revision = EventRevision.model_validate(
            {
                **data,
                "id": revision_id,
                "session_id": session.id,
                "revision": session.current_revision + 1,
                "origin": origin,
                "confirmed": confirmed,
                "semantic_review": "user_confirmed" if confirmed else "pending_user_review",
                "model_id": self.provider.model_id if origin == "model" and self.provider else None,
                "prompt_version": READER_VERSION if origin == "model" else None,
            }
        )
        session = transition(session, "STRUCTURED" if confirmed else "NEEDS_INPUT")
        session = session.model_copy(
            update={
                "current_revision": revision.revision,
                "error_code": None,
                "pending_questions": [] if confirmed else questions(draft),
            }
        )
        return self._save(session, revision)

    def confirm(self, session_id: UUID, expected_revision: int) -> AnalysisSession:
        revision = self.repository.revision(session_id, expected_revision)
        draft = EventDraft.model_validate(
            revision.model_dump(
                exclude={
                    "id",
                    "session_id",
                    "revision",
                    "created_at",
                    "origin",
                    "model_id",
                    "prompt_version",
                    "confirmed",
                    "semantic_review",
                }
            )
        )
        return self.edit(session_id, expected_revision, draft, confirmed=True)

    def answer(
        self,
        session_id: UUID,
        expected_revision: int,
        raw: str,
        kept_unknown_ids: list[UUID] | None = None,
    ) -> AnalysisSession:
        session = self.repository.load(session_id)
        if (
            session.status in {"READING", "FORECASTING"}
            or any(r.status == "running" for r in session.research_runs)
            or session.current_revision != expected_revision
        ):
            raise Conflict("读取执行中或事件版本冲突")
        source = SourceText.from_raw(raw, kind="answer")
        if expected_revision:
            old = self.repository.revision(session_id, expected_revision)
            draft = EventDraft.model_validate(
                old.model_dump(include=EventDraft.model_fields.keys())
            )
        else:
            draft = EventDraft()
        kept = set(kept_unknown_ids or [])
        if not kept <= {item.id for item in draft.unknowns}:
            raise ValueError("保留未知的 ID 不存在")
        draft = draft.model_copy(
            update={
                "unknowns": [
                    item.model_copy(update={"resolution_status": "kept_unknown"})
                    if item.id in kept
                    else item
                    for item in draft.unknowns
                ]
            }
        )
        session = session.model_copy(update={"sources": [*session.sources, source]})
        return self._append(session, expected_revision, draft, origin="manual")

    def read(
        self,
        session_id: UUID,
        *,
        force: bool = False,
        retry_interrupted: bool = False,
    ) -> AnalysisSession:
        if self.provider is None:
            raise AppError("未配置模型。请配置服务，或显式使用 edit 导入人工档案")
        session = self.repository.load(session_id)
        if session.status == "FORECASTING":
            raise Conflict("预测执行中；中断后先显式 forecast abandon")
        if any(r.status == "running" for r in session.research_runs):
            raise Conflict("检索执行中，不能重新读取；中断后先显式 abandon")
        if session.status == "READING":
            if not retry_interrupted:
                raise AppError(
                    "上次读取可能中断且费用未知；检查后使用 --retry-interrupted 显式恢复"
                )
            attempts = [
                attempt.model_copy(
                    update={"status": "failed", "error_code": "interrupted_cost_unknown"}
                )
                if attempt.status == "started"
                else attempt
                for attempt in session.attempts
            ]
            session = self._save(
                transition(session, "FAILED").model_copy(
                    update={
                        "attempts": attempts,
                        "error_code": "interrupted_cost_unknown",
                    }
                )
            )
        elif session.current_revision and not force:
            return session
        session = self._save(transition(session, "READING"))
        initial = {
            "sources": [source.model_dump(mode="json") for source in session.sources],
            "schema": EventDraft.model_json_schema(),
        }
        if session.current_revision:
            initial["previous_draft"] = self.repository.revision(
                session.id, session.current_revision
            ).model_dump(mode="json")
        base_user = json.dumps(initial, ensure_ascii=False)
        session, draft = ModelCalls(self.repository, self.provider, sleep=self.sleep).run(
            session,
            READER_SYSTEM,
            base_user,
            EventDraft.model_json_schema(),
            lambda content: self._validate_reader(session, content),
        )
        return self._append(session, session.current_revision, draft, origin="model")

    def _validate_reader(self, session: AnalysisSession, content: str) -> EventDraft:
        draft = EventDraft.model_validate_json(content)
        draft.validate_sources(session.sources, reader=True)
        if not draft.claims:
            raise ValueError("Reader 未提取任何原文陈述，拒绝空成功结果")
        return draft
