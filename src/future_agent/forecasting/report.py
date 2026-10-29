"""Deterministic report: every value comes from the saved structured forecast."""

from future_agent.domain.forecasts import Forecast
from future_agent.domain.lifecycle import FrozenForecast


def render_report(forecast: Forecast, *, frozen: FrozenForecast | None = None) -> str:
    if frozen is not None and frozen.forecast != forecast:
        raise ValueError("冻结记录与报告预测不一致")
    question = forecast.event.question
    assert question is not None
    lines = [
        "预测报告（已冻结；实验评分资格须另行检查）"
        if frozen is not None
        else "预测报告（草稿，尚未冻结，不用于实验评分）",
        f"预测ID：{forecast.id}",
        f"问题：{question.question}",
        f"窗口：{question.window_start.isoformat()} 至 {question.window_end.isoformat()}",
        f"判定规则：{question.resolution_rule}",
        f"核验来源：{question.resolution_source_policy}",
        f"事件版本：{forecast.event.id} / {forecast.event.revision}",
        f"证据截止：{forecast.evidence_cutoff.isoformat()}",
        f"方法：{forecast.method} / {forecast.method_version}",
        f"校准状态：{forecast.calibration_status}（主观估计未经独立校准）"
        if forecast.method == "llm_judgment"
        else "校准状态：not_applicable（条件情景）",
        f"模型：{forecast.model_id}；提示词：{forecast.prompt_version}",
        f"结果定义审查：{forecast.spec.definition_review}",
    ]
    if forecast.draft.judgment is not None:
        judgment = forecast.draft.judgment
        labels = {
            "probability": "概率预测（未校准）",
            "directional": "方向判断",
            "conditional": "条件分析（无总体预测）",
            "exploration": "开放探索",
            "unable": "无法判断",
        }
        definitions = {o.key: o.definition for o in forecast.spec.outcomes}
        lead = [
            f"问题：{question.question}",
            f"窗口：{question.window_start.isoformat()} 至 {question.window_end.isoformat()}",
            f"交付：{labels[judgment.delivery]}",
            f"当前判断：{judgment.conclusion}",
        ]
        if judgment.primary_outcome:
            lead.append("首选结果：" + definitions[judgment.primary_outcome])
        if judgment.delivery == "probability":
            lead.append(
                "主观概率（未校准）："
                + "；".join(
                    f"{definition.definition}：{probability!r}"
                    for definition, probability in zip(
                        forecast.spec.outcomes, forecast.probabilities, strict=True
                    )
                )
            )
        if judgment.ranking:
            lead.append("结果排序：" + " → ".join(definitions[k] for k in judgment.ranking))
        lead.extend(
            [
                "参照起点：" + judgment.reference_basis,
                "本次差异与调整：" + judgment.differences_and_adjustment,
                "关键因素：" + "；".join(judgment.key_drivers),
                "未知的影响：" + ("；".join(judgment.unknown_effects) or "无新增说明"),
                "改变判断的条件：" + "；".join(judgment.change_conditions),
            ]
        )
        if judgment.no_numeric_reason:
            lead.append("未提供概率：" + judgment.no_numeric_reason)
        if forecast.spec.guidance.action_conditions:
            lead.append("行动/适用条件：" + "；".join(forecast.spec.guidance.action_conditions))
        lead.append("目标默认与假设：" + ("；".join(forecast.spec.guidance.assumptions) or "无"))
        lines = [*lead, "", *lines]
    if frozen is not None:
        lines.extend(
            [f"冻结时间：{frozen.frozen_at.isoformat()}", f"冻结内容 SHA256：{frozen.input_hash}"]
        )
    if forecast.event.origin == "fixture":
        lines.append("离线/夹具许可输入：不代表真实实验或服务能力。")
    elif not forecast.assessment.experiment:
        lines.append("普通分析：未登记为独立前瞻实验；不据此宣称预测效果。")
    if forecast.draft.refusal_reason:
        lines.append(f"数值估计拒绝原因：{forecast.draft.refusal_reason}")
    for definition, outcome, probability in zip(
        forecast.spec.outcomes, forecast.draft.outcomes, forecast.probabilities, strict=True
    ):
        lines.extend(
            [
                "",
                f"方向：{definition.key} — {definition.definition}",
                "概率：未提供" if probability is None else f"概率：{probability!r}",
                f"推断理由（非已核实事实）：{outcome.reasoning}",
                "支持引用："
                + (", ".join(map(str, outcome.supporting_evidence)) or "无，支持证据缺失"),
                "反证引用：" + (", ".join(map(str, outcome.opposing_evidence)) or "无，反证缺失"),
                "条件/假设：" + "；".join(outcome.conditions),
                "观察信号：" + "；".join(outcome.watch_signals),
            ]
        )
    if forecast.normalization_original_sum is not None:
        lines.append(f"舍入归一化：原总和 {forecast.normalization_original_sum!r}，容差 1e-6。")
    lines.extend(["", "假设：", *forecast.draft.assumptions, "当前未知："])
    lines.extend(
        u.description for u in forecast.event.unknowns if u.resolution_status != "answered"
    )
    lines.extend(
        [
            "限制与缺口：",
            *forecast.assessment.limitations,
            *forecast.assessment.gaps,
            forecast.assessment.case_skip_reason,
            "引用来源：",
        ]
    )
    documents = {d.id: d for d in forecast.evidence_documents}
    for span in forecast.evidence_spans:
        doc = documents[span.document_id]
        lines.append(
            f"{span.id} | {doc.source.url} | 获取 {doc.source.retrieved_at} | "
            f"SHA256 {doc.content_hash} | [{span.start}:{span.end}] {span.quote}"
        )
    return "\n".join(lines) + "\n"
