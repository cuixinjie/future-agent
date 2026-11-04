"""Acquisition protocol and durable partial pages, without live network."""

import json

import pytest

from future_agent.application.ports import AppError
from future_agent.domain.reference import ReferenceTarget
from future_agent.providers import release_history
from future_agent.storage.reference import AcquisitionJournal


def target():
    return ReferenceTarget(
        repository="example/product",
        product="product",
        channel="stable",
        definition="github-stable-release-30d-1.0",
    )


def test_acquisition_records_exact_raw_and_remains_unreviewed(monkeypatch, tmp_path):
    raw = json.dumps(
        [dict(tag_name="v1.0", published_at="2025-01-01T00:00:00Z", draft=False, prerelease=False)]
    )

    async def get(url, timeout):
        assert url == "https://api.github.com/repos/example/product/releases?per_page=100&page=1"
        assert 0 < timeout <= 30
        return raw

    monkeypatch.setattr(release_history, "_github_json", get)
    journal = AcquisitionJournal(tmp_path)
    value = release_history.collect_releases(target(), "fixture", journal.record)
    assert value.materials[0].raw == raw
    assert value.history.records[0].version == "v1.0"
    assert value.review_origin == "unreviewed" and not value.history.complete
    assert json.loads(journal.path.read_text(encoding="utf-8"))["content_hash"]


@pytest.mark.parametrize(
    "raw", ["[]", '{"message":"rate limit"}', "invalid", '[{"tag_name":"v1", "published_at":null}]']
)
def test_acquisition_empty_unknown_and_error_are_not_reviewed_success(monkeypatch, tmp_path, raw):
    async def get(url, timeout):
        return raw

    monkeypatch.setattr(release_history, "_github_json", get)
    journal = AcquisitionJournal(tmp_path)
    if raw == "[]":
        assert (
            release_history.collect_releases(target(), "fixture", journal.record).history.complete
            is False
        )
    else:
        with pytest.raises((ValueError, AppError)):
            release_history.collect_releases(target(), "fixture", journal.record)
    # Even rejected payloads retain the actual source snapshot for audit.
    assert journal.path.read_text(encoding="utf-8")


def test_acquisition_rejects_non_repository_before_network():
    with pytest.raises(AppError, match="owner/repo"):
        release_history.collect_releases(
            target().model_copy(update={"repository": "../private/path"}), "fixture"
        )
