"""Search plans and durable research accounting, bound to immutable event revisions."""

import re
from typing import Literal
from uuid import UUID, uuid4

from pydantic import Field

from future_agent.domain.contracts import Contract, Text, UtcTime, now

DEFAULT_MAX_SEARCH_QUERIES = 8
DEFAULT_MAX_PAGES = 16


class SearchQuery(Contract):
    query: Text
    purpose: Literal["current_state", "counterevidence"]
    language: Text = "all"
    time_filter: Literal["day", "week", "month", "year"] | None = None
    expected_evidence_type: Text = "official status, announcements or primary records"


class SearchPlan(Contract):
    event_revision_id: UUID
    question_id: UUID
    planner_version: Literal["search-rules-1.0"] = "search-rules-1.0"
    cutoff: UtcTime
    queries: list[SearchQuery] = Field(min_length=2, max_length=2)
    skipped: list[Text] = Field(
        default_factory=lambda: ["机制、案例和统计查询留待证据评估后按需开展；不为凑数扩展检索。"]
    )


def public_terms(value: str) -> str:
    """Accept an explicit public topic, never derive a query from personal narratives."""
    if (
        not value.strip()
        or len(value) > 120
        or re.search(r"[@\r\n]|https?://|\b\d{7,}\b|(?:\+?\d[ -]?){7,}", value)
        or any(ord(char) < 32 for char in value)
    ):
        raise ValueError("请提供简短公开对象，不含网址、邮箱、电话或私人叙述")
    return value.strip()


class SearchCandidate(Contract):
    url: Text
    title: Text | None = None
    snippet: Text | None = None
    # Search-engine date strings are clues, never verified publication dates.
    date_hint: Text | None = None


class SearchReply(Contract):
    candidates: list[SearchCandidate]
    warnings: list[Text] = Field(default_factory=list)


class NetworkAttempt(Contract):
    kind: Literal["search", "page"]
    target: Text
    started_at: UtcTime = Field(default_factory=now)
    status: Literal["started", "succeeded", "failed"] = "started"
    error_code: Text | None = None
    reserved_seconds: float = Field(gt=0)
    elapsed_seconds: float | None = Field(default=None, ge=0)
    cost: None = None


class ResearchRun(Contract):
    id: UUID = Field(default_factory=uuid4)
    plan: SearchPlan
    status: Literal["running", "completed", "partial", "failed", "cancelled"] = "running"
    attempts: list[NetworkAttempt] = Field(default_factory=list)
    candidate_ids: list[UUID] = Field(default_factory=list)
    document_ids: list[UUID] = Field(default_factory=list)
    gaps: list[Text] = Field(default_factory=list)
    cancel_requested: bool = False
    origin: Literal["network", "fixture"] = "network"
