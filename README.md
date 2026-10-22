# 预测未来智能体

有证据、可追溯、可核验的预测助手，采用模块化单体：交互入口调用应用服务，应用层依赖领域对象与自身声明的接口，基础设施实现接口，组合入口负责装配。

当前已实现 **M1（T01–T05）、M2（T06–T08）及 M3-T09/T10**：读取、澄清、可编辑事件档案、实验协议锁定，以及资料导入、证据快照、精确引用和截止资格筛选。离线流程已验证，真实模型服务尚未验收；已支持 SearXNG 搜索与静态网页读取，真实搜索及外部网页兼容性尚未验收；已支持人工引用审查驱动的数据条件评估、方法资格和修订失效；已支持情景优先、满足资格后显式开启的未校准主观概率、结构化预测草稿及模板报告；已支持不可覆盖的冻结、修订关联、手动核验历史及预测步骤恢复；T11 评分尚未实现。包版本 `0.1.0.dev0` 表示开发状态。详细契约见 [M1 实现说明](docs/06-M1实现与验收.md)、[T06 使用及迁移说明](docs/07-T06证据导入与快照.md) 和 [T07 使用及兼容说明](docs/08-T07搜索与网页读取.md)及 [T08 使用及迁移说明](docs/09-T08数据条件评估.md)。

## 安装与运行

需要 Git、uv 和 Python 3.12。仓库通过 `.python-version` 选择 3.12，通过 `requires-python` 限定当前支持的 Python 范围。若未安装该版本，可先运行 `uv python install 3.12`。在项目根目录执行：

```powershell
uv sync --locked
uv run --locked future-agent --help
```

`uv sync --locked` 按 `uv.lock` 安装项目和开发检查依赖；首次安装需要访问 Python 包索引，缺少 Python 时 uv 可能下载解释器。M1 仅引入 Pydantic 2、SQLAlchemy 2、Alembic、HTTPX 和 python-dotenv；M2-T07 增加 BeautifulSoup4 用于静态正文提取。未安装 API、UI 或统计学习框架。

先体验不联网的人工夹具流程：

```powershell
uv run --locked python examples/m1_offline.py
```

输出会话 ID、事件版本及已锁定的夹具协议 ID，数据库保存在 `data/m1-demo/app.db`。此演示使用真实应用服务、领域规则和数据库，明确标记为人工夹具，不模拟真实模型能力，也不生成预测结果。可用 `future-agent --data-dir data/m1-demo history <会话ID>` 重新读取历史。

## 冻结、核验与恢复

T10 命令、手动核验 JSON、恢复与取消语义、升级备份和实测边界见 [T10 使用及迁移说明](docs/12-T10冻结核验与恢复.md)。新迁移 `0005_lifecycle` 只新增三表；会话合同 1.3 兼容读取 1.0–1.2。

## 模型读取、澄清与确认

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

每次编辑或确认另存版本，旧档案不覆盖；确认表示用户核对了原文分类和目标，随后状态为 `STRUCTURED`。`show --sources` 才显示原始叙述与澄清答案；终端中的档案引用仍可能含个人信息，不应直接公开。无模型时可用 `create --input ...` 保存原文，再 `edit` 导入人工档案。

中断后 `resume` 查看已保存状态。仍处于 `READING` 表示调用结果与费用可能未知，只有显式使用 `resume --retry-interrupted` 才重新请求；所有重试、修复及重新读取共享已保存的剩余预算。

## 资料导入与证据快照（T06）

保存事件版本后，可执行 `future-agent evidence import <会话ID> --revision <版本> --input <资料JSON>`。资料格式见 `schema evidence` 和 `tests/fixtures/evidence/webpage.fixture.json`。使用 `evidence text <文档ID>` 查看快照，`evidence cite` 保存精确引用，`evidence select` 按截止时点筛选。命令不联网；正文、候选片段、获取失败和时间未知分别保留。相同正文共享文件，全部来源记录保留。

T06 引入 `0002_evidence`，当前迁移头为 `0005_lifecycle`，升级前备份数据库及快照目录；回退恢复备份，不自动删除历史。完整命令、时间资格、去重和限制见 [T06 说明](docs/07-T06证据导入与快照.md)。

## 搜索与网页读取（T07）

本机没有搜索实例时，可按 [Docker 部署说明](deploy/searxng/README.md) 启动 SearXNG，
然后配置 `SEARCH_BASE_URL=http://127.0.0.1:8888`。该实例已启用 JSON，只监听本机；
搜索和正文抓取分别验收，真实结果与环境限制见 [本次接入验收记录](docs/10-M1-M2真实接入验收.md)。

独立配置支持 JSON 输出的 `SEARCH_BASE_URL`，先确认完整问题，再使用允许公开的对象和主题：

```powershell
uv run --locked future-agent research plan <会话ID> --revision <当前版本> --subject "公开项目名" --focus "稳定版本发布"
uv run --locked future-agent research run <会话ID> --revision <当前版本> --subject "公开项目名" --focus "稳定版本发布"
uv run --locked future-agent research show <会话ID>
```

`plan` 不联网，`run` 才搜索并抓取。规划优先当前状态和反证，不发送私人叙述；候选、正文失败和已读快照分别保存。网页每跳验证 DNS、公网 IP 和连接目标，独立配置的本地搜索服务不放宽网页限制。默认会话最多 8 次搜索尝试、16 次页面尝试，含重试，时间与已有模型调用共享且不因重启重置。部分完成、失败或取消返回退出码 2 和缺口记录。

