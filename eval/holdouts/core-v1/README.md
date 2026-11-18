# 固定保留任务 core-v1

四个独立任务，分别测订单修订与金额汇总、闭区间修复、冲突文档事实提取、分页多服务交付。它们与 E4 仪器小集和现有技能开发夹具分开保存。输入文件只进入任务容器；可信 verifier 和控制答案只供控制器使用。

本套件用于持续回归。首次正式运行后不得根据红槽修改题目、判据或候选提示；改动必须升套件/任务版本并另立协议。不要用这些任务调参，开发实验另建夹具。任务及答案在仓库中对维护者可见，不声称第三方盲测；重复使用后也不再是未见测试集。还需要另建未参与开发的任务才能评价泛化。

两臂使用相同 deepseek-flash、默认非思考模式及相同工具/预算。baseline 是默认提示；candidate 追加固定契约与边界验证指令。仅衡量该提示机制，不能归因为整个 Chagent 版本改善。

每题每臂20次，共160槽位；重复之间重置容器、卷和服务。按 repeat→case→arm 交错执行。每槽120秒、8次模型调用、8次工具调用、USD0.05，实验总帽USD8。请求窗口32768、输出2048。价格按2026-11-18核实的官方Flash峰值费率（输入0.30、输出1.20 USD/M）作保守估算。未知费用/证据漂移/清理失败即停，失败不重跑，崩溃不自动续跑。

报告每题原始成功数与Wilson/Newcombe 95%区间；正式方向以四题Bonferroni同时区间排除零且差值达到20个百分点为准。20次重复适合发现较大变化，不能保证检出20个百分点的变化，更不能把“未观察到明确差异”说成等效。四个任务不能代表整个能力分布；运行成本、工具次数与耗时仅作描述性比较。文档任务只测已定义的事实契约，不是一般写作语义质量。

## 执行

```sh
PYTHONPATH=src:. .venv/bin/python -m eval.regression prepare \
  --out eval/results/core-v1-20261118 \
  --experiment core-v1-prompt-20261118 --image sha256:<已准备的完整镜像ID>
PYTHONPATH=src:. .venv/bin/python -m eval.regression calibrate --out eval/results/core-v1-20261118
PYTHONPATH=src:. .venv/bin/python -m eval.regression run --out eval/results/core-v1-20261118
PYTHONPATH=src:. .venv/bin/python -m eval.regression report --out eval/results/core-v1-20261118
```

prepare 保存套件字节哈希、冻结目录、协议与两臂提示。calibrate 使用合成客户端执行已知对/错控制各3次（24槽），它们不计入真实模型成绩。run 使用既有 `container_trial.execute_slot`，所有真实请求都经费用预留、永久claim与版本身份门禁。数据库采用单独评测schema，结果保留供追溯。

report.md 回答各题改善/退步/证据不足，report.json 保留缺失、环境错误、原始95%统计、校正后统计与成本，container-trials 保留产物、请求、判分与清理证据。工具面为 task-command-only-v1，关闭长期记忆与压缩。服务任务只判产物正确性，不由答案推断一定遵循了指定访问路径，访问轨迹另看真实工具记录。

现有Web平台可将 `CHAGENT_EVAL_CONTAINER_TASKS` 指向输出目录中的 `catalog`，选择同一份冻结任务开展另外的模型对照。Web当前只支持不同模型名称，提示臂实验通过本命令执行；不把Flash/Pro模型对照解释为本提示对照。若希望历史报告在Web中可见，`--out` 使用管理员个人 evaluations 目录（不要复制或改写已有claim）。本模块不创建定时任务、不自动发布或合并候选提示。

已撤回：首轮真实调用发现 rollback.action 规范形式未公开，存在假阴性。保留目录追溯，不用于能力比较；后续使用 core-v2。
