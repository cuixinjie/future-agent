"""Transactional SQLite repositories with optimistic concurrency checks."""

from pathlib import Path
from uuid import UUID

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, select, update
from sqlalchemy.engine import URL
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from future_agent.application.ports import AppError, Conflict, NotFound
from future_agent.domain.assessment import EvidenceAssessment
from future_agent.domain.events import AnalysisSession, EventRevision, transition
from future_agent.domain.evidence import EvidenceDocument, EvidenceSpan
from future_agent.domain.forecasts import Forecast
from future_agent.domain.lifecycle import (
    ForecastStep,
    FrozenForecast,
    Resolution,
    validate_resolution,
    validate_supersession,
)
from future_agent.domain.protocols import EvaluationProtocol
from future_agent.storage.models import (
    AssessmentRow,
    EvidenceRow,
    EvidenceSpanRow,
    ForecastRow,
    ForecastStepRow,
    FrozenForecastRow,
    ProtocolRow,
    ResolutionRow,
    RevisionRow,
    SessionRow,
)


class SQLiteRepository:
    def __init__(self, database: Path) -> None:
        database.parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_engine(URL.create("sqlite", database=str(database)))

    def migrate(self) -> None:
        config = Config()
        config.set_main_option("script_location", str(Path(__file__).parent / "migrations"))
        with self.engine.begin() as connection:
            tables = set(inspect(connection).get_table_names())
            if tables and "alembic_version" not in tables:
                raise AppError("数据库已有未知表，拒绝自动建表；请备份并确认迁移来源")
            config.attributes["connection"] = connection
            command.upgrade(config, "head")

    def close(self) -> None:
        self.engine.dispose()

    def save_forecast(
        self,
        forecast: Forecast,
        *,
        expected_session_version: int | None = None,
        step: ForecastStep | None = None,
    ) -> None:
        # Parse again at persistence boundary; model_copy is not a validation operation.
        forecast = Forecast.model_validate_json(forecast.model_dump_json())
        try:
            with Session(self.engine) as db, db.begin():
                event = db.get(RevisionRow, str(forecast.event.id))
                assessment = db.get(AssessmentRow, str(forecast.assessment.id))
                if not event or not assessment:
                    raise NotFound("预测关联的事件或评估不存在")
                if (
                    EventRevision.model_validate_json(event.payload) != forecast.event
                    or EvidenceAssessment.model_validate_json(assessment.payload)
                    != forecast.assessment
                ):
                    raise Conflict("预测输入与保存版本不一致")
                if expected_session_version is not None:
                    row = db.get(SessionRow, str(forecast.session_id))
                    if row is None:
                        raise NotFound("会话不存在")
                    active = AnalysisSession.model_validate_json(row.payload)
                    if (
                        active.version != expected_session_version
                        or active.status != "FORECASTING"
                        or active.current_revision != forecast.event.revision
                    ):
                        raise Conflict("保存预测时会话已变化")
                    if step is not None and (
                        active.active_forecast_step_id != step.id
                        or forecast.attempt_ids
                        != [a.id for a in active.attempts if a.forecast_step_id == step.id]
                    ):
                        raise Conflict("预测调用归属与执行步骤不一致")
                    completed = transition(active, "STRUCTURED").model_copy(
                        update={
                            "version": active.version + 1,
                            "error_code": None,
                            "active_forecast_step_id": None,
                        }
                    )
                    changed = db.execute(
                        update(SessionRow)
                        .where(SessionRow.id == row.id, SessionRow.version == active.version)
                        .values(version=completed.version, payload=completed.model_dump_json())
                    )
                    if changed.rowcount != 1:
                        raise Conflict("保存预测时会话已变化")
                db.add(
                    ForecastRow(
                        id=str(forecast.id),
                        event_revision_id=str(forecast.event.id),
                        assessment_id=str(forecast.assessment.id),
                        payload=forecast.model_dump_json(),
                    )
                )
                if step is not None:
                    row = db.get(ForecastStepRow, str(step.id))
                    if row is None or ForecastStep.model_validate_json(row.payload) != step:
                        raise Conflict("预测步骤已取消或变化")
                    if step.status != "running":
                        raise Conflict("步骤未在执行")
                    if (
                        step.session_id != forecast.session_id
                        or step.revision != forecast.event.revision
                        or step.assessment_id != forecast.assessment.id
                        or step.spec != forecast.spec
                        or step.model_id != forecast.model_id
                        or step.prompt_version != forecast.prompt_version
                        or step.method_version != forecast.method_version
                        or step.structured_mode != forecast.structured_mode
                        or step.numeric != (forecast.method == "llm_judgment")
                    ):
                        raise Conflict("预测产物与步骤输入不一致")
                    completed_step = step.model_copy(
                        update={
                            "status": "completed",
                            "forecast_id": forecast.id,
                            "version": step.version + 1,
                        }
                    )
                    changed = db.execute(
                        update(ForecastStepRow)
                        .where(
                            ForecastStepRow.id == str(step.id),
                            ForecastStepRow.version == step.version,
                        )
                        .values(
                            version=completed_step.version, payload=completed_step.model_dump_json()
                        )
                    )
                    if changed.rowcount != 1:
                        raise Conflict("预测步骤已变化")
        except IntegrityError as exc:
            raise Conflict("预测已存在，不能覆盖") from exc

    def forecast(self, forecast_id: UUID) -> Forecast:
        with Session(self.engine) as db:
            row = db.get(ForecastRow, str(forecast_id))
            if row is None:
                raise NotFound("预测不存在")
            return Forecast.model_validate_json(row.payload)

    def step(self, step_id: UUID) -> ForecastStep:
        with Session(self.engine) as db:
            row = db.get(ForecastStepRow, str(step_id))
            if row is None:
                raise NotFound("预测步骤不存在")
            return ForecastStep.model_validate_json(row.payload)

    def save_step(self, value: ForecastStep, expected_version: int | None) -> ForecastStep:
        value = ForecastStep.model_validate_json(value.model_dump_json())
        saved = (
            value.model_copy(update={"version": value.version + 1}) if expected_version else value
        )
        try:
            with Session(self.engine) as db, db.begin():
                if expected_version is None:
                    if value.version != 1 or value.status != "ready" or value.forecast_id:
                        raise Conflict("只能创建待执行步骤")
                    db.add(
                        ForecastStepRow(
                            id=str(saved.id),
                            session_id=str(saved.session_id),
                            version=saved.version,
                            payload=saved.model_dump_json(),
                        )
                    )
                else:
                    row = db.get(ForecastStepRow, str(value.id))
                    if row is None:
                        raise NotFound("预测步骤不存在")
                    old = ForecastStep.model_validate_json(row.payload)
                    immutable = set(ForecastStep.model_fields) - {
                        "status",
                        "version",
                        "forecast_id",
                    }
                    if (
                        old.version != expected_version
                        or value.version != expected_version
                        or old.model_dump(include=immutable) != value.model_dump(include=immutable)
                        or old.status in {"completed", "cancelled"}
                        or value.status not in {"running", "cancelled"}
                        or value.forecast_id is not None
                    ):
                        raise Conflict("步骤终态或输入不可覆盖")
                    changed = db.execute(
                        update(ForecastStepRow)
                        .where(
                            ForecastStepRow.id == str(value.id),
                            ForecastStepRow.version == expected_version,
                        )
                        .values(version=saved.version, payload=saved.model_dump_json())
                    )
                    if changed.rowcount != 1:
                        raise Conflict("步骤版本冲突")
        except IntegrityError as exc:
            raise Conflict("步骤已存在，请恢复原步骤") from exc
        return saved

    def frozen(self, forecast_id: UUID) -> FrozenForecast:
        with Session(self.engine) as db:
            row = db.get(FrozenForecastRow, str(forecast_id))
            if row is None:
                raise NotFound("预测尚未冻结")
            return FrozenForecast.model_validate_json(row.payload)

    def seal(self, value: FrozenForecast) -> FrozenForecast:
        value = FrozenForecast.model_validate_json(value.model_dump_json())
        try:
            with Session(self.engine) as db, db.begin():
                row = db.get(FrozenForecastRow, str(value.forecast.id))
                if row:
                    old = FrozenForecast.model_validate_json(row.payload)
                    if old.model_copy(update={"frozen_at": value.frozen_at}) != value:
                        raise Conflict("冻结预测不可覆盖")
                    return old
                row = db.get(ForecastRow, str(value.forecast.id))
                if row is None or Forecast.model_validate_json(row.payload) != value.forecast:
                    raise Conflict("冻结内容与原预测不一致")
                if value.supersedes_forecast_id:
                    parent = db.get(FrozenForecastRow, str(value.supersedes_forecast_id))
                    if parent is None:
                        raise Conflict("被修订预测尚未冻结")
                    validate_supersession(FrozenForecast.model_validate_json(parent.payload), value)
                db.add(
                    FrozenForecastRow(
                        forecast_id=str(value.forecast.id), payload=value.model_dump_json()
                    )
                )
        except IntegrityError as exc:
            raise Conflict("预测已冻结，请重读") from exc
        return value

    def resolutions(self, forecast_id: UUID) -> list[Resolution]:
        with Session(self.engine) as db:
            return [
                Resolution.model_validate_json(row.payload)
                for row in db.scalars(
                    select(ResolutionRow)
                    .where(ResolutionRow.forecast_id == str(forecast_id))
                    .order_by(ResolutionRow.sequence)
                )
            ]

    def append_resolution(self, value: Resolution) -> None:
        value = Resolution.model_validate_json(value.model_dump_json())
        try:
            with Session(self.engine) as db, db.begin():
                frozen = db.get(FrozenForecastRow, str(value.forecast_id))
                if frozen is None:
                    raise NotFound("预测尚未冻结")
                validate_resolution(FrozenForecast.model_validate_json(frozen.payload), value)
                for document in value.evidence_documents:
                    row = db.get(EvidenceRow, str(document.id))
                    if row is None or EvidenceDocument.model_validate_json(row.payload) != document:
                        raise Conflict("核验证据文档与保存记录不一致")
                for span in value.evidence_spans:
                    row = db.get(EvidenceSpanRow, str(span.id))
                    if row is None or EvidenceSpan.model_validate_json(row.payload) != span:
                        raise Conflict("核验引用与保存记录不一致")
                previous = db.scalar(
                    select(ResolutionRow)
                    .where(ResolutionRow.forecast_id == str(value.forecast_id))
                    .order_by(ResolutionRow.sequence.desc())
                    .limit(1)
                )
                if value.sequence != (
                    previous.sequence + 1 if previous else 1
                ) or value.previous_resolution_id != (UUID(previous.id) if previous else None):
                    raise Conflict("核验历史版本冲突")
                db.add(
                    ResolutionRow(
                        id=str(value.id),
                        forecast_id=str(value.forecast_id),
                        sequence=value.sequence,
                        payload=value.model_dump_json(),
                    )
                )
        except IntegrityError as exc:
            raise Conflict("核验记录已存在或历史变化，不可覆盖") from exc

    def forecasts(self, revision_id: UUID) -> list[Forecast]:
        with Session(self.engine) as db:
            return [
                Forecast.model_validate_json(row.payload)
                for row in db.scalars(
                    select(ForecastRow)
                    .where(ForecastRow.event_revision_id == str(revision_id))
                    .order_by(ForecastRow.id)
                )
            ]

    def save_assessment(self, assessment: EvidenceAssessment) -> None:
        try:
            with Session(self.engine) as db, db.begin():
                row = db.get(RevisionRow, str(assessment.event_revision_id))
                if row is None or row.session_id != str(assessment.session_id):
                    raise NotFound("评估关联的事件版本不存在")
                event = EventRevision.model_validate_json(row.payload)
                if (
                    event.revision != assessment.revision
                    or not event.question
                    or event.question.id != assessment.question_id
                ):
                    raise Conflict("评估目标与事件版本不一致")
                db.add(
                    AssessmentRow(
                        id=str(assessment.id),
                        event_revision_id=str(assessment.event_revision_id),
                        payload=assessment.model_dump_json(),
                    )
                )
        except IntegrityError as exc:
            raise Conflict("评估已存在，不能覆盖") from exc

    def assessments(self, revision_id: UUID) -> list[EvidenceAssessment]:
        with Session(self.engine) as db:
            return [
                EvidenceAssessment.model_validate_json(row.payload)
                for row in db.scalars(
                    select(AssessmentRow)
                    .where(AssessmentRow.event_revision_id == str(revision_id))
                    .order_by(AssessmentRow.id)
                )
            ]

    def save_document(self, document: EvidenceDocument) -> None:
        try:
            with Session(self.engine) as db, db.begin():
                if db.get(RevisionRow, str(document.event_revision_id)) is None:
                    raise NotFound("事件版本不存在")
                db.add(
                    EvidenceRow(
                        id=str(document.id),
                        event_revision_id=str(document.event_revision_id),
                        payload=document.model_dump_json(),
                    )
                )
        except IntegrityError as exc:
            raise Conflict("证据已存在，不能覆盖") from exc

    def document(self, document_id: UUID) -> EvidenceDocument:
        with Session(self.engine) as db:
            row = db.get(EvidenceRow, str(document_id))
            if row is None:
                raise NotFound("证据不存在")
            return EvidenceDocument.model_validate_json(row.payload)

    def documents(self, revision_id: UUID) -> list[EvidenceDocument]:
        with Session(self.engine) as db:
            return [
                EvidenceDocument.model_validate_json(row.payload)
                for row in db.scalars(
                    select(EvidenceRow)
                    .where(EvidenceRow.event_revision_id == str(revision_id))
                    .order_by(EvidenceRow.id)
                )
            ]

    def save_span(self, span: EvidenceSpan) -> None:
        try:
            with Session(self.engine) as db, db.begin():
                if db.get(EvidenceRow, str(span.document_id)) is None:
                    raise NotFound("证据不存在")
                db.add(
                    EvidenceSpanRow(
                        id=str(span.id),
                        document_id=str(span.document_id),
                        payload=span.model_dump_json(),
                    )
                )
        except IntegrityError as exc:
            raise Conflict("引用已存在，不能覆盖") from exc

    def spans(self, document_id: UUID) -> list[EvidenceSpan]:
        self.document(document_id)
        with Session(self.engine) as db:
            return [
                EvidenceSpan.model_validate_json(row.payload)
                for row in db.scalars(
                    select(EvidenceSpanRow)
                    .where(EvidenceSpanRow.document_id == str(document_id))
                    .order_by(EvidenceSpanRow.id)
                )
            ]

    def create(self, session: AnalysisSession) -> AnalysisSession:
        try:
            with Session(self.engine) as db, db.begin():
                if session.request_key:
                    row = db.scalar(
                        select(SessionRow).where(SessionRow.request_key == session.request_key)
                    )
                    if row:
                        saved = AnalysisSession.model_validate_json(row.payload)
                        if saved.request_fingerprint != session.request_fingerprint:
                            raise Conflict("幂等键已用于不同输入或配置")
                        return saved
                db.add(
                    SessionRow(
                        id=str(session.id),
                        version=session.version,
                        request_key=session.request_key,
                        payload=session.model_dump_json(),
                    )
                )
        except IntegrityError as exc:
            raise Conflict("创建冲突，请使用相同幂等键重试读取") from exc
        return session

    def load(self, session_id: UUID) -> AnalysisSession:
        with Session(self.engine) as db:
            row = db.get(SessionRow, str(session_id))
            if not row:
                raise NotFound("会话不存在")
            return AnalysisSession.model_validate_json(row.payload)

    def save(
        self,
        session: AnalysisSession,
        expected_version: int,
        revision: EventRevision | None = None,
    ) -> AnalysisSession:
        if session.version != expected_version:
            raise Conflict("会话版本不一致")
        saved = session.model_copy(update={"version": expected_version + 1})
        try:
            with Session(self.engine) as db, db.begin():
                result = db.execute(
                    update(SessionRow)
                    .where(
                        SessionRow.id == str(session.id),
                        SessionRow.version == expected_version,
                    )
                    .values(version=saved.version, payload=saved.model_dump_json())
                )
                if result.rowcount != 1:
                    raise Conflict("会话已被其他操作修改，请重新读取")
                if revision:
                    db.add(
                        RevisionRow(
                            id=str(revision.id),
                            session_id=str(session.id),
                            revision=revision.revision,
                            payload=revision.model_dump_json(),
                        )
                    )
        except IntegrityError as exc:
            raise Conflict("事件版本已存在，旧版本不可覆盖") from exc
        return saved

    def revision(self, session_id: UUID, revision: int) -> EventRevision:
        with Session(self.engine) as db:
            row = db.scalar(
                select(RevisionRow).where(
                    RevisionRow.session_id == str(session_id),
                    RevisionRow.revision == revision,
                )
            )
            if not row:
                raise NotFound("事件版本不存在")
            return EventRevision.model_validate_json(row.payload)

    def history(self, session_id: UUID) -> list[EventRevision]:
        with Session(self.engine) as db:
            return [
                EventRevision.model_validate_json(row.payload)
                for row in db.scalars(
                    select(RevisionRow)
                    .where(RevisionRow.session_id == str(session_id))
                    .order_by(RevisionRow.revision)
                )
            ]

    def protocol(self, protocol_id: UUID) -> EvaluationProtocol:
        with Session(self.engine) as db:
            row = db.get(ProtocolRow, str(protocol_id))
            if not row:
                raise NotFound("协议不存在")
            return EvaluationProtocol.model_validate_json(row.payload)

    def save_protocol(
        self,
        protocol: EvaluationProtocol,
        expected_version: int | None,
    ) -> EvaluationProtocol:
        try:
            with Session(self.engine) as db, db.begin():
                if expected_version is None:
                    if protocol.status != "draft" or protocol.version != 1:
                        raise Conflict("只能新建版本1草稿，锁定必须通过应用服务")
                    db.add(
                        ProtocolRow(
                            id=str(protocol.id),
                            version=1,
                            locked=0,
                            payload=protocol.model_dump_json(),
                        )
                    )
                else:
                    if protocol.version != expected_version + 1:
                        raise Conflict("协议版本必须递增")
                    result = db.execute(
                        update(ProtocolRow)
                        .where(
                            ProtocolRow.id == str(protocol.id),
                            ProtocolRow.version == expected_version,
                            ProtocolRow.locked == 0,
                        )
                        .values(
                            version=protocol.version,
                            locked=int(protocol.status == "locked"),
                            payload=protocol.model_dump_json(),
                        )
                    )
                    if result.rowcount != 1:
                        raise Conflict("协议已锁定、版本冲突或不存在")
        except IntegrityError as exc:
            raise Conflict("协议已存在") from exc
        return protocol
