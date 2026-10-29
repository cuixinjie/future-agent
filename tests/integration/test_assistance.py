"""Ordinary analysis with real persistence and explicit external-only fixtures."""

import json
from datetime import timedelta

import pytest
from conftest import NARRATIVE, forecast_response, make_draft

from future_agent.application.assessment import AssessmentService
from future_agent.application.assistance import AssistanceService
from future_agent.application.events import EventService
from future_agent.application.evidence import EvidenceService
from future_agent.application.forecasts import ForecastService
from future_agent.application.lifecycle import ResolutionService
from future_agent.application.ports import AppError, ModelReply
from future_agent.application.research import ResearchService
from future_agent.domain.assistance import AnalysisPlan, EvidenceReading
from future_agent.domain.contracts import now
from future_agent.domain.events import SourceText
from future_agent.domain.evidence import EvidenceImport
from future_agent.domain.forecasts import ForecastDraft, validate_judgment
from future_agent.domain.research import SearchCandidate, SearchReply
from future_agent.storage.snapshots import FileSnapshotStore


def plan_data():
    return {
        "question": "示例产品未来30天是否发布稳定版？",
        "outcome_type": "binary",
        "resolution_rule": "官方出现新稳定版为发生；完整窗口无发布为未发生",
        "resolution_source_policy": "官方发布记录",
        "duration_days": 30,
        "assumptions": ["用户未定期限，暂按30天分析，可修改"],
        "outcomes": [
            {"key": "yes", "definition": "窗口内发布稳定版"},
            {"key": "no", "definition": "完整窗口未发布稳定版"},
        ],
        "definition_reason": "同一事件在完整窗口发生与未发生互斥穷尽",
        "queries": [
            {"query": "公开软件 发布 历史周期", "purpose": "reference", "decision_gap": "节奏参照"},
            {
                "query": "公开软件 候选版本 阻塞",
                "purpose": "counterevidence",
                "decision_gap": "阻塞情况",
            },
            {"query": "公开软件 发布 日程", "purpose": "current_state", "decision_gap": "时间表"},
        ],
    }


class AssistantProvider:
    model_id = "offline-fixture-not-a-real-model"
    structured_mode = "text_json"
    output_token_limit = 4000

    def __init__(
        self,
        *,
        numeric=False,
        clarify=False,
        followup=False,
        bad_quote=False,
        delivery=None,
        plan=None,
        interrupt=None,
    ):
        self.numeric, self.clarify, self.followup = numeric, clarify, followup
        self.bad_quote, self.delivery = bad_quote, delivery
        self.plan, self.interrupt = plan, interrupt
        self.calls = []

    def generate(self, system, user, schema, timeout):
        data = json.loads(user.split("\n上次输出")[0])
        self.calls.append(data)
        version = data.get("prompt_version") or ("forecast" if "spec" in data else "reader")
        if self.interrupt is not None and self.interrupt == version:
            self.interrupt = "already-interrupted"
            raise KeyboardInterrupt
        if version == "target-research-1.4":
            output = self.plan or plan_data()
            if self.clarify and data["clarification_available"]:
                output.update(question=None, outcomes=[], unavailable_reason="私人项目对象无法确定")
                output["questions"] = [
                    {
                        "question": "准备发布哪个产品？",
                        "kind": "target",
                        "changes_judgment": "决定参照对象",
                        "cannot_research_reason": "私人项目未知",
                    }
                ]
        elif version == "evidence-reading-1.2":
            documents = data["documents"]
            output = {
                "citations": [
                    {
                        "document_id": d["document_id"],
                        "quote": "编造的原文" if self.bad_quote else d["body"],
                        "purpose": "current_state",
                        "relevance_reason": "说明发布进度",
                    }
                    for d in documents
                ],
                "numeric_basis": {
                    "status": "supported" if self.numeric else "conditional_only",
                    "source_kind": "external" if documents else "user_report",
                    "reference": "当前项目发布准备状态",
                    "relevance": "直接关系到发布进程",
                    "unknown_effect": "保留尚不确定的留存情况，不作为发布必要条件",
                    "limitations": ["未校准；缺少长期历史样本"],
                },
                "material_gaps": ["缺少具体计划日期"],
                "research_stop_reason": "已取得主要线索，剩余未知保留",
                "followup_query_indexes": data["allowed_followup_indexes"] if self.followup else [],
            }
        elif "spec" in data:
            numeric = data["method"] == "llm_judgment"
            output = forecast_response((0.7, 0.3) if numeric else (None, None))
            output["schema_version"] = "2.0"
            mode = self.delivery or ("probability" if numeric else "directional")
            decisive = mode in {"probability", "directional"}
            output["judgment"] = {
                "delivery": mode,
                "conclusion": "在当前准备条件下更倾向发布，仍可能受阻",
                "primary_outcome": "yes" if decisive else None,
                "ranking": ["yes", "no"] if decisive else [],
                "reference_kind": "judgment",
                "reference_basis": "以当前准备阶段作为定性参照",
                "differences_and_adjustment": "缺少明确日程，降低判断确定性",
                "key_drivers": ["发布准备进度与剩余阻塞"],
                "unknown_effects": ["日程仍未知"],
                "change_conditions": ["官方宣布取消或推迟则下调发布预期"],
                "no_numeric_reason": None if numeric else "信息不足以给总体数字，保留方向判断",
            }
        else:
            source = SourceText.model_validate(data["sources"][0])
            output = make_draft(source).model_dump(mode="json")
        return ModelReply(
            content=json.dumps(output, ensure_ascii=False), input_tokens=100, output_tokens=100
        )


