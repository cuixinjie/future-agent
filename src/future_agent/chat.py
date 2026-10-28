"""Foreground terminal interaction. All business operations go through application services."""

import unicodedata
from collections.abc import Callable
from uuid import UUID

from pydantic import ValidationError

from future_agent.application.assessment import AssessmentService
from future_agent.application.assistance import AssistanceService
from future_agent.application.events import EventService
from future_agent.application.evidence import EvidenceService
from future_agent.application.forecasts import ForecastService
from future_agent.application.lifecycle import ResolutionService
from future_agent.application.ports import AppError
from future_agent.application.report_export import ReportExportService
from future_agent.application.research import ResearchService
from future_agent.domain.assessment import AssessmentReview, ReviewedEvidence
from future_agent.domain.contracts import now
from future_agent.domain.events import AnalysisSession, EventDraft, EventRevision, ForecastQuestion
from future_agent.domain.evidence import EvidenceImport, EvidenceSpan
from future_agent.domain.forecasts import Forecast, ForecastSpec, OutcomeDefinition
from future_agent.domain.lifecycle import ResolutionInput


class ChatExit(Exception):
    pass


class Navigate(Exception):
    def __init__(self, command: str) -> None:
        self.command = command


HELP = """操作指南

  常用    /continue 继续分析   /status 状态与预算   /report 查看报告
          /save 保存报告       /help 帮助           /exit 退出
  会话    /new 新建            /select 选择         /history 历史预测
  分析    /read 读取档案       /answer 补充信息     /unknown 保留未知
          /target 编辑目标     /research 研究       /forecast 预测
  管理    /freeze 冻结或修订   /resolve 登记结果    /retry 恢复中断
          /cancel 取消

直接输入文字可描述事情或补充信息；也可输入“继续、状态、报告、保存、退出、帮助”。"""


