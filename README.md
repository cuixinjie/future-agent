# 预测未来智能体 · Future Agent

描述一件事，系统负责构建可研究的问题、调查证据并形成可追溯的判断。采用 Python 3.12 和模块化单体，本地单用户、CLI 优先。

**当前软件版本：`0.1.0a1`（Alpha 实验版）**，设计文档版本为 0.3。适合本地试用、开发与实验准备，预测效果尚未验证。普通预测主线 R01–R05、M1/M2、M3-T09/T10/T11 和 M5 交互式 CLI 已实现；R06-B 新版独立实验适配已有固定夹具验收，R06-A 真实服务与语义验收尚未全部完成。

## 能做什么

- 从自然语言叙述构建预测目标、观察窗口和核验规则，整个会话最多一轮三个澄清问题。
- 经授权检索公开资料，保存正文快照与精确引用，区分证据、推断、假设和未知。
- 按依据输出主观概率、方向判断、条件分析、未来探索或无法判断，保留失败原因和预算记录。
- 恢复会话，导出 Markdown/JSON 报告，冻结预测并追加修订及手动核验。
- 使用独立协议比较发布频率基线、同模型直接预测和完整流程，分别统计覆盖与适用评分。

API、Web 界面、后台任务、自动监测和自动训练尚未实现。首个计划中的前瞻效果实验是公开软件项目未来 30 天是否发布稳定版本，目前尚未运行。

## 已知限制

最近完整离线测试 **424 项通过**，但不代表真实模型质量或预测准确率。2026-10-29 的独立试运行中，公网 DNS 解析正常，真实正文获取、引用抽查和报告生成走通；搜索上游仍有失败，研究因配额耗尽仅部分完成，真实语义质量与预测效果仍需进一步验证。程序不会放宽公网检查，也不会把通道失败伪装成成功预测。详见 [复验记录](docs/reference/15-R06真实链路复验-2026-10-29.md)。

请先核对 [Alpha 发布说明](docs/releases/0.1.0a1.md) 和 [R06 验收记录](docs/reference/14-R06验收与新版实验.md)。

## 安装与开始使用

需要 Git、uv 和 Python 3.12。仓库的 `.python-version` 与 `pyproject.toml` 限定 Python 版本。在项目根目录执行：

```powershell
git clone https://github.com/cuixinjie/future-agent.git
cd future-agent
uv sync --locked
if (-not (Test-Path -LiteralPath .env)) { Copy-Item -LiteralPath .env.example -Destination .env }
# 编辑 .env，设置模型地址、模型名称和密钥；搜索服务单独配置
uv run --locked future-agent doctor
uv run --locked future-agent chat
```

仅在尚无 `.env` 时复制模板，保留已有配置。`uv sync --locked` 首次可能下载依赖和解释器。`doctor` 默认检查配置、不联网；`doctor --live` 最多发出两次真实模型探测，可能产生费用。模型通道与搜索/网页通道分别验证。

模型使用 `LLM_BASE_URL`、`LLM_MODEL`、`LLM_API_KEY`；叙述、答案与分析资料会发送至该服务。搜索使用独立的 `SEARCH_BASE_URL`。可按 [本地 SearXNG 部署说明](deploy/searxng/README.md) 配置服务；不配置搜索时会明确记录无检索限制，不生成模拟证据。凭据放在已忽略的 `.env`，不写入代码或报告。

## 普通预测与报告

普通路径：描述事情 → 系统拟定目标和显式假设 → 必要时一轮澄清 → 授权公开查询 → 自动研究、引用和判断 → 查看、保存报告。

整个普通会话最多一轮三个澄清问题，可以跳过，恢复和主动补充不重置额度。用户无需填写 ISO 时间、摘录网页或逐项审查结果定义。相对窗口在剩余生成预算之后开始，报告显示准确起止；已开始的固定窗口不能悄悄移动。

有依据时尝试未校准主观概率；没有数字时区分方向判断、条件分析、未来探索和无法判断。执行完成不等于形成了预测，也不证明预测准确。

