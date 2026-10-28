"""Offline A/B/C closed loop with real existing services, files, SQLite and CLI."""

import json
from datetime import timedelta
from uuid import uuid4

import pytest
from conftest import FixtureProvider, forecast_response

from future_agent.application.assessment import AssessmentService
from future_agent.application.evaluation import EvaluationService
from future_agent.application.evaluation_report import evaluation_report
from future_agent.application.events import EventService
from future_agent.application.evidence import EvidenceService
from future_agent.application.forecasts import ForecastService
from future_agent.application.lifecycle import ResolutionService
from future_agent.application.ports import AppError, Conflict, ModelError
from future_agent.application.protocols import ProtocolService
from future_agent.domain.assessment import AssessmentReview, ReviewedEvidence
from future_agent.domain.contracts import now
from future_agent.domain.evaluation import ReleaseHistory, ReleaseRecord, SealInput, baseline
from future_agent.domain.events import EventDraft
from future_agent.domain.evidence import EvidenceImport, EvidenceSpan
from future_agent.domain.lifecycle import ResolutionInput
from future_agent.domain.protocols import release_question
from future_agent.storage.snapshots import FileSnapshotStore
from future_agent.storage.sqlite import SQLiteRepository


@pytest.fixture
def experiment(repo, tmp_path, protocol_fixture, request):
    data = protocol_fixture.model_dump(mode="json")
    modern = str(getattr(request, "param", "")).startswith("v2")
    if modern:
        data["schema_version"] = "2.0"
        data["update_policy"] = "separate_not_scored"
        data["compute_comparison"] = "B一次直接调用；C包含读取研究与修复，另报额外成本"
        data["metrics"] += ["delivery", "citation_errors", "human_inputs", "tokens", "elapsed"]
    cutoff = now() + timedelta(minutes=10)
    data.update(evidence_cutoff=cutoff, forecast_at=cutoff + timedelta(hours=1))
    for method in data["methods"]:
        method["method_version"] = {
            "A": "release-frequency-1.0",
            "B": "direct-1.0",
            "C": "judgment-1.0",
        }[method["arm"]]
        method["prompt_version"] = {"A": None, "B": "direct-1.0", "C": "forecast-1.0"}[
            method["arm"]
        ]
        method["final_output_tokens"] = 4000
        if modern and method["arm"] == "C":
            method.update(method_version="judgment-2.0", prompt_version="forecast-2.0")
    data["budget"]["max_tokens_per_project"] = 1000000
    if getattr(request, "param", None) == "two":
        second = dict(data["projects"][0])
        second.update(id=str(uuid4()), name="第二个预登记夹具项目")
        data["projects"].append(second)
    protocol = type(protocol_fixture).model_validate(data)
    protocols = ProtocolService(repo)
    protocols.create(protocol)
    protocol = protocols.lock(protocol.id, 1)
    project = protocol.projects[0]
    events = EventService(repo)
    session = events.create("原始发布预测叙述。")
    question = release_question(project, protocol.forecast_at).model_copy(
        update={"forecast_at": cutoff}
    )
    events.edit(session.id, 0, EventDraft(question=question), origin="fixture", confirmed=True)
    evidence = EvidenceService(repo, repo, FileSnapshotStore(tmp_path))
    body = "完整官方发布记录覆盖360天。v1.0.0公开稳定发布。"
    document = evidence.import_document(
        session.id,
        1,
        EvidenceImport(
            url=str(project.history_source),
            raw_text=body,
            access_status="read_success",
            retrieved_at=now(),
            origin="fixture",
        ),
    )
    span = evidence.cite(
        session.id, 1, EvidenceSpan(document_id=document.id, quote=body, start=0, end=len(body))
    )
    assessments = AssessmentService(repo, repo, evidence)
    assessment = assessments.assess(
        session.id,
        1,
        AssessmentReview(
            reviewer="夹具人工审查",
            allow_numeric=True,
            **(
                {
                    "numeric_basis": {
                        "status": "conditional_only"
                        if getattr(request, "param", "") == "v2-nonnumeric"
                        else "supported",
                        "source_kind": "external",
                        "reference": "官方发布历史",
                        "relevance": "发布节奏参照",
                        "unknown_effect": "保留计划未知",
                        "limitations": ["未校准"],
                    }
                }
                if modern
                else {}
            ),
            critical_questions=["关键发布条件仍未知"]
            if getattr(request, "param", True) is False
            else [],
            evidence=[
                ReviewedEvidence(
                    span_id=span.id, purpose="current_state", relevance_reason="官方发布记录"
                )
            ],
        ),
        experiment=False,
    )
    provider = FixtureProvider(
        [
            json.dumps(forecast_response((0.6, 0.4))),
            json.dumps(forecast_response((0.8, 0.2), span.id)),
        ]
    )
    forecasts = ForecastService(repo, repo, assessments, provider)
    service = EvaluationService(repo, repo, forecasts, ResolutionService(repo, repo, evidence))
    service.start(protocol.id)
    history = ReleaseHistory(
        document_id=document.id,
        coverage_start=cutoff - timedelta(days=360),
        coverage_end=cutoff,
        complete=getattr(request, "param", True) != "incomplete",
        reviewer="人工夹具",
        coverage_quote="完整官方发布记录覆盖360天。",
        records=[
            ReleaseRecord(
                version="v1.0.0",
                product_channel=project.product_channel,
                published_at=cutoff - timedelta(days=15),
                quote="v1.0.0公开稳定发布。",
            )
        ],
    )
    seal = SealInput(
        project_id=project.id,
        session_id=session.id,
        revision=1,
        assessment_id=assessment.id,
        history=history,
    )
    if modern:
        from future_agent.domain.assistance import AnalysisPlan
        from future_agent.domain.evaluation import release_spec
        from future_agent.domain.forecasts import ForecastSpec

        old_spec = release_spec()
        plan = AnalysisPlan(
            question=question.question,
            outcome_type="binary",
            window_start=question.window_start,
            window_end=question.window_end,
            resolution_rule=question.resolution_rule,
            resolution_source_policy=question.resolution_source_policy,
            definition_reason=old_spec.definition_review,
            outcomes=[{"key": o.key, "definition": o.definition} for o in old_spec.outcomes],
        )
        seal = seal.model_copy(
            update={
                "spec": ForecastSpec(
                    schema_version="2.0",
                    outcomes=old_spec.outcomes,
                    distribution=old_spec.distribution,
                    definition_review=old_spec.definition_review,
                    guidance=plan,
                )
            }
        )
    service.seal(protocol.id, seal)
    return service, protocol, project, seal, span, tmp_path


