"""Imported evidence contracts; offsets address the LF-normalized snapshot."""

import hashlib
from typing import Literal, Self
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID, uuid4

from pydantic import Field, model_validator

from future_agent.domain.events import Contract, Text, UtcTime, now


def canonical_url(value: str) -> str:
    parts = urlsplit(value)
    if (
        parts.scheme.lower() not in {"http", "https"}
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or any(char.isspace() or ord(char) < 32 for char in value)
        or "\\" in value
    ):
        raise ValueError("来源必须是无凭据的 HTTP(S) URL")
    host = parts.hostname.encode("idna").decode().lower()
    if ":" in host:
        host = f"[{host}]"
    port = parts.port
    if port is not None and (parts.scheme.lower(), port) not in {("http", 80), ("https", 443)}:
        host += f":{port}"
    # Query parameters and path semantics are retained; only fragments are removed.
    return urlunsplit((parts.scheme.lower(), host, parts.path or "/", parts.query, ""))


class Snapshot(Contract):
    raw: Text
    normalized: Text
    content_hash: Text

    @model_validator(mode="after")
    def validate_content(self) -> Self:
        if self.normalized != self.raw.replace("\r\n", "\n").replace("\r", "\n"):
            raise ValueError("快照规范化只能转换换行")
        if self.content_hash != hashlib.sha256(self.normalized.encode("utf-8")).hexdigest():
            raise ValueError("快照哈希不一致")
        return self

    @classmethod
    def from_raw(cls, raw: str) -> Self:
        text = raw.replace("\r\n", "\n").replace("\r", "\n")
        return cls(raw=raw, normalized=text, content_hash=hashlib.sha256(text.encode()).hexdigest())


class EvidenceSource(Contract):
    schema_version: Literal["1.1"] = "1.1"

    @model_validator(mode="before")
    @classmethod
    def upgrade_v1(cls, value: object) -> object:
        if isinstance(value, dict) and value.get("schema_version") == "1.0":
            return {**value, "schema_version": "1.1"}
        return value

    url: Text
    final_url: Text | None = None
    date_hint: Text | None = None
    publisher: Text | None = None
    title: Text | None = None
    published_at: UtcTime | None = None
    modified_at: UtcTime | None = None
    retrieved_at: UtcTime | None = None
    access_status: Literal["read_success", "candidate_only", "fetch_failed"]
    snippet: Text | None = None
    failure_code: (
        Literal["timeout", "network", "http_error", "unsupported", "unavailable"] | None
    ) = None
    origin: Literal["manual", "fixture", "network"] = "manual"

    @model_validator(mode="after")
    def validate_import(self) -> Self:
        canonical_url(self.url)
        if self.final_url:
            canonical_url(self.final_url)
        if (self.access_status == "fetch_failed") != (self.failure_code is not None):
            raise ValueError("获取失败必须保留标准错误类别")
        if self.published_at and self.modified_at and self.modified_at < self.published_at:
            raise ValueError("修改时间不能早于发布时间")
        if self.retrieved_at and any(
            date > self.retrieved_at for date in (self.published_at, self.modified_at) if date
        ):
            raise ValueError("来源日期晚于获取时间")
        return self


class EvidenceImport(EvidenceSource):
    raw_text: Text | None = None

    @model_validator(mode="after")
    def body_required(self) -> Self:
        if (self.access_status == "read_success") != (self.raw_text is not None):
            raise ValueError("只有正文已读状态可携带正文，已读状态必须提供正文")
        return self


class EvidenceDocument(Contract):
    id: UUID = Field(default_factory=uuid4)
    event_revision_id: UUID
    source: EvidenceSource
    canonical_url: Text
    imported_at: UtcTime = Field(default_factory=now)
    content_hash: Text | None = None
    snapshot_id: UUID | None = None

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if self.canonical_url != canonical_url(self.source.url):
            raise ValueError("来源规范 URL 不一致")
        read = self.source.access_status == "read_success"
        if read != (self.snapshot_id is not None) or read != (self.content_hash is not None):
            raise ValueError("已读来源必须关联快照和哈希")
        if self.source.retrieved_at and self.source.retrieved_at > self.imported_at:
            raise ValueError("不能导入未来获取的资料")
        return self


class EvidenceSpan(Contract):
    id: UUID = Field(default_factory=uuid4)
    document_id: UUID
    quote: Text
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    claim_ids: list[UUID] = Field(default_factory=list)

    def validate_snapshot(self, text: str) -> None:
        if (
            self.start >= self.end
            or self.end > len(text)
            or text[self.start : self.end] != self.quote
        ):
            raise ValueError("引用与规范化正文切片不一致")


class EvidenceEligibility(Contract):
    document_id: UUID
    eligible: bool
    reasons: list[Text]


def eligibility(
    document: EvidenceDocument, cutoff: UtcTime, *, experiment: bool
) -> EvidenceEligibility:
    reasons = []
    source = document.source
    if source.access_status != "read_success":
        reasons.append(source.access_status)
    if source.retrieved_at is None:
        reasons.append("acquisition_time_unknown")
    for name, date in (
        ("retrieved", source.retrieved_at),
        ("imported", document.imported_at),
        ("published", source.published_at),
        ("modified", source.modified_at),
    ):
        if date and date > cutoff:
            reasons.append(f"{name}_after_cutoff")
    if experiment and source.origin == "fixture":
        reasons.append("fixture_excluded_from_experiment")
    return EvidenceEligibility(document_id=document.id, eligible=not reasons, reasons=reasons)
