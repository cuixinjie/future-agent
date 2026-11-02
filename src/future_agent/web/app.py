"""Single-process local web server; HTTP is only a thin adapter over the services."""

import json
import os
import webbrowser
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from pydantic import ValidationError

from future_agent.application.assistance import AssistanceService
from future_agent.application.ports import AppError, Conflict, NotFound
from future_agent.config import Settings
from future_agent.domain.assistance import AssistanceState
from future_agent.domain.lifecycle import ResolutionInput
from future_agent.web.jobs import Job, JobRunner, job_view
from future_agent.web.services import WebServices, build_services
from future_agent.web.views import overview, session_detail, session_summary

UI_DIR = Path(__file__).parent / "ui"
MAX_BODY_BYTES = 256 * 1024
LOOPBACK = {"127.0.0.1", "localhost", "::1"}
ALLOWED_ASSETS = {"app.js", "style.css"}
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
}

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    ),
}


def _state_summary(state: AssistanceState) -> dict[str, Any]:
    return {
        "stage": state.stage,
        "forecast_id": str(state.forecast_id) if state.forecast_id else None,
    }


class WebApplication:
    """Composition retained for the server lifetime; jobs run one at a time."""

    def __init__(self, services: WebServices) -> None:
        self.services = services
        self.runner = JobRunner(max_workers=1)
        self.assistance: AssistanceService = services.assistance

    # -- jobs -----------------------------------------------------------------

    def start_job(
        self, kind: str, session_id: UUID, work: Callable[[Callable[[str], None]], dict[str, Any]]
    ) -> Job:
        if self.runner.active(session_id) is not None:
            raise Conflict("该会话已有执行中的任务；请等待完成")
        return self.runner.submit(kind, session_id, work)

    def prepare(self, session_id: UUID) -> Job:
        def work(emit: Callable[[str], None]) -> dict[str, Any]:
            state = self.assistance.prepare(session_id, progress=emit)
            return _state_summary(state)

        return self.start_job("prepare", session_id, work)

    def retry(self, session_id: UUID) -> Job:
        """Explicitly abandon an interrupted step, then re-plan with the remaining budget."""

        def work(emit: Callable[[str], None]) -> dict[str, Any]:
            session = self.services.events.get(session_id)
            if session.status == "READING":
                emit("正在终止中断的读取；旧调用费用按未知保留。")
                self.services.events.abandon_read(session_id)
            for run in session.research_runs:
                if run.status == "running":
                    emit("正在终止中断的研究。")
                    self.services.research.cancel(session_id, run.id, abandon=True)
            if session.status == "FORECASTING" and session.active_forecast_step_id:
                step = self.services.forecasts.step(session.active_forecast_step_id)
                if step.status not in {"completed", "cancelled"}:
                    emit("正在终止中断的预测步骤。")
                    self.services.forecasts.cancel(step.id)
                self.services.forecasts.abandon(session_id)
            emit("正在重新整理目标；不会自动重发已失败的调用。")
            state = self.assistance.prepare(session_id, progress=emit)
            return _state_summary(state)

        return self.start_job("retry", session_id, work)

    def execute(self, session_id: UUID, *, allow_search: bool) -> Job:
        def work(emit: Callable[[str], None]) -> dict[str, Any]:
            return self._drive(session_id, allow_search, emit)

        return self.start_job("execute", session_id, work)

    def answer(self, session_id: UUID, answer: str, *, allow_search: bool) -> Job:
        def work(emit: Callable[[str], None]) -> dict[str, Any]:
            self.assistance.answer(session_id, answer)
            return self._drive(session_id, allow_search, emit)

        return self.start_job("answer", session_id, work)

    def authorize(self, session_id: UUID, *, approved: bool, allow_search: bool) -> Job:
        def work(emit: Callable[[str], None]) -> dict[str, Any]:
            self.assistance.authorize_followup(session_id, approved)
            return self._drive(session_id, allow_search, emit)

        return self.start_job("authorize", session_id, work)

    def freeze(
        self,
        session_id: UUID,
        *,
        reason: str | None,
        supersedes: UUID | None,
        early_outcomes: list[str],
    ) -> Job:
        state = self.assistance.state(session_id)
        if state.forecast_id is None:
            raise Conflict("尚无预测可冻结")
        if reason and supersedes is None:
            raise AppError("冻结说明只用于另存修订；首次冻结不需要填写")

        def work(emit: Callable[[str], None]) -> dict[str, Any]:
            emit("正在校验证据快照并封存预测。")
            frozen = self.services.lifecycle.freeze(
                state.forecast_id,
                supersedes=supersedes,
                reason=reason,
                early_outcomes=early_outcomes,
            )
            return {
                "forecast_id": str(frozen.forecast.id),
                "frozen_at": frozen.frozen_at.isoformat(),
                "input_hash": frozen.input_hash,
            }

        return self.start_job("freeze", session_id, work)

    def resolve(
        self, session_id: UUID, value: ResolutionInput, expected_previous: UUID | None
    ) -> Job:
        state = self.assistance.state(session_id)
        if state.forecast_id is None:
            raise Conflict("尚无已冻结预测可登记核验")

        def work(emit: Callable[[str], None]) -> dict[str, Any]:
            emit("正在核对证据归属并追加核验记录。")
            resolution = self.services.lifecycle.record(
                state.forecast_id, value, expected_previous=expected_previous
            )
            return {"resolution_id": str(resolution.id), "status": resolution.status}

        return self.start_job("resolve", session_id, work)

    def export(self, forecast_id: UUID) -> Job:
        def work(emit: Callable[[str], None]) -> dict[str, Any]:
            emit("正在写入数据目录内的报告文件。")
            markdown, payload = self.services.exports.save(forecast_id)
            return {"markdown": str(markdown), "json": str(payload)}

        return self.runner.submit("export", None, work)

    def _drive(
        self, session_id: UUID, allow_search: bool, emit: Callable[[str], None]
    ) -> dict[str, Any]:
        """Mirror the assisted CLI loop without any automatic retry of failed calls."""
        state = self.assistance.prepare(session_id, progress=emit)
        for _ in range(4):
            if state.forecast_id:
                return _state_summary(state)
            if state.stage in {"clarification", "unanswerable", "search_authorization"}:
                return _state_summary(state)
            if not state.plan or not state.plan.usable():
                return _state_summary(state)
            try:
                self.assistance.execute(session_id, allow_search=allow_search, progress=emit)
            except AppError:
                paused = self.assistance.state(session_id)
                if paused.stage in {"clarification", "search_authorization"}:
                    return _state_summary(paused)
                raise
            state = self.assistance.state(session_id)
        return _state_summary(state)

    # -- lifecycle ------------------------------------------------------------

    def close(self) -> None:
        self.runner.shutdown()
        self.services.repository.close()


