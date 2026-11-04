"""Historical data contracts and pure release reference eligibility/calculation."""

from datetime import timedelta

from future_agent.domain.assistance import AnalysisPlan
from future_agent.domain.contracts import UtcTime
from future_agent.domain.evaluation import baseline
from future_agent.domain.reference import (
    MAX_ANCHOR_DELAY_SECONDS,
    ReferenceEstimate,
    ReleasePosterior,
)
from future_agent.domain.reference_data import HistoricalMaterial as HistoricalMaterial
from future_agent.domain.reference_data import ReleaseDataset as ReleaseDataset
from future_agent.domain.reference_quality import audit_release_dataset


def estimate_release_reference(
    plan: AnalysisPlan,
    candidates: list[ReleaseDataset],
    cutoff: UtcTime,
    start: UtcTime,
    end: UtcTime,
) -> ReferenceEstimate:
    common = dict(cutoff=cutoff, window_start=start, window_end=end, target=plan.reference_target)

    def missing(reason: str, excluded: list[str] | None = None) -> ReferenceEstimate:
        return ReferenceEstimate(
            status="not_applicable", reason=reason, excluded=excluded or [], **common
        )

    if (
        plan.reference_target is None
        or plan.outcome_type != "binary"
        or plan.window_start is not None
        or plan.duration_days != 30
        or plan.analysis_kind != "baseline"
        or plan.action_conditions
        or end - start != timedelta(days=30)
    ):
        return missing("未匹配同产品/通道的相对30天稳定发布定义；未使用统计历史参照")
    excluded = []
    for dataset in sorted(candidates, key=lambda d: (d.imported_at, str(d.id)), reverse=True):
        if dataset.target != plan.reference_target:
            excluded.append(f"{dataset.id}: 产品/通道/定义不匹配")
            continue
        history = dataset.history
        delay = (start - history.coverage_end).total_seconds()
        if dataset.review_origin != "human":
            excluded.append(f"{dataset.id}: 未经人工完整性与产品映射审查")
            continue
        if dataset.imported_at > cutoff or any(m.retrieved_at > cutoff for m in dataset.materials):
            excluded.append(f"{dataset.id}: 截止后获取或入库")
            continue
        if not 0 < delay <= MAX_ANCHOR_DELAY_SECONDS:
            excluded.append(f"{dataset.id}: 历史锚点至目标起点超出3601秒")
            continue
        audit = audit_release_dataset(dataset)
        if audit.machine_status != "passed":
            excluded.append(f"{dataset.id}: 来源质量不合格：" + "; ".join(audit.issues))
            continue
        try:
            labels = baseline(history, history.coverage_end, dataset.target.channel)
        except ValueError as exc:
            excluded.append(f"{dataset.id}: {exc}")
            continue
        return ReferenceEstimate(
            source_audit_version=audit.audit_version,
            status="computed",
            reason="已审查的同产品/通道历史窗口频率与贝叶斯参照",
            dataset_id=dataset.id,
            dataset_hash=dataset.manifest_hash,
            history_anchor=history.coverage_end,
            anchor_delay_seconds=delay,
            labels=labels,
            numerator=sum(labels),
            denominator=12,
            frequency=sum(labels) / 12,
            bayesian_estimate=ReleasePosterior.from_successes(sum(labels)),
            sources=[m.url for m in dataset.materials],
            excluded=excluded,
            limitations=[
                "贝叶斯参照采用预先固定的Beta(1,1)均匀先验，未从真实预测结果拟合",
                "后验预测假设各窗口在给定发生率下独立且未来机制不变；实际相关或节奏变化会使假设失效",
                "平滑估计未经独立校准，不自动替代最终主观概率，也不证明优于原始频率",
                "12个相邻窗口相关且样本有限",
                "发布节奏可能变化，未建模距上次发布时间",
                "观察频率不是已校准的未来概率",
                "历史锚点早于最终证据截止，间隔已记录",
            ],
            **common,
        )
    return missing("无合格完整历史；未使用统计历史参照", excluded)
