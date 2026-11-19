# Terminal-Bench TB0 预检

2026-11-19 固定 Harbor 0.25.0 和 Terminal-Bench 4.1.0。选择 4.1.0 的完整发行集，包含 66 题及独立验证器，覆盖计划中的 CPU、GPU 和多服务边界。版本、Git 提交、任务树及官方镜像清单哈希见 [pins.json](pins.json)。Harbor 在独立 Python 3.13 环境中由 [uv.lock](uv.lock) 固定，实测版本为 CPython 3.13.9，锁定 95 个包。 [Harbor 0.25.0](https://github.com/harbor-framework/harbor/releases/tag/v0.25.0) · [Terminal-Bench 4.1.0](https://github.com/harbor-framework/terminal-bench/releases/tag/v4.1.0)

## 实测结果

- 复用了本机已有任务快照，按官方提交的 Git 树重算目录对象，核对全部 2,903 个文件的 Git blob、执行位及文件集合，再复制到本机 `eval/results/tb0-20261119/tasks-v4.1.0`，对副本重复预检。66/66 题由 Harbor 的 `Task(path)` 成功解析，任务级 SHA-256 和静态能力见 [compatibility.json](compatibility.json)。
- 66 题均使用独立 verifier，无多步骤声明，Agent 时限均为 28,800 秒。11 题声明环境 Compose，5 题有 collect hook，1 题声明技能目录，3 题要求加速器。默认参数下 Agent 阶段均解析为 `public`，64 题 verifier 为 `public`，2 题为 `no-network`。矩阵里的费用信号来自静态扫描，其空值仅表示扫描未命中。构建、验证时的实际联网与使用量需通过执行确认。
- 官方镜像清单覆盖 157 个镜像条目。固定提交的发布脚本指定 `linux/amd64`。另按镜像 digest 查询注册表，核实 8 个候选任务的 20 个镜像均只声明 `linux/amd64`，见 [registry-platforms.json](registry-platforms.json)。Compose 的外部依赖镜像另列在矩阵中。Darwin arm64 本机有 8 个逻辑 CPU、24 GiB 内存。Docker daemon 未运行，VM 资源分配、镜像可拉取性和 amd64 模拟执行均待 TB1 复核。 [官方构建脚本](https://github.com/harbor-framework/terminal-bench/blob/4070d6cef56a7d20cb5dc98d5e10ada88a6b9440/scripts/release-build/release_prebuilt.py)
- 单服务首题选 `music-harmony@2.2.1`。2 CPU、4 GiB 内存声明、约 10 GiB 存储声明、无 GPU、无 Compose。解析确认有一个声明产物，独立验证时限 60 秒。验证入口调用本地 Python 检查，依赖锁包含 RewardKit 0.2.4 和 music21 7.3.3，人工检查入口和直接依赖时未发现模型调用。
- 多服务候选选 `freight-dispatch-shift@2.2.0`。同为 2 CPU、4 GiB 内存声明和约 10 GiB 存储声明，`main` 依赖通过健康检查的 `event-feed`。解析确认有一个声明产物，独立验证时限 300 秒。验证入口为本地检查，包含回环 HTTP 模拟服务。该题用于验证 Compose 服务链，另选声明 sidecar collect 的任务覆盖跨服务产物采集。

## 接入接口

已将以下接口所在的 11 个 Python 文件从固定提交下载，与实际安装的 wheel 逐字节核对。文件摘要见 [source-audit.json](source-audit.json)。

| 接缝 | 已核实的调用方式 |
|---|---|
| 外部 Agent | `AgentFactory.create_agent_from_import_path` 加载 `BaseAgent` 子类。实现 `setup(environment)` 与 `run(instruction, environment, context)`。Harbor 在调用前按任务设置默认执行用户。 [工厂](https://github.com/harbor-framework/harbor/blob/fdba28fc411e47e8765420a1a063b4918fce3def/src/harbor/agents/factory.py#L248) · [接口](https://github.com/harbor-framework/harbor/blob/fdba28fc411e47e8765420a1a063b4918fce3def/src/harbor/agents/base.py#L670) |
| 命令与文件 | `BaseEnvironment.exec(command, cwd, env, timeout_sec, user)` 返回 `ExecResult`。Docker 实现默认进入 `main`，另有按服务执行和文件传输方法。TB2 应把 Agent 工具绑定到主环境。超时及取消可结束宿主 Compose 子进程，容器内部后代进程的行为要实跑验证。 [Docker 执行](https://github.com/harbor-framework/harbor/blob/fdba28fc411e47e8765420a1a063b4918fce3def/src/harbor/environments/docker/docker.py#L1355) |
| 采集与验证 | `SingleStepTrial._run` 依序运行 Agent、上传日志、收集产物、停止 Agent 环境，再启动独立 verifier。需要 sidecar 产物时，`_collect_artifacts_phased` 先采集 main、尝试停止 main，再采集 sidecar。验证器接收采集结果，从 `reward.json` 或 `reward.txt` 读取官方结果。 [Trial](https://github.com/harbor-framework/harbor/blob/fdba28fc411e47e8765420a1a063b4918fce3def/src/harbor/trial/single_step.py#L37) · [采集](https://github.com/harbor-framework/harbor/blob/fdba28fc411e47e8765420a1a063b4918fce3def/src/harbor/trial/trial.py#L1623) · [判分](https://github.com/harbor-framework/harbor/blob/fdba28fc411e47e8765420a1a063b4918fce3def/src/harbor/verifier/verifier.py#L198) |
| 取消与清理 | `Trial.run` 在取消后记录异常、尝试恢复输出，并在 `finally` 调用 `_finalize`。Agent 和独立 verifier 的环境停止由 Harbor 调用，Chagent 记录槽位和成本。TB1 需观察实际 Docker 资源回收，包含异常退出。 [取消与收尾](https://github.com/harbor-framework/harbor/blob/fdba28fc411e47e8765420a1a063b4918fce3def/src/harbor/trial/trial.py#L601) |

## 复验

在 Chagent 仓库根目录运行。已有任务快照时，将最后一步的 `--tasks` 指向已有快照即可，预检会校验其内容和执行位。

```sh
tb=eval/benchmarks/terminal_bench
work=eval/results/tb0-recheck
mkdir -p "$work"
git clone --depth 1 --branch v4.1.0 https://github.com/harbor-framework/terminal-bench.git "$work/upstream"
test "$(git -C "$work/upstream" rev-parse HEAD)" = 4070d6cef56a7d20cb5dc98d5e10ada88a6b9440
curl -fsSL --compressed 'https://api.github.com/repos/harbor-framework/terminal-bench/git/trees/d9f7ae82365a09d0727b01c7040ba4d4fc1d7986?recursive=1' -o "$work/tasks-tree.json"
curl -fsSL 'https://github.com/harbor-framework/terminal-bench/releases/download/v4.1.0/prebuilt-images.json' -o "$work/prebuilt-images.json"
uv run --locked --project "$tb" --python 3.13 python "$tb/preflight.py" \
  --tasks "$work/upstream/tasks" --git-tree "$work/tasks-tree.json" \
  --images "$work/prebuilt-images.json" --registry-evidence "$tb/registry-platforms.json" \
  --output "$work/compatibility.json"
```

下节记录 TB1 使用的精确发行版发布包、Docker 实跑和原始证据。

## TB1 官方参考解与空操作对照

2026-11-19 从 Harbor Hub 解析完整数据集 `terminal-bench/terminal-bench@4.1.0`（66 题），得到固定数据集 digest `sha256:5881c1b1c74a96f07baef00a7c869e09802704bc0dc912c870e7acccc11440da`。按数据集记录的任务 digest 下载两题。发布包的 17 和 88 个文件均与 Hub 文件清单的 SHA-256 及大小一致。与 TB0 的 Git 源文件相比，首题只改动了 `task.toml`，多服务题还改动了 Compose 配置。发布包里声明的五个预构建镜像与官方清单一致，实跑使用发布包。 [Harbor Hub 4.1.0](https://hub.harborframework.com/datasets/terminal-bench/terminal-bench/5)

| 任务 | 官方 oracle | 官方 nop | 产物 |
|---|---:|---:|---|
| music-harmony@2.2.1 | 1.0（96.934 秒） | 0.0（14.072 秒） | oracle 收集到 `/app/harmony.mxl`，nop 缺失 |
| freight-dispatch-shift@2.2.0 | 1.0（33.130 秒） | 0.0（20.625 秒） | oracle 收集到 `/workspace/dispatch`，nop 缺失 |

四槽均用 Harbor 0.25.0 内置 Agent、独立 verifier，保留原始 `reward.json`，退出码为 0、`exception_info=null`。Docker Desktop 29.4.1 / Compose v5.1.3 在 Apple Silicon 上执行固定 `linux/amd64` 镜像，镜像烟测返回 `x86_64`。未调整任务资源、网络或时限，单并发，每组一槽，零重试。

两次多服务任务均确认 `event-feed` 先健康、`main` 后启动。四槽均在 Agent 环境销毁后才创建 verifier，实际镜像 digest 与发布包一致。Docker 事件记录 10 个独立容器和 8 个独立网络全部销毁，每槽前后的容器、网络和卷清单一致。本批验证了多服务健康依赖和主服务产物采集，sidecar collect hook 仍待专项。原始结果、产物、镜像引用与 Docker 事件均保存在本机 `eval/results/tb1-20261119`，摘要及证据哈希见 [tb1-evidence.json](tb1-evidence.json)。本批零候选模型调用。

### TB1 复验命令

从仓库根目录执行，输出路径使用新的目录。下载的两个任务摘要来自 4.1.0 数据集元数据，文件哈希和镜像摘要记录在上述证据中。预检 Docker 的 amd64 执行能力后，按表中顺序运行四槽。

```sh
tb="$PWD/eval/benchmarks/terminal_bench"
work="$PWD/eval/results/tb1-recheck"
mkdir -p "$work"
cd "$work"
export HARBOR_TELEMETRY=0
"$tb/.venv/bin/harbor" tasks download \
  'terminal-bench/music-harmony@sha256:b77713c760d519822ee4f0da9e555c9e124241c2544d0e759a11b52eb7d26c24' -o "$work/published"
"$tb/.venv/bin/harbor" tasks download \
  'terminal-bench/freight-dispatch-shift@sha256:5f6e51724b93f5c5fdfbd8836cd6bd84962c51f2242ff52f4067a6ba4085d9df' -o "$work/published"
for task in music-harmony freight-dispatch-shift; do
  for arm in oracle nop; do
    env -i HOME="$HOME" PATH="$PATH" DOCKER_DEFAULT_PLATFORM=linux/amd64 HARBOR_TELEMETRY=0 \
      "$tb/.venv/bin/harbor" run --path "$work/published/$task" --agent "$arm" \
      --env docker --jobs-dir "$work/jobs" --job-name "$task-$arm" \
      --n-concurrent 1 --n-attempts 1 --max-retries 0 --delete --no-stream || exit 1
  done
done
```