T07 的会话与来源合同升级为 1.1（T10 会话进一步升为 1.3），旧 1.0 可读取，保存后的新格式不兼容旧程序，升级前备份整个 DATA_DIR；回退恢复备份。T07 当时的数据库版本为 `0002_evidence`，当前迁移头为 `0005_lifecycle`。取消、中断处理、网络限制、迁移和离线验证边界见 [T07 说明](docs/08-T07搜索与网页读取.md)。

## 情景与主观预测（T09）

先确认目标并完成数据条件评估，再按当前问题审查 [结果定义示例](examples/forecast-spec.json)。
默认输出无概率情景；显式 `--numeric` 仍须通过人工数值许可和证据资格。

```powershell
uv run --locked future-agent forecast run <会话ID> --revision <版本> --assessment-id <评估ID> --input data/forecast-spec.json
uv run --locked future-agent forecast show <预测ID>
uv run --locked future-agent forecast report <预测ID>
uv run --locked future-agent forecast history <会话ID> --revision <版本>
```

模型失败不保存空成功结果，非法概率不会自动裁剪。报告来自保存的结构化草稿，主观数值未经校准。
T09 当时新增 `0004_forecasts` 并升级会话至 1.2；当前 T10 新增 `0005_lifecycle`，会话为 1.3、兼容读取 1.0–1.2。升级前备份完整 DATA_DIR；
旧评估保留但须重新评估，回退恢复备份。预测输入会发送给配置模型，输出可能含私人内容。
完整规则、失败与中断命令及验收边界见 [T09说明](docs/11-T09情景与主观预测.md)。

## 实验协议

```powershell
uv run --locked future-agent protocol validate --input examples/protocol.fixture.json
uv run --locked future-agent protocol create --input examples/protocol.fixture.json
uv run --locked future-agent protocol lock <协议ID> --expected-version 1
uv run --locked future-agent protocol show <协议ID>
```

示例协议为 `purpose=fixture`，并非真实实验预登记。正式协议使用 `prospective`，需填写真实选样依据、项目、官方来源、发布定义、Tc/T0、A/B/C 方法版本和预算。锁定后不可修改，锁定时间不得晚于 Tc。`protocol edit --input ... --expected-version ...` 仅允许修改未锁定草稿。可用 `schema draft`、`schema protocol` 查看契约，或 `release-template --project <项目JSON> --forecast-at <带时区时间>` 生成30天稳定发布问题草稿。

私人输入、数据库和缓存放在已忽略的 `data/`，凭据放 `.env`。配置只在入口读取；进程环境变量覆盖指定 `.env`。`--env-file` 可选择文件，`--no-env` 禁用文件读取但保留进程环境变量。`DATABASE_URL` 只允许 SQLite 文件，路径必须在批准的 `DATA_DIR` 中；未配置时使用 `DATA_DIR/app.db`。

## 开发检查

```powershell
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked pytest
```

Ruff 检查 Python 3.12 代码，行宽 100；pytest 从 `tests/` 收集领域、持久化、适配器和 CLI 流程测试。测试中的固定 HTTP 响应和本地模拟服务均为显式离线夹具，不证明真实模型兼容性或预测效果。

可用独立环境复核安装，避免复用开发环境的包；以下 PowerShell 命令使用新的环境目录，并在结束后恢复当前进程设置：

```powershell
$previousProjectEnvironment = $env:UV_PROJECT_ENVIRONMENT
try {
    $env:UV_PROJECT_ENVIRONMENT = Join-Path $env:TEMP ("future-agent-check-" + [guid]::NewGuid())
    uv sync --locked --no-editable
    uv run --locked --no-editable future-agent --help
    uv run --locked --no-editable pytest
} finally {
    $env:UV_PROJECT_ENVIRONMENT = $previousProjectEnvironment
}
```

该命令保留临时环境供排查，包下载缓存可以复用。纯运行环境可使用 `uv sync --locked --no-dev`；运行时不需要 pytest 或 Ruff。

## 设计与实施顺序

首次开发依次阅读[设计理念](docs/01-设计理念.md)、[架构文档](docs/02-架构文档.md)、[详细设计](docs/03-设计文档.md)、[开发指南](docs/04-AI编程开发指南.md)和[重要变更日志](CHANGELOG.md)。

- M1：已实现读取、澄清、可编辑档案、明确预测问题和实验协议；真实服务验收待配置。
- M2：T06 资料导入、快照、引用和截止资格已实现；T07 网络检索、静态网页读取和共享预算已实现；T08 人工审查驱动的数据条件评估、方法资格和修订失效已实现。
- M3：情景或未校准主观预测、报告、冻结、核验及 A/B/C 比较。
- M4 按独立评估选做；M5 提供 API、工作进程及界面，不依赖 M4 成功。

首个前瞻实验限定为公开软件项目未来 30 天稳定版本发布预测。工程检查通过不能作为预测准确性的证据。

## 数据条件评估（T08）

确认目标并保存正文引用后，使用 `assessment run <会话ID> --revision <版本> --input data/review.json` 提交显式人工审查；`schema review` 输出输入结构，`assessment show <会话ID> --revision <版本>` 查看历史和失效原因。默认情景路径，关键条件不足时返回最多三个补充问题；可用方法严格受数据资格及实现能力约束，T09 已启用情景与主观判断。评估不生成预测。升级前备份 DATA_DIR，新增迁移 `0003_assessment` 不重写旧记录。详见 [T08 说明](docs/09-T08数据条件评估.md)。