@pytest.mark.parametrize(
    "experiment,delivery",
    [
        (version, delivery)
        for version in ("v2", "v2-nonnumeric")
        for delivery in ("probability", "directional", "conditional", "exploration", "unable")
        if version != "v2-nonnumeric" or delivery != "probability"
    ],
    indirect=["experiment"],
)
def test_v2_independent_protocol_freeze_and_delivery_coverage(experiment, delivery):
    service, protocol, project, seal, span, path = experiment
    assessment = service.assessments.history(seal.session_id, seal.revision)[0].assessment
    scenario = "llm_judgment" not in assessment.available_methods
    output = forecast_response((0.8, 0.2) if delivery == "probability" else (None, None), span.id)
    output["schema_version"] = "2.0"
    numeric = delivery == "probability"
    decisive = delivery in {"probability", "directional"}
    output["refusal_reason"] = None if numeric or scenario else "不足以给出数字"
    output["judgment"] = {
        "delivery": delivery,
        "conclusion": "保留当前判断与限制",
        "primary_outcome": "yes" if decisive else None,
        "ranking": ["yes", "no"] if decisive else [],
        "reference_kind": "history",
        "reference_basis": "官方历史发布",
        "differences_and_adjustment": "计划未知限制确定性",
        "key_drivers": ["发布准备"],
        "unknown_effects": ["计划未知"],
        "change_conditions": ["出现新的阻塞"],
        "no_numeric_reason": None if numeric else "缺少总体概率依据",
    }
    service.forecasts.provider = FixtureProvider(
        [
            json.dumps(forecast_response((0.6, 0.4))),
            json.dumps(output),
        ]
    )
    run = run_all(experiment)
    entry = run.entries[2]
    assert entry.status == ("submitted" if numeric else "scenario" if scenario else "refused")
    frozen = service.lifecycle.repository.frozen(entry.prediction.frozen_forecast_id)
    assert frozen.forecast.method_version == "judgment-2.0"
    report = evaluation_report(service, run)
    assert report["arms"]["C"]["coverage"] == int(numeric)
    assert report["arms"]["C"]["deliveries"] == {delivery: 1}
    assert report["entries"][2]["probability"] == (0.8 if numeric else None)
    assert report["entries"][2]["brier"] is None
    reopened = SQLiteRepository(path / "app.db")
    try:
        assert reopened.evaluation(protocol.id) == run
        assert reopened.evaluation(protocol.id).snapshots[0].spec == seal.spec
    finally:
        reopened.close()


