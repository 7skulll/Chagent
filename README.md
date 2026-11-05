# Chagent

本地单用户 Agent：在网页对话，Python 进程调用模型和工具，PostgreSQL 保存可检查的历史、请求、工具结果与事件。

容器入口使用统一的可编程 Agent：同一会话可读写工作区文件、执行命令、运行测试并读取、输入或停止长进程，无需切换 work/code。命令在容器内运行，可以联网安装依赖。长进程可通过 `process(action=write)` 输入文字或关闭 stdin；没有 PTY。右侧文件面板支持文本多标签编辑、手动保存、版本冲突、回收恢复，以及 PDF 分页/图片缩放预览。旧会话的 mode 字段仅作为历史数据保留，运行时使用相同工具面。

新安装请通过 `compose.yaml` 启动；它设置可执行的容器档。已有 `.env` 的 `trusted-development`、`isolated` 或 `workspace-write` 执行配置不能用于本机命令执行，启动时会明确拒绝。

当前生产网页接受 `boundary=container`、`capability=full`，`gate` 可为 `none` 或 `approval`；两者都要求显式声明并通过实际容器检查。`configured` 与只读工作区不会被自动提升。三轴由开关推导，不单独设置。完整行为见[统一可编程执行契约](docs/统一可编程执行契约.md)。

`gate=approval` 同时管两件事：执行类调用（`exec_command`、`run_tests`、`process(write)`）和**会覆盖工作区文件的写入**（`write_file`、`edit_file`、`apply_patch`）。工作区是宿主目录的 bind 挂载，一次覆盖写入不经过任何命令就直接改你的真实文件，所以写入按不可逆性分级审批：新建普通文件不问，覆盖、打补丁、以及在新钩子/配置路径放入文件才问。`compose.yaml` 默认取 `gate=approval`；无人值守运行需显式设 `CHAGENT_APPROVAL=never`。

统一工具面复用现有 Runner、文件与命令工具。容器路径已用真实模型完成 Spring Boot 项目创建、构建、启动和 HTTP 验证；旧 seatbelt 执行路径已移除。此前按模式交付的验收记录仍保留作历史证据；当前行为以本页和代码为准。

**自主建站目标尚未交付。** 当前 Chagent 主控与工具都在容器里；任意宿主工作区切换、持续的网站服务入口和同页宿主审批尚不构成完整闭环。目标架构、迁移和验收门槛统一见[自主建站整体方案](docs/宿主执行方案.md)。

## 启动

### 实验性原型：经本机终端确认后执行宿主命令

这条方案 A 原型独立于容器启动，默认关闭，**不是自主建站目标架构，也未通过真实模型端到端验收**。它只接受申请，每次在启动它的宿主终端显示完整命令与工作目录；用户必须在该终端输入当次申请 ID 才执行，聊天回复不能代替确认。代理只支持有超时的一次性命令，暂不管理持续运行的宿主服务。

1. 在 `.env` 设置 `CHAGENT_HOST_EXEC=approval`，并设置随机的 `CHAGENT_HOST_TOKEN`（至少 32 字符）。`CHAGENT_HOST_ENDPOINT` 通常为 `http://host.docker.internal:8766`。该 token 只允许容器提交申请，不代表用户批准。
2. 每次启动时由用户指定工作区根目录；`CHAGENT_WORKSPACES` 与代理的 `--host-root` 必须指向**同一个**宿主绝对路径。在**本机交互终端**启动代理：

   ```sh
   PYTHONPATH=src uv run python -m chagent.hostexec.service --env-file .env --host-root '/绝对路径/工作区'
   ```

3. 另一个终端以相同路径设置 `CHAGENT_WORKSPACES`，再运行 `docker compose up --build`。启用时网页启动会探测代理；不可达或没有交互终端则启动失败。已在当前 macOS Docker Desktop 环境验证容器能连到代理健康接口；其他环境需另行验证。默认代理申请端口监听所有接口，应通过本机防火墙限制非 Docker 来源；确认只在代理终端完成。

当前只能在同一挂载根目录内切换工作区。跨宿主目录的自动重挂载暂时关闭：数据库目前用容器路径 `/workspaces` 标识工作区，直接重挂载会把旧会话错误地关联到新宿主目录。修复持久化身份后才能开放这条路径；更换挂载根目录前不要把旧会话当成新目录的会话使用。

