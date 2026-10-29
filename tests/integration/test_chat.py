"""Terminal paths with real services, SQLite and files; only external boundaries are fixtures."""

import json
import os
import shutil
import subprocess
from uuid import uuid4

import pytest
from conftest import (
    NARRATIVE,
    FixtureProvider,
    complete_question,
    forecast_response,
    forecast_spec,
    make_draft,
)

from future_agent.application.assessment import AssessmentService
from future_agent.application.events import EventService
from future_agent.application.evidence import EvidenceService
from future_agent.application.forecasts import ForecastService
from future_agent.application.lifecycle import ResolutionService
from future_agent.application.network import NetworkError
from future_agent.application.ports import AppError, ModelError, ModelReply
from future_agent.application.report_export import ReportExportService
from future_agent.application.research import ResearchService
from future_agent.chat import Chat
from future_agent.config import Settings
from future_agent.domain.assessment import AssessmentReview
from future_agent.domain.events import CallAttempt, EventDraft, SourceText
from future_agent.domain.evidence import EvidenceImport
from future_agent.storage.reports import FileReportStore
from future_agent.storage.snapshots import FileSnapshotStore
from future_agent.terminal import display_width, terminal_layout


class Terminal:
    def __init__(self, answers):
        self.answers = iter(answers)
        self.lines = []

    def read(self, prompt):
        self.lines.append(prompt)
        try:
            return next(self.answers)
        except StopIteration:
            raise EOFError from None

    @property
    def text(self):
        return "\n".join(self.lines)


def setup_chat(repo, tmp_path, provider, answers=(), search=None):
    terminal = Terminal(answers)
    events = EventService(repo, provider)
    evidence = EvidenceService(repo, repo, FileSnapshotStore(tmp_path))
    assessments = AssessmentService(repo, repo, evidence)
    forecasts = ForecastService(repo, repo, assessments, provider)
    lifecycle = ResolutionService(repo, repo, evidence)
    chat = Chat(
        events,
        evidence,
        assessments,
        forecasts,
        lifecycle,
        ResearchService(repo, evidence, search, None, sleep=lambda _: None),
        ReportExportService(forecasts, lifecycle, FileReportStore(tmp_path)),
        create_options={},
        read=terminal.read,
        write=terminal.lines.append,
    )
    return chat, terminal


def test_chat_welcome_and_help_keep_navigation_accessible(repo, tmp_path):
    chat, terminal = setup_chat(repo, tmp_path, FixtureProvider([]), ["/help", "/exit"])
    assert chat.run() == 0
    assert "预测未来  ·  终端助手" in terminal.lines[0]
    assert "操作指南" not in terminal.lines[0]
    assert "操作指南" in terminal.text and "/continue" in terminal.text
    assert any(line.endswith("\n> ") for line in terminal.lines)
    assert "模型会接收叙述" in terminal.text


def test_narrow_terminal_preserves_long_chinese_content_and_paragraphs():
    text = "  • 关键依据：" + "中文与ASCII混排e\u0301，不应丢失正文。" * 8
    rendered = terminal_layout(text + "\n\n下一步：/details", width=40)
    assert all(display_width(line) <= 40 for line in rendered.splitlines())
    assert "\n\n下一步：/details" in rendered
    assert "".join(rendered.split()) == "".join((text + "下一步：/details").split())
    assert rendered.splitlines()[1].startswith("    ")


def test_internal_reader_questions_hidden_in_ordinary_status(repo, tmp_path):
    chat, terminal = setup_chat(repo, tmp_path, FixtureProvider([]))
    session = chat.events.create(NARRATIVE)
    chat.events.edit(session.id, 0, make_draft(session.sources[0]), confirmed=False)
    chat.sid = session.id
    chat.dossier()
    assert "待澄清：" in terminal.text
    terminal.lines.clear()
    chat.assistance = object()
    chat.dossier()
    assert "待澄清：" not in terminal.text
    assert chat.session().pending_questions


