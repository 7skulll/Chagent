# Chagent

本机单用户 Agent：在本地网页对话，Python 进程调用模型和工具，PostgreSQL 保存可检查的历史、请求、工具结果与事件。

默认使用 **work 模式**读取资料、理解文档、修改文件并生成工作区输出。右侧文件面板支持文本多标签编辑、手动保存、版本冲突、回收恢复，以及 PDF 分页/图片缩放预览。网页现支持按配置开启 **只读代码分析**：读代码、查看 Git 历史与差异、运行适用测试；工作区由内核禁写，命令断网，process 仅 read/stop。显式 `trusted-development` 配置现支持网页可写 code：创建、编辑、命令和测试无需逐次审批；命令仍隔离断网，process 仅 read/stop。普通 work 与可写 code 共用文件工具，显式只读 code 保持禁写。**work 与 code 已分离且可在聊天区直接切换**：按钮作用于之后的 run，进行中的 run 保持它启动时的工具面；切到 code 先跑隔离预检。支持范围见[实施计划](docs/实施计划.md)。

可信开发配置借鉴 pi 的直接工具循环，复用现有 Runner 与工具，在工作区与 isolated 边界内完成创建、修改、执行、测试及修复；work 现已允许工作区文件编辑，见[工作区 F1–F4 实施与验收](docs/工作区F1-F4验收-2026-11-03.md)。配置方法见[模型配置](docs/模型配置.md)，真实验收见[开发版验收](docs/代码开发验收-2026-11-03.md)。后续优先验证真实项目，再完善交互审批、网络和扩展回归。

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

只读 code 底层已实现：`CHAGENT_SANDBOX=isolated` 配合 `CHAGENT_SANDBOX_WRITABLE=false` 时，code 裁掉工作区写工具，命令由内核拒绝写入，process 仅允许 read/stop；私有 HOME/tmp 按 run 分配。网页默认只开放满足这些条件且宿主命令开启、命令断网的只读 code；可写 code 必须另外显式启用可信开发配置。work 工具面保持原有入口设置。

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

`CHAGENT_CODE_MODE=off` 保持默认。生产模型的 code 命令要求 macOS `CHAGENT_SANDBOX=isolated`：默认拒绝、明确读取白名单、独立 HOME/tmp、环境清空、探针失败不降级。旧 workspace-write 只是诊断兼容档，通常仍可读工作区外文件。网页接受 isolated + network=false + host_commands=true；默认 configured 要求 sandbox_writable=false，trusted-development 要求 sandbox_writable=true 且 approval=never。详见[执行边界契约](docs/执行边界设计.md)与[隔离验收](docs/code模式执行隔离.md)。

命令/测试和长进程已有底层实现；长进程只属于本 run，正常结束、失败、取消与超时回收进程组，没有 PTY 或重启接管。`process write` 还未进入审批；网页只读档直接禁止 write/EOF，后续交互版仍需 C3。

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

最近完整回归（2026-11-03，F5）**580 项通过、0 跳过**；只读 R4 的 77 项检查通过，真实项目浏览器验收见[只读验收记录](docs/只读code验收-2026-11-02.md)；C2 的 14 项正反向探针和实际 Shell/Git/Python 测试/长进程通过。真实 work 流程的已验收范围及未测项见[浏览器验收记录](docs/work模式真实验收-2026-11-02.md)，不要把合成项目当作真实项目回归。

CLI 保留一次性运行与确定性 `--tool` 诊断，不再扩张为第二套产品入口。从源码启动 CLI 时需显式指定已存在的安全工作区，例如 `--workspace /你的资料目录`；继续会话沿用保存的工作区。

完整架构、配置、接口、历史与证据见[文档目录](docs/README.md)。

工作区 F1–F5 已实施分组导航、文件管理、右侧编辑器、Agent/输出统一及 PDF/图片预览。接口、证据与未验证范围见[工作区 F1–F4 实施与验收](docs/工作区F1-F4验收-2026-11-03.md)。预览范围及完整验证见 [F5 验收](docs/工作区F5验收-2026-11-03.md)。
