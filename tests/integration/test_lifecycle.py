"""T10 exercises real services, SQLite, files and subprocesses; only model I/O is a fixture."""

import json
from datetime import timedelta
from uuid import uuid4

import pytest
from conftest import FixtureProvider, forecast_response, forecast_spec
from sqlalchemy import event, text
from test_assessment import add_body, context, review
from test_cli import checked, run_cli
from test_forecasts import count, engine

from future_agent.application.lifecycle import ResolutionService
from future_agent.application.ports import AppError, Conflict, NotFound
from future_agent.domain.contracts import now
from future_agent.domain.lifecycle import FrozenForecast, ResolutionInput, fingerprint
from future_agent.storage.sqlite import SQLiteRepository

__all__ = ["context"]


def setup(context, *, body=False, request_id=None):
    _, evidence, assessments, sid = context
    span = add_body(context)[1] if body else None
    assessment = assessments.assess(sid, 1, review(span))
    service = engine(context, FixtureProvider([json.dumps(forecast_response())] * 3))
    forecast = service.run(sid, 1, assessment.id, forecast_spec(), request_id=request_id)
    lifecycle = ResolutionService(service.repository, service.repository, evidence)
    return service, lifecycle, forecast, span


def resolution(status="pending", **changes):
    return ResolutionInput(
        status=status, resolver="人工核验夹具", reason="按冻结规则审查", **changes
    )


def clock_at(monkeypatch, value):
    monkeypatch.setattr("future_agent.application.lifecycle.now", lambda: value)


def test_freeze_immutable_snapshot_hash_idempotency_and_revision(context, repo):
    service, lifecycle, forecast, _ = setup(context, body=True)
    sealed = lifecycle.freeze(forecast.id)
    assert sealed.forecast == forecast
    assert lifecycle.freeze(forecast.id) == sealed
    changed = sealed.model_dump(mode="json")
    changed["forecast"]["draft"]["assumptions"] = ["事后改写"]
    with pytest.raises(ValueError, match="哈希"):
        FrozenForecast.model_validate(changed)
    with pytest.raises(Conflict, match="不能覆盖"):
        repo.save_forecast(forecast)
    with pytest.raises(Conflict, match="不可覆盖"):
        lifecycle.freeze(forecast.id, early_outcomes=[forecast.spec.outcomes[0].key])
    updated = service.run(forecast.session_id, 1, forecast.assessment.id, forecast.spec)
    revision = lifecycle.freeze(updated.id, supersedes=forecast.id, reason="补充判断")
    assert revision.supersedes_forecast_id == forecast.id
    assert repo.frozen(forecast.id) == sealed
    assert count(repo) == 2


def test_revision_rejects_different_definitions_and_non_frozen_parent(context):
    service, lifecycle, forecast, _ = setup(context)
    updated = service.run(forecast.session_id, 1, forecast.assessment.id, forecast.spec)
    with pytest.raises(NotFound):
        lifecycle.freeze(updated.id, supersedes=forecast.id, reason="更改")
    lifecycle.freeze(forecast.id)
    modified = forecast.spec.model_copy(update={"definition_review": "另一种定义审查"})
    service.provider = FixtureProvider([json.dumps(forecast_response())])
    other = service.run(forecast.session_id, 1, forecast.assessment.id, modified)
    with pytest.raises(Conflict, match="独立预测"):
        lifecycle.freeze(other.id, supersedes=forecast.id, reason="定义改变")


def test_freeze_checks_actual_retained_body(context, tmp_path):
    _, lifecycle, forecast, _ = setup(context, body=True)
    doc = forecast.evidence_documents[0]
    (tmp_path / "evidence" / "text" / f"{doc.content_hash}.txt").write_text(
        "损坏", encoding="utf-8"
    )
    with pytest.raises(ValueError):
        lifecycle.freeze(forecast.id)


