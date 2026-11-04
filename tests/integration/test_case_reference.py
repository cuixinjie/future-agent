"""Real storage/workflow with clearly marked external fixtures; no live model claims."""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text
from test_assistance import AssistantProvider, plan_data, setup

from future_agent.application.ports import Conflict
from future_agent.application.reference import ReferenceService
from future_agent.domain.assistance import AnalysisPlan
from future_agent.domain.case_reference import (
    CaseDataset,
    CaseFact,
    CaseSource,
    CaseTarget,
    HistoricalCase,
    estimate_cases,
)
from future_agent.domain.contracts import fingerprint, now
from future_agent.domain.evidence import Snapshot
from future_agent.forecasting.report import render_report
from future_agent.storage.reference import SQLiteHistoricalRepository
from future_agent.storage.snapshots import FileSnapshotStore


def signed(value):
    value = dict(value)
    value["manifest_hash"] = fingerprint({k: v for k, v in value.items() if k != "manifest_hash"})
    return CaseDataset.model_validate(value)


def dataset(domain="politics"):
    sources, cases = {}, []
    edition_years = [2008, 2012, 2016] if domain == "politics" else [2014, 2018, 2022]
    for year in edition_years:
        facts = []
        for offset in (-4, 0):
            source_year = year + offset
            url = (
                f"https://www.archives.gov/electoral-college/{source_year}"
                if domain == "politics"
                else f"https://www.uefa.com/fixture-{source_year}"
            )
            quote = (
                f"President | Fixture {source_year} [{'R' if source_year % 8 == 0 else 'D'}]"
                if domain == "politics"
                else f"Fixture {source_year} 1-0 Other team (aet)"
            )
            raw = "OFFLINE FIXTURE, NOT A REAL SOURCE\n" + quote
            source = CaseSource(
                url=url,
                text=raw,
                content_hash=hashlib.sha256(raw.encode()).hexdigest(),
                retrieved_at=now() - timedelta(seconds=10),
                capture_provider="offline-fixture",
                attribution="offline fixture only",
            )
            sources[source.content_hash] = source
            facts.append(
                CaseFact(
                    source_hash=source.content_hash,
                    quote=quote,
                    available_by=datetime(source_year + 1, 1, 22, tzinfo=UTC),
                    availability_basis="fixture",
                )
            )
        target = CaseTarget(
            definition="us-election-party-retention-1.0"
            if domain == "politics"
            else "mens-world-cup-final-extra-time-1.0",
            edition=year,
        )
        cases.append(
            HistoricalCase(
                key=f"fixture-{year}",
                target=target,
                question=f"Fixture {year}?",
                rule="fixture rule",
                cutoff=datetime(year, 11, 1, tzinfo=UTC),
                window_end=datetime(year + 1, 1, 21, tzinfo=UTC),
                prior_facts=[facts[0]],
                outcome="no" if domain == "politics" else "yes",
                outcome_facts=[facts[1]],
                outcome_available_by=facts[1].available_by,
            )
        )
    draft = CaseDataset.model_construct(
        id=uuid4(),
        domain=domain,
        version="fixture-1",
        selection_rule="offline fixture",
        sources=list(sources.values()),
        cases=cases,
        imported_at=now(),
        manifest_hash="pending",
    )
    return signed(draft.model_dump(mode="json"))


def plan(target):
    year = target.edition
    value = {
        **plan_data(),
        "reference_target": target.model_dump(mode="json"),
        "question": "下一届是否符合已登记结果定义？",
        "queries": [],
        "window_start": (now() + timedelta(minutes=10)).isoformat(),
        "window_end": datetime(year + 1, 1, 21, tzinfo=UTC).isoformat(),
    }
    return AnalysisPlan.model_validate(value)


def check(service, target):
    p = plan(target)
    return service.check(p, now(), p.window_start, p.window_end)


