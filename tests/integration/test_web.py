"""The local web adapter: real SQLite, real HTTP, explicit offline fixtures."""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest
from conftest import NARRATIVE
from test_assistance import AssistantProvider

from future_agent.application.assessment import AssessmentService
from future_agent.application.assistance import AssistanceService
from future_agent.application.events import EventService
from future_agent.application.evidence import EvidenceService
from future_agent.application.forecasts import ForecastService
from future_agent.application.lifecycle import ResolutionService
from future_agent.application.report_export import ReportExportService
from future_agent.application.research import ResearchService
from future_agent.config import Settings
from future_agent.storage.reports import FileReportStore
from future_agent.storage.snapshots import FileSnapshotStore
from future_agent.storage.sqlite import SQLiteRepository
from future_agent.web.app import Handler, WebApplication
from future_agent.web.services import WebServices


class Client:
    def __init__(self, base: str) -> None:
        self.base = base

    def call(self, method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(
            self.base + path, data=data, method=method, headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def raw(self, path: str) -> tuple[int, bytes, str]:
        with urllib.request.urlopen(self.base + path, timeout=30) as response:
            return response.status, response.read(), response.headers.get("Content-Type", "")

    def wait(self, job_id: str) -> dict:
        for _ in range(150):
            status, body = self.call("GET", f"/api/jobs/{job_id}")
            assert status == 200
            job = body["job"]
            if job["status"] in {"done", "failed"}:
                return job
            time.sleep(0.05)
        raise AssertionError("job did not finish")


@pytest.fixture
def web(tmp_path):
    settings = Settings(data_dir=tmp_path)
    repository = SQLiteRepository(settings.database_path())
    repository.migrate()
    provider = AssistantProvider()
    events = EventService(repository, provider)
    evidence = EvidenceService(repository, repository, FileSnapshotStore(tmp_path))
    assessments = AssessmentService(repository, repository, evidence)
    forecasts = ForecastService(repository, repository, assessments, provider)
    lifecycle = ResolutionService(repository, repository, evidence)
    research = ResearchService(repository, evidence, None, None)
    services = WebServices(
        settings=settings,
        repository=repository,
        provider=provider,
        events=events,
        evidence=evidence,
        assessments=assessments,
        forecasts=forecasts,
        lifecycle=lifecycle,
        research=research,
        assistance=AssistanceService(events, research, assessments, forecasts),
        exports=ReportExportService(forecasts, lifecycle, FileReportStore(tmp_path)),
    )
    application = WebApplication(services)
    handler = type("BoundHandler", (Handler,), {"application": application})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield Client(f"http://127.0.0.1:{server.server_address[1]}"), application
    finally:
        server.shutdown()
        server.server_close()
        application.close()


def test_ui_assets_and_session_api_roundtrip(web):
    client, _ = web
    status, body, content_type = client.raw("/")
    assert status == 200
    assert "text/html" in content_type
    assert "预测未来" in body.decode("utf-8")

    status, body, content_type = client.raw("/assets/app.js")
    assert status == 200
    assert "text/javascript" in content_type
    assert "const state" in body.decode("utf-8")

    for path in ("/index.html", "/some/deep/path"):
        status, body, content_type = client.raw(path)
        assert status == 200
        assert "text/html" in content_type
        assert "预测未来" in body.decode("utf-8")

    status, body = client.call("GET", "/api/health")
    assert status == 200
    assert body["model_ready"] is True
    assert set(body["defaults"]) == {
        "max_calls",
        "max_run_seconds",
        "max_total_tokens",
        "max_search_queries",
        "max_pages",
    }

    status, body = client.call("POST", "/api/sessions", {"text": NARRATIVE})
    assert status == 201
    session_id = body["session"]["id"]

    status, body = client.call("GET", "/api/sessions")
    assert status == 200
    assert [item["id"] for item in body["sessions"]] == [session_id]

    status, body = client.call("GET", f"/api/sessions/{session_id}")
    assert status == 200
    session = body["session"]
    assert session["source_text"] == NARRATIVE.replace("\r\n", "\n")
    assert session["assistance"]["stage"] is None
    assert session["forecast"] is None


def test_ui_rejects_bad_input_paths_and_assets(web):
    client, _ = web
    assert client.call("POST", "/api/sessions", {"text": "太短"})[0] == 400
    assert client.call("POST", "/api/sessions", {})[0] == 400

    status, body = client.call("POST", "/api/sessions", {"text": NARRATIVE})
    session_id = body["session"]["id"]
    assert client.call("POST", f"/api/sessions/{session_id}/unknown", {})[0] == 404
    assert client.call("GET", "/api/jobs/00000000-0000-0000-0000-000000000000")[0] == 404
    assert client.call("GET", "/api/sessions/not-a-uuid")[0] == 404
    assert client.call("GET", "/api/nope")[0] == 404
    assert client.call("GET", "/assets/../app.py")[0] == 404
    assert client.call("PUT", "/api/sessions")[0] == 405


def test_prepare_execute_freeze_and_resolve_through_http(web):
    client, _ = web
    status, body = client.call("POST", "/api/sessions", {"text": NARRATIVE})
    session_id = body["session"]["id"]

    status, body = client.call("POST", f"/api/sessions/{session_id}/prepare", {})
    assert status == 202
    assert client.wait(body["job"]["id"])["status"] == "done"

    status, body = client.call("GET", f"/api/sessions/{session_id}")
    state = body["session"]["assistance"]
    assert state["stage"] == "planned"
    assert state["plan"]["outcomes"]

    status, body = client.call(
        "POST", f"/api/sessions/{session_id}/execute", {"allow_search": False}
    )
    assert status == 202
    assert client.wait(body["job"]["id"])["status"] == "done"

    status, body = client.call("GET", f"/api/sessions/{session_id}")
    session = body["session"]
    assert session["status"] == "STRUCTURED"
    forecast = session["forecast"]
    assert forecast is not None
    assert forecast["delivery"] == "directional"
    assert forecast["frozen"] is None
    assert [outcome["percent"] for outcome in forecast["outcomes"]] == [None, None]

    status, body = client.call("POST", f"/api/sessions/{session_id}/freeze", {})
    assert status == 202
    assert client.wait(body["job"]["id"])["status"] == "done"

    status, body = client.call("GET", f"/api/sessions/{session_id}")
    assert body["session"]["forecast"]["frozen"]["input_hash"]

    status, body = client.call(
        "POST",
        f"/api/sessions/{session_id}/resolve",
        {"status": "pending", "resolver": "tester", "reason": "等待官方结果"},
    )
    assert status == 202
    assert client.wait(body["job"]["id"])["status"] == "done"

    status, body = client.call("GET", f"/api/sessions/{session_id}")
    assert [item["status"] for item in body["session"]["resolutions"]] == ["pending"]

    status, body, content_type = client.raw(f"/api/forecasts/{forecast['id']}/report.md")
    assert status == 200
    assert "text/markdown" in content_type
    assert "问题：" in body.decode("utf-8")


def test_freeze_reason_requires_a_superseded_forecast(web):
    client, _ = web
    status, body = client.call("POST", "/api/sessions", {"text": NARRATIVE})
    session_id = body["session"]["id"]
    client.wait(client.call("POST", f"/api/sessions/{session_id}/prepare", {})[1]["job"]["id"])
    client.wait(
        client.call("POST", f"/api/sessions/{session_id}/execute", {"allow_search": False})[1][
            "job"
        ]["id"]
    )
    status, body = client.call("POST", f"/api/sessions/{session_id}/freeze", {"reason": "修订说明"})
    assert status == 400
    assert "另存修订" in body["error"]


def test_retry_interrupted_read_and_delete_persisted_report(web):
    from uuid import UUID

    from future_agent.domain.events import transition

    client, application = web
    sid = client.call("POST", "/api/sessions", {"text": NARRATIVE})[1]["session"]["id"]
    repo = application.services.repository
    original = repo.load(UUID(sid))
    repo.save(transition(original, "READING"), original.version)
    status, result = client.call("POST", f"/api/sessions/{sid}/retry", {})
    assert status == 202
    assert client.wait(result["job"]["id"])["status"] == "done"
    assert repo.load(UUID(sid)).max_calls == original.max_calls
    result = client.call("POST", f"/api/sessions/{sid}/execute", {"allow_search": False})[1]
    assert client.wait(result["job"]["id"])["status"] == "done"
    forecast_id = client.call("GET", f"/api/sessions/{sid}")[1]["session"]["forecast"]["id"]
    assert client.call("DELETE", f"/api/sessions/{sid}")[0] == 200
    assert client.call("GET", f"/api/sessions/{sid}")[0] == 404
    assert client.call("GET", f"/api/forecasts/{forecast_id}/report.md")[0] == 404
    assert client.call("GET", "/api/sessions")[1]["sessions"] == []
    assert client.call("DELETE", f"/api/sessions/{sid}")[0] == 404


def test_active_job_reconnect_and_delete_guard(web):
    client, application = web
    from uuid import UUID

    sid = client.call("POST", "/api/sessions", {"text": NARRATIVE})[1]["session"]["id"]
    release = threading.Event()
    try:
        job = application.start_job("prepare", UUID(sid), lambda emit: release.wait(10) or {})
        status, body = client.call("GET", f"/api/sessions/{sid}")
        assert status == 200
        assert body["active_job"]["id"] == str(job.id)
        assert client.call("DELETE", f"/api/sessions/{sid}")[0] == 409
        assert client.call("POST", f"/api/sessions/{sid}/retry", {})[0] == 409
    finally:
        release.set()


def test_frozen_history_cannot_be_deleted(web):
    client, _ = web
    sid = client.call("POST", "/api/sessions", {"text": NARRATIVE})[1]["session"]["id"]
    for action in ("prepare", "execute", "freeze"):
        result = client.call("POST", f"/api/sessions/{sid}/{action}", {})[1]
        assert client.wait(result["job"]["id"])["status"] == "done"
    assert client.call("DELETE", f"/api/sessions/{sid}")[0] == 409
    assert client.call("GET", f"/api/sessions/{sid}")[1]["session"]["forecast"]["frozen"]


def test_forecast_progress_and_stop_discard_late_response(web, monkeypatch):
    from uuid import UUID

    client, application = web
    sid = client.call("POST", "/api/sessions", {"text": NARRATIVE})[1]["session"]["id"]
    result = client.call("POST", f"/api/sessions/{sid}/prepare", {})[1]
    assert client.wait(result["job"]["id"])["status"] == "done"
    entered, release = threading.Event(), threading.Event()
    generate = application.services.provider.generate

    def blocked(*args, **kwargs):
        if application.services.events.get(UUID(sid)).status == "FORECASTING":
            entered.set()
            assert release.wait(10)
        return generate(*args, **kwargs)

    monkeypatch.setattr(application.services.provider, "generate", blocked)
    result = client.call("POST", f"/api/sessions/{sid}/execute", {})[1]
    try:
        assert entered.wait(10)
        job = client.call("GET", f"/api/jobs/{result['job']['id']}")[1]["job"]
        assert job["session_status"] == "FORECASTING"
        assert any("最终判断" in line["message"] for line in job["progress"])
        assert any("模型请求开始" in line["message"] for line in job["progress"])
        assert client.call("POST", f"/api/sessions/{sid}/cancel-forecast", {})[0] == 200
    finally:
        release.set()
    assert client.wait(result["job"]["id"])["status"] == "failed"
    saved = client.call("GET", f"/api/sessions/{sid}")[1]["session"]
    assert saved["forecast"] is None
    assert saved["error_code"] == "cancelled"
    assert client.call("POST", f"/api/sessions/{sid}/cancel-forecast", {})[0] == 409


@pytest.mark.parametrize("phase", ["planning", "reading"])
def test_stop_model_phases_retains_cost_and_can_retry(web, monkeypatch, phase):
    client, application = web
    sid = client.call("POST", "/api/sessions", {"text": NARRATIVE})[1]["session"]["id"]
    if phase == "reading":
        job = client.call("POST", f"/api/sessions/{sid}/prepare", {})[1]["job"]
        assert client.wait(job["id"])["status"] == "done"
    entered, release = threading.Event(), threading.Event()
    generate = application.services.provider.generate

    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return generate(*args, **kwargs)

    monkeypatch.setattr(application.services.provider, "generate", blocked)
    action = "prepare" if phase == "planning" else "execute"
    job = client.call("POST", f"/api/sessions/{sid}/{action}", {})[1]["job"]
    try:
        assert entered.wait(10)
        assert client.call("POST", f"/api/sessions/{sid}/stop", {})[0] == 200
        snapshot = client.call("GET", f"/api/jobs/{job['id']}")[1]["job"]
        assert snapshot["stop_requested"]
        assert "metrics" in snapshot
    finally:
        release.set()
    assert client.wait(job["id"])["status"] == "failed"
    saved = client.call("GET", f"/api/sessions/{sid}")[1]["session"]
    assert saved["forecast"] is None
    assert saved["assistance"]["budgets"]["calls_used"] > 0
    monkeypatch.setattr(application.services.provider, "generate", generate)
    retry = client.call("POST", f"/api/sessions/{sid}/retry", {})[1]["job"]
    assert client.wait(retry["id"])["status"] == "done"


def test_verification_queue_only_due_unresolved_and_disputed(web, monkeypatch):
    from datetime import timedelta

    from future_agent.domain.contracts import now

    client, application = web
    sid = client.call("POST", "/api/sessions", {"text": NARRATIVE})[1]["session"]["id"]
    for action in ("prepare", "execute", "freeze"):
        job = client.call("POST", f"/api/sessions/{sid}/{action}", {})[1]["job"]
        assert client.wait(job["id"])["status"] == "done"
    assert client.call("GET", "/api/sessions")[1]["verification_queue"] == []
    monkeypatch.setattr(
        "future_agent.application.lifecycle.now", lambda: now() + timedelta(days=40)
    )
    queue = client.call("GET", "/api/sessions")[1]["verification_queue"]
    assert len(queue) == 1 and queue[0]["session_id"] == sid
    for status in ("disputed", "void"):
        detail = client.call("GET", f"/api/sessions/{sid}")[1]["session"]
        job = client.call(
            "POST",
            f"/api/sessions/{sid}/resolve",
            {
                "status": status,
                "resolver": "tester",
                "reason": "test resolution",
                "expected_previous": detail["last_resolution_id"],
            },
        )[1]["job"]
        assert client.wait(job["id"])["status"] == "done"
        queue = client.call("GET", "/api/sessions")[1]["verification_queue"]
        assert bool(queue) == (status == "disputed")