class Handler(BaseHTTPRequestHandler):
    application: WebApplication
    server_version = "future-agent-web"

    def log_message(self, format: str, *args: Any) -> None:
        return  # the CLI prints its own start/stop lines; keep the console quiet

    # -- plumbing -------------------------------------------------------------

    def _send(self, status: int, body: bytes, content_type: str, *, download: str | None = None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in SECURITY_HEADERS.items():
            self.send_header(key, value)
        if download:
            self.send_header("Content-Disposition", f'inline; filename="{download}"')
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, status: int, payload: Any) -> None:
        self._send(
            status, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json"
        )

    def send_error_json(
        self, status: int, message: str, *, fields: list[str] | None = None
    ) -> None:
        payload: dict[str, Any] = {"error": message}
        if fields:
            payload["fields"] = fields
        self.send_json(status, payload)

    def body(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError as exc:
            raise AppError("请求长度无效") from exc
        if length <= 0:
            return {}
        if length > MAX_BODY_BYTES:
            raise AppError("请求内容过大")
        raw = self.rfile.read(length)
        try:
            value = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AppError("请求内容不是有效 JSON") from exc
        if not isinstance(value, dict):
            raise AppError("请求内容必须是 JSON 对象")
        return value

    def _route(self, method: str) -> None:
        application = self.application
        path = urlsplit(self.path).path
        parts = [part for part in path.split("/") if part]
        try:
            if method == "GET" and not parts:
                return self.serve_static("index.html")
            if method == "GET" and len(parts) == 2 and parts[0] == "assets":
                return self.serve_asset(parts[1])
            if method == "GET" and parts == ["api", "health"]:
                return self.send_json(200, overview(application.services))
            if parts[:1] != ["api"]:
                if method == "GET" and parts[:1] != ["assets"]:
                    return self.serve_static("index.html")
                return self.send_error_json(404, "页面不存在")
            if len(parts) == 2 and parts[1] == "sessions":
                if method == "GET":
                    sessions = application.services.events.list_sessions()
                    return self.send_json(200, {"sessions": [session_summary(s) for s in sessions]})
                if method == "POST":
                    return self.create_session(application)
            if len(parts) == 3 and parts[1] == "sessions":
                session_id = self.parse_uuid(parts[2])
                if method == "GET":
                    return self.send_json(
                        200,
                        {
                            "session": session_detail(application.services, session_id),
                            "active_job": job_view(active)
                            if (active := application.runner.active(session_id))
                            else None,
                        },
                    )
                if method == "DELETE":
                    if application.runner.active(session_id):
                        raise Conflict("该会话仍有任务运行，请等待完成")
                    application.services.events.delete(session_id)
                    return self.send_json(200, {"deleted": str(session_id)})
            if len(parts) == 4 and parts[1] == "sessions" and method == "POST":
                return self.session_action(application, self.parse_uuid(parts[2]), parts[3])
            if len(parts) == 3 and parts[1] == "jobs" and method == "GET":
                return self.job_status(self.parse_uuid(parts[2]))
            if len(parts) == 4 and parts[1] == "forecasts" and parts[3] == "report.md":
                if method != "GET":
                    return self.send_error_json(405, "不支持的方法")
                return self.report_markdown(application, self.parse_uuid(parts[2]))
            if len(parts) == 4 and parts[1] == "forecasts" and parts[3] == "export":
                if method != "POST":
                    return self.send_error_json(405, "不支持的方法")
                return self.export_report(application, self.parse_uuid(parts[2]))
            return self.send_error_json(404, "接口不存在")
        except ValidationError as exc:
            fields = [".".join(map(str, item["loc"])) or "root" for item in exc.errors()]
            return self.send_error_json(422, "提交的数据不符合约定", fields=fields)
        except NotFound as exc:
            return self.send_error_json(404, str(exc) or "未找到")
        except Conflict as exc:
            return self.send_error_json(409, str(exc))
        except AppError as exc:
            return self.send_error_json(400, str(exc))
        except (ValueError, KeyError) as exc:
            return self.send_error_json(422, str(exc) or "数据校验失败")
        except OSError:
            return self.send_error_json(500, "本地文件或数据库操作失败；未覆盖历史数据")

    def parse_uuid(self, value: str) -> UUID:
        try:
            return UUID(value)
        except ValueError as exc:
            raise NotFound("标识格式不正确") from exc

    # -- routes ---------------------------------------------------------------

    def serve_static(self, name: str) -> None:
        path = UI_DIR / name
        if not path.is_file():
            return self.send_error_json(404, "页面不存在")
        self._send(
            200, path.read_bytes(), CONTENT_TYPES.get(path.suffix, "application/octet-stream")
        )

    def serve_asset(self, name: str) -> None:
        if name not in ALLOWED_ASSETS:
            return self.send_error_json(404, "资源不存在")
        path = UI_DIR / name
        self._send(200, path.read_bytes(), CONTENT_TYPES.get(path.suffix, "text/plain"))

    def create_session(self, application: WebApplication) -> None:
        body = self.body()
        text = str(body.get("text") or "").strip()
        if len(text) < 10:
            raise AppError("请至少写清楚对象、时间和可核验的结果")
        session = application.services.events.create(
            text, request_key=f"web:{uuid4()}", **application.services.create_options()
        )
        self.send_json(201, {"session": session_detail(application.services, session.id)})

    def session_action(self, application: WebApplication, session_id: UUID, action: str) -> None:
        if action == "cancel-forecast":
            session = application.services.events.get(session_id)
            if session.status != "FORECASTING" or not session.active_forecast_step_id:
                raise Conflict("当前没有可停止的预测步骤")
            application.services.forecasts.cancel(session.active_forecast_step_id)
            return self.send_json(
                200,
                {"message": "已请求停止；等待当前模型请求结束，迟到结果将丢弃，费用仍可能产生。"},
            )
        if action == "prepare":
            job = application.prepare(session_id)
        elif action == "retry":
            job = application.retry(session_id)
        elif action == "execute":
            body = self.body()
            job = application.execute(session_id, allow_search=bool(body.get("allow_search")))
        elif action == "answer":
            body = self.body()
            if not isinstance(body.get("answer", ""), str):
                raise AppError("补充信息必须是文本")
            job = application.answer(
                session_id,
                str(body.get("answer") or ""),
                allow_search=bool(body.get("allow_search")),
            )
        elif action == "authorize":
            body = self.body()
            approved = body.get("approved")
            if not isinstance(approved, bool):
                raise AppError("请明确是否允许发送补充查询")
            job = application.authorize(
                session_id, approved=approved, allow_search=bool(body.get("allow_search"))
            )
        elif action == "freeze":
            body = self.body()
            supersedes = body.get("supersedes")
            early = body.get("early_outcomes") or []
            if not isinstance(early, list) or not all(isinstance(item, str) for item in early):
                raise AppError("提前核验结果必须是结果键列表")
            reason = str(body.get("reason") or "").strip() or None
            job = application.freeze(
                session_id,
                reason=reason,
                supersedes=self.parse_uuid(str(supersedes)) if supersedes else None,
                early_outcomes=early,
            )
        elif action == "resolve":
            body = self.body()
            expected = body.get("expected_previous")
            payload = {
                key: body[key]
                for key in (
                    "status",
                    "resolved_outcome",
                    "evidence_ids",
                    "resolver",
                    "reason",
                    "observed_at",
                )
                if body.get(key) not in (None, "")
            }
            value = ResolutionInput.model_validate(payload)
            job = application.resolve(
                session_id, value, self.parse_uuid(str(expected)) if expected else None
            )
        else:
            raise NotFound("操作不存在")
        self.send_json(202, {"job": job_view(job)})

    def job_status(self, job_id: UUID) -> None:
        job = self.application.runner.get(job_id)
        if job is None:
            raise NotFound("任务不存在")
        payload: dict[str, Any] = {"job": job_view(job)}
        if job.session_id is not None:
            payload["job"]["session_status"] = self.application.services.events.get(
                job.session_id
            ).status
        if job.session_id is not None and job.status in {"done", "failed"}:
            try:
                payload["session"] = session_detail(self.application.services, job.session_id)
            except AppError:
                payload["session"] = None
        self.send_json(200, payload)

    def report_markdown(self, application: WebApplication, forecast_id: UUID) -> None:
        report = application.services.lifecycle.report(forecast_id)
        self._send(
            200,
            report.encode("utf-8"),
            "text/markdown; charset=utf-8",
            download=f"forecast-{forecast_id}.md",
        )

    def export_report(self, application: WebApplication, forecast_id: UUID) -> None:
        job = application.runner.submit(
            "export",
            None,
            lambda emit: self._export(application, forecast_id, emit),
        )
        self.send_json(202, {"job": job_view(job)})

    @staticmethod
    def _export(
        application: WebApplication, forecast_id: UUID, emit: Callable[[str], None]
    ) -> dict[str, Any]:
        emit("正在写入数据目录内的报告文件。")
        markdown, payload = application.services.exports.save(forecast_id)
        return {"markdown": str(markdown), "json": str(payload)}

    def do_GET(self) -> None:
        self._route("GET")

    def do_POST(self) -> None:
        self._route("POST")

    def do_PUT(self) -> None:
        self.send_error_json(405, "不支持的方法")

    def do_DELETE(self) -> None:
        self._route("DELETE")


def _drop_bracketed_no_proxy() -> None:
    """Remove bracketed IPv6 NO_PROXY entries rejected by httpx."""
    for name in ("NO_PROXY", "no_proxy"):
        value = os.environ.get(name)
        if not value:
            continue
        kept = [
            item for item in value.split(",") if item.strip() and not item.strip().startswith("[")
        ]
        os.environ[name] = ",".join(kept)


def run_web(
    settings: Settings,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    open_browser: bool = True,
) -> None:
    if host not in LOOPBACK:
        raise AppError("Web 界面仅允许监听本机回环地址")
    if not 1 <= port <= 65535:
        raise AppError("端口必须在 1-65535 之间")
    _drop_bracketed_no_proxy()
    services = build_services(settings)
    application = WebApplication(services)
    handler = type("BoundHandler", (Handler,), {"application": application})
    try:
        server = ThreadingHTTPServer((host, port), handler)
    except OSError as exc:
        application.close()
        raise AppError(f"无法监听 {host}:{port}；端口可能已被占用") from exc
    server.daemon_threads = True
    url = f"http://{host}:{port}/"
    print(f"预测助手 Web 已启动：{url}")
    print(f"数据目录：{settings.data_dir.resolve()}")
    if services.provider is None:
        print("模型未配置：可以保存原文，但无法生成分析与判断。")
    print("按 Ctrl+C 停止服务。")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止。")
    finally:
        server.server_close()
        application.close()