def test_resolution_append_history_pending_resolved_disputed_void_and_reopen(
    context, repo, tmp_path, monkeypatch
):
    _, lifecycle, forecast, span = setup(context, body=True)
    lifecycle.freeze(forecast.id)
    pending = lifecycle.record(forecast.id, resolution(), expected_previous=None)
    assert pending.resolved_outcome is None
    clock_at(monkeypatch, forecast.event.question.window_end)
    result = lifecycle.record(
        forecast.id,
        resolution(
            "resolved", resolved_outcome=forecast.spec.outcomes[1].key, evidence_ids=[span.id]
        ),
        expected_previous=pending.id,
    )
    disputed = lifecycle.record(
        forecast.id, resolution("disputed", evidence_ids=[span.id]), expected_previous=result.id
    )
    void = lifecycle.record(forecast.id, resolution("void"), expected_previous=disputed.id)
    assert [r.status for r in lifecycle.history(forecast.id)] == [
        "pending",
        "resolved",
        "disputed",
        "void",
    ]
    with pytest.raises(Conflict):
        lifecycle.record(forecast.id, resolution(), expected_previous=disputed.id)
    with pytest.raises(Conflict):
        repo.append_resolution(void)
    reopened = SQLiteRepository(tmp_path / "app.db")
    try:
        assert reopened.resolutions(forecast.id)[-1] == void
        assert reopened.frozen(forecast.id).forecast == forecast
    finally:
        reopened.close()


@pytest.mark.parametrize("kind", ["early_negative", "foreign", "unknown_key", "no_body"])
def test_resolution_invalid_paths_leave_no_success(context, monkeypatch, kind):
    _, lifecycle, forecast, span = setup(context, body=True)
    lifecycle.freeze(forecast.id)
    clock_at(monkeypatch, forecast.event.question.window_end if kind != "early_negative" else now())
    with pytest.raises((AppError, ValueError)):
        value = resolution(
            "resolved",
            resolved_outcome="invalid" if kind == "unknown_key" else forecast.spec.outcomes[1].key,
            evidence_ids=[] if kind == "no_body" else [uuid4() if kind == "foreign" else span.id],
        )
        lifecycle.record(forecast.id, value, expected_previous=None)
    assert lifecycle.history(forecast.id) == []


def test_early_occurrence_only_predeclared_key_with_time_inside_window(context, monkeypatch):
    _, lifecycle, forecast, span = setup(context, body=True)
    key = forecast.spec.outcomes[0].key
    lifecycle.freeze(forecast.id, early_outcomes=[key])
    question = forecast.event.question
    clock_at(monkeypatch, question.window_start + timedelta(days=1))
    with pytest.raises(ValueError, match="窗口"):
        lifecycle.record(
            forecast.id,
            resolution(
                "resolved",
                resolved_outcome=key,
                evidence_ids=[span.id],
                observed_at=question.window_start,
            ),
            expected_previous=None,
        )
    recorded = lifecycle.record(
        forecast.id,
        resolution(
            "resolved",
            resolved_outcome=key,
            evidence_ids=[span.id],
            observed_at=question.window_start + timedelta(seconds=1),
        ),
        expected_previous=None,
    )
    assert recorded.status == "resolved"


@pytest.mark.parametrize("status", ["pending", "disputed", "void"])
def test_unresolved_never_carries_a_negative_outcome(status):
    with pytest.raises(ValueError, match="未决"):
        resolution(status, resolved_outcome="no")


def test_completed_step_retry_and_resume_without_model_never_duplicate(context, repo, tmp_path):
    request_id = uuid4()
    service, _, forecast, _ = setup(context, request_id=request_id)
    assert repo.step(request_id).forecast_id == forecast.id
    service.provider = None
    assert service.resume(request_id) == forecast
    assert (
        service.run(
            forecast.session_id, 1, forecast.assessment.id, forecast.spec, request_id=request_id
        )
        == forecast
    )
    assert count(repo) == 1 and len(repo.load(forecast.session_id).attempts) == 1
    with pytest.raises(Conflict, match="不同输入"):
        service.run(
            forecast.session_id,
            1,
            forecast.assessment.id,
            forecast.spec,
            numeric=True,
            request_id=request_id,
        )
    args = ("--no-env", "--data-dir", str(tmp_path), "forecast")
    assert checked(*args, "resume", str(request_id))["id"] == str(forecast.id)
    assert checked(*args, "step", str(request_id))["status"] == "completed"


def interrupted(context, request_id):
    _, _, assessments, sid = context
    assessment = assessments.assess(sid, 1, review())
    service = engine(context, FixtureProvider([KeyboardInterrupt()]))
    with pytest.raises(KeyboardInterrupt):
        service.run(sid, 1, assessment.id, forecast_spec(), request_id=request_id)
    return service, sid