class SearchFixture:
    def __init__(self):
        self.queries = []

    def search(self, query, timeout):
        self.queries.append(query.query)
        return SearchReply(candidates=[SearchCandidate(url="https://example.com/current")])


class PageFixture:
    def read(self, url, timeout):
        return EvidenceImport(
            url=url,
            access_status="read_success",
            raw_text="官方发布准备正在推进。",
            retrieved_at=now(),
            origin="network",
        )


class DynamicQueryProvider(AssistantProvider):
    def generate(self, system, user, schema, timeout):
        reply = super().generate(system, user, schema, timeout)
        data = json.loads(user.split("\n上次输出")[0])
        if data.get("prompt_version") == "evidence-reading-1.2" and data.get(
            "allow_new_followup_queries"
        ):
            value = json.loads(reply.content)
            value["followup_queries"] = [
                {
                    "query": "公开软件 新发现的阻塞",
                    "purpose": "counterevidence",
                    "decision_gap": "调查新发现的阻塞",
                }
            ]
            reply = ModelReply(
                content=json.dumps(value, ensure_ascii=False), input_tokens=100, output_tokens=100
            )
        return reply


@pytest.mark.parametrize("approved", [True, False])
def test_new_followup_requires_explicit_authorization_and_survives_resume(repo, tmp_path, approved):
    search = SearchFixture()
    service, sid = setup(repo, tmp_path, DynamicQueryProvider(), search)
    with pytest.raises(AppError, match="等待用户授权"):
        service.execute(sid, allow_search=True)
    state = service.state(sid)
    assert state.stage == "search_authorization"
    assert len(state.pending_queries) == 1
    assert "公开软件 新发现的阻塞" not in search.queries
    with pytest.raises(AppError, match="等待用户授权"):
        service.execute(sid)
    service.authorize_followup(sid, approved)
    service.execute(sid)
    assert ("公开软件 新发现的阻塞" in search.queries) is approved
    assert service.state(sid).stage == "complete"


def test_new_followup_with_private_contact_is_rejected_before_network(repo, tmp_path):
    class UnsafeProvider(DynamicQueryProvider):
        def generate(self, system, user, schema, timeout):
            reply = super().generate(system, user, schema, timeout)
            data = json.loads(user.split("\n上次输出")[0])
            if data.get("prompt_version") == "evidence-reading-1.2" and data.get(
                "allow_new_followup_queries"
            ):
                value = json.loads(reply.content)
                value["followup_queries"][0]["query"] = "私人邮箱 person@example.com"
                return ModelReply(content=json.dumps(value), input_tokens=100, output_tokens=100)
            return reply

    search = SearchFixture()
    service, sid = setup(repo, tmp_path, UnsafeProvider(), search)
    with pytest.raises(AppError):
        service.execute(sid, allow_search=True)
    assert all("@" not in query for query in search.queries)


