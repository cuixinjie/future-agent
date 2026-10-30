"""Search plans and durable research accounting, bound to immutable event revisions."""

import re
import unicodedata
from datetime import date
from typing import Literal
from uuid import UUID, uuid4

from pydantic import Field

from future_agent.domain.contracts import Contract, Text, UtcTime, now

DEFAULT_MAX_SEARCH_QUERIES = 8
DEFAULT_MAX_PAGES = 16
MAX_PUBLIC_QUERY_CHARS = 120
PUBLIC_QUERY_RULES = {
    "empty": "搜索词不能为空白",
    "length": f"搜索词最多{MAX_PUBLIC_QUERY_CHARS}字符，请精简为公开主题关键词",
    "control": "搜索词不能含换行或控制字符，请改为单行关键词",
    "url": "搜索词不能含网址，请改用公开实体名称和主题关键词",
    "email": "搜索词不能含邮箱或@标记，请移除联系方式",
    "phone": "搜索词不能含电话号码或连续长数字，请移除联系方式",
}


class SearchQuery(Contract):
    query: Text
    purpose: Literal["current_state", "counterevidence", "reference", "mechanism"]
    language: Text = "all"
    time_filter: Literal["day", "week", "month", "year"] | None = None
    expected_evidence_type: Text = "official status, announcements or primary records"


class SearchPlan(Contract):
    event_revision_id: UUID
    question_id: UUID
    planner_version: Literal[
        "search-rules-1.0",
        "target-research-1.0",
        "target-research-1.1",
        "target-research-1.2",
        "target-research-1.3",
        "target-research-1.4",
        "target-research-1.5",
    ] = "search-rules-1.0"
    cutoff: UtcTime
    queries: list[SearchQuery] = Field(min_length=1, max_length=4)
    skipped: list[Text] = Field(
        default_factory=lambda: ["机制、案例和统计查询留待证据评估后按需开展；不为凑数扩展检索。"]
    )


def public_terms(value: str) -> str:
    """Accept an explicit public topic, never derive a query from personal narratives."""
    reason = None
    if not value.strip():
        reason = "empty"
    elif len(value) > MAX_PUBLIC_QUERY_CHARS:
        reason = "length"
    elif any(unicodedata.category(char) in {"Cc", "Cf", "Zl", "Zp"} for char in value):
        reason = "control"
    elif re.search(r"https?://|www\.", value, re.IGNORECASE):
        reason = "url"
    elif "@" in value:
        reason = "email"
    else:
        # Check whole numeric runs: never erase date substrings inside a phone number.
        for match in re.finditer(r"\+?\d(?:[ -]?\d)*", value):
            number = match.group()
            if sum(char.isdigit() for char in number) < 7:
                continue
            if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", number):
                try:
                    date.fromisoformat(number)
                    continue
                except ValueError:
                    pass
            if re.fullmatch(r"(?:19|20|21)[0-9]{2}[ -](?:19|20|21)[0-9]{2}", number):
                continue
            reason = "phone"
            break
    if reason:
        raise ValueError(PUBLIC_QUERY_RULES[reason])
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
