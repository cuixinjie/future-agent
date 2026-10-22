"""CLI protocol conversion and composition root for M1 application services."""

import argparse
import json
import sys
import time
from pathlib import Path
from uuid import UUID

from alembic.util.exc import CommandError
from pydantic import BaseModel, TypeAdapter, ValidationError
from sqlalchemy.exc import SQLAlchemyError

from future_agent.application.assessment import AssessmentService
from future_agent.application.events import EventService
from future_agent.application.evidence import EvidenceService
from future_agent.application.forecasts import ForecastService
from future_agent.application.lifecycle import ResolutionService
from future_agent.application.ports import AppError, LLMProvider
from future_agent.application.protocols import ProtocolService
from future_agent.application.research import ResearchService, SearchPlanner
from future_agent.config import Settings, load_settings
from future_agent.domain.assessment import AssessmentReview, EvidenceAssessment
from future_agent.domain.events import EventDraft, EventRevision, UtcTime
from future_agent.domain.evidence import EvidenceImport, EvidenceSpan
from future_agent.domain.forecasts import Forecast, ForecastDraft, ForecastSpec
from future_agent.domain.lifecycle import ForecastStep, FrozenForecast, Resolution, ResolutionInput
from future_agent.domain.protocols import EvaluationProtocol, ReleaseProject, release_question
from future_agent.providers.compatible import CompatibleProvider
from future_agent.providers.materials import JsonMaterialImporter
from future_agent.providers.searxng import SearXNGProvider
from future_agent.providers.web import StaticPageReader
from future_agent.storage.snapshots import FileSnapshotStore
from future_agent.storage.sqlite import SQLiteRepository


def emit(value: BaseModel | dict | list) -> None:
    if isinstance(value, BaseModel):
        print(value.model_dump_json(indent=2))
    else:
        print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def read_text(path: Path) -> str:
    # newline="" preserves original newlines for the source object.
    with path.open(encoding="utf-8", newline="") as stream:
        return stream.read()


