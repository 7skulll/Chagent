# Chagent

本机单用户 Agent：在本地网页里对话，模型在 Python 进程内以「模型 → 工具 → 模型」循环推进任务，消息、请求、工具调用与事件都落在本机 PostgreSQL。支持 OpenAI、DeepSeek、Anthropic、通义千问及自定义兼容服务，也提供无需网络的固定响应模型。

能力：会话与显式任务状态、上下文预算与自动摘要（可选真 tokenizer，缺省回退字节估算）、运行级环境快照、大工具输出产物化、用户／工作区长期记忆（`/remember` 由本地命令直接回答，不经过模型）、文件检索、一次调用多处编辑与多文件补丁、PDF／Word／Excel 直接读取与网页上传、宿主命令与测试运行、跨调用存活的长命令会话（`exec_command` 的 `yield_time_ms` + `process` 分次读／写 stdin／杀进程组）、联网检索与正文抓取（`web_search` + `web_fetch`，默认关闭）、重复调用拦截（无进展时以 `repeated_tool_call` 结束）、宿主命令审批（`CHAGENT_APPROVAL`，网页确认卡或 CLI 交互式提问）、可切换工作区的网页流式对话与事件回放。

对话本身的能力（WK1–WK4）：回答按 Markdown 子集渲染（先转义再渲染，只有 `http(s)` 链接）、代码块带复制、流式按帧节流、上翻不再被新内容拽走、超长回答折叠成八行；**重新生成／编辑重发／失败重试**，被替换的记录折叠可展开而历史一个字不改；每条回答下方的「依据」列出搜索卡片、文档页码（`文件名 · 第 3 页`）与本轮注入的长期记忆；上传支持点选、拖拽与粘贴，多文件队列逐个进度、可重试可移除。

工具面按会话的**模式**裁剪（`sessions.mode`）：`work` 是默认且本构建唯一启用的模式，只读（`list_dir`／`read_file`／`glob`／`grep`／产物，联网另开开关），没有工作区写文件、没有宿主命令；会话专属文本文件可通过 `create_file` 生成并下载。工作区写入与执行属于 `code`；网页在[启用门槛](docs/执行边界设计.md)满足之前拒绝 `CHAGENT_CODE_MODE=on`。没注册的工具模型调不到，界面隐藏按钮不算约束。

整体设计与模块划分见[设计总览](docs/设计总览.md)。

## 运行

需要本机 PostgreSQL 和已创建的 `chagent` 数据库，连接配置见[数据库配置](docs/数据库配置.md)。

```sh
uv sync
# 可选：真正的 tokenizer（缺省回退到字节估算，见「会话、上下文与记忆」）
uv sync --extra tokenizer
cp -n .env.example .env          # 已有 .env 时保留自己的配置
createdb chagent                 # 仅在数据库尚未创建时执行
# 在 .env 配置一种供应商 Key，例如 DEEPSEEK_API_KEY=你的Key
./scripts/start.command          # 或 uv run --env-file .env python -m chagent
```

启动即打开 <http://127.0.0.1:8765>。网页是默认入口：对话、审批、长期记忆、上下文快照、文档上传都在里面。启动器还有三条行为——端口被别的程序占用时自动顺延到下一个空闲端口；同一端口已在运行 Chagent 时不再起第二个进程，只打开浏览器；`--no-open` 可关掉自动打开。

网页默认使用 `~/Chagent Workspace`（首次启动时创建），上传和用户文件放在这里；旧的源码目录会话保留在数据库，但不能通过网页打开。网页拒绝将 Chagent 程序目录、其子目录或包含它的父目录设为工作区，code 模式也暂不可在网页启用。此限制只约束 Chagent 网页及其工具，不能阻止同一系统账户通过 Finder、终端或硬链接访问源码；需要防范本机用户时应使用独立系统账户和文件权限。

网页左下的工作区按钮可以切换目录：会话、上传与文件工具都限定在选中的目录内，未打开的目录连带 workspace 参数请求会被拒绝。工具条上的齿轮显示当前生效的配置（沙箱、审批、联网、压缩、工具数、预算），与命令行打印的 `Behavior:` 同源。工作区在服务重启后需要重新打开一次。