```powershell
uv run --locked future-agent chat --resume <会话ID>
# 高级人工模式及原分步命令继续保留
uv run --locked future-agent chat --manual
uv run --locked future-agent --help
```

常用操作：`/continue`、`/status`、`/report`、`/save`、`/history`、`/freeze`、`/resolve`、`/exit`。恢复先展示状态，不自动重发模型请求；中断保留尝试及预算，重试需处理可能重复费用。冻结不可覆盖，修订另存。

`/save` 将 Markdown 和裁剪后的 JSON 保存至 `DATA_DIR/reports/<随机ID>/`，展示绝对路径，不覆盖历史文件。新预测导出 `forecast_report/1.1`，旧预测保留 `1.0`；排除完整原文、整页缓存与凭据，但问题和引用仍可能包含私人信息。目标无法定义时原因保存在会话状态，不生成假预测报告。

完整操作见 [交互式 CLI](docs/guides/交互式CLI.md)，脚本、人工审查和实验命令见 [分步 CLI](docs/guides/分步CLI.md)。

## 数据、兼容与实验

私人输入、数据库、正文快照与报告默认放在已忽略的 `data/`。`DATABASE_URL` 仅支持批准 `DATA_DIR` 内的 SQLite 文件，默认 `DATA_DIR/app.db`。进程环境变量覆盖 `.env`；`--env-file` 选择配置文件，`--no-env` 禁用文件读取。

当前迁移头为 `0006_evaluation`。会话 Schema 1.5 可读取 1.0–1.4；普通预测的 Spec/Draft/Forecast 为 2.0，旧预测 1.0 与冻结哈希保留。升级前停止进程并备份完整 DATA_DIR；回退恢复备份，不只修改版本号。新版具体边界见 [改造决策](docs/decisions/预测主线改造.md)。

T11 提供 A/B/C 封存、执行、手动核验和评分工具。旧 C 组锁定 `judgment-1.0`；新版 `judgment-2.0` 已有独立 Schema 2.0 协议及 spec 快照适配，不能混入旧协议。真实独立比较仍须预登记新批次，冻结首次预测并等待完整观察窗口。工具检查通过不代表真实预测有效；[R06 记录](docs/reference/14-R06验收与新版实验.md) 与 [2026-10-21 接入记录](docs/archive/10-M1-M2真实接入验收.md) 分别保留对应日期的验证范围。

## 离线演示与开发检查

```powershell
# 人工固定夹具：不调用模型、不联网，不生成预测
uv run --locked python examples/m1_offline.py
# 工程检查
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked pytest
```

演示保存至 `data/m1-demo/`，使用真实应用服务和本地存储，明确标记为夹具。测试中的固定模型响应及本地 HTTP 服务不代表真实外部服务可用。依赖按锁文件安装，目前未引入 API、UI 或统计学习框架。

## 项目与文档入口

[文档导航](docs/README.md) 区分当前设计、使用指南、阶段参考和历史验收。[开发计划](docs/05-开发计划.md) 是当前任务及完成状态的入口；R06-A 需继续扩大真实服务与语义验收，R06-C 需预登记并开展独立前瞻实验；Web 产品化可以独立推进，M4 按评估选做。

首次开发按 [AGENTS.md](AGENTS.md) 依次阅读设计理念、架构、详细设计、开发指南及 [CHANGELOG](CHANGELOG.md)。实际源码分层、示例用途与文件保留规则见文档导航。

问题反馈和改进建议可提交至 [GitHub Issues](https://github.com/cuixinjie/future-agent/issues)。提交步骤见 [贡献说明](CONTRIBUTING.md)，安全问题和脱敏要求见 [安全说明](SECURITY.md)。

## 许可证

当前尚未授予开源许可证。源码公开供查看，不代表授予复制、修改或再分发许可；相关授权请联系仓库所有者。第三方依赖遵循各自许可证。
