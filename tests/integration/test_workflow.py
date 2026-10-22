"""Real domain and SQLite workflows, with explicitly substituted external model I/O."""

import json
from pathlib import Path

import pytest
from conftest import NARRATIVE, FixtureProvider, complete_question, make_draft

from future_agent.application.events import EventService
from future_agent.application.ports import AppError, Conflict, ModelError
from future_agent.storage.sqlite import SQLiteRepository


def test_read_clarify_edit_confirm_and_restart(repo):
    service = EventService(repo)
    session = service.create(NARRATIVE, request_key="one")
    draft = make_draft(session.sources[0])
    provider = FixtureProvider([draft.model_dump_json()])
    service = EventService(repo, provider)
    read = service.read(session.id)
    assert read.status == "NEEDS_INPUT"
    assert len(read.pending_questions) <= 3
    assert read.attempts[0].input_tokens == 100
    first = repo.revision(session.id, 1)
    assert first.model_id == provider.model_id and first.prompt_version == "reader-1.0"
    with pytest.raises(ValueError):
        service.confirm(session.id, 1)
    answer = service.answer(session.id, 1, "留存率我不知道", [first.unknowns[0].id])
    assert len(answer.sources) == 2
    assert repo.revision(session.id, 2).unknowns[0].resolution_status == "kept_unknown"
    edited = draft.model_copy(update={"question": complete_question()})
    service.edit(session.id, 2, edited)
    confirmed = service.confirm(session.id, 3)
    assert confirmed.status == "STRUCTURED" and confirmed.current_revision == 4
    assert (
        repo.revision(session.id, 4).question.event_revision_id == repo.revision(session.id, 4).id
    )
    assert not repo.revision(session.id, 1).confirmed
    assert repo.revision(session.id, 4).semantic_review == "user_confirmed"
    with pytest.raises(Conflict):
        service.edit(session.id, 3, edited)
    reopened = SQLiteRepository(Path(repo.engine.url.database))
    try:
        reopened.migrate()
        assert reopened.load(session.id).status == "STRUCTURED"
        assert len(reopened.history(session.id)) == 4
        assert reopened.load(session.id).sources[0].raw == NARRATIVE
    finally:
        reopened.close()


def test_idempotency_and_concurrent_save(repo):
    service = EventService(repo)
    first = service.create("输入", request_key="stable")
    assert service.create("输入", request_key="stable").id == first.id
    with pytest.raises(Conflict):
        service.create("不同输入", request_key="stable")
    repo.save(first, first.version)
    with pytest.raises(Conflict):
        repo.save(first, first.version)


def test_invalid_output_repairs_are_bounded_and_recorded(repo):
    service = EventService(repo, FixtureProvider(["{}", "bad json", "{}", "never used"]))
    session = service.create(NARRATIVE)
    with pytest.raises(AppError, match="两次修复"):
        service.read(session.id)
    saved = repo.load(session.id)
    assert saved.status == "FAILED" and saved.error_code == "invalid_output"
    assert saved.current_revision == 0 and len(saved.attempts) == 3
    assert all(attempt.error_code == "invalid_output" for attempt in saved.attempts)


def test_repair_uses_sources_without_echoing_invalid_output(repo):
    service = EventService(repo)
    session = service.create(NARRATIVE)
    draft = make_draft(session.sources[0])
    provider = FixtureProvider(["invalid-output-secret", draft.model_dump_json()])
    read = EventService(repo, provider).read(session.id)
    assert read.status == "NEEDS_INPUT" and len(read.attempts) == 2
    assert "invalid-output-secret" not in json.dumps(provider.users)


def test_transient_retry_after_and_repairs_share_budget(repo):
    provider = FixtureProvider([ModelError("rate_limited", retry_after=0), "bad json"])
    service = EventService(repo, provider, sleep=lambda _: None)
    session = service.create(NARRATIVE, max_calls=2)
    with pytest.raises(AppError, match="预算"):
        service.read(session.id)
    saved = repo.load(session.id)
    assert len(saved.attempts) == 2 and saved.error_code == "budget_exhausted"
    with pytest.raises(AppError, match="预算"):
        service.read(session.id)
    assert len(repo.load(session.id).attempts) == 2


@pytest.mark.parametrize("code", ["authentication", "invalid_request", "context_limit"])
def test_permanent_model_errors_never_retry(repo, code):
    provider = FixtureProvider([ModelError(code)])
    service = EventService(repo, provider)
    session = service.create(NARRATIVE)
    with pytest.raises(ModelError):
        service.read(session.id)
    assert len(repo.load(session.id).attempts) == 1
    assert repo.load(session.id).error_code == code