@pytest.mark.parametrize("domain,edition", [("sports", 2030), ("politics", 2028)])
def test_immutable_import_actual_time_separate_labels_and_as_of(repo, tmp_path, domain, edition):
    store = SQLiteHistoricalRepository(repo, FileSnapshotStore(tmp_path))
    original = dataset(domain)
    saved = store.import_cases(original)
    assert saved.imported_at >= original.imported_at
    assert store.import_cases(original) == saved
    assert store.case_datasets(domain) == [saved]
    changed = original.model_dump(mode="json")
    changed["version"] = "other"
    with pytest.raises(Conflict, match="不可覆盖"):
        store.import_cases(signed(changed))
    changed["id"] = str(uuid4())
    changed["version"] = original.version
    with pytest.raises(Conflict, match="版本"):
        store.import_cases(signed(changed))
    with repo.engine.connect() as db:
        assert all(
            '"outcome"' not in p for p in db.scalars(text("select payload from historical_cases"))
        )
        assert len(db.execute(text("select * from historical_case_outcomes")).all()) == 3
    target = saved.cases[0].target.model_copy(update={"edition": edition})
    result = check(ReferenceService(store), target)
    assert result.status == "qualitative_only" and len(result.cases) == 3
    assert "frequency" not in result.model_dump()
    cutoff = saved.imported_at - timedelta(seconds=1)
    assert (
        estimate_cases(target, [saved], cutoff, cutoff, cutoff + timedelta(days=30)).status
        == "not_applicable"
    )
    current = saved.cases[1]
    replay = ReferenceService(store).replay_input(domain, current.key)
    assert "outcome" not in {k for k in replay if k != "historical_reference"}
    assert len(replay["historical_reference"]["cases"]) == 1
    assert replay["historical_reference"]["cases"][0]["key"] == saved.cases[0].key
    assert "OFFLINE FIXTURE" not in json.dumps(replay)


def test_bad_labels_quotes_hashes_and_late_prior_are_rejected():
    original = dataset().model_dump(mode="json")
    for field, value in [("outcome", "yes"), ("cutoff", "2004-01-01T00:00:00Z")]:
        changed = json.loads(json.dumps(original))
        changed["cases"][0][field] = value
        with pytest.raises(ValueError):
            signed(changed)
    original["cases"][0]["prior_facts"][0]["quote"] = "made up source quote"
    with pytest.raises(ValueError, match="原文"):
        signed(original)


def test_normal_time_label_requires_complete_goal_times():
    data = dataset("sports").model_dump(mode="json")
    raw = "Fixture 4:2\nGoals: (18' (28' (38' (59' (65' (69'"
    source = {
        **data["sources"][0],
        "text": raw,
        "content_hash": hashlib.sha256(raw.encode()).hexdigest(),
    }
    data["sources"].append(source)
    fact = {
        **data["cases"][0]["outcome_facts"][0],
        "quote": raw,
        "source_hash": source["content_hash"],
    }
    data["cases"][0].update(outcome="no", outcome_facts=[fact])
    assert signed(data).cases[0].outcome == "no"
    data["cases"][0]["outcome"] = "yes"
    with pytest.raises(ValueError, match="标签"):
        signed(data)


def test_corruption_is_failure_and_wrong_mode_is_not_applicable(repo, tmp_path):
    store = SQLiteHistoricalRepository(repo, FileSnapshotStore(tmp_path))
    saved = store.import_cases(dataset())
    service = ReferenceService(store)
    target = saved.cases[0].target.model_copy(update={"edition": 2028})
    p = plan(target).model_copy(update={"analysis_kind": "exploration"})
    assert service.check(p, now(), p.window_start, p.window_end).status == "not_applicable"
    snapshot = Snapshot.from_raw(saved.sources[0].text)
    (tmp_path / "evidence" / "text" / f"{snapshot.content_hash}.txt").write_text(
        "corrupt", encoding="utf-8"
    )
    assert check(service, target).status == "failed"


def test_replay_excludes_target_even_after_resolution_and_future_cases():
    data = dataset()
    target = data.cases[1].target
    cutoff = data.cases[1].outcome_available_by + timedelta(days=1)
    result = estimate_cases(
        target, [data], cutoff, cutoff, cutoff + timedelta(days=30), retrospective=True
    )
    assert [c.key for c in result.cases] == [data.cases[0].key]
    assert len(result.excluded) == 2
    cutoff = data.cases[0].cutoff
    empty = estimate_cases(
        data.cases[0].target,
        [data],
        cutoff,
        cutoff,
        cutoff + timedelta(days=30),
        retrospective=True,
    )
    assert empty.status == "not_applicable" and not empty.cases


def test_source_after_cutoff_and_raw_snapshot_damage_are_not_silently_usable(repo, tmp_path):
    data = dataset()
    target = data.cases[0].target.model_copy(update={"edition": 2028})
    changed = data.model_dump(mode="json")
    changed["imported_at"] = (now() + timedelta(days=1)).isoformat().replace("+00:00", "Z")
    late = signed(changed)
    cutoff = now()
    assert (
        estimate_cases(target, [late], cutoff, cutoff, cutoff + timedelta(days=30)).status
        == "not_applicable"
    )
    store = SQLiteHistoricalRepository(repo, FileSnapshotStore(tmp_path))
    saved = store.import_cases(data)
    snapshot = Snapshot.from_raw(saved.sources[0].text)
    from uuid import UUID

    snapid = UUID(hashlib.sha256(snapshot.raw.encode()).hexdigest()[:32])
    (tmp_path / "evidence" / "raw" / f"{snapid}.txt").write_text("corrupt", encoding="utf-8")
    assert check(ReferenceService(store), target).status == "failed"


