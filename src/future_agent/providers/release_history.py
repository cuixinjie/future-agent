"""Bounded public GitHub Releases acquisition; never asserts human completeness."""

import asyncio
import json
import re
import time
from collections.abc import Callable
from datetime import timedelta
from uuid import uuid4

import httpx
from pydantic import TypeAdapter

from future_agent.application.network import NetworkError
from future_agent.application.ports import AppError
from future_agent.application.reference import HistoricalMaterial, ReleaseDataset
from future_agent.domain.contracts import UtcTime, now
from future_agent.domain.evaluation import ReleaseHistory, ReleaseRecord
from future_agent.domain.lifecycle import fingerprint
from future_agent.domain.reference import ReferenceTarget
from future_agent.providers.web import PublicTransport, bounded_body, check_status, resolve


def collect_releases(
    target: ReferenceTarget,
    version: str,
    on_material: Callable[[HistoricalMaterial], None] = lambda _: None,
) -> ReleaseDataset:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", target.repository):
        raise AppError("仓库必须是公开owner/repo标识")
    anchor, started = now(), time.monotonic()
    materials, records = [], []
    for page in range(1, 201):
        if time.monotonic() - started >= 1800:
            raise AppError("采集累计30分钟预算耗尽；未声明历史完整")
        url = f"https://api.github.com/repos/{target.repository}/releases?per_page=100&page={page}"
        # Reuse the public DNS/TLS/size/timeout boundary; never follow arbitrary API links.
        raw = asyncio.run(_github_json(url, min(30, 1800 - (time.monotonic() - started))))
        material = HistoricalMaterial(url=url, raw=raw, retrieved_at=now())
        on_material(material)
        materials.append(material)
        if sum(len(m.raw.encode()) for m in materials) > 100_000_000:
            raise AppError("原始资料达到100MB预算；未声明历史完整")
        data = json.loads(raw)
        if not isinstance(data, list):
            raise AppError("发布接口未返回列表；不能作为空历史")
        for item in data:
            if item.get("published_at") is None:
                raise AppError("发布时点缺失，不能推断完整窗口")
            published = TypeAdapter(UtcTime).validate_python(item["published_at"])
            if published > anchor:
                raise AppError("采集期间出现新发布，须重新采集固定历史")
            records.append(
                ReleaseRecord(
                    version=item["tag_name"],
                    product_channel=target.channel,
                    published_at=published,
                    draft=item["draft"],
                    prerelease=item["prerelease"],
                    quote=json.dumps(item["tag_name"], ensure_ascii=False),
                )
            )
        if len(data) < 100:
            break
    else:
        raise AppError("达到200请求上限，未取得分页终点")
    history = ReleaseHistory(
        document_id=uuid4(),
        coverage_start=min((r.published_at for r in records), default=anchor - timedelta(days=360)),
        coverage_end=anchor,
        complete=False,
        reviewer="pending-human-review",
        coverage_quote=materials[0].raw[:1],
        records=records,
    )
    payload = dict(
        version=version,
        target=target,
        source_revision="GitHub Releases API / paginated snapshot",
        license="上游资料；使用及再分发范围须逐项目审查",
        selection_rule="用户指定公开仓库，开发采集；存在选择偏差",
        review_origin="unreviewed",
        reviewer="pending-human-review",
        history=history,
        materials=materials,
    )
    # Build the canonical manifest only after all acquisition fields are fixed.
    payload.update(id=uuid4(), imported_at=now())
    encoded = {
        k: (v.model_dump(mode="json") if hasattr(v, "model_dump") else v)
        for k, v in payload.items()
    }
    encoded["id"], encoded["imported_at"] = (
        str(payload["id"]),
        payload["imported_at"].isoformat().replace("+00:00", "Z"),
    )
    encoded["materials"] = [m.model_dump(mode="json") for m in materials]
    encoded["schema_version"] = "1.0"
    return ReleaseDataset.model_validate({**encoded, "manifest_hash": fingerprint(encoded)})


async def _github_json(url: str, timeout: float) -> str:
    try:
        async with asyncio.timeout(timeout):
            async with httpx.AsyncClient(
                transport=PublicTransport(resolver=resolve),
                trust_env=False,
                follow_redirects=False,
                headers={
                    "Accept": "application/vnd.github+json",
                    "Accept-Encoding": "identity",
                    "User-Agent": "future-agent/0.1",
                    "X-GitHub-Api-Version": "2026-03-10",
                },
            ) as client:
                async with client.stream("GET", url, timeout=timeout) as response:
                    check_status(response)
                    if response.headers.get("content-type", "").split(";")[0] != "application/json":
                        raise NetworkError("unsupported")
                    raw = await bounded_body(
                        response, allow_gzip=False, deadline=time.monotonic() + timeout
                    )
                    return raw.decode("utf-8")
    except (httpx.HTTPError, OSError, TimeoutError, UnicodeError):
        raise NetworkError("release_acquisition_failed") from None
