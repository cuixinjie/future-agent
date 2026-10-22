"""Offline imports exercise real domain, files, SQLite and CLI boundaries."""

from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from conftest import make_draft
from pydantic import ValidationError
from sqlalchemy import text

from future_agent.application.events import EventService
from future_agent.application.evidence import EvidenceService
from future_agent.application.ports import AppError, Conflict
from future_agent.domain.events import now
from future_agent.domain.evidence import EvidenceImport, EvidenceSpan, Snapshot, canonical_url
from future_agent.storage.snapshots import FileSnapshotStore


@pytest.fixture
def setup(repo, tmp_path):
    events = EventService(repo)
    session = events.create("我没有发布稳定版。\r\n我准备收费。最近增长很快。😀我觉得还需观察。")
    events.edit(session.id, 0, make_draft(session.sources[0]), origin="fixture")
    return EvidenceService(repo, repo, FileSnapshotStore(tmp_path)), session.id


def material(**changes):
    return EvidenceImport.model_validate(
        {
            "url": "https://EXAMPLE.org:443/releases#v1",
            "publisher": "离线网页夹具",
            "access_status": "read_success",
            "raw_text": "发布记录\r\n😀稳定版已发布。\r保留原文。",
            "retrieved_at": now() - timedelta(hours=1),
            "origin": "fixture",
            **changes,
        }
    )


def test_snapshots_sources_citations_and_dedup(setup, repo, tmp_path):
    service, sid = setup
    first = service.import_document(sid, 1, material())
    second = service.import_document(sid, 1, material(url="https://other.example/releases"))
    third = service.import_document(
        sid, 1, material(raw_text=material().raw_text.replace("\r\n", "\n").replace("\r", "\n"))
    )
    assert first.canonical_url == "https://example.org/releases"
    assert first.snapshot_id == second.snapshot_id
    assert third.snapshot_id != first.snapshot_id and third.content_hash == first.content_hash
    assert len(list((tmp_path / "evidence/text").iterdir())) == 1
    assert len(service.list_documents(sid, 1)) == 3
    snapshot = service.snapshot(first.id)
    assert snapshot.raw == material().raw_text
    quote = "😀稳定版已发布。"
    start = snapshot.normalized.index(quote)
    claim = repo.revision(sid, 1).claims[0].id
    span = service.cite(
        sid,
        1,
        EvidenceSpan(
            document_id=first.id,
            quote=quote,
            start=start,
            end=start + len(quote),
            claim_ids=[claim],
        ),
    )
    assert repo.spans(first.id) == [span]
    assert repo.document(first.id) == first
    selected = service.select(sid, 1, now(), experiment=False)
    assert sum(item.eligible for item in selected) == 1
    assert sum(item.reasons == ["duplicate_content"] for item in selected) == 2
    with pytest.raises(Conflict):
        repo.save_document(first)
    with pytest.raises(Conflict):
        service.cite(sid, 1, span)
    with pytest.raises(ValueError):
        service.cite(sid, 1, span.model_copy(update={"id": uuid4(), "start": start + 1}))
    with pytest.raises(AppError, match="不存在的陈述"):
        service.cite(sid, 1, span.model_copy(update={"claim_ids": [uuid4()]}))


def test_failed_candidate_unknown_and_cutoff(setup):
    service, sid = setup
    documents = [
        service.import_document(sid, 1, value)
        for value in (
            material(access_status="candidate_only", raw_text=None, snippet="搜索摘要"),
            material(access_status="fetch_failed", raw_text=None, failure_code="timeout"),
            material(retrieved_at=None, published_at=now() - timedelta(days=365)),
            material(origin="manual", published_at=now() - timedelta(days=365)),
        )
    ]
    for doc in documents[:2]:
        with pytest.raises(AppError, match="没有已读正文"):
            service.snapshot(doc.id)
    results = {result.document_id: result for result in service.select(sid, 1, now())}
    assert "candidate_only" in results[documents[0].id].reasons
    assert "fetch_failed" in results[documents[1].id].reasons
    assert "acquisition_time_unknown" in results[documents[2].id].reasons
    last = documents[-1]
    assert results[last.id].eligible
    boundary = {x.document_id: x for x in service.select(sid, 1, last.imported_at)}
    assert boundary[last.id].eligible
    early = {
        x.document_id: x
        for x in service.select(sid, 1, last.imported_at - timedelta(microseconds=1))
    }
    assert "imported_after_cutoff" in early[last.id].reasons
    old = service.select(sid, 1, now() - timedelta(days=1))
    assert all(not item.eligible for item in old)
    assert "fixture_excluded_from_experiment" in results[documents[0].id].reasons