def confirmed(chat):
    s = chat.events.create(NARRATIVE)
    draft = make_draft(s.sources[0]).model_copy(update={"question": complete_question()})
    chat.events.edit(s.id, 0, draft, confirmed=True)
    chat.sid = s.id
    return s.id


def saved_forecast(chat):
    event = chat.event()
    assessment = chat.assessments.assess(
        chat.sid, event.revision, AssessmentReview(reviewer="人工")
    )
    f = chat.forecasts.run(chat.sid, event.revision, assessment.id, forecast_spec())
    chat.selected = f.id
    return f


class ReaderAndForecast(FixtureProvider):
    def __init__(self):
        super().__init__([])

    def generate(self, system, user, schema, timeout):
        data = json.loads(user)
        self.users.append(data)
        if "sources" in data and "spec" not in data:
            draft = make_draft(SourceText.model_validate(data["sources"][0]))
            return ModelReply(content=draft.model_dump_json(), input_tokens=100, output_tokens=40)
        return ModelReply(
            content=json.dumps(forecast_response()), input_tokens=100, output_tokens=40
        )


def test_new_user_whole_scenario_path_without_ids_or_json(repo, tmp_path):
    q = complete_question()
    answers = [
        NARRATIVE,
        "y",
        "/unknown",
        "1",
        "/continue",
        q.question,
        "binary",
        q.forecast_at.isoformat(),
        q.window_start.isoformat(),
        q.window_end.isoformat(),
        q.resolution_rule,
        q.resolution_source_policy,
        "y",
        "2",
        "人工",
        "",
        "不检索",
        "n",
        "窗口内发布稳定版",
        "完整窗口未发布",
        "y",
        "人工确认互斥穷尽",
        "y",
        "/save",
        "/freeze",
        "n",
        "n",
        "y",
        "/save",
        "/exit",
    ]
    provider = ReaderAndForecast()
    chat, terminal = setup_chat(repo, tmp_path, provider, answers)
    assert chat.run() == 0
    assert len(provider.users) == 2
    session = chat.session()
    assert session.current_revision == 4 and len(session.attempts) == 2
    assert chat.event().unknowns[0].resolution_status == "kept_unknown"
    f = chat.forecast()
    assert f.probabilities == [None, None] and f.calibration_status == "not_applicable"
    assert chat.lifecycle.frozen(f.id)
    reports = list((tmp_path / "reports").glob("*/report.json"))
    assert len(reports) == 2
    payloads = [json.loads(p.read_text(encoding="utf-8")) for p in reports]
    assert {p["status"] for p in payloads} == {"draft", "frozen"}
    assert all(p["probabilities"] == [None, None] for p in payloads)
    assert all(NARRATIVE not in p.read_text(encoding="utf-8") for p in reports)
    assert all((p.parent / "COMPLETE").is_file() for p in reports)
    assert "费用未知" in terminal.text and "已退出" in terminal.text


def test_resume_navigation_and_report_never_calls_model(repo, tmp_path):
    chat, _ = setup_chat(repo, tmp_path, FixtureProvider([json.dumps(forecast_response())]))
    sid = confirmed(chat)
    f = saved_forecast(chat)
    original = repo.load(sid)
    provider = FixtureProvider([])
    resumed, terminal = setup_chat(
        repo, tmp_path, provider, ["/report", "/details", "/save", "/exit"]
    )
    assert resumed.run(sid) == 0
    assert provider.users == [] and repo.load(sid) == original
    assert str(f.id) in terminal.text
    summary, details = terminal.text.split("── 完整预测报告 ──", 1)
    assert "── 预测结果 ──" in summary and "/details" in summary
    assert "预测ID：" not in summary and "引用来源：" not in summary
    assert "预测ID：" in details and "引用来源：" in details
    selected, _ = setup_chat(repo, tmp_path, provider, ["2", "/exit"])
    assert selected.run() == 0 and selected.sid == sid


