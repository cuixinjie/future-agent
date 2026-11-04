"""Deterministic source/extraction audit; a machine pass is never human approval."""

import hashlib
import json
import re
from datetime import timedelta
from typing import Literal
from urllib.parse import parse_qs, urlsplit

from pydantic import Field, TypeAdapter

from future_agent.domain.contracts import Contract, Text, UtcTime, fingerprint, now
from future_agent.domain.evaluation import baseline
from future_agent.domain.reference_data import ReleaseDataset


class ReleaseQualityAudit(Contract):
    audit_version: Literal["release-source-audit-1.1"] = "release-source-audit-1.1"
    generated_at: UtcTime = Field(default_factory=now)
    dataset_hash: Text
    machine_status: Literal["passed", "rejected"]
    issues: list[Text]
    page_count: int
    terminal_page: int | None
    raw_record_count: int
    stable_candidate_count: int
    prerelease_count: int
    source_hashes: list[Text]
    human_review_required: bool = True
    limitations: list[Text] = Field(
        default_factory=lambda: [
            "分页与字段一致不能证明项目通道完整，仍需产品映射和资格人工审查",
            "现时API可能含后续编辑或删除，不能冒充历史时点可见的完整互联网",
        ]
    )


def audit_release_dataset(dataset: ReleaseDataset) -> ReleaseQualityAudit:
    issues: list[str] = []
    pages: dict[int, list[dict]] = {}
    page_size: int | None = None
    hashes: list[str] = []
    for index, material in enumerate(dataset.materials):
        hashes.append(hashlib.sha256(material.raw.encode()).hexdigest())
        parts = urlsplit(material.url)
        params = parse_qs(parts.query)
        try:
            if (
                parts.scheme != "https"
                or parts.netloc != "api.github.com"
                or parts.path != f"/repos/{dataset.target.repository}/releases"
                or set(params) != {"per_page", "page"}
                or any(len(values) != 1 for values in params.values())
            ):
                raise ValueError
            number, size = int(params["page"][0]), int(params["per_page"][0])
            if number < 1 or not 1 <= size <= 100 or (page_size is not None and size != page_size):
                raise ValueError
            page_size = size
            rows = json.loads(material.raw)
            if (
                not isinstance(rows, list)
                or len(rows) > size
                or any(not isinstance(r, dict) for r in rows)
            ):
                raise ValueError
            if number in pages:
                raise ValueError
            pages[number] = rows
        except (ValueError, TypeError, KeyError):
            issues.append(f"source[{index}]:invalid_source_or_page")
    numbers = sorted(pages)
    terminal = next((n for n in numbers if len(pages[n]) < page_size), None)
    if not numbers or numbers != list(range(1, max(numbers, default=0) + 1)):
        issues.append("pagination:missing_page")
    if terminal is None:
        issues.append("pagination:no_terminal_page")
    elif any(pages[n] for n in numbers if n > terminal):
        issues.append("pagination:records_after_terminal")
    rows = [r for n in numbers for r in pages[n]]
    if not rows:
        issues.append("coverage:empty_api_is_not_complete_release_history")

    # Verify extraction against raw fields, not merely whether a tag quote occurs somewhere.
    signatures = []
    ids: set[int] = set()
    versions = {}
    for index, row in enumerate(rows):
        try:
            if (
                type(row["id"]) is not int
                or row["id"] in ids
                or type(row["draft"]) is not bool
                or type(row["prerelease"]) is not bool
                or not isinstance(row["tag_name"], str)
                or not row["tag_name"].strip()
            ):
                raise ValueError
            ids.add(row["id"])
            timestamp = TypeAdapter(UtcTime).validate_python(row["published_at"])
            if timestamp > dataset.history.coverage_end:
                raise ValueError
            signature = (row["tag_name"], timestamp, row["draft"], row["prerelease"])
            key = re.sub(r"^v(?=\d)", "", row["tag_name"].strip().casefold())
            if key in versions and versions[key] != signature[1:]:
                issues.append(f"record[{index}]:version_conflict")
            versions[key] = signature[1:]
            signatures.append(signature)
        except (ValueError, TypeError, KeyError):
            issues.append(f"record[{index}]:invalid_id_flags_or_publication_time")
    extracted = [
        (r.version, r.published_at, r.draft, r.prerelease) for r in dataset.history.records
    ]
    if sorted(signatures) != sorted(extracted):
        issues.append("extraction:raw_fields_mismatch_or_omitted_records")
    if any(r.product_channel != dataset.target.channel for r in dataset.history.records):
        issues.append("extraction:wrong_channel")
    if dataset.history.coverage_start > dataset.history.coverage_end - timedelta(days=360):
        issues.append("coverage:less_than_360_days")
    if signatures and min(s[1] for s in signatures) > dataset.history.coverage_start:
        issues.append("coverage:claimed_start_before_observed_history")
    if not issues:
        try:
            # Only check reproducibility. Do not change persisted completeness or review origin.
            baseline(
                dataset.history.model_copy(update={"complete": True}),
                dataset.history.coverage_end,
                dataset.target.channel,
            )
        except ValueError:
            issues.append("baseline:conflicting_history")
    return ReleaseQualityAudit(
        dataset_hash=dataset.manifest_hash,
        machine_status="rejected" if issues else "passed",
        issues=issues,
        page_count=len(pages),
        terminal_page=terminal,
        raw_record_count=len(rows),
        stable_candidate_count=sum(
            r.stable(dataset.target.channel) for r in dataset.history.records
        ),
        prerelease_count=sum(r.prerelease for r in dataset.history.records),
        source_hashes=hashes,
    )


def audit_fingerprint(audit: ReleaseQualityAudit) -> str:
    return fingerprint(audit.model_dump(mode="json"))