def event_draft(revision: EventRevision) -> EventDraft:
    return EventDraft.model_validate(revision.model_dump(include=EventDraft.model_fields.keys()))


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="future-agent",
        description="预测未来智能体：M1 事件档案、实验协议及 M2 证据检索。",
        epilog="模型读取和预测会发送档案及引用；research run 将公开对象和主题发往搜索服务。",
    )
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--no-env", action="store_true", help="不读取 .env（仍读取进程环境变量）")
    parser.add_argument("--data-dir", type=Path, help="批准的本地数据目录")
    commands = parser.add_subparsers(dest="command")
    doctor_command = commands.add_parser("doctor", help="配置诊断；默认不联网")
    doctor_command.add_argument(
        "--live", action="store_true", help="最多两次真实模型调用，可能产生费用"
    )
    schema = commands.add_parser("schema", help="输出 JSON Schema")
    schema.add_argument(
        "kind",
        choices=[
            "draft",
            "protocol",
            "evidence",
            "span",
            "review",
            "assessment",
            "forecast-spec",
            "forecast-draft",
            "forecast",
            "frozen-forecast",
            "resolution-input",
            "resolution",
            "forecast-step",
        ],
    )
    for name in ("create", "analyze"):
        command = commands.add_parser(
            name, help="保存原文" if name == "create" else "保存原文并调用模型读取"
        )
        command.add_argument("--input", type=Path, required=True)
        command.add_argument("--request-key")
    for name in ("show", "history", "resume", "answer", "edit", "confirm"):
        command = commands.add_parser(name)
        command.add_argument("session_id", type=UUID)
        if name == "show":
            command.add_argument("--revision", type=int)
            command.add_argument("--editable", action="store_true")
            command.add_argument("--sources", action="store_true", help="显式显示私人原文和答案")
        if name == "resume":
            command.add_argument("--read", action="store_true", help="用已保存的原文和答案重新读取")
            command.add_argument(
                "--retry-interrupted",
                action="store_true",
                help="接受上次调用费用未知，继续使用剩余预算",
            )
        if name in ("answer", "edit", "confirm"):
            command.add_argument("--expected-revision", required=True, type=int)
        if name in ("answer", "edit"):
            command.add_argument("--input", required=True, type=Path)
        if name == "edit":
            command.add_argument("--fixture", action="store_true", help="明确标注为固定夹具导入")
        if name == "answer":
            command.add_argument("--keep-unknown", type=UUID, action="append", default=[])
    release = commands.add_parser("release-template", help="生成30天稳定版本发布问题草稿")
    release.add_argument("--project", type=Path, required=True)
    release.add_argument("--forecast-at", required=True)
    protocol = commands.add_parser("protocol", help="实验协议草稿与预登记锁定")
    sub = protocol.add_subparsers(dest="operation", required=True)
    for name in ("create", "validate", "edit"):
        command = sub.add_parser(name)
        command.add_argument("--input", type=Path, required=True)
        if name == "edit":
            command.add_argument("--expected-version", type=int, required=True)
    for name in ("show", "lock"):
        command = sub.add_parser(name)
        command.add_argument("protocol_id", type=UUID)
        if name == "lock":
            command.add_argument("--expected-version", type=int, required=True)
    evidence = commands.add_parser("evidence", help="资料导入、正文快照、引用及截止筛选（不联网）")
    evidence_sub = evidence.add_subparsers(dest="operation", required=True)
    for name in ("import", "list", "cite", "select"):
        command = evidence_sub.add_parser(name)
        command.add_argument("session_id", type=UUID)
        command.add_argument("--revision", required=True, type=int)
        if name in ("import", "cite"):
            command.add_argument("--input", required=True, type=Path)
        if name == "select":
            command.add_argument("--cutoff", required=True)
            command.add_argument(
                "--allow-fixtures", action="store_true", help="仅离线验收，非真实实验"
            )
    for name in ("show", "text", "spans"):
        command = evidence_sub.add_parser(name)
        command.add_argument("document_id", type=UUID)
    research = commands.add_parser("research", help="公开对象检索、网页读取与持久化预算")
    sub = research.add_subparsers(dest="operation", required=True)
    for name in ("plan", "run", "show", "cancel", "abandon"):
        command = sub.add_parser(name)
        command.add_argument("session_id", type=UUID)
        if name in ("plan", "run"):
            command.add_argument("--revision", type=int, required=True)
            command.add_argument("--subject", required=True, help="允许发给搜索服务的公开对象")
            command.add_argument("--focus", required=True, help="与预测目标相关的公开检索主题")
        if name in ("cancel", "abandon"):
            command.add_argument("run_id", type=UUID)
    assessment = commands.add_parser("assessment", help="数据条件评估及方法资格（不联网）")
    sub = assessment.add_subparsers(dest="operation", required=True)
    for name in ("run", "show"):
        command = sub.add_parser(name)
        command.add_argument("session_id", type=UUID)
        command.add_argument("--revision", type=int, required=True)
        if name == "run":
            command.add_argument("--input", type=Path, required=True, help="显式人工引用审查 JSON")
            command.add_argument("--allow-fixtures", action="store_true", help="仅离线验收")
    forecast = commands.add_parser("forecast", help="情景/未校准主观预测及草稿报告")
    sub = forecast.add_subparsers(dest="operation", required=True)
    run = sub.add_parser("run")
    run.add_argument("session_id", type=UUID)
    run.add_argument("--revision", type=int, required=True)
    run.add_argument("--assessment-id", type=UUID, required=True)
    run.add_argument("--input", type=Path, required=True, help="人工审查的结果定义 JSON")
    run.add_argument("--numeric", action="store_true", help="显式请求数值，仍须评估资格通过")
    run.add_argument("--request-id", type=UUID, help="调用方生成的步骤幂等 UUID，崩溃后可恢复")
    for name in ("step", "cancel", "resume"):
        command = sub.add_parser(name)
        command.add_argument("step_id", type=UUID)
        if name == "resume":
            command.add_argument(
                "--retry-interrupted",
                action="store_true",
                help="确认结果/费用可能未知，允许再次调用且不重置预算",
            )
    for name in ("freeze", "frozen"):
        command = sub.add_parser(name)
        command.add_argument("forecast_id", type=UUID)
        if name == "freeze":
            command.add_argument("--supersedes", type=UUID)
            command.add_argument("--reason")
            command.add_argument(
                "--early-outcome",
                action="append",
                default=[],
                help="预先人工确认可提前核验的发生结果键；缺省须等完整窗口",
            )
    resolution = commands.add_parser("resolution", help="追加手动核验及查看完整历史")
    resolution_sub = resolution.add_subparsers(dest="operation", required=True)
    for name in ("record", "history"):
        command = resolution_sub.add_parser(name)
        command.add_argument("forecast_id", type=UUID)
        if name == "record":
            command.add_argument("--input", type=Path, required=True)
            command.add_argument("--expected-previous", type=UUID, help="最新核验 ID；首次登记省略")
    for name in ("show", "report"):
        command = sub.add_parser(name)
        command.add_argument("forecast_id", type=UUID)
    history = sub.add_parser("history", help="读取指定事件版本的历史草稿")
    history.add_argument("session_id", type=UUID)
    history.add_argument("--revision", type=int, required=True)
    abandon = sub.add_parser("abandon", help="确认中断费用未知，保留预算并解锁")
    abandon.add_argument("session_id", type=UUID)
    return parser