def test_answers_are_saved_before_optional_reread(repo, tmp_path):
    chat, _ = setup_chat(repo, tmp_path, FixtureProvider([]), ["补充事实。", "n", "/exit"])
    sid = confirmed(chat)
    assert chat.run(sid) == 0
    assert repo.load(sid).sources[-1].raw == "补充事实。"
    assert repo.load(sid).current_revision == 2 and not chat.event().confirmed


def predict_answers(numeric=False, accept_scenario=True):
    return [
        "人工审查",
        "",
        "",
        "y" if numeric else "n",
        *(["y" if accept_scenario else "n"] if numeric else []),
        "窗口内发生",
        "完整窗口未发生",
        "y",
        "已确认互斥穷尽",
        "y",
    ]


def test_numeric_rejected_then_explicit_scenario(repo, tmp_path):
    provider = FixtureProvider([json.dumps(forecast_response())])
    chat, terminal = setup_chat(
        repo, tmp_path, provider, ["/forecast", *predict_answers(True), "/exit"]
    )
    sid = confirmed(chat)
    assert chat.run(sid) == 0
    assert "数值资格拒绝" in terminal.text
    assert chat.forecast().method == "scenario_only" and len(provider.users) == 1


def test_numeric_refusal_to_downgrade_does_not_call_model(repo, tmp_path):
    provider = FixtureProvider([])
    chat, terminal = setup_chat(
        repo, tmp_path, provider, ["/forecast", "人工", "", "", "y", "n", "/exit"]
    )
    sid = confirmed(chat)
    assert chat.run(sid) == 0
    assert "数值资格拒绝" in terminal.text and provider.users == []
    assert chat.all_forecasts() == []


def test_reviewed_body_allows_numeric_and_keeps_relevance_human(repo, tmp_path):
    provider = FixtureProvider([json.dumps(forecast_response((0.4, 0.6)))])
    answers = [
        "/forecast",
        "y",
        "公开正文可供核查。",
        "1",
        "与目标直接相关",
        "n",
        "人工审查人",
        "",
        "",
        "y",
        "窗口内发生",
        "完整窗口未发生",
        "y",
        "互斥穷尽",
        "y",
        "/exit",
    ]
    chat, terminal = setup_chat(repo, tmp_path, provider, answers)
    sid = confirmed(chat)
    chat.evidence.import_document(
        sid,
        1,
        EvidenceImport(
            url="https://example.com/current",
            raw_text="公开正文可供核查。",
            access_status="read_success",
            retrieved_at=chat.event().created_at,
        ),
    )
    assert chat.run(sid) == 0
    f = chat.forecast()
    assert f.method == "llm_judgment" and f.probabilities == [0.4, 0.6]
    assert f.assessment.review.reviewer == "人工审查人"
    assert f.assessment.review.evidence[0].relevance_reason == "与目标直接相关"
    assert "uncalibrated" in terminal.text


@pytest.mark.parametrize("issue", ["budget", "model"])
def test_failed_prediction_keeps_cost_no_empty_success(repo, tmp_path, issue):
    provider = FixtureProvider([ModelError("authentication")])
    chat, terminal = setup_chat(
        repo, tmp_path, provider, ["/forecast", *predict_answers(), "/exit"]
    )
    sid = confirmed(chat)
    if issue == "budget":
        s = repo.load(sid)
        repo.save(s.model_copy(update={"consumed_tokens": s.max_total_tokens}), s.version)
    assert chat.run(sid) == 0
    assert chat.all_forecasts() == []
    assert "已保留现有产物" in terminal.text
    if issue == "budget":
        assert provider.users == []
    else:
        assert len(repo.load(sid).attempts) == 1
    step_id = repo.load(sid).active_forecast_step_id
    cancelled, _ = setup_chat(repo, tmp_path, FixtureProvider([]), ["/cancel", "y", "/exit"])
    assert cancelled.run(sid) == 0
    assert repo.step(step_id).status == "cancelled"


