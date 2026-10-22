"""Real SQLite, snapshots and application flow, with explicit network fixtures."""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from conftest import complete_question

from future_agent.application.events import EventService
from future_agent.application.evidence import EvidenceService
from future_agent.application.network import NetworkError
from future_agent.application.ports import AppError, Conflict
from future_agent.application.research import ResearchService, SearchPlanner
from future_agent.domain.events import AnalysisSession, EventDraft, now
from future_agent.domain.evidence import EvidenceImport
from future_agent.domain.research import SearchCandidate, SearchReply
from future_agent.storage.snapshots import FileSnapshotStore


class SearchFixture:
    def __init__(self, error=None):
        self.calls = []
        self.error = error

    def search(self, query, timeout):
        self.calls.append(query)
        if self.error:
            raise self.error
        return SearchReply(
            candidates=[
                SearchCandidate(url="https://x.org/a", snippet="not body"),
                SearchCandidate(url="https://other.org/b", snippet="same text different source"),
            ]
        )


class PageFixture:
    def __init__(self, error=None):
        self.calls = []
        self.error = error

    def read(self, url, timeout):
        self.calls.append(url)
        if self.error:
            raise self.error
        return EvidenceImport(
            url=url,
            raw_text="同一份正文😀\r\n发布记录",
            access_status="read_success",
            retrieved_at=now(),
            origin="fixture",
        )


@pytest.fixture
def setup(repo, tmp_path):
    events = EventService(repo)
    session = events.create("私人原文：不要发送此叙述", max_run_seconds=100)
    events.edit(session.id, 0, EventDraft(question=complete_question()))
    session = events.confirm(session.id, 1)
    evidence = EvidenceService(repo, repo, FileSnapshotStore(tmp_path))
    search, pages = SearchFixture(), PageFixture()
    return session.id, ResearchService(repo, evidence, search, pages), evidence, search, pages


def test_complete_flow_sources_dedup_citations_and_versions(setup, repo):
    sid, service, evidence, search, pages = setup
    run = service.start(sid, 2, "ExampleProject", "stable release", fixture=True)
    assert run.status == "completed" and len(run.candidate_ids) == 4
    assert len(run.document_ids) == 2 and len(pages.calls) == 2
    assert {q.purpose for q in search.calls} == {"current_state", "counterevidence"}
    assert all("私人" not in q.query for q in search.calls)
    assert evidence.snapshot(run.document_ids[0]).normalized == "同一份正文😀\n发布记录"
    assert sum(r.eligible for r in evidence.select(sid, 2, now(), experiment=False)) == 1
    assert not any(r.eligible for r in evidence.select(sid, 2, now()))
    assert repo.load(sid).consumed_seconds > 0
    assert repo.load(sid).attempts == []
    EventService(repo).edit(sid, 2, EventDraft(question=complete_question()))
    assert service.runs(sid)[0].plan.event_revision_id == repo.revision(sid, 2).id
    with pytest.raises(Conflict):
        service.start(sid, 2, "Example", "release")


def test_no_search_config_does_not_create_fake_run(setup):
    sid, service, _, search, _ = setup
    service.search = None
    with pytest.raises(AppError, match="evidence import"):
        service.start(sid, 2, "Example", "release")
    assert service.runs(sid) == [] and search.calls == []


def test_unavailable_search_has_failure_not_empty_success(setup):
    sid, service, evidence, search, pages = setup
    search.error = NetworkError("http_error")
    run = service.start(sid, 2, "Example", "release", fixture=True)
    assert run.status == "failed" and run.document_ids == []
    assert len(run.attempts) == 2 and all(a.status == "failed" for a in run.attempts)
    assert not pages.calls and not evidence.list_documents(sid, 2)


def test_search_empty_differs_from_service_failure(setup):
    sid, service, _, _, _ = setup
    service.search.search = lambda q, t: SearchReply(candidates=[])
    run = service.start(sid, 2, "Example", "release")
    assert run.status == "partial" and "no_candidates_current_state" in run.gaps
    assert all(a.status == "succeeded" for a in run.attempts)


def test_page_failure_preserves_candidates_and_standard_reason(setup):
    sid, service, evidence, _, pages = setup
    pages.error = NetworkError("unsafe_url")
    run = service.start(sid, 2, "Example", "release", fixture=True)
    assert run.status == "partial" and "page_unsafe_url" in run.gaps
    assert len(run.candidate_ids) == 4
    assert all(
        evidence.document(i).source.access_status == "fetch_failed" for i in run.document_ids
    )
    assert not any(r.eligible for r in evidence.select(sid, 2, now(), experiment=False))