def test_quote_repeated_outside_excerpt_is_not_mislocated(repo, tmp_path):
    class RepeatedQuoteProvider(AssistantProvider):
        def generate(self, system, user, schema, timeout):
            reply = super().generate(system, user, schema, timeout)
            data = json.loads(user.split("\n上次输出")[0])
            if data.get("prompt_version") == "evidence-reading-1.2" and data["documents"]:
                value = json.loads(reply.content)
                value["citations"][0]["quote"] = "重复依据"
                return ModelReply(content=json.dumps(value), input_tokens=100, output_tokens=100)
            return reply

    class RepeatedPage(PageFixture):
        def read(self, url, timeout):
            value = super().read(url, timeout)
            body = "重复依据" + "其他文字。" * 1800 + "发布阻塞稳定版重复依据"
            return value.model_copy(update={"raw_text": body})

    search = SearchFixture()
    service, sid = setup(repo, tmp_path, RepeatedQuoteProvider(), search)
    service.research.pages = RepeatedPage()
    with pytest.raises(AppError):
        service.execute(sid, allow_search=True)


def setup(repo, tmp_path, provider, search=None):
    events = EventService(repo, provider)
    evidence = EvidenceService(repo, repo, FileSnapshotStore(tmp_path))
    assessments = AssessmentService(repo, repo, evidence)
    forecasts = ForecastService(repo, repo, assessments, provider)
    research = ResearchService(repo, evidence, search, PageFixture() if search else None)
    service = AssistanceService(events, research, assessments, forecasts)
    sid = events.create(NARRATIVE).id
    return service, sid


def test_planning_repair_receives_safe_constraint_and_reports_progress(repo, tmp_path):
    class RepairProvider(AssistantProvider):
        def __init__(self):
            super().__init__()
            self.repair_prompt = None
            self.plan_calls = 0

        def generate(self, system, user, schema, timeout):
            if "target-research-1.4" in user:
                self.plan_calls += 1
                if self.plan_calls == 1:
                    bad = plan_data()
                    bad["outcomes"] = [{"key": "secret-value", "definition": "private"}]
                    return ModelReply(content=json.dumps(bad), input_tokens=1, output_tokens=1)
                self.repair_prompt = user
            return super().generate(system, user, schema, timeout)

    provider = RepairProvider()
    service, sid = setup(repo, tmp_path, provider)
    messages = []
    assert service.prepare(sid, progress=messages.append).stage == "planned"
    assert "二元问题必须有两个结果" in provider.repair_prompt
    assert "secret-value" not in provider.repair_prompt
    assert any("正在读取并整理原文" in message for message in messages)
    assert any("重新生成" in message for message in messages)
    assert len(service.events.get(sid).attempts) == 3


def test_explicit_target_is_exposed_verbatim_with_json_window(repo, tmp_path):
    from conftest import complete_question

    question = complete_question()
    names = {
        "question",
        "outcome_type",
        "window_start",
        "window_end",
        "resolution_rule",
        "resolution_source_policy",
    }
    locked = question.model_dump(mode="json", include=names)

    class ExplicitTargetProvider(AssistantProvider):
        def generate(self, system, user, schema, timeout):
            data = json.loads(user)
            if data.get("prompt_version") == "target-research-1.4":
                assert data["locked_target"] == locked
                assert "逐字复制" in system
                self.plan = {**plan_data(), **locked}
            if "prompt_version" not in data and "sources" in data:
                source = SourceText.model_validate(data["sources"][0])
                draft = make_draft(source).model_copy(update={"question": question})
                return ModelReply(content=draft.model_dump_json(), input_tokens=1, output_tokens=1)
            return super().generate(system, user, schema, timeout)

    service, sid = setup(repo, tmp_path, ExplicitTargetProvider())
    state = service.prepare(sid)
    assert state.stage == "planned"
    assert state.plan.model_dump(mode="json", include=names) == locked


def test_ordinary_analysis_no_forms_and_no_repeat_calls(repo, tmp_path):
    provider = AssistantProvider()
    service, sid = setup(repo, tmp_path, provider)
    result = service.execute(sid)
    assert len(provider.calls) == 4
    assert result.method_version == "judgment-2.0"
    assert result.draft.judgment.delivery == "directional" and result.probabilities == [None, None]
    assert result.event.semantic_review == "system_reviewed"
    assert not result.assessment.experiment
    assert service.execute(sid).id == result.id and len(provider.calls) == 4
    lifecycle = ResolutionService(repo, repo, service.evidence)
    frozen = lifecycle.freeze(result.id)
    assert repo.frozen(result.id) == frozen
    assert "当前判断" in lifecycle.report(result.id)
    assert "普通分析" in lifecycle.report(result.id)
    assert result.event.question.window_start > result.created_at >= result.evidence_cutoff


