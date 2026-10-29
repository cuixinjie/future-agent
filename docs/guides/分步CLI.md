# 分步CLI与实验操作

供脚本和高级人工使用；普通用户直接运行[chat](交互式CLI.md)。以下带`<...>`的值及JSON文件需替换为实际记录；调用模型和研究命令会产生外部请求。具体参数以本地`--help`为准。

## 1. 查看合同与帮助

```powershell
uv run --locked future-agent --help
uv run --locked future-agent schema --help
uv run --locked future-agent schema draft
uv run --locked future-agent schema forecast-spec
uv run --locked future-agent schema review
```

Schema描述结构，不证明输入语义或资格正确。新旧格式及方法的区别集中见[数据与版本](../reference/数据与版本.md)。

## 2. 原文、档案与确认

```powershell
uv run --locked future-agent analyze --input examples/event.txt --request-key first-event
uv run --locked future-agent show <会话ID>
uv run --locked future-agent show <会话ID> --editable
uv run --locked future-agent answer <会话ID> --expected-revision <版本> --input data/answer.txt
uv run --locked future-agent resume <会话ID> --read
uv run --locked future-agent edit <会话ID> --expected-revision <版本> --input data/draft.json
uv run --locked future-agent confirm <会话ID> --expected-revision <版本>
uv run --locked future-agent history <会话ID>
```

`create --input`只保存原文；`analyze`保存后调用模型。`answer`保存新版本，`resume --read`才重新读取。目标与时间规则不全时不能确认；每次写入后使用新的实际版本号。`show --sources`显式显示私人原文，不宜直接公开。

读取中断先用`resume <会话ID>`查看；只有明确接受未知费用后使用`--retry-interrupted`。曾标记fixture的会话不能改成真实实验输入。

## 3. 证据、研究和评估

```powershell
uv run --locked future-agent schema evidence
uv run --locked future-agent evidence import <会话ID> --revision <版本> --input data/material.json
uv run --locked future-agent evidence text <文档ID>
uv run --locked future-agent schema span
uv run --locked future-agent evidence cite <会话ID> --revision <版本> --input data/span.json
uv run --locked future-agent evidence select <会话ID> --revision <版本> --cutoff <含时区时间>
uv run --locked future-agent research plan <会话ID> --revision <版本> --subject "公开项目" --focus "稳定发布"
uv run --locked future-agent research run <会话ID> --revision <版本> --subject "公开项目" --focus "稳定发布"
uv run --locked future-agent research show <会话ID>
uv run --locked future-agent assessment run <会话ID> --revision <版本> --input data/review.json
```

资料导入和`research plan`不联网，`research run`会搜索并读正文。分步研究需完整已确认目标。获取和入库都应在截止前；不能用旧发布日期把现在读取的材料变成历史快照。

`review.json`使用`schema review`，人工填写引用相关性、关键缺口和数值许可；评估本身不联网。引用须精确对应正文。网络安全、预算和研究失败规则见[运行规则](../03-设计文档.md)。

## 4. 预测、冻结与核验

```powershell
uv run --locked future-agent forecast run <会话ID> --revision <版本> --assessment-id <评估ID> --input data/forecast-spec.json
uv run --locked future-agent forecast show <预测ID>
uv run --locked future-agent forecast report <预测ID>
uv run --locked future-agent forecast history <会话ID> --revision <版本>
uv run --locked future-agent forecast freeze <预测ID>
uv run --locked future-agent forecast frozen <预测ID>
uv run --locked future-agent schema resolution-input
uv run --locked future-agent resolution record <预测ID> --input data/resolution.json
uv run --locked future-agent resolution history <预测ID>
```

人工路径默认无概率情景，`forecast run --numeric`请求主观数字仍需合格依据。结果定义示例见[forecast-spec.json](../../examples/forecast-spec.json)。实际步骤可通过`--request-id`指定幂等UUID；`forecast step/resume/cancel <步骤ID>`用于检查、恢复和取消。

修订冻结使用`--supersedes <前驱预测ID> --reason <理由>`，必须满足目标一致性。首次核验后再次追加需提供`--expected-previous <最新核验ID>`。冻结时可用`--early-outcome`声明允许提前核验的发生结果；否则等待完整窗口。

## 5. A/B/C实验

当前执行器仅支持已定义的软件稳定发布实验，不是通用公开题库运行器。每个项目使用独立C会话，先锁定协议、完成所有共享答案和资料准备，再于共同截止Tc前封存，在预测起点T0前提交。

A为历史发布频率；B为同模型一次直接预测；C使用对应方法版本的已准备输入。B/C共用模型及协议指定的输出额度；B不接收C研究结论。全部条目保留，失败或拒绝不填默认概率。模型、研究和准备成本计入记录。

```powershell
uv run --locked future-agent schema protocol
uv run --locked future-agent protocol validate --input data/protocol.json
uv run --locked future-agent protocol create --input data/protocol.json
uv run --locked future-agent protocol lock <协议ID> --expected-version <协议版本>
uv run --locked future-agent evaluation init <协议ID>
uv run --locked future-agent schema evaluation-seal
uv run --locked future-agent evaluation seal <协议ID> --input data/sealed-input.json
uv run --locked future-agent evaluation run <协议ID> <项目ID> A
uv run --locked future-agent evaluation run <协议ID> <项目ID> B
uv run --locked future-agent evaluation run <协议ID> <项目ID> C
uv run --locked future-agent evaluation show <协议ID>
uv run --locked future-agent evaluation report <协议ID>
```

封存输入包含项目/会话/事件版本/评估ID和完整历史记录；新版协议还需匹配的2.0结果定义快照。历史记录覆盖Tc之前12个完整30天窗口，含来源正文、完整性说明和逐版本引文；完整性仍需审查，程序不能凭页面存在证明无遗漏。

旧协议使用judgment-1.0，独立2.0协议使用judgment-2.0，不能改写已锁定方法。升级或新方法需新协议批次。现有JSON示例及对应用途见[示例说明](../../examples/README.md)。需要完整旧协议封存示例时，可查[历史实现说明](../archive/implementation/13-T11实验运行与评分.md)，新版适配边界查[验收记录](../archive/validation/14-R06验收与新版实验.md)；这些记录的旧状态不覆盖当前说明。

`evaluation missing/cancel`记录原因，`resolve`按项目追加共同核验，`review`记录质量审查，`finalize`收口未提交条目。完整窗口结束前不做整批评分；未决/争议/作废分开记录。所有命令的字段要求可用对应`--help`及Schema查看。
