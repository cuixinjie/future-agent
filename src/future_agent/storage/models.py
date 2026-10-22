"""Persistence rows keep immutable domain revisions separate from session state."""

from sqlalchemy import ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class SessionRow(Base):
    __tablename__ = "analysis_sessions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    version: Mapped[int] = mapped_column(Integer)
    request_key: Mapped[str | None] = mapped_column(String(200), unique=True)
    payload: Mapped[str] = mapped_column(Text)


class RevisionRow(Base):
    __tablename__ = "event_revisions"
    __table_args__ = (UniqueConstraint("session_id", "revision"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("analysis_sessions.id"))
    revision: Mapped[int] = mapped_column(Integer)
    payload: Mapped[str] = mapped_column(Text)


class ProtocolRow(Base):
    __tablename__ = "evaluation_protocols"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    version: Mapped[int] = mapped_column(Integer)
    locked: Mapped[int] = mapped_column(Integer)
    payload: Mapped[str] = mapped_column(Text)


class EvidenceRow(Base):
    __tablename__ = "evidence_documents"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    event_revision_id: Mapped[str] = mapped_column(ForeignKey("event_revisions.id"), index=True)
    payload: Mapped[str] = mapped_column(Text)


class EvidenceSpanRow(Base):
    __tablename__ = "evidence_spans"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    document_id: Mapped[str] = mapped_column(ForeignKey("evidence_documents.id"), index=True)
    payload: Mapped[str] = mapped_column(Text)


class AssessmentRow(Base):
    __tablename__ = "evidence_assessments"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    event_revision_id: Mapped[str] = mapped_column(ForeignKey("event_revisions.id"), index=True)
    payload: Mapped[str] = mapped_column(Text)


class ForecastRow(Base):
    __tablename__ = "forecasts"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    event_revision_id: Mapped[str] = mapped_column(ForeignKey("event_revisions.id"), index=True)
    assessment_id: Mapped[str] = mapped_column(ForeignKey("evidence_assessments.id"))
    payload: Mapped[str] = mapped_column(Text)


class FrozenForecastRow(Base):
    __tablename__ = "frozen_forecasts"
    forecast_id: Mapped[str] = mapped_column(ForeignKey("forecasts.id"), primary_key=True)
    payload: Mapped[str] = mapped_column(Text)


class ResolutionRow(Base):
    __tablename__ = "resolutions"
    __table_args__ = (UniqueConstraint("forecast_id", "sequence"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    forecast_id: Mapped[str] = mapped_column(ForeignKey("frozen_forecasts.forecast_id"))
    sequence: Mapped[int] = mapped_column(Integer)
    payload: Mapped[str] = mapped_column(Text)


class ForecastStepRow(Base):
    __tablename__ = "forecast_steps"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("analysis_sessions.id"))
    version: Mapped[int] = mapped_column(Integer)
    payload: Mapped[str] = mapped_column(Text)