def run_all(experiment):
    service, protocol, project, *_ = experiment
    for arm in ("A", "B", "C"):
        service.run_arm(protocol.id, project.id, arm)
    return service.get(protocol.id)


@pytest.mark.parametrize("experiment", ["v2"], indirect=True)
def test_v2_snapshot_hash_and_version_cannot_be_rewritten(experiment):
    from future_agent.domain.evaluation import EvaluationRun, ExperimentSnapshot

    service, protocol, _, seal, _, _ = experiment
    run = service.get(protocol.id)
    snapshot = run.snapshots[0]
    data = snapshot.model_dump(mode="json")
    data["spec"]["guidance"]["assumptions"] = ["封存后的新假设"]
    with pytest.raises(ValueError, match="实验输入哈希不一致"):
        ExperimentSnapshot.model_validate(data)
    assert snapshot.input_hash == snapshot.content_hash()
    with pytest.raises(Conflict, match="不可覆盖"):
        service.seal(protocol.id, seal)
    data = run.model_dump(mode="json")
    data["protocol"]["schema_version"] = "1.0"
    with pytest.raises(ValueError, match="新版方法不能混入旧协议"):
        EvaluationRun.model_validate(data)


def test_closed_loop_baseline_model_isolation_freezing_persistence_and_scores(
    experiment, monkeypatch
):
    service, protocol, project, seal, span, path = experiment
    run = run_all(experiment)
    assert [e.status for e in run.entries] == ["submitted"] * 3
    assert [e.prediction.probability for e in run.entries] == [1 / 12, 0.6, 0.8]
    users = service.forecasts.provider.users
    assert "event" not in users[0] and "assessment" not in users[0]
    assert users[0]["sources"][0]["raw"] == "原始发布预测叙述。"
    assert "history_text" in users[0] and "event" in users[1]
    c = run.entries[2].prediction
    assert service.lifecycle.repository.frozen(c.frozen_forecast_id).forecast.probabilities == [
        0.8,
        0.2,
    ]
    assert service.run_arm(protocol.id, project.id, "B") == run
    assert len(users) == 2
    finish = protocol.forecast_at + timedelta(days=30, hours=1)
    monkeypatch.setattr("future_agent.application.evaluation.now", lambda: finish)
    run = service.resolve(
        protocol.id,
        project.id,
        ResolutionInput(
            status="resolved",
            resolved_outcome="yes",
            resolver="人工夹具",
            reason="官方发布正文确认",
            evidence_ids=[span.id],
            observed_at=protocol.forecast_at + timedelta(days=1),
        ),
        None,
    )
    monkeypatch.setattr("future_agent.application.evaluation_report.now", lambda: finish)
    report = evaluation_report(service, run)
    assert report["arms"]["C"]["brier"] == pytest.approx(0.04)
    assert report["paired"]["C-B"]["mean_difference"] == pytest.approx(-0.12)
    assert report["common_ABC"]["n"] == 1
    assert report["entries"][1]["llm_calls"] == report["entries"][2]["llm_calls"] == 1
    assert report["entries"][1]["tokens"] == 140
    assert report["arms"]["B"]["cost"] is None
    reopened = SQLiteRepository(path / "app.db")
    try:
        assert reopened.evaluation(protocol.id) == run
    finally:
        reopened.close()