def test_retries_consume_durable_search_quota_and_shared_time(setup, repo):
    sid, service, _, search, _ = setup
    session = repo.load(sid)
    repo.save(
        session.model_copy(update={"max_search_queries": 2, "consumed_seconds": 99}),
        session.version,
    )
    search.error = NetworkError("temporary", retry_after=0)
    run = service.start(sid, 2, "Example", "release", fixture=True)
    assert run.status == "partial" and "budget_exhausted" in run.gaps
    assert len(search.calls) == 2
    second = service.start(sid, 2, "Example", "release", fixture=True)
    assert second.status == "partial" and not second.attempts
    assert len(search.calls) == 2 and repo.load(sid).consumed_seconds >= 99


@pytest.mark.parametrize("kind", ["search", "page"])
@pytest.mark.parametrize("failure", ["timeout", "late_success", "always_timeout"])
def test_per_call_timeout_retries_with_remaining_budget(setup, repo, monkeypatch, kind, failure):
    import future_agent.application.research as module

    sid, service, evidence, search, pages = setup
    session = repo.load(sid)
    repo.save(session.model_copy(update={"max_run_seconds": 600}), session.version)
    clock = [0.0]
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    service.sleep = lambda seconds: clock.__setitem__(0, clock[0] + seconds)
    provider, name = (search, "search") if kind == "search" else (pages, "read")
    original = getattr(provider, name)
    calls = []

    def operation(target, timeout):
        calls.append(timeout)
        if len(calls) == 1 or failure == "always_timeout":
            clock[0] += timeout + 0.01
            if failure != "late_success":
                raise NetworkError("timeout")
        return original(target, timeout)

    monkeypatch.setattr(provider, name, operation)
    run = service.start(sid, 2, "Example", "release", fixture=True)
    attempts = [a for a in run.attempts if a.kind == kind]
    assert "budget_exhausted" not in run.gaps
    assert attempts[0].error_code == "timeout" and attempts[0].elapsed_seconds > 30
    if failure == "always_timeout":
        assert len(calls) == 6  # Three attempts for each query or page, then stop.
        assert all(a.status == "failed" and a.error_code == "timeout" for a in attempts)
        assert run.status == ("failed" if kind == "search" else "partial")
        assert not any(
            evidence.document(i).source.access_status == "read_success" for i in run.document_ids
        )
    else:
        assert len(calls) == 3
        assert run.status == "completed" and all(a.status == "succeeded" for a in attempts[1:])
    assert repo.load(sid).consumed_seconds == pytest.approx(clock[0])


@pytest.mark.parametrize("late_success", [False, True])
def test_request_exhausting_total_time_stops_without_retry(setup, repo, monkeypatch, late_success):
    import future_agent.application.research as module

    sid, service, evidence, search, _ = setup
    session = repo.load(sid)
    repo.save(session.model_copy(update={"consumed_seconds": 80}), session.version)
    clock = [0.0]
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    calls = []

    def operation(query, timeout):
        calls.append(timeout)
        clock[0] += timeout + 0.01
        if late_success:
            return SearchReply(candidates=[SearchCandidate(url="https://x.org/late")])
        raise NetworkError("timeout")

    monkeypatch.setattr(search, "search", operation)
    run = service.start(sid, 2, "Example", "release", fixture=True)
    assert calls == [20]
    assert run.status == "partial" and run.gaps == ["budget_exhausted"]
    assert run.attempts[0].error_code == "budget_exhausted"
    assert not evidence.list_documents(sid, 2)
    assert repo.load(sid).consumed_seconds == pytest.approx(100.01)


