"""Sequential research with durable shared quotas and explicit partial outcomes."""

import time
from collections.abc import Callable
from itertools import zip_longest
from typing import TypeVar
from urllib.parse import urlsplit
from uuid import UUID

from future_agent.application.evidence import EvidenceService
from future_agent.application.network import (
    NETWORK_CALL_TIMEOUT,
    NetworkError,
    PageReader,
    SearchProvider,
)
from future_agent.application.ports import AppError, Conflict, SessionRepository
from future_agent.domain.events import AnalysisSession, EventRevision
from future_agent.domain.evidence import EvidenceImport, canonical_url
from future_agent.domain.research import (
    NetworkAttempt,
    ResearchRun,
    SearchPlan,
    SearchQuery,
    public_terms,
)

MAX_NETWORK_RETRIES = 2
T = TypeVar("T")


class SearchPlanner:
    def plan(self, event: EventRevision, subject: str, focus: str) -> SearchPlan:
        if not event.confirmed or not event.question or event.question.missing():
            raise AppError("研究需要已确认且完整的预测问题")
        subject, focus = public_terms(subject), public_terms(focus)
        return SearchPlan(
            event_revision_id=event.id,
            question_id=event.question.id,
            cutoff=event.question.forecast_at,
            queries=[
                SearchQuery(query=f'"{subject}" {focus} status official', purpose="current_state"),
                SearchQuery(
                    query=f'"{subject}" {focus} delay cancelled problems', purpose="counterevidence"
                ),
            ],
        )


