"""Serialized background jobs with a bounded progress log for the local UI.

Model calls and forecast steps stay in the worker thread so the HTTP handlers only
read persisted state. One worker keeps SQLite writes and token budgets serialized.
"""

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from threading import Lock
from typing import Any, Literal
from uuid import UUID, uuid4

JobStatus = Literal["queued", "running", "done", "failed"]

MAX_PROGRESS_LINES = 40


def _stamp() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class Job:
    kind: str
    session_id: UUID | None
    id: UUID = field(default_factory=uuid4)
    status: JobStatus = "queued"
    created_at: str = field(default_factory=_stamp)
    progress: list[dict[str, str]] = field(default_factory=list)
    result: dict[str, Any] | None = None
    error: str | None = None


Work = Callable[[Callable[[str], None]], dict[str, Any]]


class JobRunner:
    def __init__(self, *, max_workers: int = 1, keep: int = 200) -> None:
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="web-job")
        self._jobs: dict[UUID, Job] = {}
        self._order: list[UUID] = []
        self._lock = Lock()
        self._keep = keep

    def submit(self, kind: str, session_id: UUID | None, work: Work) -> Job:
        job = Job(kind=kind, session_id=session_id)
        with self._lock:
            self._jobs[job.id] = job
            self._order.append(job.id)
            for stale in self._order[: -self._keep]:
                self._jobs.pop(stale, None)
            self._order = self._order[-self._keep :]
        self._executor.submit(self._run, job, work)
        return job

    def _append(self, job: Job, message: str) -> None:
        with self._lock:
            job.progress.append({"at": _stamp(), "message": message})
            del job.progress[:-MAX_PROGRESS_LINES]

    def _run(self, job: Job, work: Work) -> None:
        with self._lock:
            job.status = "running"
        self._append(job, "开始执行。")
        try:
            result = work(lambda message: self._append(job, message))
        except Exception as exc:  # surfaced to the UI, never retried automatically
            with self._lock:
                job.error = str(exc) or exc.__class__.__name__
                job.status = "failed"
        else:
            with self._lock:
                job.result = result
                job.status = "done"
            self._append(job, "已完成。")

    def get(self, job_id: UUID) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def active(self, session_id: UUID) -> Job | None:
        with self._lock:
            for job_id in reversed(self._order):
                job = self._jobs.get(job_id)
                if job and job.session_id == session_id and job.status in {"queued", "running"}:
                    return job
        return None

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)


def job_view(job: Job) -> dict[str, Any]:
    return {
        "id": str(job.id),
        "kind": job.kind,
        "session_id": str(job.session_id) if job.session_id else None,
        "status": job.status,
        "created_at": job.created_at,
        "progress": list(job.progress),
        "result": job.result,
        "error": job.error,
    }
