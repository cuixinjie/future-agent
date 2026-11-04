"""Source audits reject corrupt candidates even when a file claims human review."""

import json
from datetime import timedelta

import pytest
from test_reference import check, dataset

from future_agent.application.ports import Conflict
from future_agent.application.reference import ReferenceService
from future_agent.domain.contracts import fingerprint
from future_agent.domain.reference_data import ReleaseDataset
from future_agent.domain.reference_quality import audit_release_dataset
from future_agent.storage.reference import SQLiteHistoricalRepository
from future_agent.storage.snapshots import FileSnapshotStore


def corrupt(kind):
    data = dataset().model_dump(mode="json")
    rows = json.loads(data["materials"][0]["raw"])
    if kind == "timestamp":
        rows[0]["published_at"] = rows[1]["published_at"]
    elif kind == "id":
        rows[0]["id"] = rows[1]["id"]
    elif kind == "flags":
        rows[0]["draft"] = 1
    elif kind == "omitted":
        rows.append({**rows[0], "id": 999})
    elif kind == "missing_page":
        data["materials"][0]["url"] = data["materials"][0]["url"].replace("&page=1", "&page=2")
    elif kind == "host":
        data["materials"][0]["url"] = "https://example.com/history"
    data["materials"][0]["raw"] = json.dumps(rows)
    data["manifest_hash"] = fingerprint({k: v for k, v in data.items() if k != "manifest_hash"})
    return ReleaseDataset.model_validate(data)


@pytest.mark.parametrize("kind", ["timestamp", "id", "flags", "omitted", "missing_page", "host"])
def test_source_quality_is_checked_on_import_and_existing_database_read(repo, tmp_path, kind):
    value = corrupt(kind)
    assert audit_release_dataset(value).machine_status == "rejected"
    store = SQLiteHistoricalRepository(repo, FileSnapshotStore(tmp_path))
    service = ReferenceService(store)
    with pytest.raises(Conflict, match="不能绕过来源审计"):
        service.import_dataset(value)
    # Existing datasets imported by older code must not bypass the new query gate.
    store.import_dataset(value)
    result = check(service, value.target)
    assert result.status == "not_applicable" and result.frequency is None
    assert any("来源质量不合格" in reason for reason in result.excluded)


def test_valid_source_pass_retains_human_review_requirement():
    audit = audit_release_dataset(dataset())
    assert audit.machine_status == "passed"
    assert audit.human_review_required


def test_short_history_cannot_be_made_complete_by_boolean():
    data = dataset().model_dump(mode="json")
    data["history"]["coverage_start"] = (
        dataset().history.coverage_end - timedelta(days=30)
    ).isoformat()
    # Revalidate to canonical UTC before recomputing the complete manifest.
    data["history"]["coverage_start"] = data["history"]["coverage_start"].replace("+00:00", "Z")
    data["manifest_hash"] = fingerprint({k: v for k, v in data.items() if k != "manifest_hash"})
    value = ReleaseDataset.model_validate(data)
    assert "coverage:less_than_360_days" in audit_release_dataset(value).issues
