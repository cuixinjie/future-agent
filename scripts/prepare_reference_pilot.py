"""Prepare an isolated, reproducible quality pilot from already captured source files.

No network, model calls, human approval, or production database writes occur here.
"""

import argparse
import hashlib
import json
import random
import re
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from pydantic import TypeAdapter

from future_agent.application.reference import ReferenceService
from future_agent.domain.contracts import UtcTime, fingerprint, now
from future_agent.domain.evaluation import ReleaseHistory, ReleaseRecord
from future_agent.domain.reference import ReferenceTarget
from future_agent.domain.reference_quality import audit_release_dataset
from future_agent.domain.release_reference import HistoricalMaterial, ReleaseDataset
from future_agent.storage.reference import SQLiteHistoricalRepository
from future_agent.storage.snapshots import FileSnapshotStore
from future_agent.storage.sqlite import SQLiteRepository


def load_source(path: Path, root: Path) -> dict:
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("Source file leaves the approved pilot root")
    data = json.loads(path.read_text(encoding="utf-8"))
    if len(data["raw"].encode()) > 2 * 1024 * 1024:
        raise ValueError("Single source exceeds the existing 2MB acquisition limit")
    return data


def write_new(path: Path, data: object) -> None:
    with path.open("x", encoding="utf-8") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2, default=str)


def prepare_releases(root: Path, output: Path, plan: dict) -> list[dict]:
    products = {
        "python/cpython": "CPython",
        "nodejs/node": "Node.js",
        "microsoft/TypeScript": "TypeScript",
        "pandas-dev/pandas": "pandas",
        "numpy/numpy": "NumPy",
    }
    repository = SQLiteRepository(output / "quarantine.db")
    repository.migrate()
    service = ReferenceService(SQLiteHistoricalRepository(repository, FileSnapshotStore(output)))
    summary = []
    try:
        for repo in plan["pilot_repositories"]:
            folder = root / "raw" / repo.replace("/", "__")
            pages = sorted(folder.glob("release20-*.json")) or sorted(
                folder.glob("releases-*.json")
            )
            sources = [load_source(p, root) for p in pages]
            normalized, history_records = [], []
            for source in sources:
                rows = json.loads(source["raw"])
                for index, row in enumerate(rows):
                    timestamp = TypeAdapter(UtcTime).validate_python(row["published_at"])
                    record = ReleaseRecord(
                        version=row["tag_name"],
                        product_channel="stable",
                        published_at=timestamp,
                        draft=row["draft"],
                        prerelease=row["prerelease"],
                        quote=json.dumps(row["tag_name"], ensure_ascii=False),
                    )
                    history_records.append(record)
                    normalized.append(
                        dict(
                            id=row["id"],
                            version=record.version,
                            published_at=timestamp.isoformat(),
                            modified_at=row.get("updated_at"),
                            draft=record.draft,
                            prerelease=record.prerelease,
                            classification="stable_candidate"
                            if record.stable("stable")
                            else "excluded_candidate",
                            exclusion_reason="draft/prerelease/version rule"
                            if not record.stable("stable")
                            else None,
                            source_url=source["url"],
                            source_json_pointer=f"/{index}",
                            extraction_version="github-release-fields-1.0",
                            review_origin="program",
                        )
                    )
            anchor = TypeAdapter(UtcTime).validate_python(plan["created_at"])
            # Empty channels remain missing, rather than acquiring an invented coverage start.
            if not history_records:
                item = dict(
                    repository=repo,
                    status="excluded",
                    records=0,
                    reason="Empty GitHub Releases channel; not evidence of no project releases",
                    approved_for_prediction=False,
                )
                write_new(output / (repo.replace("/", "__") + "-audit.json"), item)
                summary.append(item)
                continue
            history = ReleaseHistory(
                document_id=uuid5(NAMESPACE_URL, plan["manifest_hash"] + repo),
                coverage_start=min(r.published_at for r in history_records),
                coverage_end=anchor,
                complete=False,
                reviewer="pending-human-review",
                records=history_records,
                coverage_quote=history_records[-1].quote,
            )
            candidate = ReleaseDataset.model_construct(
                id=uuid5(NAMESPACE_URL, plan["manifest_hash"] + repo + "candidate-v1"),
                version="quality-pilot-v1",
                target=ReferenceTarget(
                    repository=repo,
                    product=products[repo],
                    channel="stable",
                    definition="github-stable-release-30d-1.0",
                ),
                source_revision="connected GitHub read-only captures; original headers unavailable",
                license=(
                    "Pinned repository LICENSE captured; "
                    "release-text reuse scope pending human review"
                ),
                selection_rule=plan["manifest_hash"],
                review_origin="unreviewed",
                reviewer="pending-human-review",
                history=history,
                materials=[
                    HistoricalMaterial(url=s["url"], raw=s["raw"], retrieved_at=s["retrieved_at"])
                    for s in sources
                ],
                imported_at=now(),
                manifest_hash="pending",
            )
            payload = candidate.model_dump(mode="json", exclude={"manifest_hash"})
            candidate = ReleaseDataset.model_validate(
                {**payload, "manifest_hash": fingerprint(payload)}
            )
            audit = audit_release_dataset(candidate)
            verify = load_source(folder / "validation-first.json", root)

            def fields(r: dict) -> tuple:
                return (r["id"], r["tag_name"], r["published_at"], r["draft"], r["prerelease"])

            recheck_equal = [fields(r) for r in json.loads(verify["raw"])] == [
                fields(r) for r in json.loads(sources[0]["raw"])
            ]
            saved = service.import_dataset(candidate)
            name = repo.replace("/", "__")
            write_new(output / f"{name}-candidate.json", saved.model_dump(mode="json"))
            write_new(output / f"{name}-records.json", normalized)
            report = dict(
                repository=repo,
                dataset_id=str(saved.id),
                dataset_hash=saved.manifest_hash,
                audit=audit.model_dump(mode="json"),
                rechecked_first_page_equal=recheck_equal,
                status="awaiting_human_review"
                if audit.machine_status == "passed" and recheck_equal
                else "rejected",
                approved_for_prediction=False,
                freeze_time=anchor.isoformat(),
                license_source=load_source(folder / "license-source.json", root)["url"],
                product_source=load_source(folder / "readme-source.json", root)["url"],
                pending=[
                    "product/channel mapping",
                    "complete-history and release qualification review",
                    "reuse rights of release text",
                    "publication to a new approved dataset version",
                ],
            )
            write_new(output / f"{name}-audit.json", report)
            summary.append(report)
    finally:
        repository.close()
    return summary


