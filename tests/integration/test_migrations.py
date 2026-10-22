"""Migration creation, refusal of unknown history and explicit rollback strategy."""

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text

from future_agent.application.events import EventService
from future_agent.application.ports import AppError
from future_agent.storage.sqlite import SQLiteRepository


def test_initial_alembic_migration_is_real_and_repeatable(repo):
    with repo.engine.connect() as connection:
        assert set(inspect(connection).get_table_names()) == {
            "alembic_version",
            "analysis_sessions",
            "event_revisions",
            "evaluation_protocols",
            "evidence_documents",
            "evidence_spans",
            "evidence_assessments",
            "forecasts",
            "frozen_forecasts",
            "resolutions",
            "forecast_steps",
        }
        assert (
            connection.scalar(text("select version_num from alembic_version")) == "0005_lifecycle"
        )
    session = EventService(repo).create("保存原文")
    repo.migrate()
    assert repo.load(session.id).sources[0].raw == "保存原文"


def test_unknown_database_is_not_overwritten(tmp_path):
    repo = SQLiteRepository(tmp_path / "unknown.db")
    try:
        with repo.engine.begin() as connection:
            connection.execute(text("create table user_data (value text)"))
            connection.execute(text("insert into user_data values ('keep')"))
        with pytest.raises(AppError, match="未知表"):
            repo.migrate()
        with repo.engine.connect() as connection:
            assert connection.scalar(text("select value from user_data")) == "keep"
    finally:
        repo.close()


def test_destructive_initial_downgrade_refused(repo):
    from pathlib import Path

    import future_agent.storage.sqlite as storage_module

    session = EventService(repo).create("不可丢失")
    config = Config()
    config.set_main_option(
        "script_location", str(Path(storage_module.__file__).parent / "migrations")
    )
    with repo.engine.begin() as connection:
        config.attributes["connection"] = connection
        with pytest.raises(RuntimeError, match="不可无损回退"):
            command.downgrade(config, "base")
    assert repo.load(session.id).sources[0].raw == "不可丢失"


def test_t08_upgrade_keeps_t07_session_revision_and_evidence(tmp_path):
    from pathlib import Path

    import future_agent.storage.sqlite as storage_module
    from future_agent.domain.events import EventDraft
    from future_agent.domain.evidence import EvidenceDocument, EvidenceSource

    repository = SQLiteRepository(tmp_path / "old.db")
    try:
        config = Config()
        config.set_main_option(
            "script_location", str(Path(storage_module.__file__).parent / "migrations")
        )
        with repository.engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "0002_evidence")
        events = EventService(repository)
        session = events.create("旧档案")
        events.edit(session.id, 0, EventDraft())
        event = repository.revision(session.id, 1)
        document = EvidenceDocument(
            event_revision_id=event.id,
            source=EvidenceSource(url="https://example.org/", access_status="candidate_only"),
            canonical_url="https://example.org/",
        )
        repository.save_document(document)
        old_session = repository.load(session.id)
        repository.migrate()
        assert repository.load(session.id) == old_session
        assert repository.revision(session.id, 1) == event
        assert repository.document(document.id) == document
        assert repository.assessments(event.id) == []
    finally:
        repository.close()


def test_t09_upgrade_keeps_t08_assessment_payload_and_session(tmp_path):
    from pathlib import Path

    from conftest import complete_question

    import future_agent.storage.sqlite as storage_module
    from future_agent.application.assessment import AssessmentService
    from future_agent.application.evidence import EvidenceService
    from future_agent.domain.assessment import AssessmentReview
    from future_agent.domain.events import EventDraft
    from future_agent.storage.snapshots import FileSnapshotStore

    repository = SQLiteRepository(tmp_path / "t08.db")
    try:
        config = Config()
        config.set_main_option(
            "script_location", str(Path(storage_module.__file__).parent / "migrations")
        )
        with repository.engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "0003_assessment")
        events = EventService(repository)
        sid = events.create("迁移前的档案").id
        events.edit(sid, 0, EventDraft(question=complete_question()), confirmed=True)
        assessments = AssessmentService(
            repository,
            repository,
            EvidenceService(repository, repository, FileSnapshotStore(tmp_path)),
        )
        assessment = assessments.assess(sid, 1, AssessmentReview(reviewer="迁移夹具"))
        with repository.engine.connect() as connection:
            before = connection.execute(text("select payload from evidence_assessments")).all()
        old = repository.load(sid)
        repository.migrate()
        with repository.engine.connect() as connection:
            assert (
                connection.execute(text("select payload from evidence_assessments")).all() == before
            )
        assert repository.load(sid) == old
        assert repository.assessments(assessment.event_revision_id) == [assessment]
        assert repository.forecasts(assessment.event_revision_id) == []
    finally:
        repository.close()