def test_numeric_can_use_concrete_user_information_with_unknowns(repo, tmp_path):
    service, sid = setup(repo, tmp_path, AssistantProvider(numeric=True))
    result = service.execute(sid)
    assert result.probabilities == [0.7, 0.3] and result.event.unknowns
    assert result.assessment.assessor_version == "evidence-assessor-2.0"
    assert result.draft.judgment.delivery == "probability"
    report = ResolutionService(repo, repo, service.evidence).report(result.id)
    summary = report.split("预测报告（", 1)[0]
    assert summary.startswith("问题：") and "窗口：" in summary
    assert "主观概率（未校准）：窗口内发布稳定版：0.7；完整窗口未发布稳定版：0.3" in summary


def test_resume_after_followup_reexamines_evidence(repo, tmp_path, monkeypatch):
    provider = AssistantProvider(followup=True)
    service, sid = setup(repo, tmp_path, provider, SearchFixture())
    research = service._research

    def interrupt_after_followup(session_id, indexes):
        research(session_id, indexes)
        if service.state(session_id).research_rounds == 2:
            raise KeyboardInterrupt

    monkeypatch.setattr(service, "_research", interrupt_after_followup)
    with pytest.raises(KeyboardInterrupt):
        service.execute(sid, allow_search=True)
    assert service.state(sid).reading is None
    service.execute(sid, allow_search=True)
    readings = [p for p in provider.calls if p.get("prompt_version") == "evidence-reading-1.2"]
    assert len(readings) == 2
    assert readings[-1]["allowed_followup_indexes"] == []


def test_ten_year_window_including_leap_days_is_supported():
    plan = plan_data()
    plan["duration_days"] = 3653
    assert AnalysisPlan.model_validate(plan).duration_days == 3653
    plan["duration_days"] = 3661
    with pytest.raises(ValueError):
        AnalysisPlan.model_validate(plan)


def test_complete_plan_can_raise_critical_target_clarification(repo, tmp_path):
    plan = plan_data()
    plan["questions"] = [
        {
            "question": "指哪个产品？",
            "kind": "target",
            "changes_judgment": "不同对象需要不同研究",
            "cannot_research_reason": "对象未指定",
        }
    ]
    service, sid = setup(repo, tmp_path, AssistantProvider(plan=plan))
    assert service.prepare(sid).stage == "clarification"


@pytest.mark.parametrize("answer", ["", "现金还能维持六个月"])
def test_research_clarification_persists_and_skip_uses_same_allowance(repo, tmp_path, answer):
    class PrivateProvider(AssistantProvider):
        def generate(self, system, user, schema, timeout):
            reply = super().generate(system, user, schema, timeout)
            data = json.loads(user.split("\n上次输出")[0])
            if (
                data.get("prompt_version") == "evidence-reading-1.2"
                and data["clarification_available"]
            ):
                output = json.loads(reply.content)
                output["questions"] = [
                    {
                        "question": "现金还能维持多久？",
                        "kind": "private_fact",
                        "changes_judgment": "影响生存判断",
                        "cannot_research_reason": "财务数据不公开",
                    }
                ]
                return ModelReply(content=json.dumps(output), input_tokens=100, output_tokens=100)
            return reply

    provider = PrivateProvider()
    search = SearchFixture()
    service, sid = setup(repo, tmp_path, provider, search)
    with pytest.raises(AppError, match="研究后需要"):
        service.execute(sid, allow_search=True)
    state = service.state(sid)
    assert state.stage == "clarification" and state.clarification_used
    assert service.prepare(sid).clarification_questions[0].kind == "private_fact"
    calls = len(provider.calls)
    queries = list(search.queries)
    sources = len(repo.load(sid).sources)
    service.answer(sid, answer)
    if not answer:
        assert len(provider.calls) == calls
        assert len(repo.load(sid).sources) == sources
        resumed = service.prepare(sid)
        assert resumed.stage == "reviewing"
        assert resumed.reading == state.reading
        assert resumed.approved_queries == state.approved_queries
        assert resumed.research_rounds == state.research_rounds
        assert resumed.clarification_answer
    else:
        assert len(provider.calls) > calls
        assert len(repo.load(sid).sources) == sources + 1
        assert service.state(sid).stage == "planned"
    result = service.execute(sid)
    assert result.draft.judgment.delivery == "directional"
    if not answer:
        assert len(provider.calls) == calls + 1  # Only the final judgment remains.
        assert search.queries == queries
        assert result.evidence_spans
        assert any("用户跳过，仍未知" in gap for gap in result.assessment.limitations)
        assert any("现金还能维持多久" in gap for gap in result.assessment.limitations)
    assert service.state(sid).clarification_used
    with pytest.raises(AppError, match="没有待回答"):
        service.answer(sid, "")