def test_malformed_html_preserves_failure_and_continues_other_pages(setup, repo):
    import httpx

    from future_agent.providers.web import StaticPageReader

    sid, service, evidence, _, _ = setup

    async def dns(host, port):
        return ["8.8.8.8"]

    def response(request):
        body = b"<![foo]><main>body</main>" if request.url.path == "/a" else b"<main>valid</main>"
        return httpx.Response(200, headers={"Content-Type": "text/html"}, content=body)

    service.pages = StaticPageReader(resolver=dns, transport=httpx.MockTransport(response))
    run = service.start(sid, 2, "Example", "release", fixture=True)
    assert run.status == "partial" and run.gaps == ["page_unsupported"]
    assert len(run.candidate_ids) == 4 and len(run.document_ids) == 2
    failed, succeeded = [evidence.document(i) for i in run.document_ids]
    assert (
        failed.source.access_status == "fetch_failed"
        and failed.source.failure_code == "unsupported"
    )
    assert failed.snapshot_id is None
    assert evidence.snapshot(succeeded.id).normalized == "valid"
    assert [a.status for a in run.attempts if a.kind == "page"] == ["failed", "succeeded"]
    assert repo.load(sid).research_runs[-1] == run
    assert EventService(repo).answer(sid, 2, "补充资料").current_revision == 3


def test_model_read_cannot_reset_time_exhausted_by_research(setup, repo):
    from conftest import FixtureProvider

    sid, _, _, _, _ = setup
    session = repo.load(sid)
    repo.save(
        session.model_copy(update={"consumed_seconds": session.max_run_seconds}), session.version
    )
    provider = FixtureProvider([])
    with pytest.raises(AppError, match="预算"):
        EventService(repo, provider).read(sid, force=True)
    assert provider.users == []


def test_keyboard_interrupt_preserves_unknown_attempt_and_reservation(setup, repo):
    sid, service, _, search, _ = setup

    def interrupt(query, timeout):
        raise KeyboardInterrupt

    search.search = interrupt
    with pytest.raises(KeyboardInterrupt):
        service.start(sid, 2, "Example", "release", fixture=True)
    run = repo.load(sid).research_runs[-1]
    assert run.status == "cancelled"
    assert run.attempts[-1].error_code == "interrupted_request_outcome_unknown"
    assert repo.load(sid).consumed_seconds >= 30


def test_page_quota_preserves_unread_candidates(setup, repo):
    sid, service, evidence, _, pages = setup
    session = repo.load(sid)
    repo.save(session.model_copy(update={"max_pages": 1}), session.version)
    run = service.start(sid, 2, "Example", "release", fixture=True)
    assert run.status == "partial" and "budget_exhausted" in run.gaps
    assert len(pages.calls) == 1 and len(run.candidate_ids) == 4
    assert len(evidence.list_documents(sid, 2)) == 5


def test_cancel_during_request_blocks_result_and_further_calls(setup, repo):
    sid, service, evidence, search, _ = setup

    def cancel(query, timeout):
        run = service.runs(sid)[-1]
        with pytest.raises(Conflict):
            EventService(repo).edit(sid, 2, EventDraft())
        service.cancel(sid, run.id)
        return SearchReply(candidates=[SearchCandidate(url="https://x.org")])

    search.search = cancel
    run = service.start(sid, 2, "Example", "release", fixture=True)
    assert run.status == "cancelled" and len(run.attempts) == 1
    assert not evidence.list_documents(sid, 2)


def test_interrupt_reservation_persists_and_abandon_is_explicit(setup, repo):
    sid, service, _, search, _ = setup

    def interrupt(query, timeout):
        raise SystemExit("synthetic process interruption")

    search.search = interrupt
    with pytest.raises(SystemExit):
        service.start(sid, 2, "Example", "release", fixture=True)
    saved = repo.load(sid)
    assert saved.consumed_seconds >= 30
    assert saved.research_runs[-1].attempts[-1].status == "started"
    with pytest.raises(Conflict):
        service.start(sid, 2, "Example", "release")
    service.cancel(sid, saved.research_runs[-1].id, abandon=True)
    assert repo.load(sid).research_runs[-1].status == "cancelled"
    assert repo.load(sid).consumed_seconds >= 30


def test_legacy_session_upgrades_without_resetting_model_budget(setup, repo):
    sid, _, _, _, _ = setup
    data = repo.load(sid).model_dump(mode="json")
    data["schema_version"] = "1.0"
    for field in ("max_search_queries", "max_pages", "research_runs"):
        data.pop(field)
    data["consumed_seconds"] = 42
    data["consumed_tokens"] = 12345
    restored = AnalysisSession.model_validate(data)
    assert restored.schema_version == "1.3" and restored.research_runs == []
    assert restored.consumed_seconds == 42 and restored.consumed_tokens == 12345


def test_planner_rejects_unconfirmed_or_missing_target():
    from future_agent.domain.events import EventRevision

    event = EventRevision(session_id=uuid4(), revision=1, origin="manual")
    with pytest.raises(AppError):
        SearchPlanner().plan(event, "Example", "release")


