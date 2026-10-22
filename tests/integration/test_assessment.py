"""T08 uses real snapshots, citations, persistence and subprocess CLI; no external services."""

from datetime import timedelta
from uuid import uuid4

import pytest
from conftest import NARRATIVE, complete_question, make_draft
from test_cli import checked, run_cli

from future_agent.application.assessment import AssessmentService
from future_agent.application.events import EventService
from future_agent.application.evidence import EvidenceService
from future_agent.application.ports import AppError, Conflict
from future_agent.domain.assessment import AssessmentReview, EvidenceAssessment, ReviewedEvidence
from future_agent.domain.events import EventDraft, now
from future_agent.domain.evidence import EvidenceImport, EvidenceSpan
from future_agent.storage.snapshots import FileSnapshotStore
from future_agent.storage.sqlite import SQLiteRepository


@pytest.fixture
def context(repo, tmp_path):
    events = EventService(repo)
    session = events.create(NARRATIVE)
    draft = make_draft(session.sources[0]).model_copy(update={"question": complete_question()})
    session = events.edit(session.id, 0, draft, confirmed=True)
    evidence = EvidenceService(repo, repo, FileSnapshotStore(tmp_path))
    return events, evidence, AssessmentService(repo, repo, evidence), session.id


def add_body(context, *, raw="当前状态未发布", **changes):
    events, evidence, _, sid = context
    doc = evidence.import_document(
        sid,
        1,
        EvidenceImport(
            url="https://example.org/status",
            access_status="read_success",
            raw_text=raw,
            retrieved_at=now() - timedelta(minutes=1),
            **changes,
        ),
    )
    claim = events.revision(sid).claims[0]
    span = evidence.cite(
        sid,
        1,
        EvidenceSpan(document_id=doc.id, quote=raw, start=0, end=len(raw), claim_ids=[claim.id]),
    )
    return doc, span


def review(span=None, **changes):
    return AssessmentReview(
        reviewer="离线人工审查夹具",
        evidence=[
            ReviewedEvidence(
                span_id=span.id,
                purpose="current_state",
                relevance_reason="与目标的当前发布状态相关",
            )
        ]
        if span
        else [],
        **changes,
    )


def test_current_coverage_method_gates_and_immutable_history(context, repo, tmp_path):
    events, _, service, sid = context
    _, span = add_body(context)
    result = service.assess(sid, 1, review(span, allow_numeric=True))
    assert result.covered_claim_ids == [events.revision(sid).claims[0].id]
    assert len(result.uncovered_claim_ids) == 3
    assert len(result.unresolved_unknown_ids) == 1
    assert result.recommended_method == "llm_judgment"
    assert result.methods[1].data_eligible
    assert all(m.implemented for m in result.methods[:2])
    assert (
        result.available_methods == ["scenario_only", "llm_judgment"]
        and result.case_branch == "skipped"
    )
    assert "counterevidence_not_reviewed" in result.gaps
    assert EvidenceAssessment.model_validate_json(result.model_dump_json()) == result
    with pytest.raises(Conflict, match="不能覆盖"):
        repo.save_assessment(result)
    reopened = SQLiteRepository(tmp_path / "app.db")
    try:
        loaded = AssessmentService(reopened, reopened, service.evidence).history(sid, 1)
        assert loaded[0].assessment == result and not loaded[0].stale
    finally:
        reopened.close()


def test_no_evidence_is_explicit_scenario_path_without_fake_success(context):
    _, _, service, sid = context
    result = service.assess(sid, 1, review(allow_numeric=True))
    assert result.status == "assessed"
    assert result.recommended_method == "scenario_only"
    assert result.available_methods == ["scenario_only"]
    assert "no_reviewed_current_evidence" in result.gaps
    assert "no_reviewed_current_evidence" in result.methods[1].reasons
    assert result.eligibility == [] and len(result.uncovered_claim_ids) == 4


def test_critical_conditions_return_clarification_and_answer_invalidates(context):
    events, _, service, sid = context
    _, span = add_body(context)
    assessment = service.assess(
        sid, 1, review(span, allow_numeric=True, critical_questions=["稳定版通道是否改变？"])
    )
    assert assessment.status == "needs_input" and not assessment.methods[1].data_eligible
    assert assessment.recommended_method == "scenario_only"
    events.answer(sid, 1, "通道未改变")
    view = service.history(sid, 1)[0]
    assert view.stale and view.invalidation_reasons == ["event_revision_changed"]
    assert view.assessment == assessment
    with pytest.raises(Conflict, match="当前事件版本"):
        service.assess(sid, 1, review())
    with pytest.raises(AppError, match="已确认"):
        service.assess(sid, 2, review())


def test_numeric_opt_in_and_counterevidence_not_treated_as_current(context):
    _, _, service, sid = context
    _, span = add_body(context)
    result = service.assess(sid, 1, review(span))
    assert "numeric_estimation_not_enabled" in result.methods[1].reasons
    counter = AssessmentReview(
        reviewer="人工",
        allow_numeric=True,
        evidence=[
            ReviewedEvidence(
                span_id=span.id, purpose="counterevidence", relevance_reason="阻塞事项"
            )
        ],
    )
    result = service.assess(sid, 1, counter)
    assert "counterevidence_not_reviewed" not in result.gaps
    assert not result.covered_claim_ids and not result.methods[1].data_eligible