class Chat:
    def __init__(
        self,
        events: EventService,
        evidence: EvidenceService,
        assessments: AssessmentService,
        forecasts: ForecastService,
        lifecycle: ResolutionService,
        research: ResearchService,
        exports: ReportExportService,
        *,
        create_options: dict[str, int | float],
        assistance: AssistanceService | None = None,
        read: Callable[[str], str] = input,
        write: Callable[[str], None] = print,
    ) -> None:
        self.events, self.evidence, self.assessments = events, evidence, assessments
        self.forecasts, self.lifecycle, self.research, self.exports = (
            forecasts,
            lifecycle,
            research,
            exports,
        )
        self.create_options, self.read, self.write = create_options, read, write
        self.assistance = assistance
        self.sid: UUID | None = None
        self.selected: UUID | None = None

    def say(self, value: object) -> None:
        # External text must not execute terminal escape/control sequences.
        self.write(self.terminal_text(str(value)))

    @staticmethod
    def terminal_text(value: str) -> str:
        return "".join(
            c for c in value if c in "\n\t" or unicodedata.category(c) not in {"Cc", "Cf"}
        )

    def ask(self, prompt: str, *, empty: bool = False) -> str:
        while True:
            label = "" if prompt == "你" else self.terminal_text(prompt) + "\n"
            value = self.read(label + "\n> ").strip()
            if value.startswith("/"):
                command = value.lower()
                if command in {"/status", "/report", "/save", "/help"}:
                    self.dispatch(command)
                    continue
                raise Navigate(command)
            if value or empty:
                return value
            self.say("请输入内容；可随时 /exit。")

    def yes(self, prompt: str) -> bool:
        while True:
            value = self.ask(prompt + " [y/是；n/否，默认否]", empty=True).lower()
            if value in {"y", "yes", "是", "确认"}:
                return True
            if value in {"", "n", "no", "否"}:
                return False
            self.say("请明确输入 y 或 n。")

    def choose(self, prompt: str, labels: list[str]) -> int:
        for index, label in enumerate(labels, 1):
            self.say(f"  {index}. {label}")
        while True:
            value = self.ask(prompt)
            if value.isdecimal() and 1 <= int(value) <= len(labels):
                return int(value) - 1
            self.say("请输入列表中的编号。")

    def session(self) -> AnalysisSession:
        if self.sid is None:
            raise AppError("请先 /new 或 /select 选择会话。")
        return self.events.get(self.sid)

    def event(self) -> EventRevision:
        return self.events.revision(self.session().id)

    def run(self, resume: UUID | None = None) -> int:
        self.say("\n预测未来  ·  终端助手\n" + "─" * 40)
        self.say(
            "普通模式  ·  系统拟定目标、研究并判断，最多一轮三个澄清问题"
            if self.assistance
            else "高级人工模式  ·  逐项审查目标、证据和结果定义"
        )
        self.say("输入描述开始，或输入 /help 查看操作；/exit 退出。")
        self.say("模型会接收叙述、档案和引用；公开搜索词另行确认。费用未知，主观概率未经校准。")
        pending = None
        try:
            if resume:
                self.sid = self.events.get(resume).id
                self.status()
            else:
                pending = "/select" if self.events.list_sessions() else "/new"
            while True:
                try:
                    value = pending or self.ask("你")
                    pending = None
                    aliases = {
                        "继续": "/continue",
                        "状态": "/status",
                        "报告": "/report",
                        "保存": "/save",
                        "退出": "/exit",
                        "帮助": "/help",
                    }
                    value = aliases.get(value, value)
                    if value.startswith("/"):
                        self.dispatch(value)
                    elif self.sid is None:
                        self.new(value)
                    else:
                        self.answer(value)
                except Navigate as exc:
                    pending = exc.command
                except ValidationError as exc:
                    fields = [
                        ".".join(map(str, e["loc"])) or "root"
                        for e in exc.errors(include_input=False)
                    ]
                    self.say("校验失败，输入值已隐藏。请检查：" + ", ".join(fields))
                except AppError as exc:
                    self.say(str(exc))
                    self.say("已保留现有产物；可 /status、/retry 或修改输入。")
                except ValueError:
                    self.say("数据规则校验失败；请检查目标、时间、引用与结果定义。")
        except (ChatExit, EOFError):
            self.say("已退出；已完成产物保存在数据目录，未完成步骤保留原状态。")
            if self.sid:
                self.say(f"继续：future-agent chat --resume {self.sid}（使用相同的数据目录配置）")
            return 0
        except KeyboardInterrupt:
            self.say("已中断；尝试与剩余预算保留。重新打开后 /retry 检查，调用费用可能未知。")
            if self.sid:
                self.say(f"会话：{self.sid}")
            return 130

    def dispatch(self, command: str) -> None:
        actions = {
            "/help": lambda: self.say(HELP),
            "/status": self.status,
            "/new": self.new,
            "/select": self.select_session,
            "/continue": self.guide,
            "/read": self.read_event,
            "/answer": self.answer,
            "/unknown": self.keep_unknown,
            "/target": self.target,
            "/research": self.guide if self.assistance else self.search,
            "/forecast": self.guide if self.assistance else self.predict,
            "/report": self.report,
            "/save": self.save,
            "/history": self.history,
            "/freeze": self.freeze,
            "/resolve": self.resolve,
            "/retry": self.retry,
            "/cancel": self.cancel,
        }
        if command == "/exit":
            raise ChatExit
        if command not in actions:
            self.say("未知操作；输入 /help 查看支持的操作。")
            return
        actions[command]()

    def select_session(self) -> None:
        sessions = self.events.list_sessions()
        labels = [
            "新建会话",
            *[
                f"{s.created_at.astimezone().isoformat()} | {s.status} | "
                f"事件 v{s.current_revision} | {s.id}"
                for s in sessions
            ],
        ]
        choice = self.choose("选择会话（不会自动重发请求）", labels)
        if choice == 0:
            self.new()
        else:
            self.sid, self.selected = sessions[choice - 1].id, None
            self.status()

    def new(self, raw: str | None = None) -> None:
        raw = raw or self.ask("描述你想分析的事情（可分多次补充）")
        session = self.events.create(raw, **self.create_options)
        self.sid, self.selected = session.id, None
        self.say(f"叙述已保存。会话：{self.sid}")
        if self.yes(
            "在本会话预算内调用模型分析并生成判断？"
            if self.assistance
            else "调用配置的模型读取叙述，消耗本会话预算？"
        ):
            self.guide() if self.assistance else self.read_event()

    def status(self) -> None:
        s = self.session()
        self.say("\n── 会话状态 ──")
        self.say(f"会话 {s.id}\n阶段 {s.status} | 事件 v{s.current_revision}")
        attempts = [a for r in s.research_runs for a in r.attempts]
        self.say(
            f"剩余预算：模型 {max(0, s.max_calls - len(s.attempts))}/{s.max_calls} 次；"
            f"token {max(0, s.max_total_tokens - s.consumed_tokens)}；"
            f"时间 {max(0, s.max_run_seconds - s.consumed_seconds):.1f} 秒；"
            f"搜索 {max(0, s.max_search_queries - sum(a.kind == 'search' for a in attempts))} 次；"
            f"页面 {max(0, s.max_pages - sum(a.kind == 'page' for a in attempts))} 次。费用未知。"
        )
        if s.error_code:
            self.say(f"失败类别：{s.error_code}")
        if s.active_forecast_step_id:
            step = self.forecasts.step(s.active_forecast_step_id)
            self.say(f"预测步骤：{step.id} / {step.status}；/retry 检查，/cancel 取消。")
        for run in s.research_runs:
            self.say(f"研究 {run.id}：{run.status}；缺口：{'；'.join(run.gaps) or '无已记录缺口'}")
        if s.current_revision:
            self.dossier()
        if self.assistance and s.assistance:
            self.say(
                f"自动分析阶段：{s.assistance.stage}；澄清轮次：{int(s.assistance.clarification_used)}/1"
            )
            if s.assistance.stop_reason:
                self.say(s.assistance.stop_reason)
        self.say("下一步：/continue 引导；恢复会话不会自动联网或重发请求。")

    def dossier(self) -> None:
        event = self.event()
        self.say(f"档案 v{event.revision} | {'已确认' if event.confirmed else '待人工审查'}")
        for claim in event.claims:
            self.say(f"陈述 [{claim.assertion_type}/{claim.verification_status}] {claim.quote}")
        for label, items in (
            ("参与者", event.actors),
            ("时间线", event.timeline),
            ("约束", event.constraints),
            ("计划", event.planned_actions),
        ):
            self.say(f"{label}：{'；'.join(items) or '未知/未提供'}")
        for item in event.ambiguities:
            self.say(
                f"模糊项：{item.question}；备选解释：{'；'.join(item.candidate_interpretations)}"
            )
        for item in event.unknowns:
            self.say(
                f"当前未知 [{item.resolution_status}]：{item.description}；"
                f"影响：{item.impact_reason}"
            )
        if event.question:
            q = event.question
            self.say(
                f"目标：{q.question}\n类型：{q.outcome_type}；证据截止：{q.forecast_at}\n"
                f"窗口：{q.window_start} 至 {q.window_end}\n判定：{q.resolution_rule}\n"
                f"核验来源：{q.resolution_source_policy}"
            )
        if not self.assistance:
            for question in self.session().pending_questions:
                self.say(f"待澄清：{question}")

    def read_event(self) -> None:
        s = self.session()
        if s.status == "READING":
            raise AppError("读取可能中断；请 /retry 明确处理费用未知。")
        if s.current_revision and not self.yes("用全部叙述与答案重新读取，消耗剩余模型预算？"):
            return
        self.say("正在读取叙述；Ctrl+C 可中断，调用尝试已持久化。")
        self.events.read(s.id, force=bool(s.current_revision))
        self.selected = None
        self.dossier()
        self.say("可直接输入补充，或 /unknown 保留未知，/continue 核对目标。")

    def answer(self, raw: str | None = None) -> None:
        s = self.session()
        raw = raw or self.ask("补充或纠正信息（保留未知用 /unknown）")
        self.events.answer(s.id, s.current_revision, raw)
        self.selected = None
        self.say("答案已保存为新版本；旧预测保留，下游需重新评估。")
        if self.assistance:
            self.guide()
            return
        if self.yes("让模型结合答案重新整理档案？"):
            self.read_event()

    def keep_unknown(self) -> None:
        event = self.event()
        items = [u for u in event.unknowns if u.resolution_status == "open"]
        if not items:
            self.say("没有待保留的当前未知。")
            return
        index = self.choose("选择保留未知的项目", [u.description for u in items] + ["全部保留"])
        ids = [u.id for u in items] if index == len(items) else [items[index].id]
        self.events.answer(event.session_id, event.revision, "用户明确保留当前未知。", ids)
        self.selected = None
        self.say("已保留未知；没有将缺失值补成否或零。")

    def target(self) -> None:
        event = self.event()
        old = event.question.model_dump() if event.question else {}
        self.say(
            "逐项填写目标；回车保留已有值。时间使用含时区 ISO 格式，如 2026-11-01T00:00:00+08:00。"
            "证据截止不得早于拟使用资料的获取/入库时间，且不晚于窗口开始；截止后资料不会自动纳入。"
        )
        fields = {
            "question": "可核验的预测问题",
            "outcome_type": "结果类型 binary/categorical/continuous",
            "forecast_at": "证据截止时点",
            "window_start": "窗口开始",
            "window_end": "窗口结束",
            "resolution_rule": "结果发生/未发生的判定规则",
            "resolution_source_policy": "未来核验来源",
        }
        for key, label in fields.items():
            value = self.ask(f"{label} [{old.get(key) or '未填写'}]", empty=True)
            if value:
                old[key] = value
        question = ForecastQuestion.model_validate(old)
        draft = EventDraft.model_validate(event.model_dump(include=EventDraft.model_fields.keys()))
        self.events.edit(
            event.session_id,
            event.revision,
            EventDraft.model_validate({**draft.model_dump(), "question": question}),
        )
        self.selected = None
        self.say("目标已另存为新事件版本；尚需 /continue 人工确认。")

    def guide(self) -> None:
        if self.assistance:
            self.assisted_guide()
            return
        s = self.session()
        if s.status in {"READING", "FORECASTING"} or any(
            r.status == "running" for r in s.research_runs
        ):
            self.status()
            self.say("存在执行中/中断步骤，请 /retry 或 /cancel；不会自动重发。")
            return
        if not s.current_revision:
            if self.yes("读取已保存叙述？"):
                self.read_event()
            return
        event = self.event()
        if not event.confirmed:
            if not event.question or event.question.missing():
                self.target()
            self.dossier()
            if not self.yes("已审查陈述分类、未知、目标、时间及判定来源，确认档案？"):
                return
            self.events.confirm(self.sid, self.event().revision)
            self.selected = None
        choice = self.choose(
            "下一步", ["检索公开资料", "明确不检索，审查已有资料并预测", "返回，稍后继续"]
        )
        if choice == 0:
            self.search()
            if not self.yes("检索状态已展示，继续审查资料并预测？"):
                return
        elif choice == 2:
            return
        self.predict()

    def assisted_guide(self) -> None:
        self.session()
        self.say("正在整理目标与研究计划；已有分析步骤和预算会保留。")
        state = self.assistance.prepare(self.sid, progress=self.say)
        if state.stage == "search_authorization":
            self.say("研究发现新的信息缺口，拟补充以下公开查询：")
            for query in state.pending_queries:
                self.say(f"• {query.query}；目的：{query.decision_gap}")
            self.assistance.authorize_followup(self.sid, self.yes("允许发送以上新增查询？"))
            state = self.assistance.state(self.sid)
        if state.stage == "clarification":
            self.say("以下是本次唯一一轮澄清；可以一起回答，不知道或不想补充可直接回车。")
            for question in state.clarification_questions:
                self.say(f"• {question.question}\n  对判断的影响：{question.changes_judgment}")
            answer = self.ask("补充信息（回车保留未知并继续）", empty=True)
            state = self.assistance.answer(self.sid, answer)
        if state.stage == "unanswerable":
            self.say("无法判断：" + (state.stop_reason or "当前信息无法形成可核验的目标"))
            self.say("原因已保存；可主动补充目标后继续。")
            return
        if state.forecast_id:
            self.selected = state.forecast_id
            self.report()
            return
        plan = state.plan
        self.say(f"拟分析：{plan.question}\n核验规则：{plan.resolution_rule}")
        self.say("系统默认与假设：" + ("；".join(plan.assumptions) or "无新增默认"))
        if plan.action_conditions:
            self.say("行动/适用条件：" + "；".join(plan.action_conditions))
        if not plan.window_start:
            self.say(
                "窗口由系统安排在本次预算允许的生成时间之后开始，"
                f"持续 {plan.duration_days} 天；准确起止见报告。"
            )
        allow_search = False
        if state.stage == "planned" and plan.queries and self.research.search:
            self.say("拟发送的公开查询（后两条仅在重要缺口尚未解决时使用）：")
            for query in plan.queries:
                self.say(f"• {query.query}；目的：{query.decision_gap}")
            allow_search = self.yes("允许发送以上公开查询并自动读取、分析正文？")
        self.say("正在研究并形成判断；无需手工摘录或填写结果审查表。")
        try:
            result = self.assistance.execute(self.sid, allow_search=allow_search)
        except AppError:
            if self.assistance.state(self.sid).stage in {"clarification", "search_authorization"}:
                self.assisted_guide()
                return
            raise
        self.selected = result.id
        self.report()
        self.say("可 /save 保存、/freeze 冻结；后续 /resolve 登记实际结果。")

    def search(self) -> None:
        s, event = self.session(), self.event()
        subject = self.ask("允许发往搜索服务的公开对象（不要填私人叙述）")
        focus = self.ask("允许发往搜索服务的简短公开主题")
        if not self.yes(f"将公开对象“{subject}”与主题“{focus}”发往搜索服务并读取网页？"):
            return
        self.say("正在检索与读取正文；失败和候选片段会单独记录。")
        run = self.research.start(s.id, event.revision, subject, focus)
        self.say(f"研究状态：{run.status}；缺口：{'；'.join(run.gaps) or '无已记录缺口'}")

    def review_evidence(self) -> list[ReviewedEvidence]:
        event = self.event()
        selected = self.evidence.select(self.sid, event.revision, event.question.forecast_at)
        eligible = {e.document_id for e in selected if e.eligible}
        reviewed = []
        for doc in self.evidence.list_documents(self.sid, event.revision):
            self.say(f"来源：{doc.source.url} | {doc.source.access_status}")
            if doc.id not in eligible:
                reasons = next((e.reasons for e in selected if e.document_id == doc.id), [])
                self.say(f"不具备截止/去重资格：{reasons}；不作为预测正文引用。")
                continue
            text = self.evidence.snapshot(doc.id).normalized
            self.say(text)
            while self.yes("从上方正文选择一段与目标相关的精确引用？"):
                quote = self.ask("粘贴原文片段")
                start = text.find(quote)
                if start < 0 or text.find(quote, start + 1) >= 0:
                    self.say("片段未找到或出现多次，请选择唯一的较长原文片段。")
                    continue
                purpose = ["current_state", "counterevidence", "mechanism"][
                    self.choose("人工判断用途", ["当前状态", "反证", "机制"])
                ]
                reason = self.ask("人工说明：此片段为何与当前目标相关？")
                span = self.evidence.cite(
                    self.sid,
                    event.revision,
                    EvidenceSpan(
                        document_id=doc.id,
                        quote=quote,
                        start=start,
                        end=start + len(quote),
                        claim_ids=[],
                    ),
                )
                reviewed.append(
                    ReviewedEvidence(span_id=span.id, purpose=purpose, relevance_reason=reason)
                )
        return reviewed

    def outcome_spec(self) -> ForecastSpec:
        event = self.event()
        outcomes = []
        if event.question.outcome_type == "binary":
            for key, label in (("yes", "发生"), ("no", "完整窗口未发生")):
                outcomes.append(
                    OutcomeDefinition(key=key, definition=self.ask(f"结果“{label}”的精确定义"))
                )
            distribution = "exclusive_exhaustive"
        else:
            for index in range(12):
                definition = self.ask(
                    f"第 {index + 1} 个结果定义（至少两个，之后回车结束）", empty=index >= 2
                )
                if not definition:
                    break
                outcomes.append(
                    OutcomeDefinition(key=f"outcome_{index + 1}", definition=definition)
                )
            distribution = (
                "exclusive_exhaustive"
                if self.yes("这些结果互斥且穷尽所有有效结果？")
                else "independent"
            )
        for item in outcomes:
            self.say(f"{item.key}：{item.definition}")
        if not self.yes("已审查上述结果与目标、判定规则一致？"):
            raise AppError("结果定义尚未确认，未调用预测。")
        return ForecastSpec(
            outcomes=outcomes,
            distribution=distribution,
            definition_review=self.ask("填写人工审查说明（含互斥/穷尽或独立的依据）"),
        )

    def predict(self) -> None:
        event = self.event()
        if not event.confirmed:
            raise AppError("请先 /continue 确认档案。")
        reviewed = self.review_evidence()
        reviewer = self.ask("资料审查人名称")
        critical = []
        for index in range(3):
            value = self.ask(f"第 {index + 1} 个阻断数值判断的关键问题（无则回车）", empty=True)
            if not value:
                break
            critical.append(value)
        limitation = self.ask("补充资料限制或无检索原因（可回车）", empty=True)
        numeric = self.yes("显式申请未校准主观概率？默认生成无概率情景")
        assessment = self.assessments.assess(
            self.sid,
            event.revision,
            AssessmentReview(
                reviewer=reviewer,
                evidence=reviewed,
                critical_questions=critical,
                limitations=[limitation] if limitation else [],
                allow_numeric=numeric,
            ),
        )
        self.say(
            f"可用方法：{', '.join(assessment.available_methods)}\n"
            f"缺口：{'；'.join(assessment.gaps)}"
        )
        if numeric and "llm_judgment" not in assessment.available_methods:
            self.say(
                "数值资格拒绝："
                + "；".join(
                    r for m in assessment.methods if m.method == "llm_judgment" for r in m.reasons
                )
            )
            if not self.yes("明确改用无概率条件情景？"):
                return
            numeric = False
        spec = self.outcome_spec()
        if not self.yes("将已确认档案、结果定义及审查引用发往模型生成预测，消耗剩余预算？"):
            return
        self.say("正在生成预测；步骤及预算已持久化。")
        result = self.forecasts.run(self.sid, event.revision, assessment.id, spec, numeric=numeric)
        self.selected = result.id
        self.report()
        self.say("可 /save 保存，/freeze 冻结，/resolve 后续登记实际结果。")

    def all_forecasts(self) -> list[Forecast]:
        return [
            f
            for e in self.events.history(self.session().id)
            for f in self.forecasts.history(self.sid, e.revision)
        ]

    def forecast(self) -> Forecast:
        items = self.all_forecasts()
        if self.selected is not None:
            return self.forecasts.get(self.selected)
        if not items:
            raise AppError("尚无已保存预测；可 /continue 或 /forecast。")
        self.selected = max(items, key=lambda f: (f.created_at, str(f.id))).id
        return self.forecasts.get(self.selected)

    def report(self) -> None:
        self.say("\n── 预测报告 ──")
        f = self.forecast()
        if f.event.revision != self.session().current_revision:
            self.say("这是旧事件版本的历史报告，不能解释为当前目标的结果。")
        self.say(self.lifecycle.report(f.id))

    def save(self) -> None:
        paths = self.exports.save(self.forecast().id)
        self.say("已保存报告（包含问题、推断与引用，请按需保管）：\n" + "\n".join(map(str, paths)))

    def history(self) -> None:
        items = sorted(self.all_forecasts(), key=lambda f: (f.created_at, str(f.id)), reverse=True)
        if not items:
            raise AppError("尚无预测历史。")
        labels = []
        for f in items:
            frozen = self.lifecycle.frozen(f.id)
            kind = (
                "修订已冻结"
                if frozen and frozen.supersedes_forecast_id
                else "已冻结"
                if frozen
                else "草稿"
            )
            labels.append(
                f"事件 v{f.event.revision} | {kind} | {f.created_at.astimezone().isoformat()} | "
                f"{f.event.question.question}"
            )
        self.selected = items[self.choose("选择报告", labels)].id
        self.report()

    def freeze(self) -> None:
        f = self.forecast()
        if self.lifecycle.frozen(f.id):
            self.say("此预测已冻结，保留原内容。更新须新建预测后另存修订。")
            return
        previous = [self.lifecycle.frozen(p.id) for p in self.all_forecasts() if p.id != f.id]
        previous = [p for p in previous if p is not None]
        supersedes, reason, early = None, None, []
        if previous and self.yes("本次是对旧冻结预测的修订（目标/窗口/规则变化须独立冻结）？"):
            old = previous[
                self.choose(
                    "选择前驱预测",
                    [f"{p.forecast.id} | {p.forecast.event.question.question}" for p in previous],
                )
            ]
            supersedes = old.forecast.id
            reason = self.ask("修订原因")
            early = old.early_resolution_outcomes
        else:
            for outcome in f.spec.outcomes:
                if self.yes(
                    f"预先允许发生结果“{outcome.key}: {outcome.definition}”"
                    "凭窗口内发生证据提前核验？"
                ):
                    early.append(outcome.key)
        if self.yes("冻结当前预测快照（不可覆盖）？"):
            frozen = self.lifecycle.freeze(
                f.id, supersedes=supersedes, reason=reason, early_outcomes=early
            )
            self.say(f"已冻结：{frozen.frozen_at}；SHA256 {frozen.input_hash}")

    def resolve(self) -> None:
        f = self.forecast()
        if not self.lifecycle.frozen(f.id):
            raise AppError("先 /freeze 冻结，再登记结果。")
        history = self.lifecycle.history(f.id)
        for item in history:
            self.say(f"核验 #{item.sequence}：{item.status} | {item.resolver} | {item.reason}")
        status = ["pending", "resolved", "disputed", "void"][
            self.choose(
                "登记核验状态", ["未决 pending", "已核验 resolved", "争议 disputed", "作废 void"]
            )
        ]
        outcome, observed, ids = None, None, []
        if status == "resolved":
            outcome = f.spec.outcomes[
                self.choose("实际结果", [o.definition for o in f.spec.outcomes])
            ].key
            observed = self.ask("结果发生时间（含时区；完整窗口无发生可回车）", empty=True) or None
            self.say("请提供按冻结规则选取的结果来源和已读取正文；此资料仅作核验，不回填预测。")
            url = self.ask("结果来源 URL")
            publisher = self.ask("发布者")
            body = self.ask("粘贴结果正文（单行；不得用搜索片段替代正文）")
            doc = self.evidence.import_document(
                self.sid,
                f.event.revision,
                EvidenceImport(
                    url=url,
                    publisher=publisher,
                    access_status="read_success",
                    raw_text=body,
                    retrieved_at=now(),
                    origin="manual",
                ),
            )
            quote = self.ask("粘贴用于核验的精确引用")
            text = self.evidence.snapshot(doc.id).normalized
            start = text.find(quote)
            if start < 0 or text.find(quote, start + 1) >= 0:
                raise AppError("引用未找到或不唯一；资料已保存，可重新登记。")
            span = self.evidence.cite(
                self.sid,
                f.event.revision,
                EvidenceSpan(
                    document_id=doc.id,
                    quote=quote,
                    start=start,
                    end=start + len(quote),
                    claim_ids=[],
                ),
            )
            ids = [span.id]
        value = ResolutionInput(
            status=status,
            resolved_outcome=outcome,
            observed_at=observed,
            evidence_ids=ids,
            resolver=self.ask("核验人"),
            reason=self.ask("核验理由"),
        )
        if self.yes("按上述状态追加核验记录，保留旧历史？"):
            result = self.lifecycle.record(
                f.id, value, expected_previous=history[-1].id if history else None
            )
            self.say(f"核验已保存：#{result.sequence} {result.status}")

    def retry(self) -> None:
        s = self.session()
        self.status()
        if not self.yes(
            "确认旧进程已停止，接受中断结果/费用未知及可能重复费用，使用剩余预算恢复？"
        ):
            return
        if s.status == "READING":
            if self.assistance and s.assistance:
                self.events.abandon_read(s.id)
                self.guide()
            else:
                self.events.read(s.id, retry_interrupted=True)
                self.dossier()
        elif s.active_forecast_step_id:
            result = self.forecasts.resume(s.active_forecast_step_id, retry_interrupted=True)
            self.selected = result.id
            self.report()
        elif any(r.status == "running" for r in s.research_runs):
            for run in s.research_runs:
                if run.status == "running":
                    self.research.cancel(s.id, run.id, abandon=True)
            self.say("旧研究已明确终止；预算与未知费用保留，/research 可另开研究。")
        else:
            self.say("没有待恢复的中断步骤；失败读取可 /read，其他步骤可 /continue。")

    def cancel(self) -> None:
        s = self.session()
        if not self.yes("取消当前步骤并阻止后续调用，保留全部产物和预算消耗？"):
            return
        if s.active_forecast_step_id:
            step = self.forecasts.step(s.active_forecast_step_id)
            if step.status not in {"completed", "cancelled"}:
                self.forecasts.cancel(step.id)
            if s.status == "FORECASTING":
                self.forecasts.abandon(s.id)
        for run in s.research_runs:
            if run.status == "running":
                self.research.cancel(s.id, run.id, abandon=True)
        if s.status == "READING":
            self.events.abandon_read(s.id)
            self.say("读取已取消；未重发请求，旧调用费用未知。")
        self.say("已停止本次引导，不会自动调用后续步骤。")