def doctor(settings: Settings, live: bool, provider: LLMProvider | None) -> None:
    result = {
        "configuration": "ready" if settings.model_ready() else "model_not_configured",
        "structured_mode": settings.llm_structured_mode,
        "structured_guarantee": "schema_requested_local_validation_required"
        if settings.llm_structured_mode == "json_schema"
        else "text_json_local_validation_only",
        "database_path_valid": bool(settings.database_path()),
        "model_service": "not_tested",
        "search_service": "not_tested" if settings.search_base_url else "not_configured",
        "cost": None,
    }
    if live:
        if provider is None:
            raise AppError("真实诊断需要 LLM_BASE_URL 和 LLM_MODEL")
        if settings.max_llm_calls < 2:
            raise AppError("真实诊断需要两次调用预算")
        probe = {
            "type": "object",
            "properties": {"ok": {"type": "boolean"}},
            "required": ["ok"],
            "additionalProperties": False,
        }
        usages = []
        remaining_tokens = settings.max_total_tokens
        deadline = time.monotonic() + settings.max_run_seconds
        for schema in ({}, probe):
            allowance = len(json.dumps(schema).encode()) + 100 + provider.output_token_limit
            if remaining_tokens < allowance or time.monotonic() >= deadline:
                raise AppError("真实诊断预算不足，停止新增调用")
            reply = provider.generate(
                'Return only JSON: {"ok":true}',
                "Probe",
                schema,
                min(30, deadline - time.monotonic()),
            )
            probe_result = json.loads(reply.content)
            if (
                not isinstance(probe_result, dict)
                or set(probe_result) != {"ok"}
                or probe_result["ok"] is not True
            ):
                raise AppError("真实诊断返回格式未通过检查")
            usages.append(
                {"input_tokens": reply.input_tokens, "output_tokens": reply.output_tokens}
            )
            remaining_tokens -= (
                reply.input_tokens + reply.output_tokens
                if reply.input_tokens is not None and reply.output_tokens is not None
                else allowance
            )
            if remaining_tokens < 0 or time.monotonic() > deadline:
                raise AppError("真实诊断已超出预算，停止新增调用")
        result.update(model_service="tested_this_configuration", calls=2, usage=usages)
    emit(result)


def execute(
    args: argparse.Namespace,
    settings: Settings,
    service: EventService,
    protocols: ProtocolService,
) -> None:
    if args.command in ("create", "analyze"):
        session = service.create(
            read_text(args.input),
            request_key=args.request_key,
            max_calls=settings.max_llm_calls,
            max_run_seconds=settings.max_run_seconds,
            max_total_tokens=settings.max_total_tokens,
            max_search_queries=settings.max_search_queries,
            max_pages=settings.max_pages,
        )
        print(f"session_id={session.id}", file=sys.stderr)
        if args.command == "analyze":
            session = service.read(session.id)
        emit(session.model_dump(exclude={"sources"}, mode="json"))
    elif args.command == "show":
        session = service.get(args.session_id)
        if args.sources:
            emit([source.model_dump(mode="json") for source in session.sources])
        elif session.current_revision:
            revision = service.revision(session.id, args.revision)
            emit(event_draft(revision) if args.editable else revision)
        else:
            emit(session.model_dump(exclude={"sources"}, mode="json"))
    elif args.command == "history":
        emit([revision.model_dump(mode="json") for revision in service.history(args.session_id)])
    elif args.command == "resume":
        session = service.get(args.session_id)
        if (
            args.read
            or (session.current_revision == 0 and session.status != "READING")
            or args.retry_interrupted
        ):
            session = service.read(
                session.id, force=args.read, retry_interrupted=args.retry_interrupted
            )
        emit(session.model_dump(exclude={"sources"}, mode="json"))
    elif args.command == "edit":
        draft = EventDraft.model_validate_json(read_text(args.input))
        emit(
            service.edit(
                args.session_id,
                args.expected_revision,
                draft,
                origin="fixture" if args.fixture else "manual",
            ).model_dump(exclude={"sources"}, mode="json")
        )
    elif args.command == "answer":
        emit(
            service.answer(
                args.session_id, args.expected_revision, read_text(args.input), args.keep_unknown
            ).model_dump(exclude={"sources"}, mode="json")
        )
    elif args.command == "confirm":
        emit(
            service.confirm(args.session_id, args.expected_revision).model_dump(
                exclude={"sources"}, mode="json"
            )
        )
    elif args.command == "protocol":
        if args.operation in ("create", "validate", "edit"):
            protocol = EvaluationProtocol.model_validate_json(read_text(args.input))
            if args.operation == "create":
                protocol = protocols.create(protocol)
            elif args.operation == "edit":
                protocol = protocols.edit(protocol, args.expected_version)
            emit(protocol)
        elif args.operation == "show":
            emit(protocols.get(args.protocol_id))
        else:
            emit(protocols.lock(args.protocol_id, args.expected_version))


