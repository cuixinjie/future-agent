# 分步 CLI 与旧实验操作

本页面向高级人工操作、脚本及 `judgment-1.0` 实验。普通预测使用 [交互式 CLI](交互式CLI.md)，无需按本页逐项填写。以下“默认情景”“显式数值许可”只适用于人工/旧实验路径。

## A/B/C 实验与评分

T11 的协议版本要求、Tc/T0、C 准备流程、封存 JSON、A/B/C 命令、取消/中断、共同核验与评分见 [T11使用及迁移说明](../reference/13-T11实验运行与评分.md)。T11 夹具协议见 [示例](../../examples/protocol.t11.fixture.json)，不用于真实效果结论。

```powershell
uv run --locked future-agent evaluation --help
uv run --locked pytest tests/integration/test_evaluation.py -q --tb=short
```

当前迁移头为 `0006_evaluation`，仅新增实验表；升级前备份完整 DATA_DIR。人工/旧实验评估规则为 `evidence-assessor-1.2`；普通模式为 `evidence-assessor-2.0`，旧评估保留并失效，下次预测前重新评估。没有新增依赖、API 或界面，也没有开展 T12 真实前瞻实验。

## 冻结、核验与恢复

T10 命令、手动核验 JSON、恢复与取消语义、升级备份和实测边界见 [T10 使用及迁移说明](../reference/12-T10冻结核验与恢复.md)。新迁移 `0005_lifecycle` 只新增三表；本次普通主线会话合同为 1.5，兼容读取 1.0–1.4，调用记录 1.1 新增步骤归属。该审查修复本身未新增表；T11 另增加 `0006_evaluation`，历史冻结记录及哈希保留。

## 分步高级操作：模型读取、澄清与确认

复制 `.env.example` 为 `.env`，填写模型地址、模型标识与密钥。兼容端点是 `LLM_BASE_URL/chat/completions`；用户显式配置的本地服务也可使用。叙述、澄清答案和旧档案会发往该服务。搜索配置独立，M1 不使用搜索服务。

```powershell
uv run --locked future-agent doctor
# 可选：最多两次真实探测调用，可能产生费用
uv run --locked future-agent doctor --live
uv run --locked future-agent analyze --input examples/event.txt --request-key first-event
uv run --locked future-agent show <会话ID>
uv run --locked future-agent resume <会话ID>
```

`doctor` 默认只检查配置，不联网。`analyze` 先保存原文并输出会话 ID，再请求读取；服务未配置或失败时不会自动转为演示数据。默认 `LLM_STRUCTURED_MODE=text_json` 明确使用文本 JSON 和本地校验；服务支持 Schema 参数时可显式选择 `json_schema`，不兼容时返回错误，不静默切换。

每轮最多显示三个待澄清问题。将补充答案保存为 UTF-8 文件，再执行：

```powershell
uv run --locked future-agent answer <会话ID> --expected-revision 1 --input data/answer.txt
uv run --locked future-agent resume <会话ID> --read
```

`answer` 只保存答案和新事件版本，`resume --read` 才会重新调用模型。可在 `answer` 后添加 `--keep-unknown <未知项ID>`，明确保留背景未知。目标、时间窗口和核验规则仍缺失时不能确认。

查看和编辑档案使用 JSON；PowerShell 写文件时请使用 UTF-8：

```powershell
uv run --locked future-agent show <会话ID> --editable | Set-Content -Encoding utf8 data/draft.json
# 编辑 data/draft.json，再使用当前事件版本导入
uv run --locked future-agent edit <会话ID> --expected-revision <当前版本> --input data/draft.json
uv run --locked future-agent confirm <会话ID> --expected-revision <编辑后的版本>
uv run --locked future-agent history <会话ID>
```

每次编辑或确认另存版本，旧档案不覆盖；确认表示用户核对了原文分类和目标，随后状态为 `STRUCTURED`。曾标记为夹具的会话，其后续编辑、回答、读取及确认继续标记为夹具，研究和预测报告保留此属性；真实输入应另建会话。`show --sources` 才显示原始叙述与澄清答案；终端中的档案引用仍可能含个人信息，不应直接公开。无模型时可用 `create --input ...` 保存原文，再 `edit` 导入人工档案。

中断后 `resume` 查看已保存状态。仍处于 `READING` 表示调用结果与费用可能未知，只有显式使用 `resume --retry-interrupted` 才重新请求；所有重试、修复及重新读取共享已保存的剩余预算。

## 资料导入与证据快照（T06）

保存事件版本后，可执行 `future-agent evidence import <会话ID> --revision <版本> --input <资料JSON>`。资料格式见 `schema evidence` 和 `tests/fixtures/evidence/webpage.fixture.json`。使用 `evidence text <文档ID>` 查看快照，`evidence cite` 保存精确引用，`evidence select` 按截止时点筛选。命令不联网；正文、候选片段、获取失败和时间未知分别保留。相同正文共享文件，全部来源记录保留。