@pytest.mark.parametrize("stage", ["reader", "forecast"])
def test_ctrl_c_reopen_does_not_retry_then_explicit_cancel(repo, tmp_path, stage):
    provider = FixtureProvider([KeyboardInterrupt()])
    chat, _ = setup_chat(repo, tmp_path, provider)
    if stage == "reader":
        s = chat.events.create(NARRATIVE)
        sid = s.id
        chat, terminal = setup_chat(repo, tmp_path, provider, ["/read"])
    else:
        sid = confirmed(chat)
        chat, terminal = setup_chat(repo, tmp_path, provider, ["/forecast", *predict_answers()])
    assert chat.run(sid) == 130
    interrupted = repo.load(sid)
    assert interrupted.attempts[-1].status == "started"
    resumed, _ = setup_chat(repo, tmp_path, FixtureProvider([]), ["/retry", "n", "/exit"])
    assert resumed.run(sid) == 0 and repo.load(sid) == interrupted
    cancelled, _ = setup_chat(repo, tmp_path, FixtureProvider([]), ["/cancel", "y", "/exit"])
    assert cancelled.run(sid) == 0
    after = repo.load(sid)
    assert after.status == "FAILED" and after.attempts[-1].error_code == "interrupted_cost_unknown"
    assert after.consumed_tokens == interrupted.consumed_tokens
    if stage == "forecast":
        assert repo.step(after.active_forecast_step_id).status == "cancelled"


def test_interrupted_forecast_explicit_retry_uses_same_step_and_budget(repo, tmp_path):
    provider = FixtureProvider([KeyboardInterrupt(), json.dumps(forecast_response())])
    chat, _ = setup_chat(repo, tmp_path, provider, ["/forecast", *predict_answers()])
    sid = confirmed(chat)
    assert chat.run(sid) == 130
    before = repo.load(sid)
    resumed, _ = setup_chat(repo, tmp_path, provider, ["/retry", "y", "/exit"])
    assert resumed.run(sid) == 0
    assert len(repo.load(sid).attempts) == 2
    f = resumed.forecast()
    assert repo.step(before.active_forecast_step_id).forecast_id == f.id
    assert repo.load(sid).consumed_tokens > before.consumed_tokens


class FailedSearch:
    def search(self, query, timeout):
        raise NetworkError("unavailable")


def test_search_failure_stays_visible_and_not_read_body(repo, tmp_path):
    chat, terminal = setup_chat(
        repo,
        tmp_path,
        FixtureProvider([]),
        ["/research", "Example", "release", "y", "/exit"],
        search=FailedSearch(),
    )
    # Research requires both adapters. A never-called page port is enough for failed search.
    chat.research.pages = object()
    sid = confirmed(chat)
    assert chat.run(sid) == 0
    run = repo.load(sid).research_runs[-1]
    assert run.status in {"partial", "failed"} and run.gaps
    assert not chat.evidence.list_documents(sid, 1)
    assert "研究状态" in terminal.text


def test_goal_revision_retains_frozen_report_and_invalidates_confirmation(repo, tmp_path):
    provider = FixtureProvider([json.dumps(forecast_response())])
    chat, _ = setup_chat(repo, tmp_path, provider)
    sid = confirmed(chat)
    f = saved_forecast(chat)
    frozen = chat.lifecycle.freeze(f.id)
    revised, terminal = setup_chat(
        repo,
        tmp_path,
        FixtureProvider([]),
        ["/target", "新的预测目标？", "", "", "", "", "", "", "/report", "/freeze", "/exit"],
    )
    assert revised.run(sid) == 0
    assert revised.event().question.question == "新的预测目标？"
    assert not revised.event().confirmed
    assert revised.lifecycle.frozen(f.id) == frozen
    assert "旧事件版本" in terminal.text and "此预测已冻结" in terminal.text


def test_pending_resolution_keeps_append_history(repo, tmp_path):
    chat, _ = setup_chat(repo, tmp_path, FixtureProvider([json.dumps(forecast_response())]))
    sid = confirmed(chat)
    f = saved_forecast(chat)
    chat.lifecycle.freeze(f.id)
    resumed, _ = setup_chat(
        repo, tmp_path, FixtureProvider([]), ["/resolve", "1", "核验人", "窗口未结束", "y", "/exit"]
    )
    assert resumed.run(sid) == 0
    history = chat.lifecycle.history(f.id)
    assert len(history) == 1 and history[0].status == "pending"
    assert history[0].resolved_outcome is None