只有一个命名 Key 时自动使用该供应商的预设模型；多个 Key 时用 `CHAGENT_PROVIDER` 或 `--provider` 选择，`--model` 可临时切换模型。若 `uv run` 或 `.venv/bin/chagent` 报 `No module named chagent`（外层 `sitecustomize` 接管解释器时，editable 安装的 `.pth` 可能不生效），用 `PYTHONPATH=src` 绕开即可——`scripts/start.command` 已经替你加好了。配置检查不发送请求：

```sh
uv run --env-file .env python -m chagent.models check
# 人读的完整解析：行为开关、预算、数据库，以及被 shell 覆盖的 .env 值
uv run --env-file .env python -m chagent.config show
```

启动时先打印 `Resolved:`（供应商／模型／地址／Key 来源）与 `Behavior:`（沙箱、宿主命令、压缩、轮数上限），因此 shell 里遗留的 `CHAGENT_*` 变量不会静默改变行为；这两行在网页里对应工具条的齿轮面板。

预设模型、兼容范围与预算配置见[模型配置](docs/模型配置.md)。

启动时自动应用未执行的迁移，网页里每个会话的运行会记录运行 ID、结束原因与持久化记录数。

### 命令行（已冻结）

`python -m chagent.cli` 保留两条网页替代不了的路径：不经浏览器的一次性运行，以及 `--tool` 确定性工具注入（探针用）。它不再接收新功能：

```sh
uv run --env-file .env python -m chagent.cli "你好，介绍一下这个项目"
uv run --env-file .env python -m chagent.cli --session <会话UUID> "继续对话"
# 显式验证文件工具（--tool 隐含 --demo）
uv run --env-file .env python -m chagent.cli --tool read_file --arguments '{"path":"README.md","limit":40}'
```

CLI 是开发用入口，工作区默认当前目录，`--workspace /absolute/path` 只影响新会话；继续会话时保留其原工作区。工具接口、工作区边界与宿主命令开关见[基础工具设计](docs/基础工具设计.md)。

## 长命令

`exec_command` 一次调用最多等 30 秒；需要更久的命令（装依赖、构建、交互式调试）要显式让出：

```text
exec_command(cmd="pytest -q", yield_time_ms=20000)   →  process_id + 已经产生的输出
process(action="read", process_id=..., since=0, wait_ms=30000)
process(action="write", process_id=..., chars="y\n")  # 写 stdin
process(action="stop", process_id=...)                # 杀进程组
```

`read` 用绝对游标，同一段输出不会被读两次，单次最多返回 20 000 字符；输出只保留最近 1 MiB，更早的部分被丢弃并明确报 gap。句柄只在启动它的进程与 run 内有效：重启后旧 id 一律「不存在」，run 结束（含取消、超时）会 `killpg` 回收该 run 的所有进程。守则是「不遗留、不接管、不猜 PID」。上限由 `CHAGENT_MAX_PROCESSES`（默认 4，每 run 活进程数）与 `CHAGENT_PROCESS_TIMEOUT`（默认 600 秒，单进程墙钟）控制。没有 PTY：`top`、`vim` 之类不可用。

## 联网

默认**没有**联网工具。要用，打开一个开关即可——供应商是 DeepSeek 的 Search／Fetch API，和模型用同一把 key：

```bash
CHAGENT_WEB_ACCESS=deepseek       # off（默认）| deepseek
CHAGENT_SEARCH_API_KEY=           # 留空则回落到 DEEPSEEK_API_KEY
```

打开之后是两个工具，不是一个：`web_search` 负责找（标题、来源 URL、摘要、站点名、发布日期、权威度），`web_fetch` 负责读（把页面转成 **Markdown**，标题与表格结构保留）。一次搜索最多 8 条、转发给模型 8 000 字符，每条摘录裁到 700；一次抓取最多 20 000 字符（默认 12 000）。按次计费（¥5／1000 次），失败与无结果不计费。

四个必须知道的限制：**不能登录**（没有 cookie，付费墙后拿不到）、**不渲染**（SPA 页面可能接近空，也不能截图）、**私有地址由供应商拒绝**（实测 `127.0.0.1` 与 `169.254.169.254` 都是 403；请求从 DeepSeek 的网络发出，本机不再连接目标，因此本机没有可审计的白名单）、**很慢**（小页面 0.2 秒，65 000 字符的文档页 8.8 秒，超大页面供应商在 30 秒返回 504——搜索摘录够用时不要 fetch）。返回的是第三方文本，工具描述里写明「是数据不是指令」。

