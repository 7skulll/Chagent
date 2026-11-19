# Terminal-Bench TB2 外部终端 Agent 实施方案

TB2.1 和 TB2.2 已验收，TB2.3 已执行但未验收（官方 verifier 未启动，没有原始判分），2026-11-19。TB0 已固定 Harbor 0.25.0 和 Terminal-Bench 4.1.0，TB1 已在本机验证两道发布任务的官方 oracle 与 nop。TB2 的交付是让真实 Chagent 运行循环经 Harbor 的主任务环境完成一轮候选执行，保留 Chagent 调用证据和 Harbor 官方原始判分。 [TB1 验收与复验](../eval/benchmarks/terminal_bench/README.md#tb1-官方参考解与空操作对照)

## 首版契约

- profile 为 `chagent-terminal-v1`，只声明 `exec_command`，参数包括命令与单次超时。工具使用 Harbor 主环境的默认用户与工作目录，输出退出码、标准输出与错误输出及截断状态。命令时限、返回给模型的输出上限和候选总时限在槽位计划中固定。以任务工作目录为准，禁止对官方命令路径做宿主工作区还原。
- Harbor 管理任务环境和验证器的完整生命周期。Chagent 只持有主环境执行接口和控制侧槽位，工具参数不能选容器、服务、用户或后端。候选可使用题目允许的服务接口。凭据、数据库、官方参考解及验证文件保留在各自的控制或验证侧。禁止挂载 Chagent 源码或凭据给候选容器。
- 每槽新会话与隔离评测 schema 下的记录，使用空临时目录作为 AgentRunner 的宿主控制工作区。固定首版提示和声明哈希，关闭长期记忆、压缩、技能与工具发现。官方题目文本以 Harbor 传入的指令为准。文件工具、持久进程、官方任务技能与长任务策略归 TB3，网页归 TB5。
- 官方 `reward` 原值及分项照存。另记候选运行结局、工具超时、Harbor 基础设施异常、判分与清理。无官方判分时保留为空。短预算实跑标为单题诊断。

## 实施批次

| 批次 | 实施内容 | 完成判据 |
|---|---|---|
| TB2.1 桥接与冻结 | 在 Terminal-Bench 独立环境中新增外部 `BaseAgent` 和诊断 CLI。程序化构造 Harbor `Job`，注册 trial 事件钩子，复用 AgentRunner 和独立评测库，注册单工具。冻结发布包、镜像、提示和工具面，在执行入口中先认领永久槽位再调度一次 Harbor trial | 以真实 Harbor Job 和工厂核对装配，再用模拟环境验证调用链。复核时另以真实 Docker 跑一槽冒烟。内部槽位、Harbor trial UUID、Chagent session/run ID 可互查。伪客户端验证主环境绑定，错误绑定、重入、宿主工具调用、冻结输入漂移与重复槽位均被拒绝 |
| TB2.2 零付费 Docker 验收 | 使用固定发布包和真实 Docker。先用脚本化合成客户端，分别验 `music-harmony` 的已知正确产物和缺失产物。正确产物来自验收夹具，由控制端确认，保留夹具来源和哈希。随后用 `freight-dispatch-shift` 验证脚本化合成客户端可经真实 AgentRunner 访问题目声明的服务接口。另做取消、超时和证据缺失用例 | 正负控制分别由官方 verifier 返回预期原始分数，重复运行不共享会话或容器。工具调用、产物传递及任务/验证资源清理均可查。阻断费用与越界用例不触发真实供应商请求 |
| TB2.3 真实单题诊断（已执行，判据未达成） | 前两批通过后，从已提交的待验执行代码为 `music-harmony` 单独冻结一个真实候选槽位。固定模型、API 参数、请求窗口、预算与超时。验证已核实价目覆盖最坏一次请求，再按该协议执行 | 一次真实候选调用链从 Chagent 经 Harbor 命令工具到官方 verifier，保存原始判分、调用和用量。模型未解出题可如实记录。执行、判分、费用或清理证据缺失时维持未完成或不可核验状态。**现状：链路、调用、用量与清理证据齐备，候选在工具上限处中止，verifier 未启动，原始判分为空，故本批未验收** |

## 代码接缝与证据

1. **复用 AgentRunner 的装配逻辑。** `create_task_runner` 当前硬编码 A–D 的 `TaskExecution`，该适配器依赖本地 Docker 任务状态和固定的 Python 路径。抽出受控的 runner 装配入口，由内部任务与 Harbor 分别传入自己的执行对象。对 Harbor 后端显式声明文件快照不可用，Runner 记录该事实，产物证据以 Harbor collect 为准。保留产品与 E1 的现有入口、工具行为和事件语义。 [现有装配](../src/chagent/bootstrap.py#L244) · [内部执行适配器](../src/chagent/hostexec/task_runner.py)
2. **固定绑定与路径处理。** 利用 `Job.add_hook` 获取 `TrialHookEvent.trial_id`，连同单槽生命周期和外部 Agent 控制目录核对后绑定。以 Runner 的 `on_started` 保存 run ID，工具调用再核对 `ToolContext` 的 task、session、run。编码前复查调用投影及供应商请求边界，测试官方绝对路径与默认工作目录能原样到达 Harbor，控制端路径及密钥不会进请求或命令环境。官方验证器和服务管理接口不注册到工具表。 [Harbor 试验事件](https://github.com/harbor-framework/harbor/blob/fdba28fc411e47e8765420a1a063b4918fce3def/src/harbor/trial/hooks.py#L38) · [工具上下文](../src/chagent/tools/context.py#L21)
3. **复用请求级预算。** `BudgetClient` 负责模型调用数、工具调用数、逐请求预留及用量结算。付费之前，校验发布包与镜像、模型与工具配置、价目、总帽及单槽帽、调用窗口与输出上限。区分候选命令、候选整轮、Harbor 构建与验证时限。关闭供应商自动重试及 Harbor trial 重试，单并发。响应流中断而无用量时，保留未知开销和已认领槽位。 [现有预算门禁](../eval/budgets.py#L34)
4. **单独验证取消与回收。** Harbor 的等待取消会传播到 AgentRunner，需核对 DB 终态、请求用量状态和执行退出证据。工具达到超时或被取消时，宿主 Compose 子进程退出只能证明客户端停止等待。真实 Docker 验收要验证候选残留命令无法继续影响采集。若当前后端无法证明该点，停止该槽并保留不确定状态，交 Harbor 收尾并执行收尾核验；此项未通过前维持零付费验证。Chagent 无权接管或直接销毁任务容器。 [Harbor 收尾代码](https://github.com/harbor-framework/harbor/blob/fdba28fc411e47e8765420a1a063b4918fce3def/src/harbor/trial/trial.py#L601)
5. **保存独立的官方记录。** 新增小型 CLI，提供冻结、执行和报告入口。复用现有账本的槽位认领与不可覆盖写入能力，保持旧评测记录兼容。记录 Harbor trial UUID、发布包和镜像 digest、Chagent 代码与配置身份、请求及工具轨迹、原始 reward、产物哈希、用量和清理证据。类型与阶段可审计，核心文件缺失时报告明确标记。复用 TB1 的按 trial 归属核对 Docker 事件方式。 [TB1 证据与审计脚本](../eval/benchmarks/terminal_bench/README.md#tb1-官方参考解与空操作对照)

TB2.2 已在真实 Docker 验收，TB2.3 的候选预算、供应商价目和单槽额度见下节冻结协议。


## TB2.1 实现与复验

`tb2.py` 使用 TB1 核验过的发布包文件清单固定两道诊断任务，绑定文件执行位、镜像、指令、工具面、依赖锁和 checkout 身份。认领槽位后构造单 trial 的 Harbor Job。`harbor_agent.py` 结合 Harbor 的 trial UUID、主环境 session ID、Agent 日志目录及 Chagent session/run ID 核对每次绑定。命令异常时封闭工具、保留未知状态。Chagent 只释放自己的绑定，报告将 Harbor 清理标为未验证。

TB2.1 初验使用真实 Harbor 配置、Agent 工厂与 Chagent Runner/PostgreSQL，执行环境用测试替身。复核补跑一槽真实 Docker，合成客户端经 `exec_command` 执行 `pwd`，返回 `/app`。独立验证器对缺失产物给出官方原始 reward `0.0`，按 trial 核实两个容器与两个网络均已销毁。完整正负配对、多服务与取消验收见下节。此批无付费调用。 [验收第 93 节](验收记录汇总.md#93-tb21-桥接复核与单槽-docker-冒烟2026-11-19)

从仓库根目录准备一个新的诊断槽位。修改 checkout 后重新冻结。输出位于 `eval/results` 或仓库外。

```sh
tb=eval/benchmarks/terminal_bench
uv sync --locked --project "$tb" --python 3.13
PYTHONPATH=src:. "$tb/.venv/bin/python" -m eval.benchmarks.terminal_bench.tb2 prepare \
  --task eval/results/tb1-20261119/published/music-harmony --out eval/results/tb2-diagnostic
PYTHONPATH=src:. "$tb/.venv/bin/python" -m eval.benchmarks.terminal_bench.tb2 report \
  --out eval/results/tb2-diagnostic
CHAGENT_TEST_DATABASE_URL=postgresql:///chagent HARBOR_TELEMETRY=0 PYTHONPATH=src:. \
  "$tb/.venv/bin/python" -m unittest discover -s "$tb/tests" -v
```

`tb2 run --out <槽位目录>` 会启动真实 Docker 与官方验证器并永久认领槽位，供下一批运行。可通过 `CHAGENT_EVAL_DATABASE_URL` 设置评测连接，诊断记录写入独立 schema。历史 `report` 核验归档计划哈希、认领、trial/run 绑定与原始结果，并重建摘要。当前 checkout 的身份校验用于执行阶段。


## TB2.2 零付费 Docker 验收

五个控制使用同一受限工具面、各自的永久槽位和独立会话。正控仅使用 TB1 oracle 已验证产物作为固定夹具，其字节 SHA-256 在 prepare 前核对并写入计划。官方发布包、镜像、验证器和任务资源配置保持原样。

| 控制 | 实际结果 |
|---|---|
| `music-harmony` 正确产物 | 采集的产物 SHA-256 等于 TB1 夹具，官方独立 verifier 返回 1.0 |
| `music-harmony` 缺失产物 | 产物缺失，独立 verifier 返回 0.0 |
| `freight-dispatch-shift` 声明服务 | `event-feed` 健康后 main 启动，候选在 main 访问声明的健康接口得到 HTTP 200。服务探针未生成求解产物，官方 verifier 返回 0.0 |
| 命令超时 | 2 秒工具时限触发。Harbor 停止 main 后才进入恢复收尾，主服务停止时间早于测试计划的延迟写入。官方 reward 缺失，报告为 incomplete |
| 运行取消 | 观察到该槽的 Docker exec 启动事件后取消，Chagent 数据库记录 cancelled。Harbor 停止 main 后才进入恢复收尾，主服务停止时间早于测试计划的延迟写入。官方 reward 缺失，报告为 incomplete |

超时和取消后的命令结果保留未知。适配器封闭工具并经 Harbor 的 `stop_service("main")` 停止主服务，容器和网络的最终销毁仍由 Harbor 完成。Docker 事件显示两槽分别在命令启动后约 3.150 秒和 1.185 秒停止 main，早于测试命令安排的 20 秒延迟写入。五槽共核对 9 个唯一容器和 8 个唯一网络均已销毁，结束时按 ID 查询无残留。这个证据覆盖两种中断路径，持久进程和其他长任务策略归 TB3。

原始 reward、候选记录、Docker 事件和调用摘要见 [TB2.2 诊断清单](diagnostics/tb2-2-docker-20261119.json)。另用归档副本删除原始结果，报告如预期拒绝。非合成客户端、越界工具、与冻结命令不一致以及夹具漂移均在调用前拒绝。零付费模型调用。

从仓库根目录复现。正控夹具取自 TB1 的官方 oracle 输出，目录参数应使用新的路径。

```sh
tb=eval/benchmarks/terminal_bench
fixture=$(find eval/results/tb1-20261119/runs/music-harmony-oracle -name harmony.mxl -print -quit)
PYTHONPATH=src:. "$tb/.venv/bin/python" -m eval.benchmarks.terminal_bench.tb22 \
  --published eval/results/tb1-20261119/published \
  --artifact "$fixture" --out eval/results/tb22-recheck --db postgresql:///chagent
```


### 审计与中断补强

从冻结的命令参数核验 PostgreSQL 调用账本。新槽位计划分别保存执行脚本与依赖锁的 SHA-256。只读报告兼容历史计划，不要求当前代码匹配历史 checkout。付费槽位另从提交后的待验代码冻结，随后继续由执行入口检查工作树身份。

销毁事件采集最多等待 10 秒，过期仍缺证据则报错。主服务停止操作最多等待 30 秒，重复取消期间保持收尾，结束后再传播取消。历史报告会复核归档中的 Docker 事件，展示清理状态和绑定后的取消线索。中断判据来自同一容器的命令启动和停止时间，以及采集事件的先后顺序。目录为空只说明归档没有采集到对应文件。 [补强复跑证据](diagnostics/tb2-2-audit-20261119.json)

离线复算下面任一诊断，输出到新文件。历史计划没有逐文件执行脚本哈希，复算时该字段为空，原诊断中单独记录的源码摘要保留作参照。

```sh
tb=eval/benchmarks/terminal_bench
PYTHONPATH=src:. "$tb/.venv/bin/python" "$tb/scripts/audit-tb22.py" \
  --archive eval/results/tb22-attempt.INdxK1/controls \
  --output eval/results/tb22-audit-recheck.json
PYTHONPATH=src:. "$tb/.venv/bin/python" "$tb/scripts/audit-tb22.py" --smoke \
  --archive eval/results/tb2-1-review-20261119 \
  --output eval/results/tb21-audit-recheck.json
```

## TB2.3 单题付费协议

诊断只允许一个 `music-harmony` 发布包槽位。使用 DeepSeek 官方 OpenAI 兼容端点、`deepseek-flash` 别名、非思考模式，候选最多 6 次模型请求、5 次工具调用和 180 秒。请求窗口为 16,384 token，输出上限 1,024 token。模型和 Harbor 重试均为 0。按 2026-11-19 核实的峰值价，输入和输出每百万 token 分别为 0.30 和 1.20 美元。缓存命中的输入按更高的未命中价计入保守估算。单次请求预留上限为 0.006144 美元，单槽上限为 0.04 美元。 [DeepSeek 官方价目](https://api-docs.deepseek.com/quick_start/pricing/)

准备与启动都检查已提交、无本地改动的执行 checkout。准备冻结任务包、镜像、模型和工具声明及价格。每次调用前核查实际请求并预留费用，缺用量或费用超出预留时拦截该回应的工具调用。执行一次即永久消耗槽位。凭据从控制端读取，任务容器由 Harbor 管理。官方原始分数、候选结局和清理证据分别记录。

在已提交的独立工作树根目录执行。参数指向现有发布包、评测输出目录及本机密钥配置文件。

```sh
tb=eval/benchmarks/terminal_bench
uv sync --locked --project "$tb" --python 3.13
PYTHONPATH=src:. "$tb/.venv/bin/python" -m eval.benchmarks.terminal_bench.tb23 prepare \\
  --task "$published/music-harmony" --out "$out"
PYTHONPATH=src:. "$tb/.venv/bin/python" -m eval.benchmarks.terminal_bench.tb23 run \\
  --out "$out" --db postgresql:///chagent --env-file "$env_file" --confirm-paid
PYTHONPATH=src:. "$tb/.venv/bin/python" -m eval.benchmarks.terminal_bench.tb23 report --out "$out"
```


### TB2.3 真实运行结果

执行快照为独立分支 `codex/tb2-3` 的 `1f38ac06faac0bd2ccb2be806b01b75445ad9eb1`，冻结时工作树干净。已消费一槽。DeepSeek 返回四次用量，已执行五次命令。第四次回应请求的额外工具批次超出冻结上限，整批在工具执行前被拒绝。Harbor 保留失败与收尾证据，独立 verifier 未启动，官方原始 reward 为空。

四次合计有 7,149 个未命中输入 token、5,248 个缓存命中输入 token、482 个输出 token。按预登记上界估算为 0.0042975 美元，低于 0.04 美元单槽帽。第四次回应由预算器计量后拦截，数据库未写入该终止回应的正文和单条用量。诊断清单将前三次逐条记录与预算器累计数的差额标为重算值。主任务容器和任务网络有销毁记录，按 ID 核验均不存在。原槽保留，无重试。

[真实运行诊断及证据哈希](diagnostics/tb2-3-live-20261119.json) · [验收第 96 节](验收记录汇总.md#96-tb23-真实单题诊断触发工具调用上限2026-11-19)


### TB2.3 判据缺口与收口路径

本批缺的是官方原始判分，不是分数低。批次判据明确允许「模型未解出题可如实记录」，0.0 也是有效观测；实际是独立 verifier 没有机会启动，所以状态只能停在 incomplete。判据因此未达成，本批记为已执行、未验收。

根因在执行接缝：`ChagentTerminalAgent.run` 对任何非 `model_stop` 的候选结局都向上抛错，Harbor 收到异常后跳过验证阶段。本次触发路径是冻结的 5 次工具额度先用尽，第 4 个回应继续请求工具批次，预算器以 `tool call budget exhausted; batch was not executed` 拒绝整批，runner 抛 `RunFailedError`。这个行为对评测是系统性偏差：凡是没做完的候选都只能得到 incomplete，无法区分「模型弱」与「环境故障」，而每个槽位都是永久消耗。

两条收口路径尚未决定，选定其一后再更新上表状态：

1. **补一个真实槽位。** 先改接缝，让候选因工具上限、预算耗尽或工具超时结束时记录 `end_reason` 后正常返回，由官方 verifier 对当时工作区打分；只对命令结果未知或基础设施不确定保持中止。同时把工具额度从 5 放宽到 10–12，费用仍由轮次与单请求预留决定，不随工具数线性增长。随后从新提交冻结一个新槽；旧槽已认领，不能复用。
2. **改判据、把判分覆盖后移。** 将本批收窄为「付费链路诊断」，明确不产出能力观测，把「真实候选→官方判分」纳入 TB4 固定子集的验收门槛。此路径不再产生新的付费槽，但需同步修改本文件批次表、[实施计划](实施计划.md) 状态表与 TB4 判据，避免出现完成判据和实际结果两张皮。