@pytest.mark.parametrize("early", [True, False])
def test_resolution_wizard_enforces_window_and_saves_real_quotes(repo, tmp_path, early):
    from datetime import timedelta

    from future_agent.domain.contracts import now

    chat, _ = setup_chat(repo, tmp_path, FixtureProvider([json.dumps(forecast_response())]))
    sid = confirmed(chat)
    event = chat.event()
    question = event.question.model_copy(
        update={
            "forecast_at": now() - timedelta(days=1),
            "window_start": now() - timedelta(hours=12),
            "window_end": now() + timedelta(days=29),
        }
    )
    draft = EventDraft.model_validate(event.model_dump(include=EventDraft.model_fields.keys()))
    chat.events.edit(
        sid, event.revision, draft.model_copy(update={"question": question}), confirmed=True
    )
    f = saved_forecast(chat)
    chat.lifecycle.freeze(f.id, early_outcomes=["yes"] if early else [])
    answers = [
        "/resolve",
        "2",
        "1",
        (now() - timedelta(hours=1)).isoformat(),
        "https://example.com/releases",
        "官方记录",
        "版本1.0已经公开发布。",
        "版本1.0已经公开发布。",
        "核验人",
        "按官方记录确认发生",
        "y",
        "/exit",
    ]
    resumed, terminal = setup_chat(repo, tmp_path, FixtureProvider([]), answers)
    assert resumed.run(sid) == 0
    history = chat.lifecycle.history(f.id)
    if early:
        assert len(history) == 1 and history[0].status == "resolved"
        assert history[0].resolved_outcome == "yes"
        assert history[0].evidence_spans[0].quote == "版本1.0已经公开发布。"
    else:
        assert history == [] and "数据规则校验失败" in terminal.text


def test_export_excludes_sources_cached_bodies_and_session_secrets(repo, tmp_path):
    chat, _ = setup_chat(repo, tmp_path, FixtureProvider([json.dumps(forecast_response())]))
    confirmed(chat)
    f = saved_forecast(chat)
    s = chat.session()
    chat.events.answer(s.id, s.current_revision, "PRIVATE_SENTINEL_NOT_FOR_EXPORT")
    paths = chat.exports.save(f.id)
    for p in paths:
        assert p.is_absolute() and p.is_relative_to(tmp_path)
        text = p.read_text(encoding="utf-8")
        assert "PRIVATE_SENTINEL_NOT_FOR_EXPORT" not in text and NARRATIVE not in text
    result = json.loads(paths[1].read_text(encoding="utf-8"))
    assert result["kind"] == "forecast_report" and result["probabilities"] == [None, None]
    assert "sources" not in result and "event" not in result


def test_export_does_not_overwrite_existing_bundle(tmp_path, monkeypatch):
    fixed = uuid4()
    monkeypatch.setattr("future_agent.storage.reports.uuid4", lambda: fixed)
    store = FileReportStore(tmp_path)
    first = store.save("first", {})
    with pytest.raises(FileExistsError):
        store.save("second", {})
    assert first[0].read_text(encoding="utf-8") == "first"


def test_export_failure_leaves_no_completion_marker(tmp_path, monkeypatch):
    def fail(_):
        raise OSError("synthetic write failure")

    monkeypatch.setattr("future_agent.storage.reports.os.fsync", fail)
    with pytest.raises(OSError):
        FileReportStore(tmp_path).save("partial", {})
    assert list((tmp_path / "reports").glob("*/COMPLETE")) == []


def test_report_path_cannot_escape_data_root(tmp_path):
    data, outside = tmp_path / "data", tmp_path / "outside"
    data.mkdir()
    outside.mkdir()
    try:
        (data / "reports").symlink_to(outside, target_is_directory=True)
    except OSError:
        if os.name != "nt":
            pytest.skip("Host does not permit directory symlinks")
        import _winapi

        _winapi.CreateJunction(str(outside), str(data / "reports"))
    with pytest.raises(AppError, match="越出"):
        FileReportStore(data).save("x", {})
    assert list(outside.iterdir()) == []