def test_one_clarification_round_skip_survives_reopen(repo, tmp_path):
    provider = AssistantProvider(clarify=True)
    service, sid = setup(repo, tmp_path, provider)
    state = service.prepare(sid)
    assert state.stage == "clarification" and state.clarification_used
    with pytest.raises(AppError):
        service.execute(sid)
    service.answer(sid, "")
    result = service.execute(sid)
    assert result.event.unknowns
    assert repo.load(sid).assistance.clarification_answer
    plans = [p for p in provider.calls if p.get("prompt_version") == "target-research-1.4"]
    assert [p["clarification_available"] for p in plans] == [True, False]
    with pytest.raises(AppError):
        service.answer(sid, "不再接受第二轮自动追问")


def test_research_extracts_quotes_and_final_cutoff_includes_collected_bodies(repo, tmp_path):
    search = SearchFixture()
    provider = AssistantProvider(numeric=True, followup=True)
    service, sid = setup(repo, tmp_path, provider, search)
    result = service.execute(sid, allow_search=True)
    assert len(search.queries) == 3 and repo.load(sid).assistance.research_rounds == 2
    assert result.evidence_spans and result.probabilities == [0.7, 0.3]
    assert result.assessment.review.review_origin == "model"
    for document in result.evidence_documents:
        assert document.imported_at <= result.evidence_cutoff
        assert document.derived_from
        assert (
            service.evidence.document(document.derived_from).content_hash == document.content_hash
        )
    assert all(
        e.eligible or "duplicate_content" in e.reasons or "candidate_only" in e.reasons
        for e in result.assessment.eligibility
    )


def test_no_query_authorization_no_network(repo, tmp_path):
    search = SearchFixture()
    service, sid = setup(repo, tmp_path, AssistantProvider(), search)
    service.execute(sid)
    assert search.queries == []


def test_bad_automatic_citation_is_rejected_not_empty_success(repo, tmp_path):
    service, sid = setup(repo, tmp_path, AssistantProvider(bad_quote=True), SearchFixture())
    with pytest.raises(AppError, match="引用或语义规则"):
        service.execute(sid, allow_search=True)
    assert repo.load(sid).status == "FAILED"
    assert not service.forecasts.history(sid, repo.load(sid).current_revision)


@pytest.mark.parametrize("delivery", ["conditional", "unable", "exploration"])
def test_non_predictions_are_explicit(repo, tmp_path, delivery):
    plan = plan_data()
    if delivery == "exploration":
        plan["analysis_kind"] = "exploration"
    service, sid = setup(repo, tmp_path, AssistantProvider(delivery=delivery, plan=plan))
    result = service.execute(sid)
    assert result.draft.judgment.delivery == delivery
    assert result.draft.judgment.primary_outcome is None
    assert result.probabilities == [None, None]


def test_cannot_define_target_stops_without_fabricated_forecast(repo, tmp_path):
    plan = plan_data()
    plan.update(question=None, outcomes=[], unavailable_reason="不知道要预测哪个对象的什么结果")
    provider = AssistantProvider(plan=plan)
    service, sid = setup(repo, tmp_path, provider)
    assert service.prepare(sid).stage == "unanswerable"
    with pytest.raises(AppError):
        service.execute(sid)
    assert len(provider.calls) == 2


def test_fixed_past_window_is_not_shifted(repo, tmp_path):
    plan = plan_data()
    plan.update(
        window_start=(now() - timedelta(days=1)).isoformat(),
        window_end=(now() + timedelta(days=29)).isoformat(),
    )
    service, sid = setup(repo, tmp_path, AssistantProvider(plan=plan))
    with pytest.raises(AppError, match="绝对窗口"):
        service.execute(sid)
    assert not repo.load(sid).research_runs


