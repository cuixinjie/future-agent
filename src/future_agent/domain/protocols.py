"""Pre-registration rules for the first prospective software-release experiment."""

from datetime import timedelta
from typing import Literal, Self
from uuid import UUID, uuid4

from pydantic import Field, HttpUrl, model_validator

from future_agent.domain.events import Contract, ForecastQuestion, Text, UtcTime, now

WINDOW_DAYS = 30
HISTORY_WINDOWS = 12
GRACE_DAYS = 7
RELEASE_RULE = (
    "在 (T0,T0+30×24小时] 内，唯一产品/通道的官方公开稳定发布至少一次记为1；"
    "排除 draft、alpha、beta、RC、nightly 和项目定义的其他预发布；补丁稳定版计入。"
    "同版本标签、公告、安装包合并，编辑旧说明不算新发布；使用官方公开发布时间。"
    "完整窗口结束且官方记录充分、无合格发布才记为0；来源缺失 pending，"
    "冲突 disputed，规则不可用 void。7天核验宽限期不延长事件发生窗口。"
)
SNAPSHOT_POLICY = "Tc封存；仅接受Tc前已获取的资料，旧发布日期不等于历史快照；禁止读入Tc后材料"
SHARED_INPUT_POLICY = (
    "A/B/C共用项目身份、官方地址和历史发布快照；B/C共享澄清答案，"
    "B不接收C档案与研究结论；C仅读取合格截止快照"
)
MISSING_POLICY = (
    "所有预登记条目保留；失败、拒绝、迟交及缺失逐条记录；概率缺失不补0或0.5，"
    "不计算该条Brier；结果未知保持pending，冲突disputed，规则不可用void"
)


class ReleaseProject(Contract):
    id: UUID = Field(default_factory=uuid4)
    name: Text
    official_url: HttpUrl
    product_channel: Text
    stable_version_rule: Text
    official_resolution_source: HttpUrl
    publication_time_rule: Text
    component_branch_rule: Text
    history_complete_windows: int = Field(ge=HISTORY_WINDOWS)
    history_source: HttpUrl
    history_coverage_statement: Text


class ExcludedProject(Contract):
    name: Text
    reason: Text


class ArmMethod(Contract):
    arm: Literal["A", "B", "C"]
    method_version: Text
    model_id: Text | None = None
    prompt_version: Text | None = None
    final_output_tokens: int = Field(default=2000, ge=1)


class ExperimentBudget(Contract):
    max_llm_calls_per_project: int = Field(ge=1)
    max_search_queries_per_project: int = Field(ge=1)
    max_pages_per_project: int = Field(ge=1)
    max_seconds_per_project: int = Field(gt=0)
    max_tokens_per_project: int = Field(gt=0)
    max_cost: float | None = Field(default=None, ge=0)