def test_crash_recovery_keeps_attempts_budget_and_returns_same_result(context, repo):
    step_id = uuid4()
    service, sid = interrupted(context, step_id)
    crashed = repo.load(sid)
    with pytest.raises(Conflict, match="重复计费"):
        service.resume(step_id)
    service.provider = FixtureProvider([json.dumps(forecast_response())])
    result = service.resume(step_id, retry_interrupted=True)
    assert repo.step(step_id).status == "completed"
    attempts = repo.load(sid).attempts
    assert len(attempts) == 2 and attempts[0].error_code == "interrupted_cost_unknown"
    assert attempts[0].charged_tokens == crashed.attempts[0].charged_tokens
    assert repo.load(sid).consumed_tokens > crashed.consumed_tokens
    assert service.resume(step_id) == result and count(repo) == 1


def test_cancel_after_crash_and_changed_versions_cannot_resume(context, repo):
    step_id = uuid4()
    service, sid = interrupted(context, step_id)
    cancelled = service.cancel(step_id)
    assert cancelled.status == "cancelled" and service.cancel(step_id) == cancelled
    with pytest.raises(Conflict, match="取消"):
        service.resume(step_id, retry_interrupted=True)
    service.abandon(sid)
    assert count(repo) == 0 and repo.load(sid).attempts[0].cost is None


@pytest.mark.parametrize("change", ["model", "event", "assessment"])
def test_resume_rechecks_inputs_before_call(context, repo, change):
    step_id = uuid4()
    service, sid = interrupted(context, step_id)
    service.provider = FixtureProvider([])
    if change == "model":
        service.provider.model_id = "changed-model"
    else:
        service.abandon(sid)
        if change == "event":
            context[0].answer(sid, 1, "补充信息")
        else:
            add_body(context)
    with pytest.raises(Conflict):
        service.resume(step_id, retry_interrupted=True)
    assert service.provider.users == [] and count(repo) == 0


def test_cancel_during_model_call_retains_usage_and_discards_late_success(context, repo):
    _, _, assessments, sid = context
    assessment = assessments.assess(sid, 1, review())
    step_id = uuid4()
    service = engine(context, None)

    class Cancelling(FixtureProvider):
        def generate(self, *args):
            service.cancel(step_id)
            return super().generate(*args)

    service.provider = Cancelling([json.dumps(forecast_response())])
    with pytest.raises(AppError, match="取消"):
        service.run(sid, 1, assessment.id, forecast_spec(), request_id=step_id)
    assert repo.load(sid).error_code == "cancelled"
    assert repo.load(sid).attempts[-1].input_tokens == 100
    assert count(repo) == 0 and repo.step(step_id).status == "cancelled"


def test_atomic_step_completion_failure_rolls_back_forecast_then_explicit_resume(context, repo):
    _, _, assessments, sid = context
    assessment = assessments.assess(sid, 1, review())
    step_id = uuid4()
    service = engine(context, FixtureProvider([json.dumps(forecast_response())] * 2))

    def fail_completion(conn, cursor, statement, parameters, ctx, many):
        if statement.startswith("UPDATE forecast_steps") and '"completed"' in str(parameters):
            raise ValueError("模拟提交失败")

    event.listen(repo.engine, "before_cursor_execute", fail_completion)
    try:
        with pytest.raises(ValueError, match="提交失败"):
            service.run(sid, 1, assessment.id, forecast_spec(), request_id=step_id)
    finally:
        event.remove(repo.engine, "before_cursor_execute", fail_completion)
    assert count(repo) == 0 and repo.step(step_id).status == "running"
    service.resume(step_id, retry_interrupted=True)
    assert count(repo) == 1 and repo.step(step_id).status == "completed"


def test_cli_freeze_and_resolution_history_are_cross_process(context, repo, tmp_path):
    _, _, forecast, _ = setup(context)
    args = ("--no-env", "--data-dir", str(tmp_path))
    sealed = checked(*args, "forecast", "freeze", str(forecast.id))
    assert checked(*args, "forecast", "frozen", str(forecast.id)) == sealed
    path = tmp_path / "resolution.json"
    path.write_text(resolution().model_dump_json(), encoding="utf-8")
    recorded = checked(*args, "resolution", "record", str(forecast.id), "--input", str(path))
    assert checked(*args, "resolution", "history", str(forecast.id)) == [recorded]
    rejected = run_cli(*args, "resolution", "record", str(forecast.id), "--input", str(path))
    assert rejected.returncode != 0
    assert "observed_at" in checked("schema", "resolution-input")["properties"]


