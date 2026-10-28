# Chagent

当前实现：Python 基础类型、PostgreSQL 存储、可持久化的模型/工具循环、基础文件和命令工具、会话与上下文管理，以及本地网页对话。支持配置 API Key 连接 OpenAI、DeepSeek、Anthropic、通义千问及自定义兼容服务。

## 运行

需要本机 PostgreSQL 服务和已创建的 `chagent` 数据库。连接配置参见[数据库配置](docs/数据库配置.md)。

```sh
uv sync
# 首次配置时复制；已有 .env 时保留自己的配置
cp -n .env.example .env
# 数据库尚未创建时执行 createdb chagent
# 在 .env 配置一种供应商 Key，例如 DEEPSEEK_API_KEY=你的Key
uv run --env-file .env python -m chagent "你好，介绍一下这个项目"
```

只有一个命名供应商 Key 时自动使用其预设模型；多个 Key 时使用 `CHAGENT_PROVIDER` 或 `--provider` 选择。具体模型可通过 `--model` 切换。配置检查不会发送请求：

```sh
uv run --env-file .env python -m chagent.models check
uv run --env-file .env python -m chagent --demo "无需 API Key 的离线演示"
```

完整配置、默认模型及兼容范围见[模型配置](docs/模型配置.md)。

运行时自动应用尚未执行的迁移，输出会话 ID、运行 ID、最终回答和持久化记录数量。使用输出的会话 ID 继续：

```sh
uv run --env-file .env python -m chagent --session <会话UUID> "继续对话"
```

工作区默认当前目录，可用 `--workspace /absolute/path` 指定新会话的工作区。继续既有会话时保留其原工作区。`--demo` 明确选择无网络的固定响应模型，默认调用 Echo。

显式验证文件工具：

```sh
uv run --env-file .env python -m chagent --tool read_file --arguments '{"path":"README.md","limit":40}'
```

基础工具的接口、工作区规则与宿主命令开关见[基础工具设计](docs/基础工具设计.md)。

## 网页对话

```sh
uv run --env-file .env python -m chagent.web
# 无需 API Key 的演示：
uv run --env-file .env python -m chagent.web --demo
```

打开 <http://127.0.0.1:8765>。支持创建和切换会话、加载历史、流式回答、工具状态与停止生成；刷新后恢复当前会话。`--workspace` 指定工作区，`--port` 更改端口；`--provider` 和 `--model` 选择服务端配置的模型。

网页服务使用 FastAPI 与 Pydantic 校验接口参数，仍为回环地址上的单进程本地 Agent。离线可用的接口文档在 <http://127.0.0.1:8765/docs>，OpenAPI 定义在 `/openapi.json`；文档资源不依赖 CDN。

详细接口与运行方式见[网页对话设计](docs/网页对话设计.md)。

## 会话与上下文

```sh
uv run --env-file .env python -m chagent.sessions list --limit 20
uv run --env-file .env python -m chagent.sessions history <会话UUID> --limit 20
uv run --env-file .env python -m chagent.sessions context <会话UUID> --limit 5
```

支持会话重命名、归档/恢复，以及带版本号的显式任务状态。上下文接近预算时自动压缩旧轮次或当前运行内已结束的工具批次，保留原始用户输入和最新完整交换。大工具输出保存为产物，模型和网页都可分页读取。第三版增加用户／工作区长期记忆：关键词检索、候选审核、来源、冲突、编辑与删除，网页可查看每轮注入的版本与用量。token 使用可替换的保守估算。

设计说明：[会话与上下文](docs/会话与上下文设计.md)、[第一版摘要](docs/上下文压缩设计.md)、[第二版长任务与工具产物](docs/长任务与工具产物设计.md)、[第三版长期记忆](docs/长期记忆设计.md)。

```sh
uv run --env-file .env python -m chagent.sessions summaries <会话UUID> --limit 10
uv run --env-file .env python -m chagent.memory list
```

网页右上角“长期记忆”管理已生效记忆与候选；聊天可发送 `/remember workspace project.database = 使用 PostgreSQL`。显式指令在运行完成事务内直接写入；自动提取改为入队后由后台 worker 完成，不再占用回答的等待时间——单次命令行在打印结果后处理队列，网页由进程内 worker 持续处理，关停时未完成的任务保留给下次启动。

上下文构建按持久消息 ID 缓存投影，并按累计成本选择轮次，长历史不再重复序列化。设计、实测数据与边界见[关键路径优化](docs/关键路径优化.md)。

## 验证

主链路性能基线、故障复现与分阶段改造方案见[主链路诊断与优化方案](docs/主链路诊断与优化方案.md)。诊断脚本使用独立临时 schema 和固定模型，不调用外部供应商。
已配置供应商 Key 后的付费链路测试、用量和参考设计见[真实链路测试与 Claude 参考设计](docs/真实链路测试与Claude参考设计.md)。真实测试与默认回归分开运行。

前两项运行时改造已完成：[结束控制与 RunState 验收](docs/结束控制与RunState验收.md)。运行结束原因同时保存在返回值、数据库和事件中；工具正常终止允许无最终模型回答；活动历史在提交成功后增量更新。

第三步已完成：[工具批次调度与核心验收](docs/工具批次调度与核心验收.md)。明确可并发的工具有界并行，修改操作形成工作区屏障，结果按调用顺序提交，取消时区分未启动与结果未知。通过 `CHAGENT_MAX_TOOL_CONCURRENCY` 设置并发上限（默认 4）。

```sh
CHAGENT_TEST_DATABASE_URL=postgresql:///chagent uv run python -m unittest discover -s tests -v
```

数据库测试为每个用例建立临时 schema 并清理，不读写业务表的数据。未指定测试数据库地址时，数据库集成测试跳过。

实现边界与数据流见[最小链路说明](docs/最小链路.md)。
