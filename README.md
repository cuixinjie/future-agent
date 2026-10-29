# 预测未来智能体 · Future Agent

一个本地运行的预测研究助手：输入事件描述，系统整理问题、经授权查找公开资料，并输出带依据和限制的判断。支持保存报告、冻结预测和后续手动核验。

**当前软件版本：0.1.0a1（Alpha）。** 目前提供 Python 3.12 命令行程序，适合本地试用与研究开发。预测准确性尚未通过独立前瞻实验验证。

## 当前能做什么

- 自动拟定预测目标、时间窗口和核验规则；普通会话最多一轮、三个澄清问题。
- 经授权搜索公开资料、读取网页正文，保存快照和精确引用。
- 按依据给出未校准主观概率、方向判断、条件分析、未来探索或无法判断。
- 恢复会话，导出 Markdown/JSON，冻结预测、另存修订及手动记录实际结果。
- 运行软件稳定发布问题的 A/B/C 实验工具，比较历史频率、直接模型判断和准备后的预测流程。

尚未提供 Web/API、多用户、后台监测、通用统计预测、自动校准或训练。公开题库接入及预测质量补强仍在[开发计划](docs/05-开发计划.md)中。

## 安装与运行

准备 Git 和 uv，在终端执行：

```powershell
git clone https://github.com/cuixinjie/future-agent.git
cd future-agent
uv sync --locked
if (-not (Test-Path -LiteralPath .env)) { Copy-Item -LiteralPath .env.example -Destination .env }
```

编辑 `.env`，填写 `LLM_BASE_URL`、`LLM_MODEL` 和服务所需的 `LLM_API_KEY`，然后运行：

```powershell
uv run --locked future-agent doctor
uv run --locked future-agent chat
```

`doctor` 默认只检查配置、不联网。`doctor --live` 最多发出两次真实模型探测，可能产生费用。首次安装可能下载 Python 和依赖；所需 Python 范围为3.12。

模型会接收叙述、答案和分析资料。搜索服务单独配置 `SEARCH_BASE_URL`，使用支持 JSON 的 SearXNG，可参考[本地部署](deploy/searxng/README.md)。没有搜索配置时会记录资料限制，不使用模拟网页替代。完整配置见[运行规则](docs/03-设计文档.md)。

## 一次预测怎么用

输入描述 → 查看系统拟定的问题与假设 → 必要时回答或跳过一轮澄清 → 授权公开查询 → 自动研究与判断 → 保存报告。

常用命令：`/continue` 继续，`/status` 看状态，`/report` 看结果概览，`/details` 看完整报告，`/save` 导出，`/freeze` 冻结，`/resolve` 核验，`/help` 帮助，`/exit` 退出。

```powershell
uv run --locked future-agent chat --resume <会话ID>
# 需要逐项人工审查时
uv run --locked future-agent chat --manual
```

报告写入 `DATA_DIR/reports/`，历史文件不会被覆盖。恢复先显示状态；中断调用的费用可能未知，重试可能重复计费。详见[使用手册](docs/guides/交互式CLI.md)。

## 使用边界与数据

主观概率未经独立校准，引用正确也不代表推断正确。真实模型、搜索和正文通道可能超时或部分失败；服务故障不会被伪装为预测成功。已有一次真实正文到报告的链路记录，但真实质量和预测效果仍待验证，日期与范围见[历史验收](docs/archive/README.md)。

`.env`、默认 `data/`、数据库和缓存已被 Git 忽略。报告中的问题、判断和引用仍可能包含私人信息，公开前自行检查。升级前停止程序并备份完整数据目录；数据兼容性见[数据与版本](docs/reference/数据与版本.md)。

## 开发与文档

[文档导航](docs/README.md)提供唯一的当前文档入口；[开发计划](docs/05-开发计划.md)只列尚待完成的工作。开发者先读[执行规则](AGENTS.md)，反馈见[贡献说明](CONTRIBUTING.md)与[安全说明](SECURITY.md)。

```powershell
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked pytest -q --tb=short
uv build
```

无需真实模型的固定演示见[示例说明](examples/README.md)。离线测试与演示不能证明预测效果。变更记录见[CHANGELOG](CHANGELOG.md)，发布时状态见[0.1.0a1说明](docs/releases/0.1.0a1.md)。

## 许可证

当前尚未授予开源许可证。源码公开供查看，不代表授予复制、修改或再分发许可；相关授权请联系仓库所有者。第三方依赖遵循各自许可证。
