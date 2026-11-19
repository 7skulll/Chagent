# Terminal-Bench TB0 预检

2026-11-19 固定 Harbor 0.25.0 和 Terminal-Bench 4.1.0。选择 4.1.0 的完整发行集，包含 66 题及独立验证器，覆盖计划中的 CPU、GPU 和多服务边界。版本、Git 提交、任务树及官方镜像清单哈希见 [pins.json](pins.json)。Harbor 在独立 Python 3.13 环境中由 [uv.lock](uv.lock) 固定，实测版本为 CPython 3.13.9，锁定 95 个包。 [Harbor 0.25.0](https://github.com/harbor-framework/harbor/releases/tag/v0.25.0) · [Terminal-Bench 4.1.0](https://github.com/harbor-framework/terminal-bench/releases/tag/v4.1.0)

## 实测结果

- 复用了本机已有任务快照，按官方提交的 Git 树重算目录对象，核对全部 2,903 个文件的 Git blob、执行位及文件集合，再复制到本机 `eval/results/tb0-20261119/tasks-v4.1.0`，对副本重复预检。66/66 题由 Harbor 的 `Task(path)` 成功解析，任务级 SHA-256 和静态能力见 [compatibility.json](compatibility.json)。
- 66 题均使用独立 verifier，无多步骤声明，Agent 时限均为 28,800 秒。11 题声明环境 Compose，5 题有 collect hook，1 题声明技能目录，3 题要求加速器。默认参数下 Agent 阶段均解析为 `public`，64 题 verifier 为 `public`，2 题为 `no-network`。矩阵里的费用信号来自静态扫描，其空值仅表示扫描未命中。构建、验证时的实际联网与使用量需通过执行确认。
- 官方镜像清单覆盖 157 个镜像条目。固定提交的发布脚本指定 `linux/amd64`。另按镜像 digest 查询注册表，核实 8 个候选任务的 20 个镜像均只声明 `linux/amd64`，见 [registry-platforms.json](registry-platforms.json)。Compose 的外部依赖镜像另列在矩阵中。Darwin arm64 本机有 8 个逻辑 CPU、24 GiB 内存。TB0 当时未启动 Docker。后续 TB1 已核实本机 amd64 执行和两道发布任务的镜像与运行链路，其余题目仍以静态预检为准。 [官方构建脚本](https://github.com/harbor-framework/terminal-bench/blob/4070d6cef56a7d20cb5dc98d5e10ada88a6b9440/scripts/release-build/release_prebuilt.py)
- 单服务首题选 `music-harmony@2.2.1`。2 CPU、4 GiB 内存声明、约 10 GiB 存储声明、无 GPU、无 Compose。解析确认有一个声明产物，独立验证时限 60 秒。验证入口调用本地 Python 检查，依赖锁包含 RewardKit 0.2.4 和 music21 7.3.3，人工检查入口和直接依赖时未发现模型调用。
- 多服务候选选 `freight-dispatch-shift@2.2.0`。同为 2 CPU、4 GiB 内存声明和约 10 GiB 存储声明，`main` 依赖通过健康检查的 `event-feed`。解析确认有一个声明产物，独立验证时限 300 秒。验证入口为本地检查，包含回环 HTTP 模拟服务。该题用于验证 Compose 服务链，另选声明 sidecar collect 的任务覆盖跨服务产物采集。

## 接入接口

已将以下接口所在的 11 个 Python 文件从固定提交下载，与实际安装的 wheel 逐字节核对。文件摘要见 [source-audit.json](source-audit.json)。

| 接缝 | 已核实的调用方式 |
|---|---|
| 外部 Agent | `AgentFactory.create_agent_from_import_path` 加载 `BaseAgent` 子类。实现 `setup(environment)` 与 `run(instruction, environment, context)`。Harbor 在调用前按任务设置默认执行用户。 [工厂](https://github.com/harbor-framework/harbor/blob/fdba28fc411e47e8765420a1a063b4918fce3def/src/harbor/agents/factory.py#L248) · [接口](https://github.com/harbor-framework/harbor/blob/fdba28fc411e47e8765420a1a063b4918fce3def/src/harbor/agents/base.py#L670) |
| 命令与文件 | `BaseEnvironment.exec(command, cwd, env, timeout_sec, user)` 返回 `ExecResult`。Docker 实现默认进入 `main`，另有按服务执行和文件传输方法。TB2 应把 Agent 工具绑定到主环境。超时及取消可结束宿主 Compose 子进程，容器内部后代进程的行为要实跑验证。 [Docker 执行](https://github.com/harbor-framework/harbor/blob/fdba28fc411e47e8765420a1a063b4918fce3def/src/harbor/environments/docker/docker.py#L1355) |
| 采集与验证 | `SingleStepTrial._run` 依序运行 Agent、上传日志、收集产物、停止 Agent 环境，再启动独立 verifier。需要 sidecar 产物时，`_collect_artifacts_phased` 先采集 main、尝试停止 main，再采集 sidecar。验证器接收采集结果，从 `reward.json` 或 `reward.txt` 读取官方结果。 [Trial](https://github.com/harbor-framework/harbor/blob/fdba28fc411e47e8765420a1a063b4918fce3def/src/harbor/trial/single_step.py#L37) · [采集](https://github.com/harbor-framework/harbor/blob/fdba28fc411e47e8765420a1a063b4918fce3def/src/harbor/trial/trial.py#L1623) · [判分](https://github.com/harbor-framework/harbor/blob/fdba28fc411e47e8765420a1a063b4918fce3def/src/harbor/verifier/verifier.py#L198) |
| 取消与清理 | `Trial.run` 在取消后记录异常、尝试恢复输出，并在 `finally` 调用 `_finalize`。Agent 和独立 verifier 的环境停止由 Harbor 调用，Chagent 记录槽位和成本。TB1 已核对四个正常结束槽位的资源回收，异常退出仍待专项。 [取消与收尾](https://github.com/harbor-framework/harbor/blob/fdba28fc411e47e8765420a1a063b4918fce3def/src/harbor/trial/trial.py#L601) |

## 复验

从 Chagent 仓库根目录运行。权威任务树是固定提交的 GitHub `tasks` 子树，根 ID 为 `d9f7ae82365a09d0727b01c7040ba4d4fc1d7986`。历史复验可使用本机的 `eval/results/tb0-20261119/tasks-git-tree.gz` 与已验证快照 `tasks-v4.1.0`，同时设置 `--git-tree` 和 `--tasks`。`preflight.py` 会验证文件集合、Git 对象、内容和执行位。四份历史截断下载均保存在单独的 `eval/results/tb0-20261119-incomplete-downloads/`，接口源码证据保存在完整的 `harbor-source/` 中。

矩阵的 `verifier.declared_resources` 对应 Git 源码显式声明，`resolved_resources` 与 `resource_source` 按固定 Harbor 版本解析。66 道题均使用独立 verifier，其中 60 道继承任务环境配置。两道 TB1 题还在 `published_package` 记录已核验发布包的数据，其他题的发布包字段为空。两题都解析为 2 CPU、4096 MB 内存、10240 MB 存储配置。实际负载与后端约束另看运行证据，TB1 的 Agent 和 verifier 先后执行。

```sh
tb="$PWD/eval/benchmarks/terminal_bench"
work="$PWD/eval/results/tb0-recheck"
mkdir -p "$work"
uv sync --locked --project "$tb" --python 3.13
git clone --depth 1 --branch v4.1.0 https://github.com/harbor-framework/terminal-bench.git "$work/upstream"
test "$(git -C "$work/upstream" rev-parse HEAD)" = 4070d6cef56a7d20cb5dc98d5e10ada88a6b9440 || exit 1
curl -fsSL --compressed 'https://api.github.com/repos/harbor-framework/terminal-bench/git/trees/d9f7ae82365a09d0727b01c7040ba4d4fc1d7986?recursive=1' -o "$work/tasks-tree.json.part" || exit 1
curl -fsSL 'https://github.com/harbor-framework/terminal-bench/releases/download/v4.1.0/prebuilt-images.json' -o "$work/prebuilt-images.json.part" || exit 1
"$tb/.venv/bin/python" "$tb/preflight.py" \
  --tasks "$work/upstream/tasks" --git-tree "$work/tasks-tree.json.part" \
  --images "$work/prebuilt-images.json.part" --output "$work/compatibility.json" || exit 1
mv "$work/tasks-tree.json.part" "$work/tasks-tree.json"
mv "$work/prebuilt-images.json.part" "$work/prebuilt-images.json"

"$tb/.venv/bin/python" "$tb/scripts/verify-sources.py" --output "$work"
"$tb/.venv/bin/python" "$tb/scripts/probe-platforms.py" \
  --images "$work/prebuilt-images.json" --output "$work/registry-platforms.json"
```

`verify-sources.py` 可加 `--source-dir` 从现有接口文件离线复验。要复算包含两道发布包数据的完整矩阵，使用生成的 `--registry-evidence`，并传入下一节生成的 `--published-root "$work/published"` 和 `--published-evidence "$work/published-evidence.json"`。


## TB1 官方参考解与空操作对照

2026-11-19 从 Harbor Hub 解析完整数据集 `terminal-bench/terminal-bench@4.1.0`（66 题），得到固定数据集 digest `sha256:5881c1b1c74a96f07baef00a7c869e09802704bc0dc912c870e7acccc11440da`。按数据集记录的任务 digest 下载两题。发布包的 17 和 88 个文件均与 Hub 文件清单的 SHA-256 及大小一致。与 TB0 的 Git 源文件相比，首题只改动了 `task.toml`，多服务题还改动了 Compose 配置。发布包里声明的五个预构建镜像与官方清单一致，实跑使用发布包。 [Harbor Hub 4.1.0](https://hub.harborframework.com/datasets/terminal-bench/terminal-bench/5)

| 任务 | 官方 oracle | 官方 nop | 产物 |
|---|---:|---:|---|
| music-harmony@2.2.1 | 1.0（96.934 秒） | 0.0（14.072 秒） | oracle 收集到 `/app/harmony.mxl`，nop 缺失 |
| freight-dispatch-shift@2.2.0 | 1.0（33.130 秒） | 0.0（20.625 秒） | oracle 收集到 `/workspace/dispatch`，nop 缺失 |

四槽均用 Harbor 0.25.0 内置 Agent、独立 verifier，保留原始 `reward.json`，退出码为 0、`exception_info=null`。Docker Desktop 29.4.1 / Compose v5.1.3 在 Apple Silicon 上执行固定 `linux/amd64` 镜像，镜像烟测返回 `x86_64`。未调整任务资源、网络或时限，单并发，每组一槽，零重试。

两次多服务任务均确认 `event-feed` 先健康、`main` 后启动。四槽均在 Agent 环境销毁后才创建 verifier，实际镜像 digest 与发布包一致。Docker 事件记录 10 个独立容器和 8 个独立网络全部销毁，每槽前后的容器、网络和卷清单一致。本批验证了多服务健康依赖和主服务产物采集，sidecar collect hook 仍待专项。原始结果、产物、镜像引用与 Docker 事件均保存在本机 `eval/results/tb1-20261119`，摘要及证据哈希见 [tb1-evidence.json](tb1-evidence.json)。本批零候选模型调用。

### TB1 复验命令

从仓库根目录执行，复用上一步已校验的镜像清单。选择独立的 TB1 输出目录时，把清单路径作为 `--images` 传入。准备脚本从固定数据集 digest 下载两份发布包，核验 Hub 的逐文件哈希、大小和官方镜像引用，再生成供审计使用的元数据。

```sh
tb="$PWD/eval/benchmarks/terminal_bench"
work="$PWD/eval/results/tb0-recheck"
HARBOR_TELEMETRY=0 "$tb/.venv/bin/python" "$tb/scripts/prepare-published.py" \
  --results "$work" --images "$work/prebuilt-images.json" || exit 1

docker info --format '{"Architecture":{{json .Architecture}},"OSType":{{json .OSType}},"NCPU":{{json .NCPU}},"MemTotal":{{json .MemTotal}},"ServerVersion":{{json .ServerVersion}}}' > "$work/docker-engine.json" || exit 1
docker compose version > "$work/compose-version.txt" || exit 1
docker run --rm --platform linux/amd64 --network none --entrypoint uname \
  harborframework/terminal-bench@sha256:5ec316b098f4eef4c05e7916314f980dcc27e6be64a096f78bb799705439648f \
  -m > "$work/amd64-smoke.txt" || exit 1
test "$(cat "$work/amd64-smoke.txt")" = x86_64 || exit 1

for task in music-harmony freight-dispatch-shift; do
  for arm in oracle nop; do
    "$tb/.venv/bin/python" "$tb/scripts/run-control.py" \
      --published-root "$work/published" --published-evidence "$work/published-evidence.json" \
      --runs-dir "$work/runs" \
      --task "$task" --agent "$arm" --harbor "$tb/.venv/bin/harbor" || exit 1
  done
done

"$tb/.venv/bin/python" "$tb/scripts/audit-controls.py" \
  --results "$work" --date "$(date +%F)" --output "$work/tb1-evidence.json"
```

复核历史四槽仅需最后的审计命令，把 `--results` 改为 `eval/results/tb1-20261119`、日期改为 `2026-11-19`，并将 `--output` 指向临时文件。审计读取原始结果、判分、产物和 Docker 事件，核对失败会返回错误。新版采集器保存全局清单供诊断，并以 trial 的资源清单及销毁事件判定清理。历史槽位继续使用原有前后清单与事件。


## TB2 外部终端 Agent 现状

`harbor_agent.py` 实现 Harbor `BaseAgent`，把 `exec_command` 绑定到主环境；`tb2.py` 负责发布包与镜像冻结、槽位认领、Job 装配和历史报告；`tb22.py` 提供零付费控制与离线审计入口；`tb23.py` 与 `live_policy.py` 承担真实付费单槽的冻结协议、逐请求预留和来源哈希。专项测试在 `tests/test_tb2.py`、`test_tb22.py`、`test_tb23.py`。

| 批次 | 状态 | 证据 |
|---|---|---|
| TB2.1 桥接与冻结 | 已完成 | 真实 `Job.create` 装配、合成客户端与 PostgreSQL 调用链、单槽真实 Docker 冒烟；`publish`/镜像/指令/工具面/checkout 身份冻结 |
| TB2.2 零付费 Docker 控制 | 已验收 | 五槽：正控 1.0、缺失 0.0、服务探针 0.0、超时与取消未判分；9 容器 8 网络按 ID 核对无残留；两份离线审计可复算 |
| TB2.3 真实单题诊断 | 已执行，未验收 | 一槽、4 次请求、5 次工具、$0.0042975；候选在工具额度处中止，官方 verifier 未启动，原始判分为空 |

TB2.3 的缺口是官方原始判分，不是分数偏低：判据允许 0.0，但 verifier 没有机会运行。根因与两条收口路径见 [TB2 实施方案](../../../docs/Terminal-Bench-TB2实施方案.md#tb23-判据缺口与收口路径)，验收判定见[验收第 96 节](../../../docs/验收记录汇总.md#96-tb23-真实单题诊断触发工具调用上限2026-11-19)。运行与复算命令见该方案文档，不在本文件重复。
