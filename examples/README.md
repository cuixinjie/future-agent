# 示例用途

这些文件是开发/人工操作示例，不是已验证的真实预测。先运行普通程序的读者应回到[项目README](../README.md)。

| 文件 | 用途 |
|---|---|
| [m1_offline.py](m1_offline.py) | 无模型、无网络的原文/档案/持久化演示，数据写入data/m1-demo，不生成真实预测 |
| [event.txt](event.txt) | analyze命令的示例叙述 |
| [forecast-spec.json](forecast-spec.json) | 人工路径的结果定义示例，需结合实际目标修改 |
| [protocol.t11.fixture.json](protocol.t11.fixture.json) | 旧方法A/B/C实验的固定夹具协议，不是正式实验名单 |
| [protocol.fixture.json](protocol.fixture.json) | 早期协议合同示例，方法版本不适用于当前A/B/C执行器；保留用于历史合同示例 |

```powershell
uv run --locked python examples/m1_offline.py
```

文件名中的M1/T11是历史任务标签，不是多个软件版本。示例仍由文档或代码使用，保留原名称以避免已有操作引用失效。真实数据须另建会话/协议，不能去掉fixture标记冒充真实输入。
