"""Bounded live acceptance through the ordinary services; never substitute fixtures."""

import argparse
import json
from pathlib import Path

from future_agent.application.ports import AppError
from future_agent.config import load_settings
from future_agent.domain.contracts import now
from future_agent.web.services import build_services


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="明确允许两会话真实模型调用")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--domain", choices=["all", "sports", "politics"], default="all")
    args = parser.parse_args()
    if not args.live:
        parser.error("真实验收必须显式--live，不提供模拟回退")
    args.output.mkdir(parents=True, exist_ok=False)
    settings = load_settings()
    if not settings.model_ready() or not settings.historical_reference_enabled:
        raise AppError("真实模型或历史参照未配置")
    settings.max_llm_calls = min(settings.max_llm_calls, 8)
    settings.max_run_seconds = min(settings.max_run_seconds, 300)
    settings.max_total_tokens = min(settings.max_total_tokens, 300000)
    services = build_services(settings)
    narratives = {
        "sports": "请预测2030年男子足球世界杯决赛是否进入加时赛。"
        "yes表示正常90分钟及伤停补时结束后进入加时，no表示正常完赛且无需加时；"
        "比赛取消或中止不能判为no。观察窗口从2026-11-05T00:00:00Z到2031-01-01T00:00:00Z，"
        "只核验2030届这场决赛，以FIFA或参赛足协官方比赛记录核验。"
        "本次不联网，允许使用本地历史案例；没有当前球队资料就明确保留未知，不要追问私人事实。",
        "politics": "请预测2028年美国总统选举是否由上届（2024年）总统选举获胜党继续获胜。"
        "yes表示官方选举人团获胜总统的党派与2024年相同，no表示不同；不是普选票胜者或个人连任。"
        "观察窗口从2026-11-05T00:00:00Z到2029-01-21T00:00:00Z，"
        "以美国国家档案馆选举人团结果核验，未决不补成no。"
        "本次不联网，允许使用本地历史案例；缺少当前民调等资料须保留未知，不要追问私人事实。",
    }
    summaries = []
    try:
        for domain, narrative in narratives.items():
            if args.domain != "all" and args.domain != domain:
                continue
            session = services.events.create(narrative, **services.create_options())
            result_summary = dict(
                domain=domain, session_id=str(session.id), started_at=now().isoformat()
            )
            print(domain, "session", session.id, flush=True)
            try:
                state = services.assistance.prepare(
                    session.id, progress=lambda s, domain=domain: print(domain, s, flush=True)
                )
                if state.stage == "clarification":
                    services.assistance.answer(session.id, "")
                result = services.assistance.execute(
                    session.id,
                    allow_search=False,
                    progress=lambda s, domain=domain: print(domain, s, flush=True),
                )
                binding = result.reference_binding
                if (
                    binding is None
                    or binding.estimate.method_version != "case-reference-1.0"
                    or not binding.estimate.cases
                ):
                    raise AppError("真实链路未匹配案例；保留产物，验收不通过")
                report_paths = services.exports.save(result.id)
                step = services.forecasts.step(
                    services.assistance.state(session.id).forecast_request_id
                )
                assert step.reference_binding == binding
                assert services.forecasts.resume(step.id) == result
                result_summary.update(
                    status="passed",
                    forecast_id=str(result.id),
                    method_version=result.method_version,
                    delivery=result.draft.judgment.delivery,
                    cases=len(binding.estimate.cases),
                    dataset_hash=binding.estimate.dataset_hash,
                    report_paths=[str(p.resolve()) for p in report_paths],
                    conclusion=result.draft.judgment.conclusion,
                )
            except AppError as exc:
                result_summary.update(status="failed", error=str(exc))
            current = services.events.get(session.id)
            result_summary.update(
                calls=len(current.attempts),
                consumed_seconds=current.consumed_seconds,
                completed_at=now().isoformat(),
            )
            summaries.append(result_summary)
            (args.output / "summary.json").write_text(
                json.dumps(summaries, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            print(domain, result_summary["status"], flush=True)
    finally:
        if services.provider is not None:
            services.provider.close()
        services.repository.close()
    return 0 if all(s["status"] == "passed" for s in summaries) else 1


if __name__ == "__main__":
    raise SystemExit(main())