def main() -> int:
    parser = make_parser()
    args = parser.parse_args()
    if args.command is None:
        parser.print_help()
        return 0
    repo = provider = None
    try:
        if args.command == "schema":
            contracts = {
                "draft": EventDraft,
                "protocol": EvaluationProtocol,
                "evidence": EvidenceImport,
                "span": EvidenceSpan,
                "review": AssessmentReview,
                "assessment": EvidenceAssessment,
                "forecast-spec": ForecastSpec,
                "forecast-draft": ForecastDraft,
                "forecast": Forecast,
                "frozen-forecast": FrozenForecast,
                "resolution-input": ResolutionInput,
                "resolution": Resolution,
                "forecast-step": ForecastStep,
            }
            emit(contracts[args.kind].model_json_schema())
            return 0
        if args.command == "release-template":
            emit(
                release_question(
                    ReleaseProject.model_validate_json(read_text(args.project)),
                    TypeAdapter(UtcTime).validate_python(args.forecast_at),
                )
            )
            return 0
        settings = load_settings(None if args.no_env else args.env_file)
        if args.data_dir:
            settings.data_dir = args.data_dir
        needs_provider = (
            args.command == "analyze"
            or args.command == "resume"
            or (args.command == "doctor" and args.live)
            or (args.command == "forecast" and args.operation in {"run", "resume"})
        )
        if needs_provider and settings.model_ready():
            provider = CompatibleProvider(settings)
        if args.command == "doctor":
            doctor(settings, args.live, provider)
            return 0
        repo = SQLiteRepository(settings.database_path())
        repo.migrate()
        if args.command in {"forecast", "resolution"}:
            evidence = EvidenceService(repo, repo, FileSnapshotStore(settings.data_dir))
            forecasts = ForecastService(
                repo, repo, AssessmentService(repo, repo, evidence), provider
            )
            lifecycle = ResolutionService(repo, repo, evidence)
            if args.command == "resolution":
                if args.operation == "history":
                    emit([r.model_dump(mode="json") for r in lifecycle.history(args.forecast_id)])
                else:
                    emit(
                        lifecycle.record(
                            args.forecast_id,
                            ResolutionInput.model_validate_json(read_text(args.input)),
                            expected_previous=args.expected_previous,
                        )
                    )
                return 0
            if args.operation == "run":
                emit(
                    forecasts.run(
                        args.session_id,
                        args.revision,
                        args.assessment_id,
                        ForecastSpec.model_validate_json(read_text(args.input)),
                        numeric=args.numeric,
                        request_id=args.request_id,
                    )
                )
            elif args.operation == "step":
                emit(repo.step(args.step_id))
            elif args.operation == "cancel":
                emit(forecasts.cancel(args.step_id))
            elif args.operation == "resume":
                emit(forecasts.resume(args.step_id, retry_interrupted=args.retry_interrupted))
            elif args.operation == "freeze":
                emit(
                    lifecycle.freeze(
                        args.forecast_id,
                        supersedes=args.supersedes,
                        reason=args.reason,
                        early_outcomes=args.early_outcome,
                    )
                )
            elif args.operation == "frozen":
                emit(repo.frozen(args.forecast_id))
            elif args.operation == "history":
                emit(
                    [
                        f.model_dump(mode="json")
                        for f in forecasts.history(args.session_id, args.revision)
                    ]
                )
            elif args.operation == "abandon":
                forecasts.abandon(args.session_id)
                emit({"status": "abandoned", "cost": None})
            else:
                if args.operation == "report":
                    print(lifecycle.report(args.forecast_id), end="")
                else:
                    emit(forecasts.get(args.forecast_id))
            return 0
        if args.command == "assessment":
            evidence = EvidenceService(repo, repo, FileSnapshotStore(settings.data_dir))
            assessment = AssessmentService(repo, repo, evidence)
            if args.operation == "run":
                emit(
                    assessment.assess(
                        args.session_id,
                        args.revision,
                        AssessmentReview.model_validate_json(read_text(args.input)),
                        experiment=not args.allow_fixtures,
                    )
                )
            else:
                emit(
                    [
                        v.model_dump(mode="json")
                        for v in assessment.history(args.session_id, args.revision)
                    ]
                )
            return 0
        if args.command == "evidence":
            evidence = EvidenceService(repo, repo, FileSnapshotStore(settings.data_dir))
            execute_evidence(args, evidence)
            return 0
        if args.command == "research":
            evidence = EvidenceService(repo, repo, FileSnapshotStore(settings.data_dir))
            research = ResearchService(
                repo,
                evidence,
                SearXNGProvider(settings)
                if args.operation == "run" and settings.search_base_url
                else None,
                StaticPageReader() if args.operation == "run" else None,
            )
            if args.operation == "plan":
                emit(
                    SearchPlanner().plan(
                        EventService(repo).revision(args.session_id, args.revision),
                        args.subject,
                        args.focus,
                    )
                )
            elif args.operation == "run":
                result = research.start(args.session_id, args.revision, args.subject, args.focus)
                emit(result)
                return 0 if result.status == "completed" else 2
            elif args.operation == "show":
                emit([r.model_dump(mode="json") for r in research.runs(args.session_id)])
            else:
                emit(
                    research.cancel(
                        args.session_id, args.run_id, abandon=args.operation == "abandon"
                    )
                )
            return 0
        execute(args, settings, EventService(repo, provider), ProtocolService(repo))
        return 0
    except ValidationError as exc:
        fields = [
            ".".join(map(str, item["loc"])) or "root" for item in exc.errors(include_input=False)
        ]
        print("校验失败（输入值已隐藏），请检查字段：" + ", ".join(fields), file=sys.stderr)
        return 2
    except (AppError, ValueError) as exc:
        print(
            str(exc) if isinstance(exc, AppError) else "数据规则校验失败，请检查引用、目标和时间。",
            file=sys.stderr,
        )
        return 2
    except (OSError, SQLAlchemyError, CommandError):
        print("本地文件或数据库操作失败；未清理或覆盖历史数据。", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("执行已中断；已保存产物与调用尝试，可通过 resume 查看。", file=sys.stderr)
        return 130
    finally:
        if provider:
            provider.close()
        if repo:
            repo.close()


def execute_evidence(args: argparse.Namespace, service: EvidenceService) -> None:
    if args.operation == "import":
        emit(
            service.import_from(
                args.session_id,
                args.revision,
                JsonMaterialImporter(args.input),
            )
        )
    elif args.operation == "cite":
        emit(
            service.cite(
                args.session_id,
                args.revision,
                EvidenceSpan.model_validate_json(read_text(args.input)),
            )
        )
    elif args.operation == "list":
        emit(
            [
                item.model_dump(mode="json")
                for item in service.list_documents(
                    args.session_id,
                    args.revision,
                )
            ]
        )
    elif args.operation == "select":
        emit(
            [
                item.model_dump(mode="json")
                for item in service.select(
                    args.session_id,
                    args.revision,
                    TypeAdapter(UtcTime).validate_python(args.cutoff),
                    experiment=not args.allow_fixtures,
                )
            ]
        )
    elif args.operation == "text":
        emit(service.snapshot(args.document_id))
    elif args.operation == "spans":
        emit([item.model_dump(mode="json") for item in service.spans(args.document_id)])
    else:
        emit(service.document(args.document_id))