class EvaluationProtocol(Contract):
    schema_version: Literal["1.0", "2.0"] = "1.0"
    id: UUID = Field(default_factory=uuid4)
    version: int = Field(default=1, ge=1)
    status: Literal["draft", "locked"] = "draft"
    purpose: Literal["fixture", "prospective"] = "fixture"
    created_at: UtcTime = Field(default_factory=now)
    registered_at: UtcTime | None = None
    locked_at: UtcTime | None = None
    selection_source: Text
    sampling_method: Text
    projects: list[ReleaseProject] = Field(min_length=1)
    exclusions: list[ExcludedProject] = Field(default_factory=list)
    batch_id: Text
    evidence_cutoff: UtcTime
    forecast_at: UtcTime
    window_days: Literal[30] = WINDOW_DAYS
    history_windows: Literal[12] = HISTORY_WINDOWS
    grace_days: Literal[7] = GRACE_DAYS
    result_rule: Literal[RELEASE_RULE] = RELEASE_RULE
    snapshot_policy: Literal[SNAPSHOT_POLICY] = SNAPSHOT_POLICY
    shared_input_policy: Literal[SHARED_INPUT_POLICY] = SHARED_INPUT_POLICY
    methods: list[ArmMethod]
    budget: ExperimentBudget
    metrics: list[Text]
    missing_policy: Literal[MISSING_POLICY] = MISSING_POLICY
    analysis_plan: Text
    update_policy: Literal["separate_not_scored"] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    compute_comparison: Text | None = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def valid_protocol(self) -> Self:
        if self.evidence_cutoff > self.forecast_at:
            raise ValueError("Tc 必须不晚于 T0")
        if self.registered_at and self.registered_at > self.evidence_cutoff:
            raise ValueError("协议必须在证据截止时点之前预登记")
        if self.locked_at and self.locked_at > self.evidence_cutoff:
            raise ValueError("协议必须在证据截止时点之前锁定")
        if self.status == "locked" and (not self.locked_at or not self.registered_at):
            raise ValueError("锁定协议必须记录预登记与锁定时点")
        if self.status == "draft" and (self.locked_at or self.registered_at):
            raise ValueError("草稿不能冒充已经锁定/预登记")
        if self.locked_at and (
            self.created_at > self.locked_at or self.registered_at > self.locked_at
        ):
            raise ValueError("创建、预登记与锁定时点顺序矛盾")
        arms = {method.arm: method for method in self.methods}
        if len(self.methods) != 3 or set(arms) != {"A", "B", "C"}:
            raise ValueError("必须唯一登记 A/B/C 三组")
        if self.schema_version == "2.0":
            if self.update_policy is None or self.compute_comparison is None:
                raise ValueError("新版协议须预登记更新预测另存不计首次评分及额外计算预算差异")
            if (arms["C"].method_version, arms["C"].prompt_version) != (
                "judgment-2.0",
                "forecast-2.0",
            ):
                raise ValueError("新版协议必须明确登记judgment-2.0/forecast-2.0")
            if not {"delivery", "citation_errors", "human_inputs", "tokens", "elapsed"} <= set(
                self.metrics
            ):
                raise ValueError("新版协议须预登记交付、引用错误、人工输入及计算成本指标")
        elif arms["C"].method_version == "judgment-2.0":
            raise ValueError("新版方法不能混入旧协议")
        if not arms["B"].model_id or arms["B"].model_id != arms["C"].model_id:
            raise ValueError("B/C 必须使用同一明确模型版本")
        if not arms["B"].prompt_version or not arms["C"].prompt_version:
            raise ValueError("B/C 必须登记提示词版本")
        if arms["B"].final_output_tokens != arms["C"].final_output_tokens:
            raise ValueError("B/C 最终输出额度必须相同")
        if arms["A"].model_id or arms["A"].prompt_version:
            raise ValueError("A 是纯规则基线，不使用模型或提示词")
        if self.budget.max_tokens_per_project < sum(
            arms[arm].final_output_tokens for arm in ("B", "C")
        ):
            raise ValueError("总 token 预算不足以覆盖 B/C 的最终输出额度")
        ids = [project.id for project in self.projects]
        names = [project.name.casefold() for project in self.projects]
        if len(set(ids)) != len(ids) or len(set(names)) != len(names):
            raise ValueError("入选项目不能重复")
        if set(names) & {project.name.casefold() for project in self.exclusions}:
            raise ValueError("项目不能同时入选和排除")
        if not {"brier", "paired_C-A", "paired_C-B", "coverage", "cost"} <= set(self.metrics):
            raise ValueError("缺少主要误差、配对比较、覆盖率或成本指标")
        for project in self.projects:
            urls = (
                project.official_url,
                project.official_resolution_source,
                project.history_source,
            )
            if any(url.username or url.password for url in urls):
                raise ValueError("官方来源 URL 不得包含凭据")
            if self.purpose == "prospective" and any(
                url.host.endswith(".example") or url.host == "example.com" for url in urls
            ):
                raise ValueError("真实协议不能使用示例来源地址")
        if self.purpose == "prospective" and any(
            "fixture" in (method.model_id or "").lower() for method in self.methods
        ):
            raise ValueError("真实协议不能使用夹具模型标识")
        return self


def release_question(
    project: ReleaseProject, forecast_at: UtcTime, *, evidence_cutoff: UtcTime | None = None
) -> ForecastQuestion:
    return ForecastQuestion(
        question=f"{project.name} 的 {project.product_channel} 未来30天是否至少发布一次稳定版？",
        forecast_at=evidence_cutoff if evidence_cutoff is not None else forecast_at,
        window_start=forecast_at,
        window_end=forecast_at + timedelta(days=WINDOW_DAYS),
        outcome_type="binary",
        resolution_rule=f"{RELEASE_RULE}\n项目规则：{project.stable_version_rule}\n"
        f"组件/分支：{project.component_branch_rule}\n时间：{project.publication_time_rule}",
        resolution_source_policy=str(project.official_resolution_source),
    )
