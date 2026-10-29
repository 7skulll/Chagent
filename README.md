# Chagent

本机单用户 Agent：命令行或本地网页输入，模型在 Python 进程内以「模型 → 工具 → 模型」循环推进任务，消息、请求、工具调用与事件都落在本机 PostgreSQL。支持 OpenAI、DeepSeek、Anthropic、通义千问及自定义兼容服务，也提供无需网络的固定响应模型。

能力：会话与显式任务状态、上下文预算与自动摘要、大工具输出产物化、用户／工作区长期记忆、基础文件与宿主命令工具、网页流式对话与事件回放。

整体设计与模块划分见[设计总览](docs/设计总览.md)。

## 运行

需要本机 PostgreSQL 和已创建的 `chagent` 数据库，连接配置见[数据库配置](docs/数据库配置.md)。

```sh
uv sync
cp -n .env.example .env          # 已有 .env 时保留自己的配置
createdb chagent                 # 仅在数据库尚未创建时执行
# 在 .env 配置一种供应商 Key，例如 DEEPSEEK_API_KEY=你的Key
uv run --env-file .env python -m chagent "你好，介绍一下这个项目"
```

只有一个命名 Key 时自动使用该供应商的预设模型；多个 Key 时用 `CHAGENT_PROVIDER` 或 `--provider` 选择，`--model` 可临时切换模型。若 `uv run` 报 `No module named chagent`（外层 `sitecustomize` 接管解释器时，editable 安装的 `.pth` 可能不生效），用 `PYTHONPATH=src uv run --env-file .env python -m chagent ...` 绕开即可。配置检查不发送请求：

```sh
uv run --env-file .env python -m chagent.models check
uv run --env-file .env python -m chagent --demo "无需 API Key 的离线演示"
```

预设模型、兼容范围与预算配置见[模型配置](docs/模型配置.md)。

启动时自动应用未执行的迁移，输出会话 ID、运行 ID、结束原因、最终回答与持久化记录数。继续同一会话：

```sh
uv run --env-file .env python -m chagent --session <会话UUID> "继续对话"
# 显式验证文件工具（--tool 隐含 --demo）
uv run --env-file .env python -m chagent --tool read_file --arguments '{"path":"README.md","limit":40}'
```

工作区默认当前目录，`--workspace /absolute/path` 只影响新会话；继续会话时保留其原工作区。工具接口、工作区边界与宿主命令开关见[基础工具设计](docs/基础工具设计.md)。

## 网页对话

```sh
uv run --env-file .env python -m chagent.web
uv run --env-file .env python -m chagent.web --demo    # 离线演示
```

打开 <http://127.0.0.1:8765>。支持创建与切换会话、加载历史、流式回答、工具状态与停止生成；刷新后恢复当前会话。`--workspace`、`--port`、`--provider`、`--model` 与命令行一致。接口文档在 `/docs`，OpenAPI 在 `/openapi.json`，均不依赖 CDN。详见[网页对话设计](docs/网页对话设计.md)。

## 会话、上下文与记忆

```sh
uv run --env-file .env python -m chagent.sessions list --limit 20
uv run --env-file .env python -m chagent.sessions history <会话UUID> --limit 20
uv run --env-file .env python -m chagent.sessions context <会话UUID> --limit 5
uv run --env-file .env python -m chagent.sessions summaries <会话UUID> --limit 10

uv run --env-file .env python -m chagent.memory list
```

- 会话支持重命名、归档／恢复与带版本的显式任务状态；上下文接近预算时压缩旧轮次，保留原始输入和最新完整交换；大工具输出保存为产物，模型与网页都可分页读取。见[会话与上下文设计](docs/会话与上下文设计.md)。
- 长期记忆支持关键词检索、候选审核、来源、冲突、编辑与删除；网页右上角"长期记忆"或 `/remember workspace project.database = 使用 PostgreSQL` 均可写入。见[长期记忆设计](docs/长期记忆设计.md)。
- 自动记忆提取写在 `memory_jobs` 队列里，由进程内 worker 在运行结束后处理：回答不必等第二次模型调用。单次命令在打印结果后处理一次队列，未完成的任务留到下次启动并明确提示；网页由常驻 worker 处理。
- 上下文按持久消息 ID 缓存投影、按累计成本选择轮次。数据与边界见[实施记录](docs/实施记录.md) 第 1 节。

## 验证

已交付批次的实现、实测、边界与回退方式统一记在[实施记录](docs/实施记录.md)：记忆提取移出运行与上下文投影缓存、A 批文本合并与提交后通知、B1 运行所有权与失联收敛、B2 失败事实恢复、F1 事件批量提交、F2 一个事务一道所有权门。

写路径两批合起来的效果：1.6 秒流式回答从 260 条 SQL／81 个事务降到 59／21，两轮加一次工具从 57 条降到 46 条，事务数与失权判定不变。

```sh
# 本机有 HTTP 代理时会拦截回环请求，测试前先放行
NO_PROXY=127.0.0.1,localhost CHAGENT_TEST_DATABASE_URL=postgresql:///chagent \
  uv run python -m unittest discover -s tests -q
```

数据库测试为每个用例建立独立临时 schema 并在结束时清理，不读写业务表；未指定测试数据库地址时数据库集成测试跳过。当前全量 **208 项通过**。

真实供应商的付费链路、离线性能诊断与故障复现各自使用独立脚本：`scripts/test_live_chain.py`、`scripts/benchmark_event_path.py`、`scripts/diagnose_main_path.py`、`scripts/bench_write_path.py`。结论与实测数据见[实施记录](docs/实施记录.md)与[执行效率优化](docs/执行效率优化.md)，原始样本归档在 [`docs/diagnostics/`](docs/diagnostics/)。

## 文档

| 主题 | 文档 |
|---|---|
| 架构、模块地图、数据流、版本演进 | [设计总览](docs/设计总览.md) |
| 运行终态、RunState、工具批次调度 | [运行与工具调度](docs/运行与工具调度.md) |
| 上下文预算、摘要、工具产物 | [会话与上下文设计](docs/会话与上下文设计.md) |
| 长期记忆：检索、候选、版本 | [长期记忆设计](docs/长期记忆设计.md) |
| 工具接口与工作区边界 | [基础工具设计](docs/基础工具设计.md) |
| 网页、SSE、HTTP 接口 | [网页对话设计](docs/网页对话设计.md) |
| 数据库、连接池、迁移 | [数据库配置](docs/数据库配置.md) |
| 供应商与模型预算 | [模型配置](docs/模型配置.md) |
| 已交付批次的实现、实测、边界与回退 | [实施记录](docs/实施记录.md) |
| 写入路径实测与收敛方案（F3 待实施） | [执行效率优化](docs/执行效率优化.md) |
| 路线图、诊断基线、未实施项 | [下一阶段实施方案](docs/下一阶段实施方案.md) |
| 沙箱、审批与事件通道（方案） | [执行边界设计](docs/执行边界设计.md) |

诊断与真实链路的原始结果归档在 [`docs/diagnostics/`](docs/diagnostics/)。