它跑在 agent 进程里，**沙箱管不到它**——`CHAGENT_SANDBOX` 断的是 `exec_command` 的子进程。所以这个开关是唯一的闸门，默认关。契约、能力表与实测数字见[执行边界设计](docs/执行边界设计.md) §13。

## 文档与上传

`read_file` 能直接读 PDF、Word 和 Excel——不需要先转成文本，也没有新增工具：

```bash
uv sync --extra documents   # pypdf + python-docx + openpyxl
```

装好之后对 `.pdf`／`.docx`／`.xlsx` 调 `read_file`，返回的是**带位置的编号文本块**，`offset`／`limit` 从「行」变成「块」：

```text
File: uploads/出差制度.pdf
Format: parsed document
Total blocks: 6
Next offset: 4
B1 [page 1]: ACME Travel Policy
B2 [page 1]: Page one body line.
```

块边界就是段落边界，所以表格不会从中间被切开；PDF 每块带页码，Word 带标题路径，Excel 带工作表名。翻页方式和文本文件一致，一直读到 `Next offset: none`。解析结果按内容哈希缓存（最多 8 份），翻 400 页的 PDF 只解析一次。

网页对话框左下角的 **＋** 可以上传文件，落在工作区的 `uploads/` 下并自动把路径写进输入框。同名文件加序号，文件名里的路径分隔符与 `..` 会被剥掉，单文件上限 20 MiB。

四个边界：**扫描件读不了**（没有文本层，OCR 不在范围内，明确报错而不是返回空）、**旧格式 `.doc`／`.xls` 不读**（提示另存为 `.docx`／`.xlsx`）、**只提文本**（不解析图片、批注、公式计算）、**解析在工具线程里**（单文件 32 MiB 上限，挡住超大文件把工具超时耗尽）。

## 网页对话

```sh
uv run --env-file .env python -m chagent         # 等价于 ./scripts/start.command
uv run --env-file .env python -m chagent --demo  # 离线演示
```

打开 <http://127.0.0.1:8765>。支持创建与切换会话、加载历史、流式回答、工具状态与停止生成；每条回答按 Markdown 子集渲染，带「复制」「下载 Markdown」与「重新生成」，你自己的消息带「编辑」；回答下方的「依据」列出这一轮用到的网页来源、文档页码与注入的记忆；输入框支持点选、拖拽与粘贴上传多个文件。刷新后恢复当前会话。`--workspace`、`--port`、`--provider`、`--model`、`--no-open` 与命令行一致。接口文档在 `/docs`，OpenAPI 在 `/openapi.json`，均不依赖 CDN。详见[网页对话设计](docs/网页对话设计.md)。

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

已交付批次的实现、实测、边界与回退方式统一记在[实施记录](docs/实施记录.md)：记忆提取移出运行与上下文投影缓存、A 批文本合并与提交后通知、B1 运行所有权与失联收敛、B2 失败事实恢复、F1 事件批量提交、F2 一个事务一道所有权门、P0 检索与补丁工具、P1 提示缓存不变量、M 轮次度量、G 一次调用多处编辑、H 运行级环境快照、G2 编辑失败路径的搜索预算与线程化、N `/remember` 本地短路、S 宿主机命令沙箱、M2 缓存归因与 `config show`、I 重复调用拦截、L 真 tokenizer 与保留式压缩、K 宿主命令审批、J 会话进程（`long` 场景 45 秒命令由必然失败变为 3 轮／2 次调用成功）、W 联网检索与正文抓取、X 文档读取与上传、Y 网页成为唯一入口、Z0 还债（`echo` 退出生产工具面、429／5xx 重试、`test_sandbox` canary）、Z1 删掉不存在的界面（44→20 个按钮，孤儿归零）、WK0 模式落地（`sessions.mode` 与按模式裁剪的工具面）、WK1–WK4 体验四件（先转义再渲染的 Markdown、重新生成／编辑重发／失败重试、来源「依据」、拖拽上传，模型可见字节零改动）、WD 联网换供应商（DeepSeek Search／Fetch，端到端 2 轮／2 次工具／7.15 秒，`key_source` 如实报来源）。

