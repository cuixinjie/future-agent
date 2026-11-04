"""Real SQLite/snapshots and ordinary workflow; external calls are fixtures."""

import json
from datetime import timedelta
from uuid import uuid4

import pytest
from test_assistance import AssistantProvider, plan_data, setup

from future_agent.application.ports import Conflict
from future_agent.application.reference import ReferenceService, ReleaseDataset
from future_agent.domain.assistance import AnalysisPlan
from future_agent.domain.contracts import now
from future_agent.domain.evaluation import ReleaseHistory, ReleaseRecord
from future_agent.domain.evidence import Snapshot
from future_agent.domain.historical_questions import DiagnosticBundle
from future_agent.domain.lifecycle import fingerprint
from future_agent.domain.reference import ReferenceBinding, ReferenceEstimate, ReferenceTarget
from future_agent.forecasting.report import render_report
from future_agent.storage.reference import SQLiteHistoricalRepository
from future_agent.storage.snapshots import FileSnapshotStore


def dataset(*, complete=True, origin="human", records=None):
    anchor = now() - timedelta(seconds=5)
    records = (
        records
        if records is not None
        else [
            ReleaseRecord(
                version="v1.0",
                product_channel="stable",
                published_at=anchor - timedelta(days=1),
                quote="v1.0",
            )
        ]
    )
    records = [
        *records,
        ReleaseRecord(
            version="v0-preview",
            product_channel="stable",
            published_at=anchor - timedelta(days=361),
            prerelease=True,
            quote="v0-preview",
        ),
    ]
    value = ReleaseDataset.model_construct(
        id=uuid4(),
        version="fixture-1",
        target=ReferenceTarget(
            repository="example/product",
            product="product",
            channel="stable",
            definition="github-stable-release-30d-1.0",
        ),
        source_revision="fixture-not-live",
        license="fixture",
        selection_rule="fixed fixture",
        review_origin=origin,
        reviewer="fixture-reviewer",
        history=ReleaseHistory(
            document_id=uuid4(),
            coverage_start=anchor - timedelta(days=360),
            coverage_end=anchor,
            complete=complete,
            reviewer="fixture-reviewer",
            coverage_quote="v0-preview",
            records=records,
        ),
        materials=[],
        imported_at=now(),
        manifest_hash="pending",
    ).model_dump(mode="json")
    value["materials"] = [
        dict(
            schema_version="1.0",
            url="https://api.github.com/repos/example/product/releases?per_page=100&page=1",
            raw=json.dumps(
                [
                    dict(
                        id=i + 1,
                        tag_name=r.version,
                        published_at=r.published_at.isoformat(),
                        draft=r.draft,
                        prerelease=r.prerelease,
                    )
                    for i, r in enumerate(records)
                ]
            ),
            retrieved_at=value["imported_at"],
        )
    ]
    value["manifest_hash"] = fingerprint({k: v for k, v in value.items() if k != "manifest_hash"})
    return ReleaseDataset.model_validate(value)


def plan(target):
    return AnalysisPlan.model_validate(
        {**plan_data(), "reference_target": target.model_dump(mode="json")}
    )


def check(service, target, **changes):
    cutoff = now()
    return service.check(
        plan(target).model_copy(update=changes),
        cutoff,
        cutoff + timedelta(seconds=600),
        cutoff + timedelta(days=30, seconds=600),
    )


def test_import_is_immutable_actual_timestamp_and_snapshot_integrity(repo, tmp_path):
    store = SQLiteHistoricalRepository(repo, FileSnapshotStore(tmp_path))
    original = dataset()
    saved = store.import_dataset(original)
    assert saved.imported_at >= original.imported_at
    assert store.import_dataset(original) == saved
    assert store.datasets(saved.target) == [saved]
    changed = original.model_copy(update={"version": "changed"})
    with pytest.raises(Conflict, match="不可覆盖"):
        store.import_dataset(changed)
    snap = Snapshot.from_raw(saved.materials[0].raw)
    (tmp_path / "evidence" / "text" / f"{snap.content_hash}.txt").write_text(
        "corrupt", encoding="utf-8"
    )
    assert check(ReferenceService(store), saved.target).status == "failed"


@pytest.mark.parametrize(
    "complete,origin,expected",
    [
        (True, "human", "computed"),
        (False, "human", "not_applicable"),
        (True, "unreviewed", "not_applicable"),
        (True, "fixture", "not_applicable"),
    ],
)
def test_frequency_missing_and_review_gate(repo, tmp_path, complete, origin, expected):
    store = SQLiteHistoricalRepository(repo, FileSnapshotStore(tmp_path))
    saved = store.import_dataset(dataset(complete=complete, origin=origin))
    result = check(ReferenceService(store), saved.target)
    assert result.status == expected
    assert result.frequency == (1 / 12 if expected == "computed" else None)
    assert (result.bayesian_estimate is not None) == (expected == "computed")
    if result.frequency is not None:
        assert result.labels == [1] + [0] * 11