def test_registry_failures_missing_and_pending_preserved(experiment):
    service, protocol, project, *_ = experiment
    service.forecasts.provider = FixtureProvider([ModelError("network")])
    run = service.run_arm(protocol.id, project.id, "B")
    assert run.entries[1].status == "missing"
    assert len(service.forecasts.provider.users) == 1  # B is exactly one call, no repair/retry.
    run = service.mark_missing(protocol.id, project.id, "C", "预算不足")
    report = evaluation_report(service, run)
    assert len(report["entries"]) == 3
    assert report["common_ABC"]["n"] == 0
    assert report["arms"]["C"]["brier"] is None
    assert all(r["resolution_status"] == "pending" for r in report["entries"])
    with pytest.raises(Conflict):
        service.mark_missing(protocol.id, project.id, "C", "改写失败")


@pytest.mark.parametrize("mode", ["refuse", "invalid"])
def test_refusal_scenario_and_invalid_not_imputed(experiment, mode):
    service, protocol, project, seal, *_ = experiment
    response = forecast_response(refusal="不能合理量化") if mode == "refuse" else {"outcomes": []}
    service.forecasts.provider = FixtureProvider([json.dumps(response)])
    run = service.run_arm(protocol.id, project.id, "B")
    assert run.entries[1].status == ("refused" if mode == "refuse" else "missing")
    report = evaluation_report(service, run)
    assert report["common_ABC"]["n"] == 0
    assert all(r["brier"] is None for r in report["entries"])


def test_late_submission_retains_prediction_and_prevents_scoring(experiment, monkeypatch):
    service, protocol, project, *_ = experiment
    original = service.forecasts.provider.generate

    def slow(*args):
        reply = original(*args)
        monkeypatch.setattr(
            "future_agent.application.evaluation.now",
            lambda: protocol.forecast_at + timedelta(seconds=1),
        )
        return reply

    service.forecasts.provider.generate = slow
    run = service.run_arm(protocol.id, project.id, "B")
    assert run.entries[1].status == "late" and run.entries[1].prediction.probability == 0.6
    assert service.run_arm(protocol.id, project.id, "C").entries[2].status == "late"


def test_b_after_tc_can_use_only_presealed_inputs(experiment, monkeypatch):
    service, protocol, project, seal, *_ = experiment
    monkeypatch.setattr(
        "future_agent.application.evaluation.now",
        lambda: protocol.evidence_cutoff + timedelta(minutes=1),
    )
    with pytest.raises(Conflict, match="Tc"):
        service.seal(protocol.id, seal)
    run = service.run_arm(protocol.id, project.id, "B")
    assert run.entries[1].status == "submitted"


def test_seal_immutable_and_c_rejects_input_edits(experiment):
    service, protocol, project, seal, *_ = experiment
    with pytest.raises(Conflict, match="不可覆盖"):
        service.seal(protocol.id, seal)
    session = service.sessions.load(seal.session_id)
    service.sessions.save(
        session.model_copy(
            update={
                "sources": [
                    *session.sources,
                    session.sources[0].model_copy(
                        update={"raw": "额外答案", "normalized": "额外答案"}
                    ),
                ]
            }
        ),
        session.version,
    )
    with pytest.raises(Conflict, match="输入已变化"):
        service.run_arm(protocol.id, project.id, "B")


def test_cancel_during_io_discards_late_result(experiment):
    service, protocol, project, *_ = experiment
    original = service.forecasts.provider.generate

    def cancel(*args):
        reply = original(*args)
        service.mark_missing(protocol.id, project.id, "B", "用户取消", cancelled=True)
        return reply

    service.forecasts.provider.generate = cancel
    run = service.run_arm(protocol.id, project.id, "B")
    assert run.entries[1].status == "cancelled" and run.entries[1].prediction is None
    assert evaluation_report(service, run)["entries"][1]["llm_calls"] == 1


def test_crash_remains_registered_and_no_automatic_retry(experiment):
    service, protocol, project, *_ = experiment
    service.forecasts.provider = FixtureProvider([KeyboardInterrupt()])
    with pytest.raises(KeyboardInterrupt):
        service.run_arm(protocol.id, project.id, "B")
    run = service.get(protocol.id)
    assert run.entries[1].status == "running"
    assert evaluation_report(service, run)["entries"][1]["tokens"] is None
    with pytest.raises(Conflict, match="中断"):
        service.run_arm(protocol.id, project.id, "B")
    run = service.mark_missing(protocol.id, project.id, "B", "中断费用未知")
    assert len(run.entries) == 3


