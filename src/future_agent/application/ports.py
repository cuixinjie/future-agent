"""Narrow persistence and model boundaries."""

from typing import Protocol
from uuid import UUID

from pydantic import Field, JsonValue

from future_agent.domain.events import AnalysisSession, Contract, EventRevision


class AppError(Exception):
    """A safe, user-visible error without provider payloads or credentials."""


class Conflict(AppError):
    pass


class NotFound(AppError):
    pass


class ModelError(AppError):
    def __init__(self, code: str, *, retry_after: float | None = None) -> None:
        self.code = code
        self.retry_after = retry_after
        super().__init__(f"模型调用失败：{code}")


class ModelReply(Contract):
    content: str
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)


class LLMProvider(Protocol):
    model_id: str
    structured_mode: str
    output_token_limit: int

    def generate(
        self, system: str, user: str, schema: dict[str, JsonValue], timeout: float
    ) -> ModelReply: ...


class SessionRepository(Protocol):
    def create(self, session: AnalysisSession) -> AnalysisSession: ...
    def load(self, session_id: UUID) -> AnalysisSession: ...
    def save(
        self,
        session: AnalysisSession,
        expected_version: int,
        revision: EventRevision | None = None,
    ) -> AnalysisSession: ...
    def revision(self, session_id: UUID, revision: int) -> EventRevision: ...
    def history(self, session_id: UUID) -> list[EventRevision]: ...
