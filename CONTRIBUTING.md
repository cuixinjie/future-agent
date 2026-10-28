# 贡献与反馈

当前为 Alpha 实验版，尚未授予开源许可证；提交代码贡献前请先与仓库所有者确认授权方式。

可以通过 GitHub Issues 报告问题或提出建议。请提供软件版本、操作系统、可复现步骤、预期行为和实际行为；区分离线夹具、模型服务、搜索服务与正文通道。

不要上传 `.env`、密钥、私人叙述、数据库、正文缓存或完整会话报告。请用公开或合成样例复现，并裁剪脱敏错误信息。安全问题按 [安全说明](SECURITY.md) 处理。

开发前按 [AGENTS.md](AGENTS.md) 阅读设计与执行规则。保持模块化单体和既有服务边界，一次修改围绕一个可验收目标。常规检查：

```powershell
uv sync --locked
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked pytest -q --tb=short
uv build
```

默认使用离线夹具，不在自动测试中请求付费模型。说明实际执行的检查和限制，不把流程完成或工具通过当作准确率证明。重要规则与行为变化须更新 [CHANGELOG](CHANGELOG.md)。
