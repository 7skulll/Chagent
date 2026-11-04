# Chagent

本机单用户 Agent：在本地网页对话，Python 进程调用模型和工具，PostgreSQL 保存可检查的历史、请求、工具结果与事件。

默认使用统一的可编程 Agent：同一会话可读写工作区文件、执行命令、运行测试并读取或停止长进程，无需切换 work/code。命令使用可写 isolated 沙箱且断网；交互输入尚未开放。右侧文件面板支持文本多标签编辑、手动保存、版本冲突、回收恢复，以及 PDF 分页/图片缩放预览。旧会话的 mode 字段仅作为历史数据保留，运行时使用相同工具面。

新安装的默认配置可编程；已有 `.env` 中的旧开关仍会被读取。升级旧安装时请将 `CHAGENT_CODE_MODE=on`、`CHAGENT_ALLOW_HOST_COMMANDS=true`、`CHAGENT_SANDBOX=isolated`、`CHAGENT_SANDBOX_WRITABLE=true`、`CHAGENT_SANDBOX_NETWORK=false`、`CHAGENT_APPROVAL=never`、`CHAGENT_EXECUTION_PROFILE=trusted-development` 配齐并重启。启动会检查沙箱，失败时拒绝运行。

当前网页入口只支持上述可写、无需逐次审批的配置。旧 `configured`、只读工作区或逐次审批配置不会被自动提升，网页会明确拒绝启动；底层诊断能力仍保留。完整行为见[统一可编程执行契约](docs/统一可编程执行契约.md)。

统一工具面复用现有 Runner、文件与命令工具，在工作区与 isolated 边界内完成创建、修改、执行、测试及修复。此前按模式交付的验收记录仍保留作历史证据；当前行为以本页和代码为准。后续优先验证真实项目，再完善交互输入、网络和扩展回归。

## 启动

需要 Python 3.11+、uv、本机 PostgreSQL 和已创建的 `chagent` 数据库。

```sh
uv sync
cp -n .env.example .env     # 保留已有配置，在本机填写供应商 Key
createdb chagent            # 仅在数据库尚未创建时执行
uv run --env-file .env ./scripts/start.command
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

网页使用单一工具面；命令的 HOME/tmp 按 run 私有分配。`--demo` 是无真实模型的诊断入口，不提供可编程能力。

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

网页工具运行在 agent 进程中，命令沙箱的断网不覆盖它。私有地址拒绝由供应商执行，本机不能审计完整名单。

生产网页命令要求 macOS `CHAGENT_SANDBOX=isolated`：默认拒绝、明确读取白名单、独立 HOME/tmp、环境清空、探针失败不降级。旧 workspace-write 仅为底层兼容档，不能作为网页编程配置。详见[当前执行契约](docs/统一可编程执行契约.md)与[隔离验收](docs/code模式执行隔离.md)。

命令/测试和长进程已有底层实现；长进程只属于本 run，正常结束、失败、取消与超时回收进程组，没有 PTY 或重启接管。网页 `process` 仅允许 read/stop，交互输入与 EOF 尚未开放。

## 验证与维护

```sh
# 项目未做 editable 安装，需要 PYTHONPATH=src，否则报 ModuleNotFoundError: chagent
NO_PROXY=127.0.0.1,localhost CHAGENT_TEST_DATABASE_URL=postgresql:///chagent \
  PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -q
# macOS 真正可施加 seatbelt 的环境中运行
PYTHONPATH=src .venv/bin/python scripts/probe_code_isolation.py \
  --output docs/diagnostics/code-isolation-2026-11-02.json
```

数据库测试用独立临时 schema，不读写业务表。未设置测试数据库时集成测试跳过；没有 node 或不能施加 seatbelt 时相应测试可能跳过，跳过不是通过。若宿主注入了 `NODE_OPTIONS`（node 代理 shim），前端相关用例会假失败，用 `env -u NODE_OPTIONS ...` 复跑。

合并前完整回归（2026-11-03，F5）**580 项通过、0 跳过**。合并后的回归结果以本次修复的最终测试记录为准。历史只读与 work 验收分别见[只读验收记录](docs/只读code验收-2026-11-02.md)和[浏览器验收记录](docs/work模式真实验收-2026-11-02.md)；它们不代表当前产品模式。

CLI 保留一次性运行与确定性 `--tool` 诊断，不再扩张为第二套产品入口。从源码启动 CLI 时需显式指定已存在的安全工作区，例如 `--workspace /你的资料目录`；继续会话沿用保存的工作区。

完整架构、配置、接口、历史与证据见[文档目录](docs/README.md)。

工作区 F1–F5 已实施分组导航、文件管理、右侧编辑器、Agent/输出统一及 PDF/图片预览。接口、证据与未验证范围见[工作区 F1–F4 实施与验收](docs/工作区F1-F4验收-2026-11-03.md)。预览范围及完整验证见 [F5 验收](docs/工作区F5验收-2026-11-03.md)。
