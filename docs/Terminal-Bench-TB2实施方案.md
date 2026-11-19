# Terminal-Bench TB2 外部终端 Agent 实施方案

TB2.1 已实现，TB2.2 和 TB2.3 待实施，2026-11-19。TB0 已固定 Harbor 0.25.0 和 Terminal-Bench 4.1.0，TB1 已在本机验证两道发布任务的官方 oracle 与 nop。TB2 的交付是让真实 Chagent 运行循环经 Harbor 的主任务环境完成一轮候选执行，保留 Chagent 调用证据和 Harbor 官方原始判分。 [TB1 验收与复验](../eval/benchmarks/terminal_bench/README.md#tb1-官方参考解与空操作对照)

## 首版契约

- profile 为 `chagent-terminal-v1`，只声明 `exec_command`，参数包括命令与单次超时。工具使用 Harbor 主环境的默认用户与工作目录，输出退出码、标准输出与错误输出及截断状态。命令时限、返回给模型的输出上限和候选总时限在槽位计划中固定。以任务工作目录为准，禁止对官方命令路径做宿主工作区还原。
- Harbor 管理任务环境和验证器的完整生命周期。Chagent 只持有主环境执行接口和控制侧槽位，工具参数不能选容器、服务、用户或后端。候选可使用题目允许的服务接口。凭据、数据库、官方参考解及验证文件保留在各自的控制或验证侧。禁止挂载 Chagent 源码或凭据给候选容器。
- 每槽新会话与隔离评测 schema 下的记录，使用空临时目录作为 AgentRunner 的宿主控制工作区。固定首版提示和声明哈希，关闭长期记忆、压缩、技能与工具发现。官方题目文本以 Harbor 传入的指令为准。文件工具、持久进程、官方任务技能与长任务策略归 TB3，网页归 TB5。
- 官方 `reward` 原值及分项照存。另记候选运行结局、工具超时、Harbor 基础设施异常、判分与清理。无官方判分时保留为空。短预算实跑标为单题诊断。

## 实施批次

| 批次 | 实施内容 | 完成判据 |
|---|---|---|
| TB2.1 桥接与冻结 | 在 Terminal-Bench 独立环境中新增外部 `BaseAgent` 和诊断 CLI。程序化构造 Harbor `Job`，注册 trial 事件钩子，复用 AgentRunner 和独立评测库，注册单工具。冻结发布包、镜像、提示和工具面，先认领永久槽位再启动一次 Harbor trial | 外部 Agent 能被 Harbor 正常加载。内部槽位、Harbor trial UUID、Chagent session/run ID 可互查。伪客户端验证主环境绑定，错误绑定、重入、宿主工具调用、冻结输入漂移与重复槽位均被拒绝 |
| TB2.2 零付费 Docker 验收 | 使用固定发布包和真实 Docker。先用脚本化合成客户端，分别验 `music-harmony` 的已知正确产物和缺失产物。正确产物来自验收夹具，由控制端确认，保留夹具来源和哈希。随后用 `freight-dispatch-shift` 验证模型驱动命令能访问题目声明的服务接口。另做取消、超时和证据缺失用例 | 正负控制分别由官方 verifier 返回预期原始分数，重复运行不共享会话或容器。工具调用、产物传递及任务/验证资源清理均可查。阻断费用与越界用例不触发真实供应商请求 |
| TB2.3 真实单题诊断 | 前两批通过后，为 `music-harmony` 单独冻结一个真实候选槽位。固定模型、API 参数、请求窗口、预算与超时。验证已核实价目覆盖最坏一次请求，再按该协议执行 | 一次真实候选调用链从 Chagent 经 Harbor 命令工具到官方 verifier，保存原始判分、调用和用量。模型未解出题可如实记录。执行、判分、费用或清理证据缺失时维持未完成或不可核验状态 |

## 代码接缝与证据

1. **复用 AgentRunner 的装配逻辑。** `create_task_runner` 当前硬编码 A–D 的 `TaskExecution`，该适配器依赖本地 Docker 任务状态和固定的 Python 路径。抽出受控的 runner 装配入口，由内部任务与 Harbor 分别传入自己的执行对象。对 Harbor 后端显式声明文件快照不可用，Runner 记录该事实，产物证据以 Harbor collect 为准。保留产品与 E1 的现有入口、工具行为和事件语义。 [现有装配](../src/chagent/bootstrap.py#L244) · [内部执行适配器](../src/chagent/hostexec/task_runner.py)
2. **固定绑定与路径处理。** 利用 `Job.add_hook` 获取 `TrialHookEvent.trial_id`，连同单槽生命周期和外部 Agent 控制目录核对后绑定。以 Runner 的 `on_started` 保存 run ID，工具调用再核对 `ToolContext` 的 task、session、run。编码前复查调用投影及供应商请求边界，测试官方绝对路径与默认工作目录能原样到达 Harbor，控制端路径及密钥不会进请求或命令环境。官方验证器和服务管理接口不注册到工具表。 [Harbor 试验事件](https://github.com/harbor-framework/harbor/blob/fdba28fc411e47e8765420a1a063b4918fce3def/src/harbor/trial/hooks.py#L38) · [工具上下文](../src/chagent/tools/context.py#L21)
3. **复用请求级预算。** `BudgetClient` 负责模型调用数、工具调用数、逐请求预留及用量结算。付费之前，校验发布包与镜像、模型与工具配置、价目、总帽及单槽帽、调用窗口与输出上限。区分候选命令、候选整轮、Harbor 构建与验证时限。关闭供应商自动重试及 Harbor trial 重试，单并发。响应流中断而无用量时，保留未知开销和已认领槽位。 [现有预算门禁](../eval/budgets.py#L34)
4. **单独验证取消与回收。** Harbor 的等待取消会传播到 AgentRunner，需核对 DB 终态、请求用量状态和执行退出证据。工具达到超时或被取消时，宿主 Compose 子进程退出只能证明客户端停止等待。真实 Docker 验收要验证候选残留命令无法继续影响采集。若当前后端无法证明该点，停止该槽并保留不确定状态，交 Harbor 收尾并执行收尾核验；此项未通过前维持零付费验证。Chagent 无权接管或直接销毁任务容器。 [Harbor 收尾代码](https://github.com/harbor-framework/harbor/blob/fdba28fc411e47e8765420a1a063b4918fce3def/src/harbor/trial/trial.py#L601)
5. **保存独立的官方记录。** 新增小型 CLI，提供冻结、执行和报告入口。复用现有账本的槽位认领与不可覆盖写入能力，保持旧评测记录兼容。记录 Harbor trial UUID、发布包和镜像 digest、Chagent 代码与配置身份、请求及工具轨迹、原始 reward、产物哈希、用量和清理证据。类型与阶段可审计，核心文件缺失时报告明确标记。复用 TB1 的按 trial 归属核对 Docker 事件方式。 [TB1 证据与审计脚本](../eval/benchmarks/terminal_bench/README.md#tb1-官方参考解与空操作对照)

TB2.2 将使用真实 Docker。候选预算、供应商价目和单槽额度在 TB2.3 冻结时确定。


## TB2.1 实现与复验

`tb2.py` 使用 TB1 核验过的发布包文件清单固定两道诊断任务，绑定文件执行位、镜像、指令、工具面、依赖锁和 checkout 身份。认领槽位后构造单 trial 的 Harbor Job。`harbor_agent.py` 结合 Harbor 的 trial UUID、主环境 session ID、Agent 日志目录及 Chagent session/run ID 核对每次绑定。命令异常时封闭工具、保留未知状态。Chagent 只释放自己的绑定，报告将 Harbor 清理标为未验证。

TB2.1 的验收使用真正的 Harbor 配置与 Agent 工厂加载、模拟环境执行，与真实 Chagent Runner/PostgreSQL。默认诊断命令是 `pwd`，会保留官方评分字段和诊断范围。正确产物、负控、多服务连接与取消后的实际资源状态在 TB2.2 验证。本批没有付费入口。

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

`tb2 run --out <槽位目录>` 会启动真实 Docker 与官方验证器并永久认领槽位，供下一批运行。可通过 `CHAGENT_EVAL_DATABASE_URL` 设置评测连接，诊断记录写入独立 schema。