宿主执行结果及拒绝会返回模型，宿主审计日志位于 macOS `~/Library/Logs/chagent/hostexec.jsonl`。容器命令仍沿用原有审批；宿主代理的确认独立且不能由容器网页代替。

**容器入口**：先安装并启动 Docker 兼容运行时，设置 `CHAGENT_WORKSPACES` 为已有工作区的绝对路径，在 `.env` 中配置模型密钥，然后运行 `docker compose up --build`。Compose 启动 PostgreSQL 与 Chagent；容器内工作区位于 `/workspaces`，网页和项目服务分别映射到本机 `127.0.0.1:8765`、`127.0.0.1:8080`。Compose 会覆盖 `.env` 中的本机执行档设置。镜像含 Java 21、Maven、Node 和 npm；Maven 缓存保存在独立卷。当前容器路径的验证与限制见[容器执行验收](docs/验收记录汇总.md#6-容器执行2026-11-04)。

**本机诊断入口**仍需 Python 3.11+、uv、本机 PostgreSQL 和已创建的 `chagent` 数据库。`--demo` 可用于离线诊断，生产网页的命令执行需使用容器：


```sh
uv sync
cp -n .env.example .env     # 保留已有配置，在本机填写供应商 Key
createdb chagent            # 仅在数据库尚未创建时执行
uv run --env-file .env ./scripts/start.command --demo
```

启动后打开本地网页，默认端口 8765。端口被其他程序占用时顺延；已是 Chagent 时复用原服务。`--no-open` 关闭自动打开浏览器，`--workspace /安全目录` 指定起始工作区。

默认工作区是 `~/Chagent Workspace`，首次网页启动会创建。程序源码目录、子目录及包含程序的父目录不能作为网页工作区；CLI 也检查源码目录。旧不合规会话保留数据，网页不可打开。不要把源码目录当作验证文件工具的默认工作区。

```sh
# 可选：真实 tokenizer 与 PDF/Word/Excel 解析
uv sync --extra tokenizer --extra documents
# 配置解析，不发送模型请求，不显示 Key 值
uv run --env-file .env python -m chagent.config show
# 不调用真实模型的离线网页演示
uv run --env-file .env ./scripts/start.command --demo
```

命名 Key 只有一把时自动选择相应供应商；多把时设置 `CHAGENT_PROVIDER`。支持 OpenAI、DeepSeek、Anthropic、通义及自定义兼容服务，预设和预算见[模型配置](docs/模型配置.md)。启动打印 `Resolved:` 与 `Behavior:`，网页齿轮显示同源配置。

网页使用单一工具面。容器命令使用经过筛选的环境变量，模型密钥和数据库连接串不传给子进程。`--demo` 是无真实模型的诊断入口，不提供可编程能力。

网页入口现自动加载项目 `.env`，只补充进程中缺失的变量；可用 `--env-file /路径/.env` 指定文件，已有环境变量优先。安装依赖后可直接运行或双击 `scripts/start.command`。复用旧服务会显示该服务的实际配置，新配置需要重启才生效。不会自动把 shell/工作区资料放进模型请求。

## 已有能力

- 会话创建/搜索/重命名/归档、流式回答与停止、刷新恢复、上下文和用量查看。
- Markdown、代码复制、来源卡片；重新生成/编辑重发/重试，旧记录折叠而正文不改写。
- 点选/拖拽/粘贴的多文件上传队列；文本、PDF、Word、Excel 读取，保留页码/标题/工作表位置。
- 文件检索；上下文预算、自动摘要、工具大输出产物化；用户/工作区长期记忆，显式 `/remember` 零模型调用。
- `create_file` 将受支持文本写入工作区；生成/上传/另存为撞名时由宿主确认覆盖、保留两份或取消。旧数据库产物保留历史下载和主动导出。
- 网页搜索和 Markdown 正文抓取，默认关闭。长页支持 find 定位和 offset 分页。

扫描 PDF 无 OCR，图片只存不读，旧 `.doc/.xls` 不解析；文档只提文本，不计算 Excel 公式。暂不生成 PDF/Word/Excel 文件。联网没有登录态、JS 渲染或截图能力。

## 联网与命令权限

```dotenv
CHAGENT_WEB_ACCESS=deepseek
# CHAGENT_SEARCH_API_KEY=    # 未设时回落到 DEEPSEEK_API_KEY
```

搜索/抓取由 DeepSeek 的独立 API 提供，与模型选用的供应商无关。搜索最多 8 条；抓取每次正文最多 20,000 字符，支持 find/offset，全文在工具实例中缓存 5 分钟、最多 16 页。请求可能产生供应商费用；不在文档中承诺固定价格。

网页工具运行在 agent 进程中，命令的容器边界不覆盖它。私有地址拒绝由供应商执行，本机不能审计完整名单。

默认的本机 `configured` 档不执行命令：它只读写文件、联网查资料，`CHAGENT_ALLOW_HOST_COMMANDS=true` 会被拒绝。实验性 `host_exec` 是上文所述的独立审批通道。容器入口使用 `CHAGENT_EXECUTION_PROFILE=containerized`、`CHAGENT_BOUNDARY=container` 和 `CHAGENT_SANDBOX=off`；启动时核对容器标记与显式声明，容器命令可联网。执行行为由三轴描述：`boundary`（进程跑在哪）、`capability`（能否执行命令）、`gate`（是否逐次审批）；容器并非强安全边界，取消 run 不回滚文件或外部副作用。详见[当前执行契约](docs/统一可编程执行契约.md)与[容器验收](docs/验收记录汇总.md#6-容器执行2026-11-04)。

命令/测试和长进程已有底层实现；长进程只属于本 run，正常结束、失败、取消与超时回收进程组，没有 PTY 或重启接管。网页 `process` 支持 list/read/write/stop；write 每次最多 8192 字符，可用 `close_stdin=true` 发送 EOF。一次性命令仍从 `/dev/null` 读取输入。

## 验证与维护

```sh
# 项目未做 editable 安装，需要 PYTHONPATH=src，否则报 ModuleNotFoundError: chagent
NO_PROXY=127.0.0.1,localhost CHAGENT_TEST_DATABASE_URL=postgresql:///chagent \
  PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -q
```

数据库测试用独立临时 schema，不读写业务表。未设置测试数据库时集成测试跳过，跳过不是通过。若宿主注入了 `NODE_OPTIONS`（node 代理 shim），前端相关用例会假失败，用 `env -u NODE_OPTIONS ...` 复跑。

最近的完整回归（2026-11-05，本机 PostgreSQL 临时 schema）**595 项通过、0 跳过、62.9 秒**，覆盖工作区浏览、实验性宿主代理与前端模块化阶段 0–1；这不是自主建站验收，也不含真实浏览器验收。容器内真实 `run_tests` 对 Spring Boot 项目返回 `Suite: maven`、`Result: passed (exit 0)`；容器真实模型链路与宿主 HTTP 200 的历史证据见[容器阶段验收](docs/验收记录汇总.md#6-容器执行2026-11-04)。真实 Python 项目的修复、取消与恢复见[阶段验收](docs/验收记录汇总.md#7-真实-python-项目2026-11-04)。实验性宿主代理只验证了容器到健康接口连通、自动化批准/拒绝与三次真实终端申请过期拒绝，未验收真实模型完成本机原生命令。历史只读与 work 验收分别见[只读验收记录](docs/验收记录汇总.md#1-只读-code-r3r52026-11-02)和[浏览器验收记录](docs/验收记录汇总.md#2-work-模式真实浏览器2026-11-02)；它们不代表当前产品模式。

CLI 保留一次性运行与确定性 `--tool` 诊断，不再扩张为第二套产品入口。从源码启动 CLI 时需显式指定已存在的安全工作区，例如 `--workspace /你的资料目录`；继续会话沿用保存的工作区。

完整架构、配置、接口、历史与证据见[文档目录](docs/README.md)。

工作区 F1–F5 已实施分组导航、文件管理、右侧编辑器、Agent/输出统一及 PDF/图片预览。接口、证据与未验证范围见[工作区 F1–F4 实施与验收](docs/验收记录汇总.md#4-工作区-f1f42026-11-03)。预览范围及完整验证见 [F5 验收](docs/验收记录汇总.md#5-工作区-f52026-11-03)。