def test_resolution_boundaries_and_history(experiment, monkeypatch):
    service, protocol, project, _, span, _ = experiment
    run_all(experiment)
    monkeypatch.setattr(
        "future_agent.application.evaluation.now", lambda: protocol.forecast_at + timedelta(days=1)
    )
    with pytest.raises(ValueError, match="窗口未结束"):
        service.resolve(
            protocol.id,
            project.id,
            ResolutionInput(
                status="resolved",
                resolved_outcome="no",
                resolver="人工",
                reason="提前负例",
                evidence_ids=[span.id],
            ),
            None,
        )
    run = service.resolve(
        protocol.id,
        project.id,
        ResolutionInput(
            status="resolved",
            resolved_outcome="yes",
            resolver="人工",
            reason="发生",
            evidence_ids=[span.id],
            observed_at=protocol.forecast_at + timedelta(hours=1),
        ),
        None,
    )
    assert evaluation_report(service, run)["arms"]["A"]["scored"] == 0
    previous = run.resolutions[str(project.id)][-1].id
    run = service.resolve(
        protocol.id,
        project.id,
        ResolutionInput(status="disputed", resolver="人工", reason="来源冲突"),
        previous,
    )
    assert len(run.resolutions[str(project.id)]) == 2
    with pytest.raises(Conflict, match="历史"):
        service.resolve(
            protocol.id,
            project.id,
            ResolutionInput(status="void", resolver="人工", reason="规则失效"),
            previous,
        )


def test_baseline_exact_boundaries_dedup_stability_and_incomplete(experiment):
    _, protocol, project, seal, *_ = experiment
    history = seal.history

    def record(version, days, **changes):
        return ReleaseRecord(
            version=version,
            product_channel=project.product_channel,
            published_at=protocol.evidence_cutoff - timedelta(days=days),
            quote="发布",
            **changes,
        )

    history = history.model_copy(
        update={
            "records": [
                record("v1", 0),
                record("v1", 0),
                record("v2", 30),
                record("v3", 360),
                record("v4-rc1", 65),
                record("v5", 95, prerelease=True),
                record("v6", 125, draft=True),
                record("v7", 155, excluded_reason="非主分支"),
            ]
        }
    )
    assert baseline(history, protocol.evidence_cutoff, project.product_channel) == [1, 1] + [0] * 10
    with pytest.raises(ValueError, match="不足"):
        baseline(
            history.model_copy(update={"complete": False}),
            protocol.evidence_cutoff,
            project.product_channel,
        )
    with pytest.raises(ValueError, match="冲突"):
        baseline(
            history.model_copy(update={"records": [record("v1", 0), record("v1", 1)]}),
            protocol.evidence_cutoff,
            project.product_channel,
        )
    with pytest.raises(ValueError, match="截止后"):
        baseline(
            history.model_copy(update={"records": [record("v1", -1)]}),
            protocol.evidence_cutoff,
            project.product_channel,
        )


def test_cli_report_uses_persisted_real_registry(experiment):
    from test_cli import checked

    _, protocol, _, _, _, path = experiment
    run_all(experiment)
    report = checked("--no-env", "--data-dir", str(path), "evaluation", "report", str(protocol.id))
    assert report["arms"]["C"]["coverage"] == 1
    assert len(report["entries"]) == 3


def test_unknown_project_and_duplicate_init_are_rejected(experiment):
    service, protocol, *_ = experiment
    with pytest.raises(AppError):
        service.run_arm(protocol.id, uuid4(), "A")
    with pytest.raises(Conflict):
        service.start(protocol.id)


@pytest.mark.parametrize("experiment", [False], indirect=True)
def test_c_scenario_mode_is_a_valid_output_without_probability(experiment):
    service, protocol, project, *_ = experiment
    service.forecasts.provider = FixtureProvider(
        [json.dumps(forecast_response((0.6, 0.4))), json.dumps(forecast_response())]
    )
    run = run_all(experiment)
    assert run.entries[2].status == "scenario"
    report = evaluation_report(service, run)
    assert report["arms"]["C"]["scenario_rate"] == 1
    assert report["arms"]["C"]["coverage"] == 0
    assert report["entries"][2]["probability"] is None