T06 引入 `0002_evidence`，当前迁移头为 `0006_evaluation`，升级前备份数据库及快照目录；回退恢复备份，不自动删除历史。完整命令、时间资格、去重和限制见 [T06 说明](../reference/07-T06证据导入与快照.md)。

## 搜索与网页读取（T07）

本机没有搜索实例时，可按 [Docker 部署说明](../../deploy/searxng/README.md) 启动 SearXNG，
然后配置 `SEARCH_BASE_URL=http://127.0.0.1:8888`。该实例已启用 JSON，只监听本机；
搜索和正文抓取分别验收，真实结果与环境限制见 [本次接入验收记录](../archive/10-M1-M2真实接入验收.md)。

独立配置支持 JSON 输出的 `SEARCH_BASE_URL`，先确认完整问题，再使用允许公开的对象和主题：

```powershell
uv run --locked future-agent research plan <会话ID> --revision <当前版本> --subject "公开项目名" --focus "稳定版本发布"
uv run --locked future-agent research run <会话ID> --revision <当前版本> --subject "公开项目名" --focus "稳定版本发布"
uv run --locked future-agent research show <会话ID>
```

`plan` 不联网，`run` 才搜索并抓取。规划优先当前状态和反证，不发送私人叙述；候选、正文失败和已读快照分别保存。网页每跳验证 DNS、公网 IP 和连接目标，独立配置的本地搜索服务不放宽网页限制。默认会话最多 8 次搜索尝试、16 次页面尝试，含重试，时间与已有模型调用共享且不因重启重置。部分完成、失败或取消返回退出码 2 和缺口记录。

T07 的会话与来源合同升级为 1.1（当前会话为 1.5），旧 1.0 可读取，保存后的新格式不兼容旧程序，升级前备份整个 DATA_DIR；回退恢复备份。T07 当时的数据库版本为 `0002_evidence`，当前迁移头为 `0006_evaluation`。取消、中断处理、网络限制、迁移和离线验证边界见 [T07 说明](../reference/08-T07搜索与网页读取.md)。

## 情景与主观预测（T09）

先确认目标并完成数据条件评估，再按当前问题审查 [结果定义示例](../../examples/forecast-spec.json)。
默认输出无概率情景；显式 `--numeric` 仍须通过人工数值许可和证据资格。

```powershell
uv run --locked future-agent forecast run <会话ID> --revision <版本> --assessment-id <评估ID> --input data/forecast-spec.json
uv run --locked future-agent forecast show <预测ID>
uv run --locked future-agent forecast report <预测ID>
uv run --locked future-agent forecast history <会话ID> --revision <版本>
```

模型失败不保存空成功结果，非法概率不会自动裁剪。报告来自保存的结构化预测；冻结后显示已冻结、冻结时间和内容哈希，实验评分资格仍须另行检查。主观数值未经校准。
T09 当时新增 `0004_forecasts` 并升级会话至 1.2；T10 新增 `0005_lifecycle`，当前会话为 1.5、兼容读取 1.0–1.4。升级前备份完整 DATA_DIR；
旧评估保留但须重新评估，回退恢复备份。预测输入会发送给配置模型，输出可能含私人内容。
完整规则、失败与中断命令及验收边界见 [T09说明](../reference/11-T09情景与主观预测.md)。

## 实验协议

```powershell
uv run --locked future-agent protocol validate --input examples/protocol.fixture.json
uv run --locked future-agent protocol create --input examples/protocol.fixture.json
uv run --locked future-agent protocol lock <协议ID> --expected-version 1
uv run --locked future-agent protocol show <协议ID>
```

示例协议为 `purpose=fixture`，并非真实实验预登记。正式协议使用 `prospective`，需填写真实选样依据、项目、官方来源、发布定义、Tc/T0、A/B/C 方法版本和预算。锁定后不可修改，锁定时间不得晚于 Tc。`protocol edit --input ... --expected-version ...` 仅允许修改未锁定草稿。可用 `schema draft`、`schema protocol` 查看契约，或 `release-template --project <项目JSON> --forecast-at <带时区时间>` 生成30天稳定发布问题草稿。

私人输入、数据库和缓存放在已忽略的 `data/`，凭据放 `.env`。配置只在入口读取；进程环境变量覆盖指定 `.env`。`--env-file` 可选择文件，`--no-env` 禁用文件读取但保留进程环境变量。`DATABASE_URL` 只允许 SQLite 文件，路径必须在批准的 `DATA_DIR` 中；未配置时使用 `DATA_DIR/app.db`。


## 数据条件评估（T08）

确认目标并保存正文引用后，使用 `assessment run <会话ID> --revision <版本> --input data/review.json` 提交显式人工审查；`schema review` 输出输入结构，`assessment show <会话ID> --revision <版本>` 查看历史和失效原因。默认情景路径，关键条件不足时返回最多三个补充问题；可用方法严格受数据资格及实现能力约束，T09 已启用情景与主观判断。评估不生成预测。升级前备份 DATA_DIR，新增迁移 `0003_assessment` 不重写旧记录。详见 [T08 说明](../reference/09-T08数据条件评估.md)。
