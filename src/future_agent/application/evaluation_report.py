"""Deterministic full-registry evaluation; unknown usage and outcomes remain unknown."""

from collections import Counter
from datetime import timedelta
from statistics import mean

from future_agent.application.evaluation import EvaluationService
from future_agent.domain.contracts import now
from future_agent.domain.evaluation import EvaluationRun


def evaluation_report(service: EvaluationService, run: EvaluationRun) -> dict:
    timestamp = now()
    protocol = run.protocol
    complete_window = timestamp >= protocol.forecast_at + timedelta(days=protocol.window_days)
    rows, scores = [], {}
    for project in protocol.projects:
        history = run.resolutions.get(str(project.id), [])
        resolution = history[-1] if history else None
        outcome = (
            int(resolution.resolved_outcome == "yes")
            if resolution and resolution.status == "resolved"
            else None
        )
        snap = next((s for s in run.snapshots if s.project_id == project.id), None)
        session = service.sessions.load(snap.session_id) if snap else None
        for entry in [e for e in run.entries if e.project_id == project.id]:
            p = entry.prediction.probability if entry.prediction else None
            score = (
                (p - outcome) ** 2
                if complete_window
                and entry.status == "submitted"
                and p is not None
                and outcome is not None
                else None
            )
            if score is not None:
                scores[(project.id, entry.arm)] = score
            current_attempts = (
                [a for a in session.attempts if a.forecast_step_id == entry.step_id]
                if session
                else []
            )
            attempts = (
                []
                if entry.arm == "A"
                else (
                    current_attempts
                    if entry.arm == "B"
                    else [*(snap.preparation_attempts if snap else []), *current_attempts]
                )
            )
            network = (
                [a for r in snap.research_runs for a in r.attempts]
                if snap and entry.arm == "C"
                else []
            )
            tokens_known = all(
                a.input_tokens is not None and a.output_tokens is not None for a in attempts
            )
            elapsed_known = all(a.elapsed_seconds is not None for a in [*attempts, *network])
            cost = (
                0.0
                if entry.arm == "A"
                else (
                    sum(a.cost for a in attempts)
                    if attempts and not network and all(a.cost is not None for a in attempts)
                    else None
                )
            )
            reviews = [r for r in run.reviews if r.project_id == project.id and r.arm == entry.arm]
            reviewed = reviews[-1] if reviews else None
            rows.append(
                {
                    "project_id": str(project.id),
                    "project": project.name,
                    "arm": entry.arm,
                    "status": entry.status,
                    "reason": entry.reason,
                    "input_hash": snap.input_hash if snap else None,
                    "probability": p,
                    "delivery": (
                        entry.prediction.draft.judgment.delivery
                        if entry.prediction
                        and entry.prediction.draft
                        and entry.prediction.draft.judgment
                        else None
                    ),
                    "human_inputs": sum(s.kind == "answer" for s in snap.sources) if snap else None,
                    "method": entry.prediction.method if entry.prediction else None,
                    "method_version": entry.prediction.method_version if entry.prediction else None,
                    "model_id": entry.prediction.model_id if entry.prediction else None,
                    "prompt_version": entry.prediction.prompt_version if entry.prediction else None,
                    "prediction_hash": entry.prediction_hash,
                    "calibration_status": entry.prediction.calibration_status
                    if entry.prediction
                    else None,
                    "resolution_status": resolution.status if resolution else "pending",
                    "resolution_id": str(resolution.id) if resolution else None,
                    "outcome": outcome,
                    "brier": score,
                    "scoring_exclusion": None
                    if score is not None
                    else (
                        "window_not_complete"
                        if not complete_window
                        else "prediction_not_eligible"
                        if entry.status != "submitted" or p is None
                        else "not_resolved"
                    ),
                    "llm_calls": len(attempts),
                    "search_calls": sum(a.kind == "search" for a in network),
                    "page_calls": sum(a.kind == "page" for a in network),
                    "tokens": sum(a.input_tokens + a.output_tokens for a in attempts)
                    if tokens_known
                    else None,
                    "charged_token_allowance": sum(a.charged_tokens for a in attempts),
                    "elapsed_seconds": sum(a.elapsed_seconds for a in [*attempts, *network])
                    if elapsed_known
                    else None,
                    "cost": cost,
                    "citation_errors": reviewed.citation_errors if reviewed else None,
                    "fact_omissions": reviewed.fact_omissions if reviewed else None,
                }
            )
    arms = {}
    for arm in ("A", "B", "C"):
        selected = [r for r in rows if r["arm"] == arm]
        values = [r["brier"] for r in selected if r["brier"] is not None]
        arms[arm] = {
            "registered": len(selected),
            "scored": len(values),
            "brier": mean(values) if values else None,
            "coverage": sum(
                r["status"] == "submitted" and r["probability"] is not None for r in selected
            )
            / len(selected),
            "scenario_rate": sum(r["status"] == "scenario" for r in selected) / len(selected),
            "refusal_rate": sum(r["status"] == "refused" for r in selected) / len(selected),
            "states": dict(Counter(r["status"] for r in selected)),
            "deliveries": dict(Counter(r["delivery"] for r in selected if r["delivery"])),
            "resolution_states": dict(Counter(r["resolution_status"] for r in selected)),
            "cost": sum(r["cost"] for r in selected)
            if all(r["cost"] is not None for r in selected)
            else None,
        }
    pairs = {}
    for other in ("A", "B"):
        common = [
            p.id for p in protocol.projects if (p.id, "C") in scores and (p.id, other) in scores
        ]
        pairs["C-" + other] = {
            "n": len(common),
            "project_ids": [str(p) for p in common],
            "mean_difference": mean(scores[(p, "C")] - scores[(p, other)] for p in common)
            if common
            else None,
            "C_brier": mean(scores[(p, "C")] for p in common) if common else None,
            other + "_brier": mean(scores[(p, other)] for p in common) if common else None,
        }
    common = [p.id for p in protocol.projects if all((p.id, a) in scores for a in ("A", "B", "C"))]
    return {
        "protocol_id": str(protocol.id),
        "protocol_version": protocol.version,
        "report_version": "evaluation-report-1.0",
        "purpose": protocol.purpose,
        "reported_at": timestamp.isoformat(),
        "run_version": run.version,
        "evidence_lag_seconds": (protocol.forecast_at - protocol.evidence_cutoff).total_seconds(),
        "window_complete": complete_window,
        "arms": arms,
        "paired": pairs,
        "common_ABC": {
            "n": len(common),
            "project_ids": [str(p) for p in common],
            "brier": {
                a: mean(scores[(p, a)] for p in common) if common else None for a in ("A", "B", "C")
            },
        },
        "entries": rows,
        "reviews": [r.model_dump(mode="json") for r in run.reviews],
        "limitations": [
            "固定夹具仅验证工具，不证明预测有效。"
            if protocol.purpose == "fixture"
            else "仅适用于预登记范围，主观及频率概率均未校准。",
            "配对差异仅描述共同子集，不能忽略失败/拒绝/迟交及未决。",
            "整批窗口结束前不评分；核验宽限期不延长事件窗口，之后追加核验保留历史。",
            "引用错误及事实遗漏未经人工审查保持未知；费用未知留空。",
            "不自动调参、校准或训练；不提供忽略项目相关性的置信区间。",
        ],
    }