@pytest.mark.parametrize("experiment", ["incomplete"], indirect=True)
def test_a_missing_history_never_becomes_zero(experiment):
    service, protocol, project, *_ = experiment
    run = service.run_arm(protocol.id, project.id, "A")
    assert run.entries[0].status == "missing"
    assert run.entries[0].prediction is None
    assert evaluation_report(service, run)["entries"][0]["probability"] is None


def test_shared_budget_exhaustion_and_model_mismatch(experiment):
    service, protocol, project, seal, *_ = experiment
    service.forecasts.provider.output_token_limit = 1
    with pytest.raises(Conflict, match="额度"):
        service.run_arm(protocol.id, project.id, "B")
    service.forecasts.provider.output_token_limit = 4000
    session = service.sessions.load(seal.session_id)
    # Consumption is mutable accounting; quota limits belong to the sealed configuration.
    service.sessions.save(
        session.model_copy(update={"consumed_tokens": session.max_total_tokens}), session.version
    )
    run = service.run_arm(protocol.id, project.id, "B")
    assert run.entries[1].status == "missing"
    assert not service.forecasts.provider.users


def test_quality_review_is_append_only_and_unknown_until_reviewed(experiment):
    from future_agent.domain.evaluation import EvaluationReview

    service, protocol, project, *_ = experiment
    assert (
        evaluation_report(service, service.get(protocol.id))["entries"][2]["citation_errors"]
        is None
    )
    value = EvaluationReview(
        project_id=project.id,
        arm="C",
        reviewer="人工审查",
        citation_errors=1,
        fact_omissions=2,
        reason="逐条对照原始正文及事件档案",
    )
    run = service.review(protocol.id, value)
    assert evaluation_report(service, run)["entries"][2]["fact_omissions"] == 2
    with pytest.raises(Conflict):
        service.repository.save_evaluation(run.model_copy(update={"reviews": []}), run.version)