写路径两批合起来的效果：1.6 秒流式回答从 260 条 SQL／81 个事务降到 59／21，两轮加一次工具从 57 条降到 46 条，事务数与失权判定不变。

```sh
# 本机有 HTTP 代理时会拦截回环请求，测试前先放行
NO_PROXY=127.0.0.1,localhost CHAGENT_TEST_DATABASE_URL=postgresql:///chagent \
  uv run python -m unittest discover -s tests -q
```

数据库测试为每个用例建立独立临时 schema 并在结束时清理，不读写业务表；未指定测试数据库地址时数据库集成测试跳过。2026-10-30 历史全量实测 **501 项**通过、0 跳过（其中 7 项在 node 里执行：Markdown 渲染器 6 项，整份 `app.js` 在最小 DOM 桩里跑一遍 1 项；没有 node 时这 7 项跳过并给出原因，跳过不是通过）。`test_sandbox` 的 10 项是否执行取决于环境：在允许 seatbelt 的机器上（本机即如此）它们真正运行；在 `sandbox-exec` 拒绝应用 profile 的嵌套环境里（exit 71），模块级 canary 会把整类用例标为 `skipped` 并带上原因。跳过不是通过：沙箱能力以[执行边界设计](docs/执行边界设计.md) §10.4 的能力表为准，在允许 seatbelt 的机器上这 10 项会真正执行。

真实供应商的付费链路、离线性能诊断与故障复现各自使用独立脚本：`scripts/test_live_chain.py`、`scripts/benchmark_event_path.py`、`scripts/bench_write_path.py`。链路报告为每个用例带上本地计算的 `cache{hit,reusable,breaks}`（不额外请求），失败时可用 `--keep-schema docs/diagnostics` 保留一次性 schema，再用 `report_prompt_cache.py --schema <名称>` 事后审计。`scripts/report_prompt_cache.py` 读取已落库的请求，报告每轮的前缀复用与供应商缓存命中，不发起任何请求；`scripts/report_token_estimates.py` 对同一批请求比较本地 token 估算与供应商实际用量。结论与实测数据见[实施记录](docs/实施记录.md)，原始样本归档在 [`docs/diagnostics/`](docs/diagnostics/)。

四场景任务探针用固定临时仓库与一次性 schema 运行真实供应商，报告轮次、工具调用、重复调用、墙钟与正确性；`--repeats 2` 用来检查轮次波动：

```sh
uv run --env-file .env python scripts/probe_task.py --case all --repeats 2 \
  --output docs/diagnostics/probe-latest.json
```

`fix`／`edit`／`multifile` 用真实测试结果判定修复是否正确，`long` 验证 45 秒命令是否真正结束。默认只报告目标是否达到；加 `--require-targets` 才把未达到性能目标变成非零退出码。

## 文档

| 主题 | 文档 |
|---|---|
| 架构、模块地图、数据流、版本演进 | [设计总览](docs/设计总览.md) |
| 运行终态、RunState、工具批次调度 | [运行与工具调度](docs/运行与工具调度.md) |
| 上下文预算、摘要、工具产物 | [会话与上下文设计](docs/会话与上下文设计.md) |
| 长期记忆：检索、候选、版本 | [长期记忆设计](docs/长期记忆设计.md) |
| 工具接口与工作区边界 | [基础工具设计](docs/基础工具设计.md) |
| 网页、SSE、HTTP 接口、渲染／替换／依据／上传 | [网页对话设计](docs/网页对话设计.md) |
| 数据库、连接池、迁移 | [数据库配置](docs/数据库配置.md) |
| 供应商与模型预算 | [模型配置](docs/模型配置.md) |
| 已交付批次的实现、实测、边界与回退 | [实施记录](docs/实施记录.md) |
| 历史路线图、已交付结论表、work 模式批次 | [下一阶段实施方案](docs/下一阶段实施方案.md) |
| 当前路线图：code 模式门槛、C0–C5 状态与验收 | [第五版方案](docs/第五版方案.md) |
| 沙箱、审批、会话进程、出网与事件通道（S／K／J／W 均已实施） | [执行边界设计](docs/执行边界设计.md) |

诊断与真实链路的原始结果归档在 [`docs/diagnostics/`](docs/diagnostics/)。