def test_interrupted_planning_requires_explicit_recovery(repo, tmp_path):
    provider = AssistantProvider(interrupt="target-research-1.4")
    service, sid = setup(repo, tmp_path, provider)
    with pytest.raises(KeyboardInterrupt):
        service.prepare(sid)
    before = repo.load(sid)
    with pytest.raises(AppError, match="中断"):
        service.prepare(sid)
    service.events.abandon_read(sid)
    result = service.execute(sid)
    assert result.draft.judgment and repo.load(sid).consumed_tokens > before.consumed_tokens
    assert repo.load(sid).attempts[1].error_code == "interrupted_cost_unknown"


def test_saved_forecast_recovered_if_progress_marker_missing(repo, tmp_path):
    provider = AssistantProvider()
    service, sid = setup(repo, tmp_path, provider)
    result = service.execute(sid)
    service._save(sid, stage="ready", forecast_id=None)
    assert service.execute(sid).id == result.id
    assert len(provider.calls) == 4


def test_judgment_ranking_and_type_are_hard_constraints(repo, tmp_path):
    service, sid = setup(repo, tmp_path, AssistantProvider(numeric=True))
    result = service.execute(sid)
    value = result.draft.model_dump()
    value["judgment"].update(ranking=["no", "yes"], primary_outcome="no")
    with pytest.raises(ValueError, match="矛盾"):
        validate_judgment(ForecastDraft.model_validate(value), result.spec, result.method, set())
    value["judgment"].update(delivery="unable", ranking=[], primary_outcome=None)
    with pytest.raises(ValueError, match="概率一致"):
        validate_judgment(ForecastDraft.model_validate(value), result.spec, result.method, set())


def test_plans_and_readings_reject_unsafe_or_incoherent_shapes():
    plan = plan_data()
    plan["analysis_kind"] = "conditional"
    with pytest.raises(ValueError, match="条件"):
        AnalysisPlan.model_validate(plan)
    with pytest.raises(ValueError):
        EvidenceReading.model_validate({"citations": [], "research_stop_reason": "无资料"})


def test_chat_default_assisted_path_has_no_research_forms(repo, tmp_path):
    from test_chat import setup_chat

    provider = AssistantProvider(numeric=True)
    chat, terminal = setup_chat(repo, tmp_path, provider, [NARRATIVE, "y", "/save", "/exit"])
    chat.assistance = AssistanceService(
        chat.events, chat.research, chat.assessments, chat.forecasts
    )
    assert chat.run() == 0
    assert "当前判断" in terminal.text and len(provider.calls) == 4
    assert "资料审查人名称" not in terminal.text and "粘贴原文片段" not in terminal.text
    report = json.loads(
        next((tmp_path / "reports").glob("*/report.json")).read_text(encoding="utf-8")
    )
    assert report["schema_version"] == "1.1" and report["draft"]["schema_version"] == "2.0"


def test_budget_failure_does_not_create_fake_analysis(repo, tmp_path):
    provider = AssistantProvider()
    service, sid = setup(repo, tmp_path, provider)
    s = repo.load(sid)
    repo.save(s.model_copy(update={"max_calls": 2}), s.version)
    with pytest.raises(AppError, match="预算"):
        service.execute(sid)
    assert len(provider.calls) == 2
    assert repo.load(sid).status == "FAILED" and repo.load(sid).consumed_tokens > 0
    assert not repo.forecasts(repo.revision(sid, repo.load(sid).current_revision).id)


@pytest.mark.parametrize("basis_status", ["insufficient", "conditional_only"])
def test_failed_research_without_basis_is_execution_failure(repo, tmp_path, basis_status):
    from future_agent.application.network import NetworkError

    class FailedSearch:
        def search(self, query, timeout):
            raise NetworkError("network")

    class InsufficientProvider(AssistantProvider):
        def generate(self, system, user, schema, timeout):
            reply = super().generate(system, user, schema, timeout)
            data = json.loads(reply.content)
            if "numeric_basis" in data:
                data["numeric_basis"].update(status=basis_status, source_kind="none")
                return reply.model_copy(update={"content": json.dumps(data)})
            return reply

    service, sid = setup(repo, tmp_path, InsufficientProvider(), search=FailedSearch())
    service.research.sleep = lambda _: None
    with pytest.raises(AppError, match="研究服务失败"):
        service.execute(sid, allow_search=True)
    assert service.state(sid).forecast_id is None
    assert service.state(sid).stage == "reviewing"
    assert not service.forecasts.history(sid, repo.load(sid).current_revision)
    calls = len(repo.load(sid).attempts)
    with pytest.raises(AppError, match="研究服务失败"):
        service.execute(sid)
    assert len(repo.load(sid).attempts) == calls