class ResearchService:
    def __init__(
        self,
        sessions: SessionRepository,
        evidence: EvidenceService,
        search: SearchProvider | None,
        pages: PageReader | None,
        *,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.sessions, self.evidence = sessions, evidence
        self.search, self.pages, self.sleep = search, pages, sleep

    def runs(self, session_id: UUID) -> list[ResearchRun]:
        return self.sessions.load(session_id).research_runs

    def _load(self, sid: UUID, rid: UUID) -> tuple[AnalysisSession, ResearchRun]:
        session = self.sessions.load(sid)
        for run in session.research_runs:
            if run.id == rid:
                return session, run
        raise AppError("研究记录不存在")

    def _save(self, session: AnalysisSession, run: ResearchRun, **changes) -> AnalysisSession:
        return self.sessions.save(
            session.model_copy(
                update={
                    **changes,
                    "research_runs": [run if r.id == run.id else r for r in session.research_runs],
                }
            ),
            session.version,
        )

    def cancel(self, sid: UUID, rid: UUID, *, abandon: bool = False) -> ResearchRun:
        session, run = self._load(sid, rid)
        if run.status != "running":
            return run
        update = {"cancel_requested": True}
        if abandon:
            update.update(
                status="cancelled", gaps=[*run.gaps, "interrupted_request_outcome_unknown"]
            )
            update["attempts"] = [
                a.model_copy(
                    update={"status": "failed", "error_code": "interrupted_request_outcome_unknown"}
                )
                if a.status == "started"
                else a
                for a in run.attempts
            ]
        run = run.model_copy(update=update)
        self._save(session, run)
        return run

    def _active(self, sid: UUID, rid: UUID) -> tuple[AnalysisSession, ResearchRun]:
        session, run = self._load(sid, rid)
        if run.status != "running" or run.cancel_requested:
            raise NetworkError("cancelled")
        return session, run

    def _gap(self, sid: UUID, rid: UUID, code: str) -> None:
        session, run = self._load(sid, rid)
        self._save(session, run.model_copy(update={"gaps": list(dict.fromkeys([*run.gaps, code]))}))

    def _call(
        self,
        sid: UUID,
        rid: UUID,
        kind: str,
        target: str,
        operation: Callable[[float], T],
        deadline: float,
    ) -> T:
        for retry in range(MAX_NETWORK_RETRIES + 1):
            session, run = self._active(sid, rid)
            attempts = [a for r in session.research_runs for a in r.attempts if a.kind == kind]
            limit = session.max_search_queries if kind == "search" else session.max_pages
            remaining = min(
                deadline - time.monotonic(), session.max_run_seconds - session.consumed_seconds
            )
            if len(attempts) >= limit or remaining <= 0:
                raise NetworkError("budget_exhausted")
            timeout = min(NETWORK_CALL_TIMEOUT, remaining)
            attempt = NetworkAttempt(kind=kind, target=target, reserved_seconds=timeout)
            self._save(
                session,
                run.model_copy(update={"attempts": [*run.attempts, attempt]}),
                consumed_seconds=session.consumed_seconds + timeout,
            )
            started = time.monotonic()
            error = None
            try:
                result = operation(timeout)
            except NetworkError as exc:
                error = exc
            except KeyboardInterrupt:
                # Keep the full reservation; do not guess whether a remote call completed.
                self.cancel(sid, rid, abandon=True)
                raise
            elapsed = time.monotonic() - started
            session, run = self._load(sid, rid)
            if run.status != "running":
                raise NetworkError("cancelled")
            consumed_seconds = max(0, session.consumed_seconds - timeout + elapsed)
            if time.monotonic() >= deadline or consumed_seconds >= session.max_run_seconds:
                error = NetworkError("budget_exhausted")
            elif elapsed > timeout and error is None:
                # A late response is unusable, but a per-call timeout may still be retried.
                error = NetworkError("timeout")
            finished = attempt.model_copy(
                update={
                    "status": "failed" if error else "succeeded",
                    "elapsed_seconds": elapsed,
                    "error_code": error.code if error else None,
                }
            )
            self._save(
                session,
                run.model_copy(update={"attempts": [*run.attempts[:-1], finished]}),
                consumed_seconds=consumed_seconds,
            )
            self._active(sid, rid)
            if error is None:
                return result
            if error.code not in {"network", "timeout", "temporary", "rate_limited"}:
                raise error
            if retry == MAX_NETWORK_RETRIES:
                raise error
            delay = error.retry_after if error.retry_after is not None else 2**retry
            session, run = self._active(sid, rid)
            if delay > min(
                NETWORK_CALL_TIMEOUT,
                deadline - time.monotonic(),
                session.max_run_seconds - session.consumed_seconds,
            ):
                raise error
            # Reserve retry delays before waiting; cancellation is checked every 0.1 s.
            self._save(session, run, consumed_seconds=session.consumed_seconds + delay)
            wait_until = time.monotonic() + delay
            while time.monotonic() < wait_until:
                self._active(sid, rid)
                self.sleep(min(0.1, wait_until - time.monotonic()))
        raise AssertionError("unreachable")

    def start(
        self,
        sid: UUID,
        revision: int,
        subject: str,
        focus: str,
        *,
        fixture: bool = False,
    ) -> ResearchRun:
        session = self.sessions.load(sid)
        if session.status in {"READING", "FORECASTING"} or any(
            r.status == "running" for r in session.research_runs
        ):
            raise Conflict("已有执行中的步骤；中断后需显式 abandon，预算不重置")
        if session.current_revision != revision:
            raise Conflict("仅可研究当前事件版本；旧版本产物保留")
        event = self.sessions.revision(sid, revision)
        plan = SearchPlanner().plan(event, subject, focus)
        if self.search is None or self.pages is None:
            raise AppError("未配置搜索；可显式使用 evidence import 导入资料")
        run = ResearchRun(
            plan=plan, origin="fixture" if fixture or event.origin == "fixture" else "network"
        )
        session = self.sessions.save(
            session.model_copy(update={"research_runs": [*session.research_runs, run]}),
            session.version,
        )
        deadline = time.monotonic() + session.max_run_seconds - session.consumed_seconds
        started = time.monotonic()
        initial_seconds = session.consumed_seconds
        seen: set[str] = set()
        try:
            # Search both directions before reading pages, so pages cannot starve counterevidence.
            groups = []
            for query in plan.queries:
                try:
                    reply = self._call(
                        sid,
                        run.id,
                        "search",
                        query.query,
                        lambda t, q=query: self.search.search(q, t),
                        deadline,
                    )
                except NetworkError as exc:
                    if exc.code in {"budget_exhausted", "cancelled"}:
                        raise
                    self._gap(sid, run.id, "search_" + query.purpose + "_" + exc.code)
                    continue
                for warning in reply.warnings:
                    self._gap(sid, run.id, warning)
                if not reply.candidates:
                    self._gap(sid, run.id, "no_candidates_" + query.purpose)
                group = []
                for candidate in reply.candidates:
                    self._active(sid, run.id)
                    doc = self.evidence.import_document(
                        sid,
                        revision,
                        EvidenceImport(
                            url=candidate.url,
                            title=candidate.title,
                            snippet=candidate.snippet,
                            date_hint=candidate.date_hint,
                            access_status="candidate_only",
                            origin=run.origin,
                        ),
                    )
                    session, current = self._load(sid, run.id)
                    self._save(
                        session,
                        current.model_copy(
                            update={"candidate_ids": [*current.candidate_ids, doc.id]}
                        ),
                    )
                    key = canonical_url(candidate.url)
                    if key not in seen:
                        group.append(candidate)
                        seen.add(key)
                groups.append(group)
            # Interleave directions and favor distinct hosts; never invent extra queries/pages.
            candidates = [c for pair in zip_longest(*groups) for c in pair if c is not None]
            hosts = set()
            diverse, remaining = [], []
            for candidate in candidates:
                host = urlsplit(candidate.url).hostname
                (remaining if host in hosts else diverse).append(candidate)
                hosts.add(host)
            candidates = diverse + remaining
            for candidate in candidates:
                try:
                    value = self._call(
                        sid,
                        run.id,
                        "page",
                        candidate.url,
                        lambda t, u=candidate.url: self.pages.read(u, t),
                        deadline,
                    )
                    value = value.model_copy(
                        update={
                            "origin": run.origin,
                            "date_hint": candidate.date_hint,
                        }
                    )
                except NetworkError as exc:
                    if exc.code in {"budget_exhausted", "cancelled"}:
                        raise
                    self._gap(sid, run.id, "page_" + exc.code)
                    code = (
                        exc.code
                        if exc.code in {"timeout", "network", "http_error"}
                        else "unsupported"
                    )
                    value = EvidenceImport(
                        url=candidate.url,
                        title=candidate.title,
                        snippet=candidate.snippet,
                        origin=run.origin,
                        access_status="fetch_failed",
                        failure_code=code,
                    )
                self._active(sid, run.id)
                doc = self.evidence.import_document(sid, revision, value)
                session, current = self._load(sid, run.id)
                self._save(
                    session,
                    current.model_copy(update={"document_ids": [*current.document_ids, doc.id]}),
                )
            session, current = self._active(sid, run.id)
            if time.monotonic() > deadline:
                raise NetworkError("budget_exhausted")
            status = "partial" if current.gaps else "completed"
            if not current.document_ids:
                status = (
                    "partial"
                    if any(a.status == "succeeded" for a in current.attempts)
                    else "failed"
                )
            final = current.model_copy(update={"status": status})
            self._save(session, final)
            return final
        except NetworkError as exc:
            session, current = self._load(sid, run.id)
            final = current.model_copy(
                update={
                    "status": "cancelled" if exc.code == "cancelled" else "partial",
                    "gaps": list(dict.fromkeys([*current.gaps, exc.code])),
                }
            )
            self._save(session, final)
            return final
        except KeyboardInterrupt:
            self.cancel(sid, run.id, abandon=True)
            raise
        except Exception:
            # Local failures remain visible; do not turn storage/contract errors into success.
            self._gap(sid, run.id, "local_failure_requires_abandon")
            raise
        finally:
            # Include planning, parsing, local persistence and retry delays in shared wall time.
            session, current = self._load(sid, run.id)
            total = max(session.consumed_seconds, initial_seconds + time.monotonic() - started)
            self._save(session, current, consumed_seconds=total)