def test_missing_model_never_falls_back_to_fixture(repo):
    service = EventService(repo)
    session = service.create(NARRATIVE)
    with pytest.raises(AppError, match="未配置"):
        service.read(session.id)
    assert repo.load(session.id).status == "CREATED"
    assert not repo.history(session.id)


def test_interrupted_call_keeps_reservation_and_needs_explicit_retry(repo):
    service = EventService(repo, FixtureProvider([KeyboardInterrupt()]))
    session = service.create(NARRATIVE, max_calls=1)
    with pytest.raises(KeyboardInterrupt):
        service.read(session.id)
    interrupted = repo.load(session.id)
    assert interrupted.status == "READING" and interrupted.attempts[0].status == "started"
    assert interrupted.consumed_seconds > 0
    with pytest.raises(AppError, match="中断"):
        service.read(session.id)
    with pytest.raises(AppError, match="预算"):
        service.read(session.id, retry_interrupted=True)
    saved = repo.load(session.id)
    assert saved.attempts[0].error_code == "interrupted_cost_unknown"
    assert len(saved.attempts) == 1


def test_resume_does_not_repeat_existing_read_and_reread_has_all_answers(repo):
    service = EventService(repo)
    session = service.create(NARRATIVE)
    draft = make_draft(session.sources[0])
    provider = FixtureProvider([draft.model_dump_json(), draft.model_dump_json()])
    service = EventService(repo, provider)
    service.read(session.id)
    service.read(session.id)
    assert len(provider.users) == 1
    service.answer(session.id, 1, "我保留未知")
    service.read(session.id, force=True)
    assert len(provider.users[-1]["sources"]) == 2


def test_invalid_edit_does_not_persist_partial_revision(repo):
    service = EventService(repo)
    session = service.create(NARRATIVE)
    draft = make_draft(session.sources[0])
    draft.claims[0] = draft.claims[0].model_copy(update={"end": 999})
    with pytest.raises(ValueError):
        service.edit(session.id, 0, draft)
    assert repo.load(session.id).current_revision == 0
    assert repo.history(session.id) == []


def test_token_budget_blocks_call_and_persists_across_resume(repo):
    provider = FixtureProvider([])
    service = EventService(repo, provider)
    session = service.create(NARRATIVE, max_total_tokens=1)
    for _ in range(2):
        with pytest.raises(AppError, match="预算"):
            service.read(session.id)
    assert repo.load(session.id).attempts == []
    assert provider.users == []


def test_unknown_usage_reserves_tokens_instead_of_zero(repo):
    from future_agent.application.ports import ModelReply

    class NoUsageProvider(FixtureProvider):
        def generate(self, *args, **kwargs):
            return ModelReply(content=super().generate(*args, **kwargs).content)

    service = EventService(repo)
    session = service.create(NARRATIVE)
    draft = make_draft(session.sources[0])
    saved = EventService(repo, NoUsageProvider([draft.model_dump_json()])).read(session.id)
    assert saved.consumed_tokens > 0
    assert saved.attempts[0].input_tokens is None and saved.attempts[0].cost is None
    assert saved.consumed_tokens == saved.attempts[0].reserved_tokens


def test_late_model_output_not_accepted(repo):
    import time

    class SlowProvider(FixtureProvider):
        def generate(self, *args, **kwargs):
            time.sleep(0.03)
            return super().generate(*args, **kwargs)

    service = EventService(repo)
    session = service.create(NARRATIVE, max_run_seconds=0.01)
    draft = make_draft(session.sources[0])
    with pytest.raises(AppError, match="预算"):
        EventService(repo, SlowProvider([draft.model_dump_json()])).read(session.id)
    assert repo.load(session.id).error_code == "budget_exhausted"
    assert repo.history(session.id) == []


def test_transient_errors_have_only_two_retries(repo):
    provider = FixtureProvider([ModelError("temporary") for _ in range(4)])
    delays = []
    service = EventService(repo, provider, sleep=delays.append)
    session = service.create(NARRATIVE)
    with pytest.raises(ModelError):
        service.read(session.id)
    assert len(repo.load(session.id).attempts) == 3
    assert delays == [2, 4]


def test_retry_after_longer_than_budget_does_not_sleep(repo):
    provider = FixtureProvider([ModelError("rate_limited", retry_after=60)])
    delays = []
    service = EventService(repo, provider, sleep=delays.append)
    session = service.create(NARRATIVE, max_run_seconds=10)
    with pytest.raises(ModelError):
        service.read(session.id)
    assert delays == [] and len(repo.load(session.id).attempts) == 1