def test_corruption_and_cross_revision_citations_rejected(setup, repo, tmp_path):
    service, sid = setup
    doc = service.import_document(sid, 1, material())
    event = EventService(repo).create("另一事件")
    from future_agent.domain.events import EventDraft

    EventService(repo).edit(event.id, 0, EventDraft())
    # An event from another session cannot consume this document.
    span = EvidenceSpan(document_id=doc.id, quote="发布记录", start=0, end=4)
    with pytest.raises(AppError, match="事件版本"):
        service.cite(event.id, 1, span)
    path = tmp_path / "evidence" / "text" / f"{doc.content_hash}.txt"
    path.write_text("tampered", encoding="utf-8")
    with pytest.raises(ValueError):
        service.select(sid, 1, now(), experiment=False)
    with pytest.raises(AppError, match="拒绝覆盖"):
        service.import_document(sid, 1, material())
    assert len(service.list_documents(sid, 1)) == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"raw_text": None},
        {"access_status": "candidate_only"},
        {"access_status": "fetch_failed", "raw_text": None},
        {"retrieved_at": "2020-01-01"},
        {"retrieved_at": "2020-01-01T00:00:00Z", "published_at": "2021-01-01T00:00:00Z"},
        {"url": "file:///etc/passwd"},
        {"url": "https://user:secret@example.org"},
    ],
)
def test_invalid_imports(changes):
    with pytest.raises(ValueError):
        material(**changes)


def test_future_acquisition_rejected_without_files(setup, tmp_path):
    service, sid = setup
    with pytest.raises(AppError, match="未来"):
        service.import_document(sid, 1, material(retrieved_at=now() + timedelta(days=1)))
    assert not (tmp_path / "evidence").exists()


def test_url_normalization_keeps_semantic_query():
    assert canonical_url("HTTPS://ExAmple.org:443?a=2&a=1#x") == "https://example.org/?a=2&a=1"
    assert canonical_url("http://[::1]:80/x") == "http://[::1]/x"
    assert canonical_url("http://example.org:0/x") == "http://example.org:0/x"


def test_fixed_webpage_cli_roundtrip(tmp_path):
    import json

    from test_cli import checked, run_cli

    from future_agent.domain.events import EventDraft

    args = ("--no-env", "--data-dir", str(tmp_path / "data"))
    narrative = tmp_path / "input.txt"
    narrative.write_text("测试资料导入", encoding="utf-8")
    session = checked(*args, "create", "--input", str(narrative))
    draft = tmp_path / "draft.json"
    draft.write_text(EventDraft().model_dump_json(), encoding="utf-8")
    checked(*args, "edit", session["id"], "--expected-revision", "0", "--input", str(draft))
    fixture = Path(__file__).parents[1] / "fixtures/evidence/webpage.fixture.json"
    doc = checked(
        *args, "evidence", "import", session["id"], "--revision", "1", "--input", str(fixture)
    )
    assert doc["source"]["origin"] == "fixture"
    assert checked(*args, "evidence", "show", doc["id"]) == doc
    snapshot = checked(*args, "evidence", "text", doc["id"])
    assert "\r\n" in snapshot["raw"] and "\r" not in snapshot["normalized"]
    quote = "😀稳定版 v1.0 已公开发布。"
    start = snapshot["normalized"].index(quote)
    citation = tmp_path / "span.json"
    citation.write_text(
        EvidenceSpan(
            document_id=doc["id"], quote=quote, start=start, end=start + len(quote)
        ).model_dump_json(),
        encoding="utf-8",
    )
    span = checked(
        *args, "evidence", "cite", session["id"], "--revision", "1", "--input", str(citation)
    )
    assert checked(*args, "evidence", "spans", doc["id"]) == [span]
    assert len(checked(*args, "evidence", "list", session["id"], "--revision", "1")) == 1
    cutoff = now().isoformat()
    selection_args = (
        *args,
        "evidence",
        "select",
        session["id"],
        "--revision",
        "1",
        "--cutoff",
        cutoff,
    )
    assert not checked(*selection_args)[0]["eligible"]
    assert checked(*selection_args, "--allow-fixtures")[0]["eligible"]
    rejected = run_cli(
        *args, "evidence", "select", session["id"], "--revision", "1", "--cutoff", "2020-01-01"
    )
    assert rejected.returncode == 2
    assert json.loads(fixture.read_text(encoding="utf-8"))["raw_text"] == snapshot["raw"]