def test_case_migration_preserves_existing_rows(tmp_path):
    from pathlib import Path

    from alembic import command
    from alembic.config import Config

    import future_agent.storage.sqlite as sqlite_module
    from future_agent.storage.sqlite import SQLiteRepository

    repository = SQLiteRepository(tmp_path / "migration.db")
    try:
        config = Config()
        config.set_main_option(
            "script_location", str(Path(sqlite_module.__file__).parent / "migrations")
        )
        with repository.engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "0007_reference")
            connection.execute(
                text(
                    "insert into historical_diagnostics(id,payload) "
                    "values ('old','immutable-old-data')"
                )
            )
        repository.migrate()
        with repository.engine.connect() as connection:
            assert (
                connection.scalar(text("select payload from historical_diagnostics where id='old'"))
                == "immutable-old-data"
            )
    finally:
        repository.close()


def test_case_cli_import_catalog_and_replay(tmp_path, monkeypatch, capsys):
    from future_agent.cli import main

    source = tmp_path / "cases.json"
    source.write_text(dataset().model_dump_json(), encoding="utf-8")

    def run(*args):
        monkeypatch.setattr(
            "sys.argv",
            ["future-agent", "--no-env", "--data-dir", str(tmp_path), "reference", *args],
        )
        assert main() == 0
        return json.loads(capsys.readouterr().out)

    imported = run("import-cases", str(source))
    assert imported["cases"] == 3 and imported["use"] == "qualitative_only"
    catalog = run("cases", "politics")
    assert catalog and "outcome" not in json.dumps(catalog)
    replay = run("replay-input", "politics", "fixture-2012")
    assert "outcome" not in replay
    assert [c["key"] for c in replay["historical_reference"]["cases"]] == ["fixture-2008"]


@pytest.mark.parametrize("domain,edition", [("sports", 2030), ("politics", 2028)])
def test_full_ordinary_chain_binding_report_freeze_export_and_recovery(
    repo, tmp_path, domain, edition
):
    store = SQLiteHistoricalRepository(repo, FileSnapshotStore(tmp_path))
    saved = store.import_cases(dataset(domain))
    target = saved.cases[0].target.model_copy(update={"edition": edition})

    class Provider(AssistantProvider):
        def generate(self, system, user, schema, timeout):
            data = json.loads(user.split("\n上次输出")[0])
            if data.get("prompt_version") == "analysis-plan-reference-1.1":
                data["prompt_version"] = "target-research-1.5"
                user = json.dumps(data)
            return super().generate(system, user, schema, timeout)

    provider = Provider(plan=plan(target).model_dump(mode="json"), interrupt="forecast")
    service, sid = setup(repo, tmp_path, provider)
    service.references = ReferenceService(store)
    with pytest.raises(KeyboardInterrupt):
        service.execute(sid)
    step = service.forecasts.step(service.state(sid).forecast_request_id)
    newer = saved.model_dump(mode="json")
    newer.update(id=str(uuid4()), version="fixture-2")
    store.import_cases(signed(newer))
    result = service.forecasts.resume(step.id, retry_interrupted=True)
    assert result.schema_version == "2.2"
    assert step.schema_version == "1.2"
    assert result.method_version == "judgment-case-reference-1.0"
    assert result.reference_binding == step.reference_binding
    assert result.reference_binding.estimate.dataset_id == saved.id
    assert service.state(sid).reference_preview.status == "qualitative_only"
    payload = provider.calls[-1]["historical_reference"]
    assert payload["cases"] and "sources" in payload and "materials" not in payload
    assert "text" not in payload and "frequency" not in payload
    assert result.probabilities == [None, None]
    assert saved.cases[0].key in render_report(result)
    from future_agent.application.lifecycle import ResolutionService
    from future_agent.application.report_export import ReportExportService
    from future_agent.storage.reports import FileReportStore

    lifecycle = ResolutionService(repo, repo, service.evidence)
    frozen = lifecycle.freeze(result.id)
    assert frozen.input_hash == fingerprint(result.model_dump(mode="json"))
    _, output = ReportExportService(service.forecasts, lifecycle, FileReportStore(tmp_path)).save(
        result.id
    )
    export = json.loads(output.read_text(encoding="utf-8"))
    assert export["schema_version"] == "1.3"
    assert export["historical_reference"]["estimate"]["dataset_hash"] == saved.manifest_hash