@pytest.mark.parametrize(
    "changes",
    [
        {"duration_days": 90},
        {"analysis_kind": "exploration"},
        {"window_start": now(), "window_end": now() + timedelta(days=30)},
    ],
)
def test_wrong_target_does_not_get_frequency(repo, tmp_path, changes):
    store = SQLiteHistoricalRepository(repo, FileSnapshotStore(tmp_path))
    saved = store.import_dataset(dataset())
    assert check(ReferenceService(store), saved.target, **changes).frequency is None


def test_late_import_and_delay_and_binding_tampering(repo, tmp_path):
    store = SQLiteHistoricalRepository(repo, FileSnapshotStore(tmp_path))
    saved = store.import_dataset(dataset())
    service = ReferenceService(store)
    cutoff = saved.imported_at - timedelta(microseconds=1)
    assert (
        service.check(
            plan(saved.target),
            cutoff,
            cutoff + timedelta(seconds=600),
            cutoff + timedelta(days=30, seconds=600),
        ).frequency
        is None
    )
    cutoff = now() + timedelta(hours=2)
    assert (
        service.check(
            plan(saved.target),
            cutoff,
            cutoff + timedelta(seconds=600),
            cutoff + timedelta(days=30, seconds=600),
        ).frequency
        is None
    )
    cutoff = now()
    binding = service.bind(
        uuid4(),
        plan(saved.target),
        cutoff,
        cutoff + timedelta(seconds=600),
        cutoff + timedelta(days=30, seconds=600),
    )
    assert ReferenceBinding.model_validate_json(binding.model_dump_json()) == binding
    data = binding.model_dump(mode="json")
    data["event_revision_id"] = str(uuid4())
    with pytest.raises(ValueError, match="哈希"):
        ReferenceBinding.model_validate(data)


@pytest.mark.parametrize("legacy", [None, "release-reference-1.0", "release-reference-1.1"])
@pytest.mark.parametrize("numeric", [False, True])
def test_ordinary_flow_binds_two_checks_and_freezes_without_granting_numeric(
    repo, tmp_path, monkeypatch, legacy, numeric
):
    store = SQLiteHistoricalRepository(repo, FileSnapshotStore(tmp_path))
    saved = store.import_dataset(dataset())

    class Provider(AssistantProvider):
        def generate(self, system, user, schema, timeout):
            data = json.loads(user.split("\n上次输出")[0])
            if data.get("prompt_version") in {
                "analysis-plan-reference-1.0",
                "analysis-plan-reference-1.1",
            }:
                data["prompt_version"] = "target-research-1.5"
                user = json.dumps(data)
            return super().generate(system, user, schema, timeout)

    provider = Provider(plan=plan(saved.target).model_dump(mode="json"), numeric=numeric)
    service, sid = setup(repo, tmp_path, provider)
    service.references = ReferenceService(store)
    use_reference_version(service.references, monkeypatch, legacy)
    result = service.execute(sid)
    assert service.state(sid).reference_preview.status == "computed"
    assert result.reference_binding.estimate.status == "computed"
    assert result.probabilities == ([0.7, 0.3] if numeric else [None, None])
    assert provider.calls[-1]["historical_reference"]["frequency"] == 1 / 12
    assert "materials" not in provider.calls[-1]["historical_reference"]
    assert "1/12" in render_report(result)
    assert result.method_version == (
        "judgment-reference-1.0" if legacy else "judgment-reference-1.1"
    )
    projection = provider.calls[-1]["historical_reference"]
    if legacy:
        assert "bayesian_estimate" not in projection
        assert "贝叶斯统计参照" not in render_report(result)
    else:
        assert projection["bayesian_estimate"]["predictive_probability"] == 1 / 7
        assert "(1+1)/(12+2) = 14.3%" in render_report(result)
        assert result.prompt_version == "forecast-reference-1.1"
    step = service.forecasts.step(service.state(sid).forecast_request_id)
    assert step.reference_binding == result.reference_binding
    # Publishing another dataset never changes completed/recovered inputs.
    newer = dataset().model_dump(mode="json")
    newer["version"] = "fixture-2"
    newer["manifest_hash"] = fingerprint({k: v for k, v in newer.items() if k != "manifest_hash"})
    store.import_dataset(ReleaseDataset.model_validate(newer))
    assert service.forecasts.resume(step.id) == result
    from future_agent.application.lifecycle import ResolutionService

    frozen = ResolutionService(repo, repo, service.evidence).freeze(result.id)
    assert frozen.input_hash == fingerprint(result.model_dump(mode="json"))
    from future_agent.application.report_export import ReportExportService
    from future_agent.domain.lifecycle import FrozenForecast
    from future_agent.storage.reports import FileReportStore

    original = frozen.model_dump(mode="json")
    assert FrozenForecast.model_validate(original).model_dump(mode="json") == original
    from future_agent.web.views import forecast_view

    assert forecast_view(result, frozen)["historical_reference"] == projection
    _, output = ReportExportService(
        service.forecasts,
        ResolutionService(repo, repo, service.evidence),
        FileReportStore(tmp_path),
    ).save(result.id)
    exported = json.loads(output.read_text(encoding="utf-8"))
    assert exported["historical_reference"]["estimate"] == projection
    if not legacy:
        from future_agent.domain.forecasts import Forecast

        changed = result.model_dump(mode="json")
        changed["method_version"] = "judgment-reference-1.0"
        with pytest.raises(ValueError, match="贝叶斯"):
            Forecast.model_validate(changed)


