"""Draft persistence and immutable pre-registration locking."""

from typing import Protocol
from uuid import UUID

from future_agent.application.ports import Conflict
from future_agent.domain.events import now
from future_agent.domain.protocols import EvaluationProtocol


class ProtocolRepository(Protocol):
    def protocol(self, protocol_id: UUID) -> EvaluationProtocol: ...
    def save_protocol(
        self,
        protocol: EvaluationProtocol,
        expected_version: int | None,
    ) -> EvaluationProtocol: ...


class ProtocolService:
    def __init__(self, repository: ProtocolRepository) -> None:
        self.repository = repository

    def get(self, protocol_id: UUID) -> EvaluationProtocol:
        return self.repository.protocol(protocol_id)

    def create(self, protocol: EvaluationProtocol) -> EvaluationProtocol:
        return self.repository.save_protocol(protocol, None)

    def edit(self, protocol: EvaluationProtocol, expected_version: int) -> EvaluationProtocol:
        old = self.repository.protocol(protocol.id)
        if old.status == "locked" or old.version != expected_version:
            raise Conflict("协议已锁定或版本冲突")
        if protocol.status != "draft" or protocol.created_at != old.created_at:
            raise Conflict("编辑仅可修改草稿内容，不能更改创建时间或锁定状态")
        data = protocol.model_dump()
        data["version"] = expected_version + 1
        return self.repository.save_protocol(
            EvaluationProtocol.model_validate(data), expected_version
        )

    def lock(self, protocol_id: UUID, expected_version: int) -> EvaluationProtocol:
        old = self.repository.protocol(protocol_id)
        if old.status == "locked" or old.version != expected_version:
            raise Conflict("协议已锁定或版本冲突")
        timestamp = now()
        data = old.model_dump()
        data.update(
            status="locked", version=old.version + 1, locked_at=timestamp, registered_at=timestamp
        )
        return self.repository.save_protocol(
            EvaluationProtocol.model_validate(data), expected_version
        )