def test_real_process_exit_keeps_entries_and_reserved_usage(experiment):
    import subprocess
    import sys

    service, protocol, project, _, _, path = experiment
    script = """
import os, sys
from pathlib import Path
from uuid import UUID
from future_agent.storage.sqlite import SQLiteRepository
from future_agent.storage.snapshots import FileSnapshotStore
from future_agent.application.evidence import EvidenceService
from future_agent.application.assessment import AssessmentService
from future_agent.application.forecasts import ForecastService
from future_agent.application.lifecycle import ResolutionService
from future_agent.application.evaluation import EvaluationService
class CrashProvider:
    model_id = 'offline-fixture-not-a-real-model'
    structured_mode = 'text_json'
    output_token_limit = 4000
    def generate(self, *args):
        os._exit(29)
repo = SQLiteRepository(Path(sys.argv[1]) / 'app.db')
evidence = EvidenceService(repo, repo, FileSnapshotStore(Path(sys.argv[1])))
forecasts = ForecastService(repo, repo, AssessmentService(repo, repo, evidence), CrashProvider())
service = EvaluationService(repo, repo, forecasts, ResolutionService(repo, repo, evidence))
service.run_arm(UUID(sys.argv[2]), UUID(sys.argv[3]), 'B')
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(path), str(protocol.id), str(project.id)],
        capture_output=True,
        timeout=20,
    )
    assert result.returncode == 29, result.stderr
    run = service.get(protocol.id)
    assert len(run.entries) == 3 and run.entries[1].status == "running"
    row = evaluation_report(service, run)["entries"][1]
    assert row["llm_calls"] == 1 and row["tokens"] is None
    assert row["charged_token_allowance"] > 0


def test_finalize_covers_never_run_entries_and_cannot_run_early(experiment, monkeypatch):
    service, protocol, *_ = experiment
    with pytest.raises(Conflict, match="尚未"):
        service.finalize(protocol.id)
    monkeypatch.setattr(
        "future_agent.application.evaluation.now",
        lambda: protocol.forecast_at + timedelta(seconds=1),
    )
    run = service.finalize(protocol.id)
    assert len(run.entries) == 3
    assert all(e.status == "missing" and e.reason for e in run.entries)
    assert service.finalize(protocol.id) == run


def test_cancellation_in_c_keeps_all_entries_without_prediction(experiment):
    service, protocol, project, *_ = experiment
    service.run_arm(protocol.id, project.id, "B")
    original = service.forecasts.provider.generate

    def cancel(*args):
        reply = original(*args)
        service.mark_missing(protocol.id, project.id, "C", "用户取消", cancelled=True)
        return reply

    service.forecasts.provider.generate = cancel
    run = service.run_arm(protocol.id, project.id, "C")
    assert run.entries[2].status == "cancelled"
    assert run.entries[2].prediction is None
    assert evaluation_report(service, run)["entries"][2]["llm_calls"] == 1


def test_sealed_budget_cannot_be_increased_after_cutoff(experiment):
    service, protocol, project, seal, *_ = experiment
    session = service.sessions.load(seal.session_id)
    service.sessions.save(
        session.model_copy(update={"max_calls": session.max_calls + 1}), session.version
    )
    with pytest.raises(Conflict, match="输入已变化"):
        service.run_arm(protocol.id, project.id, "B")


def test_resolution_rejects_wrong_official_source_and_missing_quote(experiment, monkeypatch):
    service, protocol, project, seal, *_ = experiment
    document = service.evidence.import_document(
        seal.session_id,
        1,
        EvidenceImport(
            url="https://wrong.example/releases",
            raw_text="声称发生",
            access_status="read_success",
            retrieved_at=now(),
        ),
    )
    span = service.evidence.cite(
        seal.session_id, 1, EvidenceSpan(document_id=document.id, quote="声称发生", start=0, end=4)
    )
    monkeypatch.setattr(
        "future_agent.application.evaluation.now", lambda: protocol.forecast_at + timedelta(days=31)
    )
    with pytest.raises(Conflict, match="官方来源"):
        service.resolve(
            protocol.id,
            project.id,
            ResolutionInput(
                status="resolved",
                resolved_outcome="yes",
                resolver="人工",
                reason="错误来源",
                evidence_ids=[span.id],
            ),
            None,
        )
    with pytest.raises(ValueError, match="快照与引用"):
        service.resolve(
            protocol.id,
            project.id,
            ResolutionInput(
                status="resolved",
                resolved_outcome="yes",
                resolver="人工",
                reason="不存在引用",
                evidence_ids=[uuid4()],
            ),
            None,
        )


def test_prediction_and_registry_history_reject_tampering(experiment):
    from future_agent.domain.evaluation import EvaluationRun

    service, protocol, *_ = experiment
    run = run_all(experiment)
    changed = run.model_dump(mode="json")
    changed["entries"][1]["prediction"]["probability"] = 0.9
    with pytest.raises(ValueError, match="哈希"):
        EvaluationRun.model_validate(changed)
    changed = run.model_dump(mode="json")
    changed["entries"].pop()
    with pytest.raises(ValueError, match="全部"):
        EvaluationRun.model_validate(changed)
    with pytest.raises((Conflict, ValueError)):
        service.repository.save_evaluation(run.model_copy(update={"snapshots": []}), run.version)


def test_no_snapshot_and_model_preflight_errors_preserve_registry(experiment):
    service, protocol, project, *_ = experiment
    service.forecasts.provider.model_id = "different-model"
    with pytest.raises(Conflict, match="模型"):
        service.run_arm(protocol.id, project.id, "B")
    assert len(service.get(protocol.id).entries) == 3
    with pytest.raises(Conflict, match="先运行"):
        service.run_arm(protocol.id, project.id, "C")


def test_t11_upgrade_preserves_t10_frozen_history_and_raw_payloads(experiment, repo, tmp_path):
    from pathlib import Path

    from alembic import command
    from alembic.config import Config
    from sqlalchemy import text

    import future_agent.storage.sqlite as storage_module

    service, protocol, *_ = experiment
    run = run_all(experiment)
    c_id = run.entries[2].prediction.frozen_forecast_id
    old = SQLiteRepository(tmp_path / "t10.db")
    config = Config()
    config.set_main_option(
        "script_location", str(Path(storage_module.__file__).parent / "migrations")
    )
    tables = [
        "analysis_sessions",
        "event_revisions",
        "evaluation_protocols",
        "evidence_documents",
        "evidence_spans",
        "evidence_assessments",
        "forecasts",
        "forecast_steps",
        "frozen_forecasts",
        "resolutions",
    ]
    try:
        with old.engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "0005_lifecycle")
        with repo.engine.connect() as source, old.engine.begin() as target:
            for table in tables:
                for row in source.execute(text(f"select * from {table}")).mappings():
                    data = dict(row)
                    target.execute(
                        text(
                            f"insert into {table} ({', '.join(data)}) "
                            f"values ({', '.join(':' + k for k in data)})"
                        ),
                        data,
                    )
        with old.engine.connect() as connection:
            before = {t: connection.execute(text(f"select * from {t}")).all() for t in tables}
        old.migrate()
        with old.engine.connect() as connection:
            assert before == {
                t: connection.execute(text(f"select * from {t}")).all() for t in tables
            }
        assert old.frozen(c_id) == repo.frozen(c_id)
        assert old.protocol(protocol.id) == protocol
    finally:
        old.close()


@pytest.mark.parametrize("experiment", ["two"], indirect=True)
def test_paired_subset_does_not_hide_unsubmitted_project(experiment, monkeypatch):
    service, protocol, project, _, span, _ = experiment
    run_all(experiment)
    finish = protocol.forecast_at + timedelta(days=31)
    monkeypatch.setattr("future_agent.application.evaluation.now", lambda: finish)
    run = service.resolve(
        protocol.id,
        project.id,
        ResolutionInput(
            status="resolved",
            resolved_outcome="no",
            resolver="人工",
            reason="完整窗口记录充分且无合格发布",
            evidence_ids=[span.id],
        ),
        None,
    )
    monkeypatch.setattr("future_agent.application.evaluation_report.now", lambda: finish)
    report = evaluation_report(service, run)
    assert len(report["entries"]) == 6
    assert report["arms"]["C"]["coverage"] == 0.5
    assert report["paired"]["C-B"]["n"] == 1
    assert report["paired"]["C-B"]["mean_difference"] == pytest.approx(0.28)
    assert report["arms"]["C"]["resolution_states"] == {"resolved": 1, "pending": 1}


def test_template_distinguishes_evidence_cutoff_and_window(experiment):
    _, protocol, project, *_ = experiment
    question = release_question(
        project, protocol.forecast_at, evidence_cutoff=protocol.evidence_cutoff
    )
    assert question.forecast_at == protocol.evidence_cutoff
    assert question.window_start == protocol.forecast_at
    with pytest.raises(ValueError):
        release_question(
            project,
            protocol.forecast_at,
            evidence_cutoff=protocol.forecast_at + timedelta(seconds=1),
        )


def test_complete_empty_history_is_legitimate_zero_not_missing(experiment):
    _, protocol, project, seal, *_ = experiment
    assert (
        baseline(
            seal.history.model_copy(update={"records": []}),
            protocol.evidence_cutoff,
            project.product_channel,
        )
        == [0] * 12
    )


def test_prospective_protocol_cannot_consume_fixture_c_lineage(experiment):
    from future_agent.domain.protocols import EvaluationProtocol

    service, protocol, project, seal, *_ = experiment
    data = protocol.model_dump(mode="json")
    data.update(
        id=str(uuid4()),
        version=1,
        status="draft",
        registered_at=None,
        locked_at=None,
        purpose="prospective",
    )
    for p in data["projects"]:
        for name in ("official_url", "official_resolution_source", "history_source"):
            p[name] = p[name].replace("fixture.example", "python.org")
    for method in data["methods"]:
        if method["arm"] != "A":
            method["model_id"] = "synthetic-test-model-revision-1"
    draft = EvaluationProtocol.model_validate(data)
    protocols = ProtocolService(service.protocols)
    protocols.create(draft)
    locked = protocols.lock(draft.id, 1)
    service.start(locked.id)
    event_service = EventService(service.sessions)
    event_service.edit(
        seal.session_id,
        1,
        EventDraft(
            question=release_question(
                locked.projects[0], locked.forecast_at, evidence_cutoff=locked.evidence_cutoff
            )
        ),
        confirmed=True,
    )
    assessment = service.assessments.assess(
        seal.session_id, 2, AssessmentReview(reviewer="真实模式门禁测试")
    )
    aligned = seal.model_copy(update={"revision": 2, "assessment_id": assessment.id})
    with pytest.raises(Conflict, match="同模型Reader"):
        service.seal(locked.id, aligned)