def test_snapshot_schema_and_safe_paths(tmp_path):
    with pytest.raises(ValidationError):
        Snapshot(raw="原文", normalized="修改", content_hash="bad")
    store = FileSnapshotStore(tmp_path)
    with pytest.raises(AppError):
        store.read(uuid4(), "../outside")


def test_import_time_is_recorded_after_snapshot_is_available(setup, monkeypatch):
    import future_agent.application.evidence as module

    service, sid = setup
    started = now()
    completed = started + timedelta(seconds=10)
    current = [started]
    monkeypatch.setattr(module, "now", lambda: current[0])
    original_put = service.snapshots.put

    def slow_put(snapshot):
        result = original_put(snapshot)
        current[0] = completed
        return result

    monkeypatch.setattr(service.snapshots, "put", slow_put)
    document = service.import_document(sid, 1, material(origin="manual"))
    assert document.imported_at == completed
    result = service.select(sid, 1, started + timedelta(seconds=5))[0]
    assert not result.eligible and "imported_after_cutoff" in result.reasons


def test_failed_snapshot_write_can_be_retried(setup, tmp_path, monkeypatch):
    import future_agent.storage.snapshots as module

    service, sid = setup
    real_fsync = module.os.fsync

    def failed_flush(fd):
        raise OSError("simulated disk failure")

    monkeypatch.setattr(module.os, "fsync", failed_flush)
    with pytest.raises(OSError, match="simulated disk failure"):
        service.import_document(sid, 1, material())
    assert service.list_documents(sid, 1) == []
    assert not list((tmp_path / "evidence").rglob("*.txt"))
    assert not list((tmp_path / "evidence").rglob("*.tmp"))
    monkeypatch.setattr(module.os, "fsync", real_fsync)
    document = service.import_document(sid, 1, material())
    assert service.snapshot(document.id).raw == material().raw_text


def test_concurrent_snapshot_writers_publish_complete_content(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    store = FileSnapshotStore(tmp_path)
    snapshot = Snapshot.from_raw("完整正文😀\r\n" * 20000)
    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(pool.map(lambda _: store.put(snapshot), range(2)))
    assert ids[0] == ids[1]
    assert store.read(ids[0], snapshot.content_hash) == snapshot
    assert not list((tmp_path / "evidence").rglob("*.tmp"))


def test_duplicate_representative_does_not_change_when_new_source_is_added(setup, repo):
    service, sid = setup
    first = service.import_document(sid, 1, material())
    second = service.import_document(sid, 1, material())
    # IDs can sort before older imports; ordering must use acquisition into this store.
    earlier_uuid = second.model_copy(update={"id": type(second.id)(int=1)})
    repo.save_document(earlier_uuid)
    results = service.select(sid, 1, now(), experiment=False)
    assert [item.document_id for item in results if item.eligible] == [first.id]


def test_reading_saved_citations_checks_snapshot_integrity(setup, tmp_path):
    service, sid = setup
    document = service.import_document(sid, 1, material())
    span = service.cite(
        sid,
        1,
        EvidenceSpan(
            document_id=document.id,
            quote="发布记录",
            start=0,
            end=4,
        ),
    )
    assert service.spans(document.id) == [span]
    (tmp_path / "evidence/text" / f"{document.content_hash}.txt").write_text(
        "损坏", encoding="utf-8"
    )
    with pytest.raises(ValueError):
        service.spans(document.id)


def test_upgrade_preserves_m1_and_refuses_downgrade(tmp_path):
    from alembic import command
    from alembic.config import Config

    import future_agent.storage.sqlite as module
    from future_agent.storage.sqlite import SQLiteRepository

    repo = SQLiteRepository(tmp_path / "upgrade.db")
    config = Config()
    config.set_main_option("script_location", str(Path(module.__file__).parent / "migrations"))
    try:
        with repo.engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "0001_m1")
        original = EventService(repo).create("保留已有数据")
        repo.migrate()
        assert repo.load(original.id) == original
        repo.migrate()
        with repo.engine.begin() as connection:
            assert (
                connection.scalar(text("select version_num from alembic_version"))
                == "0005_lifecycle"
            )
            config.attributes["connection"] = connection
            with pytest.raises(RuntimeError, match="不可无损回退"):
                command.downgrade(config, "0001_m1")
        assert repo.load(original.id) == original
    finally:
        repo.close()