def test_real_adapters_flow_through_snapshots(setup):
    import httpx

    from future_agent.config import Settings
    from future_agent.providers.searxng import SearXNGProvider
    from future_agent.providers.web import StaticPageReader

    sid, service, evidence, _, _ = setup
    service.search = SearXNGProvider(
        Settings(search_base_url="http://127.0.0.1:8080"),
        transport=httpx.MockTransport(
            lambda r: httpx.Response(
                200,
                json={
                    "results": [
                        {"url": "https://public.example/releases", "content": "candidate not read"},
                    ]
                },
            )
        ),
    )

    async def dns(host, port):
        return ["8.8.8.8"]

    service.pages = StaticPageReader(
        resolver=dns,
        transport=httpx.MockTransport(
            lambda r: httpx.Response(
                200,
                headers={"Content-Type": "text/html"},
                content="<main>真实内部提取与存储😀</main>".encode(),
            )
        ),
    )
    run = service.start(sid, 2, "Example", "release", fixture=True)
    assert run.status == "completed" and len(run.document_ids) == 1
    assert evidence.snapshot(run.document_ids[0]).normalized == "真实内部提取与存储😀"


def test_gzip_success_and_corruption_keep_research_history(setup):
    import gzip

    import httpx

    from future_agent.providers.web import StaticPageReader

    sid, service, evidence, _, _ = setup

    async def dns(host, port):
        return ["8.8.8.8"]

    def handler(request):
        compressed = gzip.compress("<main>正式发布记录😀</main>".encode())
        if request.headers["host"] == "x.org":
            compressed = compressed[:-1]
        return httpx.Response(
            200,
            headers={"Content-Type": "text/html", "Content-Encoding": "gzip"},
            stream=httpx.ByteStream(compressed),
        )

    service.pages = StaticPageReader(resolver=dns, transport=httpx.MockTransport(handler))
    run = service.start(sid, 2, "Example", "release", fixture=True)
    assert run.status == "partial" and "page_invalid_response" in run.gaps
    assert len(run.candidate_ids) == 4 and len(run.document_ids) == 2
    failed, success = [evidence.document(doc) for doc in run.document_ids]
    assert success.source.access_status == "read_success"
    assert evidence.snapshot(success.id).normalized == "正式发布记录😀"
    assert failed.source.access_status == "fetch_failed" and failed.snapshot_id is None
    assert service.runs(sid)[-1].status == "partial"


def test_research_cli_cross_process_and_private_page_blocked(tmp_path):
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from test_cli import checked, run_cli

    class Handler(BaseHTTPRequestHandler):
        calls = 0

        def do_GET(self):
            Handler.calls += 1
            payload = json.dumps(
                {"results": [{"url": "http://127.0.0.1/private", "content": "candidate only"}]}
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    args = ("--no-env", "--data-dir", str(tmp_path / "data"))
    environment = {"SEARCH_BASE_URL": f"http://127.0.0.1:{server.server_port}"}
    try:
        narrative = tmp_path / "narrative.txt"
        narrative.write_text("保密，不应出现在查询", encoding="utf-8")
        sid = checked(*args, "create", "--input", str(narrative))["id"]
        draft = tmp_path / "draft.json"
        draft.write_text(
            EventDraft(question=complete_question()).model_dump_json(), encoding="utf-8"
        )
        checked(*args, "edit", sid, "--expected-revision", "0", "--input", str(draft))
        checked(*args, "confirm", sid, "--expected-revision", "1")
        options = (sid, "--revision", "2", "--subject", "PublicProject", "--focus", "release")
        plan = checked(*args, "research", "plan", *options)
        assert Handler.calls == 0 and "保密" not in str(plan)
        response = run_cli(*args, "research", "run", *options, extra_env=environment)
        assert response.returncode == 2, response.stderr
        run = json.loads(response.stdout)
        assert run["status"] == "partial" and "page_unsafe_url" in run["gaps"]
        assert Handler.calls == 2
        saved = checked(*args, "research", "show", sid)
        assert saved[0]["id"] == run["id"] and len(saved[0]["candidate_ids"]) == 2
        text = run_cli(*args, "evidence", "text", run["document_ids"][0])
        assert text.returncode == 2 and "没有已读正文" in text.stderr
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