@pytest.mark.parametrize("kind", ["candidate_only", "fetch_failed", "unknown", "late", "fixture"])
def test_ineligible_evidence_retains_exclusion_reasons(context, kind):
    events, evidence, service, sid = context
    changes = {}
    if kind in {"candidate_only", "fetch_failed"}:
        changes.update(access_status=kind, raw_text=None)
        if kind == "fetch_failed":
            changes["failure_code"] = "timeout"
    if kind == "unknown":
        changes["retrieved_at"] = None
    if kind == "fixture":
        changes["origin"] = "fixture"
    if kind == "late":
        target = complete_question().model_copy(update={"forecast_at": now() - timedelta(hours=2)})
        event = events.revision(sid)
        draft = EventDraft.model_validate(event.model_dump(include=EventDraft.model_fields.keys()))
        events.edit(sid, 1, draft.model_copy(update={"question": target}), confirmed=True)
        revision = 2
    else:
        revision = 1
    evidence.import_document(
        sid,
        revision,
        EvidenceImport.model_validate(
            {
                "url": "https://example.org",
                "access_status": "read_success",
                "raw_text": "正文",
                "retrieved_at": now() - timedelta(minutes=1),
                **changes,
            }
        ),
    )
    result = service.assess(sid, revision, review(allow_numeric=True))
    assert not result.eligibility[0].eligible and result.eligibility[0].reasons
    assert result.recommended_method == "scenario_only"


def test_duplicate_foreign_unknown_spans_and_changed_inputs(context):
    _, evidence, service, sid = context
    _, first = add_body(context)
    result = service.assess(sid, 1, review(first))
    _, duplicate = add_body(context)
    assert service.history(sid, 1)[0].invalidation_reasons == ["evidence_inputs_changed"]
    with pytest.raises(AppError, match="评估引用"):
        service.assess(sid, 1, review(duplicate))
    missing = duplicate.model_copy(update={"id": uuid4()})
    with pytest.raises(AppError, match="评估引用"):
        service.assess(sid, 1, review(missing))
    other = EventService(service.sessions).create("其他事件")
    EventService(service.sessions).edit(
        other.id, 0, EventDraft(question=complete_question()), confirmed=True
    )
    with pytest.raises(AppError, match="评估引用"):
        service.assess(other.id, 1, review(first))
    assert service.history(sid, 1)[0].assessment == result
    assert len(evidence.list_documents(sid, 1)) == 2


def test_corrupt_snapshot_stops_assessment_and_read(context, tmp_path):
    _, _, service, sid = context
    doc, span = add_body(context)
    service.assess(sid, 1, review(span))
    (tmp_path / "evidence" / "text" / f"{doc.content_hash}.txt").write_text("坏", encoding="utf-8")
    with pytest.raises(ValueError):
        service.assess(sid, 1, review())
    with pytest.raises(ValueError):
        service.history(sid, 1)


def test_cli_assessment_restarts_and_reports_stale(context, tmp_path):
    events, _, _, sid = context
    args = ("--no-env", "--data-dir", str(tmp_path))
    path = tmp_path / "review.json"
    path.write_text(review().model_dump_json(), encoding="utf-8")
    result = checked(*args, "assessment", "run", str(sid), "--revision", "1", "--input", str(path))
    loaded = checked(*args, "assessment", "show", str(sid), "--revision", "1")
    assert loaded[0]["assessment"] == result and not loaded[0]["stale"]
    events.answer(sid, 1, "补充信息")
    assert checked(*args, "assessment", "show", str(sid), "--revision", "1")[0]["stale"]
    failed = run_cli(*args, "assessment", "run", str(sid), "--revision", "2", "--input", str(path))
    assert failed.returncode == 2 and "已确认" in failed.stderr
    assert "critical_questions" in checked("schema", "review")["properties"]


def test_assessment_refuses_active_work(context, repo):
    _, _, service, sid = context
    session = repo.load(sid)
    repo.save(session.model_copy(update={"status": "READING"}), session.version)
    with pytest.raises(Conflict, match="仍在执行"):
        service.assess(sid, 1, review())


def test_fixture_permission_is_bound_to_saved_assessment(context):
    _, _, service, sid = context
    _, span = add_body(context, origin="fixture")
    with pytest.raises(AppError, match="评估引用"):
        service.assess(sid, 1, review(span))
    result = service.assess(sid, 1, review(span), experiment=False)
    assert not result.experiment and result.methods[0].data_eligible
    assert not service.history(sid, 1)[0].stale


def test_concurrent_evidence_change_does_not_persist_assessment(context, repo, monkeypatch):
    _, evidence, service, sid = context
    original = service._inputs
    calls = 0

    def changing_inputs(*args):
        nonlocal calls
        calls += 1
        if calls == 2:
            evidence.import_document(
                sid,
                1,
                EvidenceImport(
                    url="https://example.org/new", access_status="candidate_only", snippet="新线索"
                ),
            )
        return original(*args)

    monkeypatch.setattr(service, "_inputs", changing_inputs)
    with pytest.raises(Conflict, match="证据已变化"):
        service.assess(sid, 1, review())
    assert repo.assessments(repo.revision(sid, 1).id) == []


@pytest.mark.parametrize("status", ["partial", "failed", "cancelled"])
def test_research_failures_remain_visible_and_do_not_block_scenario(context, repo, status):
    from future_agent.application.research import SearchPlanner
    from future_agent.domain.research import ResearchRun

    events, _, service, sid = context
    plan = SearchPlanner().plan(events.revision(sid), "ExampleProject", "release")
    run = ResearchRun(plan=plan, status=status, gaps=["search_current_state_timeout"])
    session = repo.load(sid)
    repo.save(session.model_copy(update={"research_runs": [run]}), session.version)
    result = service.assess(sid, 1, review())
    assert result.research_conditions[0].run_id == run.id
    assert result.research_conditions[0].status == status
    assert f"research:{run.id}:search_current_state_timeout" in result.gaps
    assert result.recommended_method == "scenario_only" and result.available_methods == [
        "scenario_only"
    ]
