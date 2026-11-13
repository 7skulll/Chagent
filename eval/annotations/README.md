# 人工标注台账

`labels.jsonl` 是本目录唯一的写入目标，由 `python -m eval.cli annotate` 追加，**不手工编辑、不删除行**。每行一条人工判断：

```json
{"sample_id":"run:<uuid>","label":"F5","verdict":"clue","dataset":"tune","reviewer":"...",
 "certainty":"observed","evidence":["tool_calls:<uuid>"],"note":"...","review_version":1,
 "run_id":"<uuid>","recorded_at":"..."}
```

## 三条规则

1. **一个样本只能属于一个数据集。** `tune` 与 `validation` 互斥。在本地 POSIX 文件系统上，使用台账旁的 `.lock` 文件持有排他锁，将读取、互斥检查、版本分配与追加放在同一临界区；读取持共享锁。锁文件不入库且使用中不可删除。加载和报告同样拒绝已有分区冲突。在同一批样本上既调规则又报告指标，等于自己给自己发证书；这条规则无法靠评审时的自觉来保证，所以放在写入口。
2. **追加，不覆盖。** 改判是同一 `(sample_id, reviewer)` 的新版本，`review_version` 自增，旧判断保留。分歧在新旧记录之间可见，而不是被最后一次写入抹掉。
3. **`root_cause` 必须给出证据。** `evidence` 是 `table:record` 引用，例如 `tool_calls:117cfdf5-…`。引用必须是非空的 `table:record`，支持 runs/tool_calls/run_events/approval_requests/compaction_calls/model_calls。写入口检查格式；`attribute` 还检查样本/run 一致、所有引用存在于当前诊断输入，并至少有一条关联该 finding。这里只验证已加载记录的归属与关联，不能替代人工对因果关系的判断；未加载的证据应先补充查询输入，不能假装已经核验。

## verdict 的含义

| verdict | 含义 |
|---|---|
| `clue` | 这条线索成立，但不构成根因（与规则同级，供后续对照） |
| `root_cause` | 已核实为该标签的根因，可让 `attribute` 把对应 finding 升到 `confirmed` |
| `not_a_defect` | 该标签不适用：被观测的行为是正确的。**负样本靠它积累**，必须在 `note` 里写清检查了什么 |

## 报告口径

`python -m eval.cli labels --diagnoses <目录> --annotations eval/annotations/labels.jsonl`

- 只对 `validation` 样本报告精确率/召回率；`tune` 只计数，不计入指标。
- 分母为 0 时输出 `null` 与原因，不输出 0 或 1。
- 两位评审结论冲突的样本标为 `disputed` 并从矩阵剔除，不做多数表决。
- 每位评审的每个标签各取最新版本；旧版本仅用于查看改判历史。确认门同样遵守这个规则：改为 clue/not_a_defect 会撤销旧确认；评审之间存在不同 verdict 时阻止升级。同一版本的冲突记录拒绝处理。
- 诊断必须携带当前 `rule_version=attr-2`；旧版本或缺版本均拒绝，同一样本出现多份诊断也拒绝，不能依赖文件排序选结果。请先重新运行 `attribute` 导出旧诊断。
- 缺少诊断的样本不计为 FN/TN；显式 `not_evaluable` 的标签也不计入混淆矩阵。报告列出缺失样本、覆盖数量与每标签的排除数，指标仅描述有诊断且可评价的子集。

如果某个标签在验证集上没有样本，报告会写"no validation sample for: …"，这表示**尚未测量**，不表示规则表现良好。