def test_completed_step_cannot_be_mutated_or_cancelled(context, repo):
    step_id = uuid4()
    service, _, _, _ = setup(context, request_id=step_id)
    with pytest.raises(Conflict):
        service.cancel(step_id)
    step = repo.step(step_id)
    with pytest.raises(Conflict):
        repo.save_step(
            step.model_copy(update={"status": "running", "forecast_id": None}), step.version
        )
    with repo.engine.connect() as conn:
        assert conn.scalar(text("select count(*) from forecast_steps")) == 1


def test_actual_process_exit_after_request_reservation_can_resume(context, repo, tmp_path):
    import subprocess
    import sys

    _, _, assessments, sid = context
    assessment = assessments.assess(sid, 1, review())
    step_id = uuid4()
    script = tmp_path / "crash.py"
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(forecast_spec().model_dump_json(), encoding="utf-8")
    script.write_text(
        """import os
import sys
from pathlib import Path
from uuid import UUID
from future_agent.application.forecasts import ForecastService
from future_agent.application.assessment import AssessmentService
from future_agent.application.evidence import EvidenceService
from future_agent.domain.forecasts import ForecastSpec
from future_agent.storage.snapshots import FileSnapshotStore
from future_agent.storage.sqlite import SQLiteRepository
class CrashProvider:
    model_id = "offline-fixture-not-a-real-model"
    structured_mode = "text_json"
    output_token_limit = 4000
    def generate(self, *args):
        os._exit(23)
root = Path(sys.argv[1])
repo = SQLiteRepository(root / "app.db")
evidence = EvidenceService(repo, repo, FileSnapshotStore(root))
service = ForecastService(repo, repo, AssessmentService(repo, repo, evidence), CrashProvider())
service.run(UUID(sys.argv[2]), 1, UUID(sys.argv[3]),
            ForecastSpec.model_validate_json((root / "spec.json").read_text(encoding="utf-8")),
            request_id=UUID(sys.argv[4]))
""",
        encoding="utf-8",
    )
    process = subprocess.run(
        [sys.executable, str(script), str(tmp_path), str(sid), str(assessment.id), str(step_id)],
        capture_output=True,
        timeout=30,
    )
    assert process.returncode == 23, process.stderr.decode(errors="replace")
    assert repo.load(sid).attempts[0].status == "started"
    assert repo.step(step_id).status == "running" and count(repo) == 0
    service = engine(context, FixtureProvider([json.dumps(forecast_response())]))
    result = service.resume(step_id, retry_interrupted=True)
    assert service.resume(step_id) == result and count(repo) == 1
    assert repo.load(sid).attempts[0].error_code == "interrupted_cost_unknown"


def test_t10_upgrade_preserves_actual_t09_forecast_and_old_session_payload(context, repo, tmp_path):
    from pathlib import Path

    from alembic import command
    from alembic.config import Config

    import future_agent.storage.sqlite as storage_module

    _, _, forecast, _ = setup(context)
    old = SQLiteRepository(tmp_path / "legacy.db")
    config = Config()
    config.set_main_option(
        "script_location", str(Path(storage_module.__file__).parent / "migrations")
    )
    try:
        with old.engine.begin() as conn:
            config.attributes["connection"] = conn
            command.upgrade(config, "0004_forecasts")
        tables = ["analysis_sessions", "event_revisions", "evidence_assessments", "forecasts"]
        with repo.engine.connect() as source, old.engine.begin() as target:
            for table in tables:
                for row in source.execute(text(f"select * from {table}")).mappings():
                    data = dict(row)
                    if table == "analysis_sessions":
                        payload = json.loads(data["payload"])
                        payload["schema_version"] = "1.2"
                        payload.pop("active_forecast_step_id", None)
                        data["payload"] = json.dumps(payload)
                    columns = ", ".join(data)
                    placeholders = ", ".join(f":{key}" for key in data)
                    target.execute(
                        text(f"insert into {table} ({columns}) values ({placeholders})"), data
                    )
        with old.engine.connect() as conn:
            before = {table: conn.execute(text(f"select * from {table}")).all() for table in tables}
        old.migrate()
        old.migrate()
        with old.engine.connect() as conn:
            assert {
                table: conn.execute(text(f"select * from {table}")).all() for table in tables
            } == before
        assert old.forecast(forecast.id) == forecast
        assert old.load(forecast.session_id).schema_version == "1.3"
        assert old.load(forecast.session_id).active_forecast_step_id is None
        assert (
            old.seal(
                FrozenForecast(
                    forecast=forecast, input_hash=fingerprint(forecast.model_dump(mode="json"))
                )
            ).forecast
            == forecast
        )
    finally:
        old.close()
