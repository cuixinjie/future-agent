"""T09 uses real application, evidence, SQLite and CLI with only the model replaced."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from uuid import uuid4

import pytest
from conftest import FixtureProvider
from conftest import forecast_response as response
from conftest import forecast_spec as spec
from sqlalchemy import text
from test_assessment import add_body, context, review
from test_cli import checked, run_cli

from future_agent.application.forecasts import ForecastService
from future_agent.application.ports import AppError, Conflict, ModelError
from future_agent.domain.events import CallAttempt
from future_agent.domain.forecasts import Forecast
from future_agent.forecasting.report import render_report
from future_agent.storage.sqlite import SQLiteRepository

# Reuse the actual T08 setup rather than duplicating the evidence workflow.
__all__ = ["context"]


def engine(context, provider):
    _, _, assessments, _ = context
    return ForecastService(assessments.sessions, assessments.repository, assessments, provider)


def count(repo):
    with repo.engine.connect() as connection:
        return connection.scalar(text("select count(*) from forecasts"))


def test_scenario_real_persistence_report_and_cross_process_cli(context, repo, tmp_path):
    _, _, assessments, sid = context
    assessment = assessments.assess(sid, 1, review())
    provider = FixtureProvider([json.dumps(response())])
    service = engine(context, provider)
    result = service.run(sid, 1, assessment.id, spec())
    assert result.method == "scenario_only" and result.probabilities == [None, None]
    assert result.model_id == provider.model_id and result.prompt_version == "forecast-1.0"
    assert result.event.revision == 1 and result.assessment == assessment
    assert result.evidence_cutoff == result.event.question.forecast_at
    assert len(result.attempt_ids) == 1 and result.calibration_status == "not_applicable"
    assert repo.load(sid).consumed_tokens == 140 and repo.load(sid).status == "STRUCTURED"
    assert provider.users[0]["cases"] == []
    report = render_report(result)
    assert report.count("概率：未提供") == 2
    assert "反证缺失" in report and "no_reviewed_current_evidence" in report
    assert "尚未冻结" in report and "留存率未知" in report
    reopened = SQLiteRepository(tmp_path / "app.db")
    try:
        assert reopened.forecast(result.id) == result
    finally:
        reopened.close()
    with pytest.raises(Conflict, match="不能覆盖"):
        repo.save_forecast(result)
    args = ("--no-env", "--data-dir", str(tmp_path), "forecast")
    assert checked(*args, "show", str(result.id)) == result.model_dump(mode="json")
    assert checked(*args, "history", str(sid), "--revision", "1") == [
        result.model_dump(mode="json")
    ]
    cli = run_cli(*args, "report", str(result.id))
    assert cli.returncode == 0 and cli.stdout == report
    assert "outcomes" in checked("schema", "forecast-spec")["properties"]
    assert "probabilities" in checked("schema", "forecast")["properties"]


def test_numeric_reviewed_evidence_normalization_report_and_versions(context, repo):
    _, _, assessments, sid = context
    doc, span = add_body(context)
    assessment = assessments.assess(sid, 1, review(span, allow_numeric=True))
    provider = FixtureProvider([json.dumps(response((0.4, 0.6000005), span.id))])
    result = engine(context, provider).run(sid, 1, assessment.id, spec(), numeric=True)
    assert result.method == "llm_judgment" and result.calibration_status == "uncalibrated"
    assert result.normalization_original_sum == 1.0000005
    assert sum(result.probabilities) == pytest.approx(1)
    assert result.evidence_documents == [doc] and result.evidence_spans == [span]
    assert result.draft.outcomes[1].probability == 0.6000005
    report = render_report(result)
    assert str(span.id) in report and doc.source.url in report and span.quote in report
    assert "舍入归一化" in report and repr(result.probabilities[0]) in report
    assert Forecast.model_validate_json(result.model_dump_json()) == result
    tampered = result.model_dump(mode="json")
    tampered["probabilities"] = [0.5, 0.5]
    with pytest.raises(ValueError):
        Forecast.model_validate(tampered)


@pytest.mark.parametrize("kind", ["no_evidence", "not_enabled", "critical"])
def test_numeric_denied_before_model_call_without_fake_success(context, repo, kind):
    _, _, assessments, sid = context
    span = None if kind == "no_evidence" else add_body(context)[1]
    assessment = assessments.assess(
        sid,
        1,
        review(
            span,
            allow_numeric=kind != "not_enabled",
            critical_questions=["发布通道是否仍有效？"] if kind == "critical" else [],
        ),
    )
    provider = FixtureProvider([])
    with pytest.raises(AppError, match="资格"):
        engine(context, provider).run(sid, 1, assessment.id, spec(), numeric=True)
    assert provider.users == [] and count(repo) == 0 and repo.load(sid).attempts == []


def test_model_may_refuse_numeric_without_filling_probability(context):
    _, _, assessments, sid = context
    span = add_body(context)[1]
    assessment = assessments.assess(sid, 1, review(span, allow_numeric=True))
    provider = FixtureProvider([json.dumps(response(refusal="证据相互矛盾，无法形成数值判断"))])
    result = engine(context, provider).run(sid, 1, assessment.id, spec(), numeric=True)
    assert result.probabilities == [None, None] and result.draft.refusal_reason
    assert "数值估计拒绝原因" in render_report(result)


@pytest.mark.parametrize("kind", ["range", "sum", "foreign_citation", "empty", "scenario_numeric"])
def test_invalid_outputs_bounded_repairs_no_success(context, repo, kind):
    _, _, assessments, sid = context
    span = add_body(context)[1]
    assessment = assessments.assess(sid, 1, review(span, allow_numeric=True))
    bad = response((0.4, 0.6))
    if kind == "range":
        bad["outcomes"][0]["probability"] = 1.1
    if kind == "sum":
        bad["outcomes"][0]["probability"] = 0.9
    if kind == "foreign_citation":
        bad["outcomes"][0]["supporting_evidence"] = [str(uuid4())]
    if kind == "empty":
        bad["outcomes"] = []
    provider = FixtureProvider([json.dumps(bad)] * 3)
    with pytest.raises(AppError, match="两次修复"):
        engine(context, provider).run(
            sid, 1, assessment.id, spec(), numeric=kind != "scenario_numeric"
        )
    saved = repo.load(sid)
    assert len(saved.attempts) == 3 and saved.error_code == "invalid_output"
    assert all(a.error_code == "invalid_output" for a in saved.attempts)
    assert count(repo) == 0 and saved.consumed_tokens == 420


def test_repair_uses_same_budget_and_retains_attempts(context, repo):
    _, _, assessments, sid = context
    assessment = assessments.assess(sid, 1, review())
    provider = FixtureProvider(["not json", json.dumps(response())])
    result = engine(context, provider).run(sid, 1, assessment.id, spec())
    assert len(result.attempt_ids) == 2
    assert [a.status for a in repo.load(sid).attempts] == ["failed", "succeeded"]


@pytest.mark.parametrize("budget", ["calls", "tokens", "time"])
def test_exhausted_shared_budget_does_not_call_model(context, repo, budget):
    _, _, assessments, sid = context
    assessment = assessments.assess(sid, 1, review())
    session = repo.load(sid)
    changes = {
        "calls": {"max_calls": 1, "attempts": [CallAttempt(status="succeeded")]},
        "tokens": {"consumed_tokens": session.max_total_tokens},
        "time": {"consumed_seconds": session.max_run_seconds},
    }[budget]
    repo.save(session.model_copy(update=changes), session.version)
    provider = FixtureProvider([])
    with pytest.raises(AppError, match="预算"):
        engine(context, provider).run(sid, 1, assessment.id, spec())
    assert provider.users == [] and count(repo) == 0


def test_service_failure_no_empty_forecast_and_retry_count(context, repo):
    _, _, assessments, sid = context
    assessment = assessments.assess(sid, 1, review())
    provider = FixtureProvider([ModelError("temporary", retry_after=0)] * 3)
    with pytest.raises(ModelError, match="temporary"):
        engine(context, provider).run(sid, 1, assessment.id, spec())
    saved = repo.load(sid)
    assert count(repo) == 0 and len(saved.attempts) == 3
    assert saved.status == "FAILED" and all(a.charged_tokens > 0 for a in saved.attempts)
    assert all(a.cost is None for a in saved.attempts)


def test_stale_assessment_unconfigured_model_and_revision_change(context, repo):
    events, _, assessments, sid = context
    assessment = assessments.assess(sid, 1, review())
    with pytest.raises(AppError, match="未配置"):
        engine(context, None).run(sid, 1, assessment.id, spec())
    add_body(context)
    with pytest.raises(Conflict, match="失效"):
        engine(context, FixtureProvider([])).run(sid, 1, assessment.id, spec())
    events.answer(sid, 1, "补充状态")
    with pytest.raises(Conflict):
        engine(context, FixtureProvider([])).run(sid, 1, assessment.id, spec())
    assert count(repo) == 0


def test_interrupted_call_lock_abandon_and_no_budget_reset(context, repo):
    events, _, assessments, sid = context
    assessment = assessments.assess(sid, 1, review())
    service = engine(context, FixtureProvider([KeyboardInterrupt()]))
    with pytest.raises(KeyboardInterrupt):
        service.run(sid, 1, assessment.id, spec())
    saved = repo.load(sid)
    assert saved.status == "FORECASTING" and saved.attempts[-1].status == "started"
    assert saved.consumed_tokens > 0 and saved.consumed_seconds > 0
    with pytest.raises(Conflict):
        events.answer(sid, 1, "不能修改")
    with pytest.raises(Conflict):
        assessments.assess(sid, 1, review())
    with pytest.raises(Conflict):
        service.run(sid, 1, assessment.id, spec())
    service.abandon(sid)
    abandoned = repo.load(sid)
    assert abandoned.consumed_tokens == saved.consumed_tokens
    assert abandoned.consumed_seconds == saved.consumed_seconds
    assert abandoned.attempts[-1].error_code == "interrupted_cost_unknown"
    assert count(repo) == 0


def test_input_change_during_call_is_rejected(context, repo):
    _, _, assessments, sid = context
    assessment = assessments.assess(sid, 1, review())

    class ChangingProvider(FixtureProvider):
        def generate(self, *args):
            add_body(context)
            return super().generate(*args)

    with pytest.raises(Conflict, match="输入已变化"):
        engine(context, ChangingProvider([json.dumps(response())])).run(
            sid, 1, assessment.id, spec()
        )
    assert count(repo) == 0 and repo.load(sid).error_code == "forecast_rejected"


def test_corrupt_snapshot_blocks_forecast_before_call(context, repo, tmp_path):
    _, _, assessments, sid = context
    doc, span = add_body(context)
    assessment = assessments.assess(sid, 1, review(span))
    (tmp_path / "evidence" / "text" / f"{doc.content_hash}.txt").write_text("坏", encoding="utf-8")
    with pytest.raises(ValueError):
        engine(context, FixtureProvider([])).run(sid, 1, assessment.id, spec())
    assert count(repo) == 0 and repo.load(sid).attempts == []


def test_old_assessor_record_is_preserved_but_requires_reassessment(context, repo):
    _, _, assessments, sid = context
    assessment = assessments.assess(sid, 1, review())
    old = assessment.model_copy(update={"id": uuid4(), "assessor_version": "evidence-assessor-1.0"})
    repo.save_assessment(old)
    view = next(v for v in assessments.history(sid, 1) if v.assessment.id == old.id)
    assert view.stale and view.invalidation_reasons == ["assessor_version_changed"]
    with pytest.raises(Conflict, match="失效"):
        engine(context, FixtureProvider([])).run(sid, 1, old.id, spec())


def test_late_model_success_is_not_accepted(context, repo, monkeypatch):
    import future_agent.application.model_calls as calls

    _, _, assessments, sid = context
    assessment = assessments.assess(sid, 1, review())
    clock = [100.0]
    monkeypatch.setattr(calls, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(calls, "CALL_TIMEOUT_SECONDS", 0.001)

    class LateProvider(FixtureProvider):
        def generate(self, *args):
            clock[0] += 0.01
            return super().generate(*args)

    with pytest.raises(AppError, match="预算"):
        engine(context, LateProvider([json.dumps(response())])).run(sid, 1, assessment.id, spec())
    assert count(repo) == 0 and repo.load(sid).consumed_tokens == 140


def test_storage_failure_rolls_back_completion_and_leaves_no_success(context, repo):
    from sqlalchemy import event

    from future_agent.storage.models import ForecastRow

    _, _, assessments, sid = context
    assessment = assessments.assess(sid, 1, review())

    def reject_insert(*args):
        raise ValueError("模拟本地写入失败")

    event.listen(ForecastRow, "before_insert", reject_insert)
    try:
        with pytest.raises(ValueError, match="写入失败"):
            engine(context, FixtureProvider([json.dumps(response())])).run(
                sid, 1, assessment.id, spec()
            )
    finally:
        event.remove(ForecastRow, "before_insert", reject_insert)
    assert count(repo) == 0 and repo.load(sid).status == "FAILED"


@pytest.mark.parametrize("mode", ["text_json", "json_schema"])
def test_cli_run_real_adapter_to_local_synthetic_http_service(context, tmp_path, mode):
    """Exercise command composition and HTTP conversion, without a paid model call."""
    _, _, assessments, sid = context
    assessment = assessments.assess(sid, 1, review())
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            body = json.dumps(
                {
                    "choices": [
                        {"message": {"content": json.dumps(response())}, "finish_reason": "stop"}
                    ],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 40},
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01))
    thread.start()
    path = tmp_path / "spec.json"
    path.write_text(spec().model_dump_json(), encoding="utf-8")
    args = (
        "--no-env",
        "--data-dir",
        str(tmp_path),
        "forecast",
        "run",
        str(sid),
        "--revision",
        "1",
        "--assessment-id",
        str(assessment.id),
        "--input",
        str(path),
    )
    try:
        result = checked(
            *args,
            extra_env={
                "LLM_BASE_URL": f"http://127.0.0.1:{server.server_port}/v1",
                "LLM_MODEL": "offline-http-fixture",
                "LLM_API_KEY": "synthetic-secret-not-for-export",
                "LLM_STRUCTURED_MODE": mode,
                "NO_PROXY": "127.0.0.1",
            },
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert result["probabilities"] == [None, None] and result["structured_mode"] == mode
    assert "synthetic-secret" not in json.dumps(result)
    assert len(requests) == 1
    assert "预测助手" in requests[0]["messages"][0]["content"]
    assert json.loads(requests[0]["messages"][1]["content"])["method"] == "scenario_only"
    if mode == "json_schema":
        assert "outcomes" in requests[0]["response_format"]["json_schema"]["schema"]["properties"]


def test_binary_definition_guard_and_continuous_numeric_guard(context, repo):
    from future_agent.domain.events import EventDraft

    events, _, assessments, sid = context
    assessment = assessments.assess(sid, 1, review())
    with pytest.raises(AppError, match="二元"):
        engine(context, FixtureProvider([])).run(
            sid, 1, assessment.id, spec().model_copy(update={"distribution": "independent"})
        )
    current = events.revision(sid)
    draft = EventDraft.model_validate(current.model_dump(include=EventDraft.model_fields.keys()))
    draft = draft.model_copy(
        update={"question": draft.question.model_copy(update={"outcome_type": "continuous"})}
    )
    events.edit(sid, 1, draft, confirmed=True)
    # Scenario support remains, while continuous numerical estimation is explicitly unimplemented.
    new_assessment = assessments.assess(sid, 2, review())
    result = engine(context, FixtureProvider([json.dumps(response())])).run(
        sid, 2, new_assessment.id, spec("independent")
    )
    assert result.probabilities == [None, None]
    from future_agent.domain.contracts import now
    from future_agent.domain.evidence import EvidenceImport, EvidenceSpan

    evidence = assessments.evidence
    doc = evidence.import_document(
        sid,
        2,
        EvidenceImport(
            url="https://example.org/continuous",
            access_status="read_success",
            raw_text="当前指标记录",
            retrieved_at=now(),
        ),
    )
    span = evidence.cite(
        sid,
        2,
        EvidenceSpan(
            document_id=doc.id,
            quote="当前指标记录",
            start=0,
            end=6,
            claim_ids=[],
        ),
    )
    numeric_assessment = assessments.assess(sid, 2, review(span, allow_numeric=True))
    assert "llm_judgment" in numeric_assessment.available_methods
    with pytest.raises(AppError, match="连续结果"):
        engine(context, FixtureProvider([])).run(
            sid, 2, numeric_assessment.id, spec(), numeric=True
        )


def test_three_category_forecast_defaults_to_scenario_then_explicit_numeric(context):
    from future_agent.domain.contracts import now
    from future_agent.domain.events import EventDraft
    from future_agent.domain.evidence import EvidenceImport, EvidenceSpan
    from future_agent.domain.forecasts import ForecastSpec, OutcomeDefinition

    events, evidence, assessments, sid = context
    current = events.revision(sid)
    draft = EventDraft.model_validate(current.model_dump(include=EventDraft.model_fields.keys()))
    question = draft.question.model_copy(
        update={
            "outcome_type": "categorical",
            "question": "发布次数是零次、一次还是至少两次？",
            "resolution_rule": "完整窗口按稳定发布规则计数，分为0、1和至少2次。",
        }
    )
    events.edit(sid, 1, draft.model_copy(update={"question": question}), confirmed=True)
    doc = evidence.import_document(
        sid,
        2,
        EvidenceImport(
            url="https://example.org/release-history",
            access_status="read_success",
            raw_text="官方发布记录",
            retrieved_at=now(),
        ),
    )
    span = evidence.cite(
        sid,
        2,
        EvidenceSpan(
            document_id=doc.id,
            quote="官方发布记录",
            start=0,
            end=6,
            claim_ids=[],
        ),
    )
    assessment = assessments.assess(sid, 2, review(span, allow_numeric=True))
    definition = ForecastSpec(
        outcomes=[
            OutcomeDefinition(key=k, definition=d)
            for k, d in [
                ("zero", "完整窗口零次发布"),
                ("one", "完整窗口一次发布"),
                ("many", "完整窗口至少两次发布"),
            ]
        ],
        distribution="exclusive_exhaustive",
        definition_review="对同一计数的互斥穷尽分档",
    )
    payload = response()
    payload["outcomes"].append(dict(payload["outcomes"][1]))
    for item, key in zip(payload["outcomes"], ("zero", "one", "many"), strict=True):
        item["key"] = key
    service = engine(context, FixtureProvider([json.dumps(payload)]))
    scenario = service.run(sid, 2, assessment.id, definition)
    assert scenario.method == "scenario_only" and scenario.probabilities == [None] * 3
    for item, p in zip(payload["outcomes"], (0.2, 0.3, 0.5), strict=True):
        item["probability"] = p
    service = engine(context, FixtureProvider([json.dumps(payload)]))
    result = service.run(sid, 2, assessment.id, definition, numeric=True)
    assert result.probabilities == [0.2, 0.3, 0.5]
    assert render_report(result).count("方向：") == 3
    with pytest.raises(AppError, match="互斥穷尽"):
        service.run(
            sid,
            2,
            assessment.id,
            definition.model_copy(update={"distribution": "independent"}),
            numeric=True,
        )
