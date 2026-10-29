"""Plain terminal layout; no ANSI sequences or changes to saved reports."""

import re
import shutil
import unicodedata

from future_agent.domain.forecasts import Forecast


def display_width(text: str) -> int:
    return sum(
        0 if unicodedata.combining(c) else 2 if unicodedata.east_asian_width(c) in "WF" else 1
        for c in text
    )


def terminal_layout(text: str, *, width: int | None = None) -> str:
    """Wrap by display cells, preserving paragraphs and hanging list indentation."""
    width = max(20, min(width or shutil.get_terminal_size((96, 24)).columns, 96))
    result = []
    for line in text.expandtabs(4).split("\n"):
        match = re.match(r"\s*(?:[-•] |\d+\. )?", line)
        indent = " " * min(display_width(match[0]), width // 3)
        current = ""
        cells = 0
        for char in line:
            size = display_width(char)
            if cells + size > width and current:
                result.append(current)
                current, cells = indent, len(indent)
            current += char
            cells += size
        result.append(current)
    return "\n".join(result)


def forecast_summary(forecast: Forecast) -> str:
    """A concise view of saved values; the full report remains separately accessible."""
    question = forecast.event.question
    assert question is not None
    lines = [
        "\n── 预测结果 ──\n",
        f"问题：{question.question}",
        f"窗口：{question.window_start.isoformat()} 至 {question.window_end.isoformat()}",
    ]
    judgment = forecast.draft.judgment
    if judgment:
        labels = {
            "probability": "概率预测（未校准）",
            "directional": "方向判断",
            "conditional": "条件分析（无总体预测）",
            "exploration": "开放探索",
            "unable": "无法判断",
        }
        lines.extend([f"交付：{labels[judgment.delivery]}", "", "当前判断：", judgment.conclusion])
        definitions = {item.key: item.definition for item in forecast.spec.outcomes}
        if judgment.primary_outcome:
            lines.extend(["", "首选结果：" + definitions[judgment.primary_outcome]])
        if judgment.ranking:
            lines.append("结果排序：" + " → ".join(definitions[key] for key in judgment.ranking))
    else:
        lines.extend(
            ["", "交付：情景分析" if forecast.method == "scenario_only" else "交付：主观估计"]
        )
    if any(p is not None for p in forecast.probabilities) or not judgment:
        lines.extend(["", "各结果："])
        for definition, probability in zip(
            forecast.spec.outcomes, forecast.probabilities, strict=True
        ):
            value = "未提供概率" if probability is None else f"概率 {probability!r}（未校准）"
            lines.append(f"  • {definition.definition}：{value}")
    if not judgment:
        lines.extend(["", "── 情景依据 ──"])
        for definition, outcome in zip(
            forecast.spec.outcomes, forecast.draft.outcomes, strict=True
        ):
            lines.extend(["", definition.definition, f"推断理由：{outcome.reasoning}"])
            if outcome.conditions:
                lines.extend(["适用条件：", *(f"  • {item}" for item in outcome.conditions)])
    if judgment:
        lines.extend(["", "── 判断依据 ──", "", f"参照起点：{judgment.reference_basis}"])
        lines.extend(["", f"本次差异与调整：{judgment.differences_and_adjustment}"])
        for label, items in (
            ("关键因素", judgment.key_drivers),
            ("未知的影响", judgment.unknown_effects),
            ("改变判断的条件", judgment.change_conditions),
        ):
            if items:
                lines.extend(["", label + "：", *(f"  • {item}" for item in items)])
        if judgment.no_numeric_reason:
            lines.extend(["", "未提供概率：" + judgment.no_numeric_reason])
    if forecast.draft.refusal_reason:
        lines.extend(["", "数值估计拒绝原因：" + forecast.draft.refusal_reason])
    limitations = forecast.assessment.limitations + forecast.assessment.gaps
    if forecast.spec.guidance:
        for label, items in (
            ("行动/适用条件", forecast.spec.guidance.action_conditions),
            ("目标默认与假设", forecast.spec.guidance.assumptions),
        ):
            if items:
                lines.extend(["", label + "：", *(f"  • {item}" for item in items)])
    if limitations:
        lines.extend(["", "限制与缺口：", *(f"  • {item}" for item in dict.fromkeys(limitations))])
    if forecast.event.origin == "fixture":
        lines.extend(["", "离线/夹具输入：不代表真实实验或服务能力。"])
    lines.extend(
        [
            "",
            f"校准状态：{forecast.calibration_status}；主观概率未经独立校准。"
            if forecast.method == "llm_judgment"
            else "校准状态：not_applicable（条件情景）",
        ]
    )
    return "\n".join(lines)


def report_details(report: str) -> str:
    """Separate existing report fields without removing or interpreting their content."""
    return "\n\n".join(line for line in report.splitlines() if line.strip())