def prepare_questions(root: Path, output: Path, plan: dict) -> dict:
    folder = root / "raw" / "forecastingresearch__forecastbench-datasets"
    source = load_source(folder / "question-source.json", root)
    raw = source["raw"].encode()
    blob_sha = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
    expected = source["url"].rsplit("/", 1)[1]
    if blob_sha != expected:
        raise ValueError("ForecastBench file does not match its pinned Git blob SHA")
    commit_capture = load_source(folder / "commits.json", root)
    commit_info = json.loads(commit_capture["raw"])[0]
    commit = commit_info["sha"]
    parent_capture = load_source(folder / "tree.json", root)
    if not parent_capture["url"].endswith("/" + commit):
        raise ValueError("Question source root does not match pinned commit")
    parent = json.loads(parent_capture["raw"])
    original_root_sha = parent["sha"]
    if parent["sha"] != commit_info["commit"]["tree"]["sha"]:
        recheck = load_source(folder / "root-tree-recheck-20261104.json", root)
        parent = json.loads(recheck["raw"])
        if parent["sha"] != commit_info["commit"]["tree"]["sha"] or not recheck["url"].endswith(
            "/" + parent["sha"]
        ):
            raise ValueError("Root tree does not match pinned commit metadata")
    license_blob = next(e["sha"] for e in parent["tree"] if e["path"] == "LICENSE")
    for tree_name, directory in [
        ("datasets-tree.json", "datasets"),
        ("question-sets-tree.json", "question_sets"),
    ]:
        expected_tree = next(e["sha"] for e in parent["tree"] if e["path"] == directory)
        capture = load_source(folder / tree_name, root)
        if not capture["url"].endswith("/" + expected_tree) or parent.get("truncated"):
            raise ValueError("Source tree provenance missing or truncated")
        parent = json.loads(capture["raw"])
        if parent["sha"] != expected_tree:
            raise ValueError("Captured tree content has a different identity")
    if parent.get("truncated") or blob_sha not in {e["sha"] for e in parent["tree"]}:
        raise ValueError("Question blob is not part of the pinned source tree")
    license_source = load_source(folder / "license.json", root)
    if not license_source["url"].endswith("/" + license_blob):
        raise ValueError("License is not part of pinned commit")
    license_raw = license_source["raw"].encode()
    if (
        hashlib.sha1(b"blob " + str(len(license_raw)).encode() + b"\0" + license_raw).hexdigest()
        != license_source["url"].rsplit("/", 1)[-1]
    ):
        raise ValueError("License snapshot hash mismatch")
    package = json.loads(source["raw"])
    # Lock source, seed, projection and filtering before touching any resolution file.
    sampling = dict(
        parent_plan=plan["manifest_hash"],
        source_blob=blob_sha,
        seed=20261103,
        purpose="question_parsing_only",
        exclude_sources=["metaculus"],
        filters="exact market close time, no combination, unique normalized question title",
        strata="source x horizon(<=90d,<=366d,>366d); round-robin deterministic seeded shuffle",
        cutoff_policy="date-only forecast deadline remains unknown exact cutoff",
        label_policy="not acquired, never infer unresolved or substitute market forecasts",
    )
    sampling["manifest_hash"] = fingerprint(sampling)
    write_new(output / "question-sampling-plan.json", sampling)
    exclusions, buckets, seen = [], defaultdict(list), set()
    for index, q in enumerate(package["questions"]):
        reason = None
        if q["source"] == "metaculus":
            reason = "upstream_AI_use_restrictions_not_approved"
        elif q.get("market_info_close_datetime") in {None, "N/A"}:
            reason = "date_only_or_missing_window_endpoint"
        elif q.get("combination_of") not in {None, "N/A"}:
            reason = "derived_shared_event_requires_family_review"
        family = re.sub(r"\W+", "", q["question"].casefold())
        if family in seen:
            reason = "duplicate_normalized_question_family"
        seen.add(family)
        if reason:
            exclusions.append(dict(source=q["source"], id=q["id"], reason=reason))
            continue
        days = (
            TypeAdapter(UtcTime).validate_python(q["market_info_close_datetime"]).date()
            - date.fromisoformat(package["forecast_due_date"])
        ).days
        if days <= 0:
            exclusions.append(
                dict(source=q["source"], id=q["id"], reason="window_not_future_at_due_date")
            )
            continue
        bucket = (q["source"], "short" if days <= 90 else "year" if days <= 366 else "long")
        buckets[bucket].append((index, q, fingerprint(family)))
    rng = random.Random(sampling["seed"])
    for key in sorted(buckets):
        buckets[key].sort(key=lambda v: (v[1]["source"], str(v[1]["id"])))
        rng.shuffle(buckets[key])
    selected = []
    while len(selected) < 10 and any(buckets.values()):
        for key in sorted(buckets):
            if buckets[key] and len(selected) < 10:
                selected.append(buckets[key].pop())
    # Explicit whitelist: no background, market forecasts, explanations, filenames or outcomes.
    projection = [
        dict(
            source=q["source"],
            original_id=q["id"],
            event_family_candidate=family,
            question=q["question"],
            rule=q["resolution_criteria"],
            rule_version=None,
            window_end=q["market_info_close_datetime"],
            original_forecast_due_date=package["forecast_due_date"],
            exact_forecast_cutoff=None,
            label=None,
            label_status="not_acquired",
            split="development",
            prior_evidence=[],
            audit_json_pointer=f"/questions/{index}",
            blockers=[
                "exact cutoff not established",
                "resolution evidence not acquired",
                "upstream reuse rights and semantic event-family review pending",
            ],
            allowed_use="parsing_diagnostic_only",
            approved_for_prediction=False,
        )
        for index, q, family in selected
    ]
    write_new(output / "questions-quarantine.json", projection)
    write_new(output / "question-exclusions.json", exclusions)
    summary = dict(
        source_blob=blob_sha,
        source_commit=commit,
        original_root_sha=original_root_sha,
        verified_root_sha=commit_info["commit"]["tree"]["sha"],
        supplemental_source_recheck="root-tree-recheck-20261104.json",
        candidates=len(package["questions"]),
        selected=len(projection),
        source_distribution=dict(Counter(q["source"] for q in package["questions"])),
        selected_source_distribution=dict(Counter(q["source"] for q in projection)),
        exclusions=dict(Counter(e["reason"] for e in exclusions)),
        approved_for_replay=0,
        approved_for_prediction=0,
        labels_imported=0,
        license="ForecastBench CC BY-SA 4.0; upstream use not automatically approved",
    )
    write_new(output / "question-audit.json", summary)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--version", default="prepared-v1")
    args = parser.parse_args()
    root = args.root.resolve()
    output = (root / args.version).resolve()
    if not output.is_relative_to(root) or output == root:
        raise ValueError("Output must be a new directory inside the approved pilot root")
    plan = json.loads((root / "collection-plan.json").read_text(encoding="utf-8"))
    if plan["manifest_hash"] != fingerprint(
        {k: v for k, v in plan.items() if k != "manifest_hash"}
    ):
        raise ValueError("Collection plan hash mismatch")
    captures = [load_source(path, root) for path in sorted((root / "raw").rglob("*.json"))]
    if sum(len(c["raw"].encode()) for c in captures) > plan["budget"]["raw_bytes"]:
        raise ValueError("Original batch exceeds its fixed byte budget")
    acquisition = json.loads((root / "acquisition-summary.json").read_text(encoding="utf-8"))
    if (
        acquisition["requests"] > plan["budget"]["requests"]
        or (acquisition["finishedAt"] - acquisition["startedAt"]) / 1000 > plan["budget"]["seconds"]
    ):
        raise ValueError("Original acquisition exceeds its fixed request/time budget")
    output.mkdir(exist_ok=False)
    releases = prepare_releases(root, output, plan)
    questions = prepare_questions(root, output, plan)
    files = {
        str(p.relative_to(output)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(output.glob("*.json"))
    }
    result = dict(
        collection_plan_hash=plan["manifest_hash"],
        prepared_at=now().isoformat(),
        release_projects=[
            dict(
                repository=r["repository"],
                status=r["status"],
                records=r.get("audit", {}).get("raw_record_count", 0),
            )
            for r in releases
        ],
        questions=questions,
        approved_datasets=0,
        expansion_allowed=False,
        artifacts=files,
        source_capture_hashes={
            str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted((root / "raw").rglob("*.json"))
        },
        acquisition_requests=acquisition["requests"],
        source_raw_bytes=sum(len(c["raw"].encode()) for c in captures),
        production_database_changed=False,
    )
    write_new(output / "quality-manifest.json", result)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