def test_old_forecast_serialization_and_schema_remain_unchanged():
    from conftest import forecast_spec

    from future_agent.domain.forecasts import forecast_schema

    draft = ForecastDraft.model_validate(forecast_response())
    assert "judgment" not in draft.model_dump()
    assert "guidance" not in forecast_spec().model_dump()
    assert "judgment" not in forecast_schema()["properties"]
    assert forecast_schema()["properties"]["schema_version"]["const"] == "1.0"
    assert forecast_schema(assisted=True)["properties"]["schema_version"]["const"] == "2.0"


def test_ordinary_numeric_basis_cannot_enter_locked_experiment(repo, tmp_path):
    service, sid = setup(repo, tmp_path, AssistantProvider(numeric=True))
    result = service.execute(sid)
    with pytest.raises(AppError, match="普通分析"):
        service.assessments.assess(
            sid, result.event.revision, result.assessment.review, experiment=True
        )


def test_unauthorized_query_candidate_rejected_before_network(repo, tmp_path):
    plan = plan_data()
    plan["queries"][0]["query"] = "私人姓名 test@example.com"
    service, sid = setup(repo, tmp_path, AssistantProvider(plan=plan), SearchFixture())
    with pytest.raises(AppError, match="引用或语义规则"):
        service.execute(sid, allow_search=True)
    assert not service.research.search.queries


def test_interrupted_forecast_resumes_same_step_and_reuses_completed_result(repo, tmp_path):
    provider = AssistantProvider(interrupt="forecast")
    service, sid = setup(repo, tmp_path, provider)
    with pytest.raises(KeyboardInterrupt):
        service.execute(sid)
    state = repo.load(sid).assistance
    request_id = state.forecast_request_id
    assert request_id and repo.step(request_id).status == "running"
    with pytest.raises(AppError, match="中断"):
        service.execute(sid)
    result = service.forecasts.resume(request_id, retry_interrupted=True)
    assert service.execute(sid).id == result.id
    assert repo.step(request_id).forecast_id == result.id and len(provider.calls) == 5


def test_durable_request_id_before_step_creation_can_continue(repo, tmp_path):
    from uuid import uuid4

    service, sid = setup(repo, tmp_path, AssistantProvider())
    result = service.execute(sid)
    # Use a new session to ensure a durable not-yet-created step can be created normally.
    service2, sid2 = setup(repo, tmp_path, AssistantProvider())
    service2.prepare(sid2)
    request_id = uuid4()
    service2._save(sid2, forecast_request_id=request_id)
    second = service2.execute(sid2)
    assert second.id != result.id and repo.step(request_id).forecast_id == second.id


def test_saved_judgment_cannot_relabel_target_or_use_old_assessment(repo, tmp_path):
    from future_agent.domain.forecasts import Forecast

    service, sid = setup(repo, tmp_path, AssistantProvider())
    result = service.execute(sid)
    data = result.model_dump(mode="json")
    data["spec"]["guidance"]["question"] = "被换成了另一个问题"
    with pytest.raises(ValueError, match="冻结问题"):
        Forecast.model_validate(data)
    data = result.model_dump(mode="json")
    data["assessment"]["assessor_version"] = "evidence-assessor-1.2"
    with pytest.raises(ValueError, match="新版普通评估"):
        Forecast.model_validate(data)


def test_cancelled_research_remains_visible_after_final_revision(repo, tmp_path):
    class InterruptedPage:
        def read(self, url, timeout):
            raise KeyboardInterrupt

    service, sid = setup(repo, tmp_path, AssistantProvider(), SearchFixture())
    service.research.pages = InterruptedPage()
    with pytest.raises(KeyboardInterrupt):
        service.execute(sid, allow_search=True)
    assert repo.load(sid).research_runs[0].status == "cancelled"
    result = service.execute(sid)
    assert any("research_cancelled" in gap for gap in result.assessment.limitations)
    assert result.probabilities == [None, None]
