"""Evidence import, citation validation and cutoff selection through narrow ports."""

from typing import Protocol
from uuid import UUID

from future_agent.application.ports import AppError, SessionRepository
from future_agent.domain.events import UtcTime, now, utc
from future_agent.domain.evidence import (
    EvidenceDocument,
    EvidenceEligibility,
    EvidenceImport,
    EvidenceSource,
    EvidenceSpan,
    Snapshot,
    canonical_url,
    eligibility,
)


class SnapshotStore(Protocol):
    def put(self, snapshot: Snapshot) -> UUID: ...
    def read(self, snapshot_id: UUID, content_hash: str) -> Snapshot: ...


class MaterialImporter(Protocol):
    def read(self) -> EvidenceImport: ...


class EvidenceRepository(Protocol):
    def save_document(self, document: EvidenceDocument) -> None: ...
    def document(self, document_id: UUID) -> EvidenceDocument: ...
    def documents(self, revision_id: UUID) -> list[EvidenceDocument]: ...
    def save_span(self, span: EvidenceSpan) -> None: ...
    def spans(self, document_id: UUID) -> list[EvidenceSpan]: ...


class EvidenceService:
    def __init__(
        self,
        repository: EvidenceRepository,
        sessions: SessionRepository,
        snapshots: SnapshotStore,
    ) -> None:
        self.repository = repository
        self.sessions = sessions
        self.snapshots = snapshots

    def import_from(
        self,
        session_id: UUID,
        revision: int,
        importer: MaterialImporter,
    ) -> EvidenceDocument:
        return self.import_document(session_id, revision, importer.read())

    def import_document(
        self,
        session_id: UUID,
        revision: int,
        value: EvidenceImport,
    ) -> EvidenceDocument:
        event = self.sessions.revision(session_id, revision)
        imported_at = now()
        if value.retrieved_at and value.retrieved_at > imported_at:
            raise AppError("不能导入未来获取的资料")
        snapshot = Snapshot.from_raw(value.raw_text) if value.raw_text is not None else None
        snapshot_id = self.snapshots.put(snapshot) if snapshot else None
        document = EvidenceDocument(
            event_revision_id=event.id,
            source=EvidenceSource.model_validate(value.model_dump(exclude={"raw_text"})),
            canonical_url=canonical_url(value.url),
            imported_at=now(),
            snapshot_id=snapshot_id,
            content_hash=snapshot.content_hash if snapshot else None,
        )
        self.repository.save_document(document)
        return document

    def document(self, document_id: UUID) -> EvidenceDocument:
        return self.repository.document(document_id)

    def spans(self, document_id: UUID) -> list[EvidenceSpan]:
        spans = self.repository.spans(document_id)
        if spans:
            snapshot = self.snapshot(document_id)
            for span in spans:
                span.validate_snapshot(snapshot.normalized)
        return spans

    def snapshot(self, document_id: UUID) -> Snapshot:
        document = self.repository.document(document_id)
        if document.snapshot_id is None or document.content_hash is None:
            raise AppError("该来源没有已读正文；候选片段不能作为正文引用")
        return self.snapshots.read(document.snapshot_id, document.content_hash)

    def cite(self, session_id: UUID, revision: int, span: EvidenceSpan) -> EvidenceSpan:
        event = self.sessions.revision(session_id, revision)
        document = self.repository.document(span.document_id)
        if document.event_revision_id != event.id:
            raise AppError("证据不属于指定事件版本")
        if not set(span.claim_ids) <= {claim.id for claim in event.claims}:
            raise AppError("引用关联了不存在的陈述")
        span.validate_snapshot(self.snapshot(document.id).normalized)
        self.repository.save_span(span)
        return span

    def list_documents(self, session_id: UUID, revision: int) -> list[EvidenceDocument]:
        event = self.sessions.revision(session_id, revision)
        return self.repository.documents(event.id)

    def select(
        self,
        session_id: UUID,
        revision: int,
        cutoff: UtcTime,
        *,
        experiment: bool = True,
    ) -> list[EvidenceEligibility]:
        cutoff = utc(cutoff)
        results = []
        seen: set[str] = set()
        documents = sorted(
            self.list_documents(session_id, revision),
            key=lambda doc: (doc.imported_at, str(doc.id)),
        )
        for document in documents:
            result = eligibility(document, cutoff, experiment=experiment)
            # Verify the actual bytes before accepting evidence. Failed records remain visible.
            if result.eligible:
                self.snapshot(document.id)
                if document.content_hash in seen:
                    result = EvidenceEligibility(
                        document_id=document.id,
                        eligible=False,
                        reasons=["duplicate_content"],
                    )
                else:
                    seen.add(document.content_hash)
            results.append(result)
        return results