def test_commands_during_prompts_and_terminal_control_filter(repo, tmp_path):
    chat, terminal = setup_chat(
        repo, tmp_path, FixtureProvider([]), ["/target", "/status", "/exit"]
    )
    sid = confirmed(chat)
    assert chat.run(sid) == 0
    assert repo.load(sid).current_revision == 1
    assert "剩余预算" in terminal.text
    chat.say("abc\x1b[2J\x9b\u202edef")
    assert "\x1b" not in terminal.lines[-1] and "\x9b" not in terminal.lines[-1]
    assert "\u202e" not in terminal.lines[-1]


def test_cli_create_reopen_without_model_in_separate_processes(tmp_path):
    env = {
        key: value for key, value in os.environ.items() if key.lower() not in Settings.model_fields
    }
    command = [shutil.which("future-agent"), "--no-env", "--data-dir", str(tmp_path), "chat"]
    first = subprocess.run(
        command,
        input="新项目准备发布。\nn\n/exit\n",
        text=True,
        encoding="utf-8",
        capture_output=True,
        timeout=20,
        env={**env, "PYTHONUTF8": "1"},
    )
    assert first.returncode == 0, first.stderr
    second = subprocess.run(
        command,
        input="2\n/status\n/exit\n",
        text=True,
        encoding="utf-8",
        capture_output=True,
        timeout=20,
        env={**env, "PYTHONUTF8": "1"},
    )
    assert second.returncode == 0, second.stderr
    assert "CREATED" in second.stdout and "模型：12/12" in second.stdout


def test_cancel_read_does_not_reset_budget(repo, tmp_path):
    chat, _ = setup_chat(repo, tmp_path, FixtureProvider([]))
    s = chat.events.create(NARRATIVE)
    started = s.model_copy(
        update={
            "status": "READING",
            "attempts": [CallAttempt(reserved_tokens=50)],
            "consumed_tokens": 50,
            "consumed_seconds": 60,
        }
    )
    repo.save(started, s.version)
    result = chat.events.abandon_read(s.id)
    assert result.consumed_tokens == 50 and result.consumed_seconds == 60
    assert result.attempts[0].status == "failed"
    assert result.attempts[0].cost is None


def test_chat_process_crash_preserves_step_and_reopens_without_request(tmp_path):
    # Abrupt termination bypasses Chat's KeyboardInterrupt handler and all finally blocks.
    script = """
import os
import sys
from pathlib import Path
sys.path[:0] = ['tests', 'tests/integration']
from test_chat import setup_chat, confirmed, predict_answers
from conftest import FixtureProvider
from future_agent.storage.sqlite import SQLiteRepository
class CrashProvider(FixtureProvider):
    def generate(self, *args, **kwargs):
        os._exit(31)
root = Path(sys.argv[1])
repo = SQLiteRepository(root / 'app.db')
repo.migrate()
chat, _ = setup_chat(repo, root, CrashProvider([]), ['/forecast', *predict_answers()])
sid = confirmed(chat)
chat.run(sid)
"""
    result = subprocess.run(
        [shutil.which("python"), "-c", script, str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 31, result.stderr
    from future_agent.storage.sqlite import SQLiteRepository

    repo = SQLiteRepository(tmp_path / "app.db")
    try:
        session = repo.list_sessions()[0]
        step = repo.step(session.active_forecast_step_id)
        assert session.status == "FORECASTING" and step.status == "running"
        assert session.attempts[-1].status == "started" and session.consumed_tokens > 0
        provider = FixtureProvider([])
        resumed, terminal = setup_chat(repo, tmp_path, provider, ["/exit"])
        assert resumed.run(session.id) == 0
        assert provider.users == [] and repo.load(session.id) == session
        assert str(step.id) in terminal.text
    finally:
        repo.close()