def test_diagnostic_labels_do_not_enter_projection_and_families_do_not_cross_split(repo, tmp_path):
    value = dict(
        source_commit="fixed-commit-fixture",
        license="fixture",
        attribution="fixture",
        selection_rule="fixed seed, fixture",
        review_origin="fixture",
        questions=[
            dict(
                source_key="q1",
                event_family="f1",
                split="development",
                question="Will it happen?",
                rule="official",
                rule_version="1",
                cutoff=now().isoformat(),
                window_end=(now() + timedelta(days=30)).isoformat(),
            )
        ],
        labels=[
            dict(source_key="q1", status="yes", known_at=now().isoformat(), evidence="SECRET-LABEL")
        ],
    )
    bundle = DiagnosticBundle.model_validate(value)
    store = SQLiteHistoricalRepository(repo, FileSnapshotStore(tmp_path))
    store.save_diagnostic(bundle)
    assert "SECRET-LABEL" not in json.dumps(
        store.diagnostic(bundle.id).prediction_inputs("development")
    )
    value["questions"].append({**value["questions"][0], "source_key": "q2", "split": "sealed_test"})
    value["labels"].append(dict(source_key="q2", status="unresolved"))
    with pytest.raises(ValueError, match="事件族"):
        DiagnosticBundle.model_validate(value)


@pytest.mark.parametrize("legacy", [None, "release-reference-1.0", "release-reference-1.1"])
def test_interrupted_forecast_resumes_pinned_reference_after_new_dataset(
    repo, tmp_path, monkeypatch, legacy
):
    store = SQLiteHistoricalRepository(repo, FileSnapshotStore(tmp_path))
    saved = store.import_dataset(dataset())

    class Provider(AssistantProvider):
        def generate(self, system, user, schema, timeout):
            data = json.loads(user.split("\n上次输出")[0])
            if data.get("prompt_version") in {
                "analysis-plan-reference-1.0",
                "analysis-plan-reference-1.1",
            }:
                data["prompt_version"] = "target-research-1.5"
                user = json.dumps(data)
            return super().generate(system, user, schema, timeout)

    provider = Provider(plan=plan(saved.target).model_dump(mode="json"), interrupt="forecast")
    service, sid = setup(repo, tmp_path, provider)
    service.references = ReferenceService(store)
    use_reference_version(service.references, monkeypatch, legacy)
    with pytest.raises(KeyboardInterrupt):
        service.execute(sid)
    step = service.forecasts.step(service.state(sid).forecast_request_id)
    newer = dataset().model_dump(mode="json")
    newer["version"] = "fixture-2"
    newer["manifest_hash"] = fingerprint({k: v for k, v in newer.items() if k != "manifest_hash"})
    store.import_dataset(ReleaseDataset.model_validate(newer))
    result = service.forecasts.resume(step.id, retry_interrupted=True)
    assert result.reference_binding == step.reference_binding
    assert result.reference_binding.estimate.dataset_id == saved.id
    assert any(a.error_code == "interrupted_cost_unknown" for a in repo.load(sid).attempts)


def use_reference_version(service, monkeypatch, legacy):
    """Construct old serialized references without adding a posterior on read."""
    if legacy is None:
        return
    current = service.check

    def old_check(*args, **kwargs):
        data = current(*args, **kwargs).model_dump(mode="json")
        data.pop("bayesian_estimate", None)
        data["method_version"] = legacy
        if legacy == "release-reference-1.0":
            data.pop("source_audit_version", None)
        result = ReferenceEstimate.model_validate(data)
        assert result.model_dump(mode="json") == data
        return result

    monkeypatch.setattr(service, "check", old_check)


def test_posterior_cannot_be_injected_into_missing_or_old_reference(repo, tmp_path):
    store = SQLiteHistoricalRepository(repo, FileSnapshotStore(tmp_path))
    saved = store.import_dataset(dataset())
    data = check(ReferenceService(store), saved.target).model_dump(mode="json")
    for version in ("release-reference-1.0", "release-reference-1.1"):
        with pytest.raises(ValueError, match="旧版"):
            ReferenceEstimate.model_validate({**data, "method_version": version})
    for status in ("not_applicable", "failed", "qualitative_only"):
        with pytest.raises(ValueError, match="缺失"):
            ReferenceEstimate.model_validate({**data, "status": status})
    with pytest.raises(ValueError, match="贝叶斯估计"):
        ReferenceEstimate.model_validate({**data, "bayesian_estimate": None})
    changed = {**data, "numerator": 2}
    with pytest.raises(ValueError, match="原始历史窗口"):
        ReferenceEstimate.model_validate(changed)
