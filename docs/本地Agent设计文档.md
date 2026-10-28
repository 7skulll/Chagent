# 本地 Agent 三版开发设计

状态：讨论稿  
更新日期：2026-10-27

| 版本 | 开发重点 | 交付标准 |
|---|---|---|
| V1 | 基础设施、数据类型、类与接口、依赖关系 | 有可以运行和验证的后端骨架 |
| V2 | 上下文、记忆、网页对话与基础评测 | 在 V1 骨架上完成可使用、可比较的 Agent |
| V3 | 由评测驱动的策略改进与长任务续接 | 每项优化有用例、对照结果和成本依据 |

## V1：基础设施与类设计

### 1. 本版目标

先把后续开发依赖的类型、类、接口和存储搭建完成，明确谁持有状态、谁执行流程、谁负责持久化。

本版完成最小可执行链路：以固定模型响应和简单工具跑通一次运行，保存消息、工具结果与事件，并能从 PostgreSQL 重新读取。不要求交付网页，也不要求实现完整的摘要、检索或评测策略。

### 2. 开发顺序

1. 建立项目目录、配置加载与启动入口。
2. 定义消息、模型请求、工具调用、运行事件等基础数据类型。
3. 建立 PostgreSQL 连接、迁移和具体 Repository。
4. 定义模型与工具接口，完成固定响应模型和简单工具实现。
5. 建立上下文与记忆的接口及最小实现。
6. 编写 Runner，通过构造函数接入各依赖。
7. 用脚本验证运行、事件、保存和重新加载。

### 3. 项目目录

以下以 Python 表达类与模块设计，具体 Web 框架与模型 SDK 在接入时确定。

```text
src/chagent/
  config.py              # Settings
  bootstrap.py           # 创建依赖与组装应用
  domain/
    messages.py          # Message、ContentBlock、ToolCall、ToolResult
    runs.py              # Run、RunStatus、AgentEvent
    context.py           # TaskState、ContextInput、ContextSnapshot
    memory.py            # Memory、MemoryQuery
  agent/
    runner.py            # AgentRunner
  models/
    base.py              # ModelClient、ModelRequest、ModelResponse、ModelEvent
    fake.py              # FakeModelClient
  tools/
    base.py              # Tool、ToolContext
    registry.py          # ToolRegistry
    executor.py          # ToolExecutor
  context/
    builder.py           # ContextBuilder、BasicContextBuilder
  memory/
    service.py           # MemoryService、BasicMemoryService
  storage/
    database.py          # Database
    repositories.py      # ConversationRepository、RunRepository、MemoryRepository
    artifacts.py         # ArtifactStore
  runtime/
    events.py            # RunEventSink
    trace.py             # TraceRecorder
  api/                   # V2 添加 HTTP 与 SSE 适配
  evals/                 # V2 添加评测实现
migrations/
tests/
scripts/
web/                     # V2 开发网页
```

目录划分职责，不要求每个文件都必须有类。数据使用 dataclass 等简单类型表达；服务优先组合，只有可替换的依赖边界定义接口。

### 4. 基础数据类型

| 类型 | 关键字段 | 用途 |
|---|---|---|
| Workspace | id、root_path | 工作目录与项目范围 |
| Session | id、workspace_id、title、created_at | 会话归属 |
| Message | id、session_id、run_id、role、content、source_refs | 原始消息，content 使用有类型的内容块 |
| ToolCall | id、name、arguments | 模型提出的工具请求 |
| ToolResult | call_id、status、content、artifact_ref | 执行结果；status 可表示 unknown |
| Run | id、session_id、status、error、started_at、ended_at | 一次用户输入引发的完整执行 |
| AgentEvent | run_id、sequence、type、payload、timestamp | 有序运行事件 |
| TaskState | session_id、goal、constraints、decisions、pending、source_refs | 当前任务状态 |
| ContextSnapshot | model_call_id、selected_items、excluded_items、token_estimate、request_ref | 本次请求的信息选择记录 |
| Memory | id、scope、scope_id、kind、content、status、source_refs、updated_at | 跨会话信息 |

Message.content 至少支持文本、工具调用和工具结果，避免将调用关系编码成普通字符串。图片和文件内容块按后续需要增加。

RunStatus 先定义 queued、running、completed、failed、cancelled、interrupted。暂不实现的交互状态随版本增加。

原始消息、任务状态、摘要和长期记忆分别存储。原始消息保留发生的事实，模型生成的状态与摘要保留来源引用，不覆盖原始记录。

### 5. 类与接口职责

#### 5.1 配置与依赖组装

| 类或函数 | 职责 | 主要入口 |
|---|---|---|
| Settings | 配置数据库地址、模型、数据目录和运行限制 | 从配置与环境读取并校验 |
| Database | 管理连接、事务和关闭 | connect、transaction、close |
| ArtifactStore | 保存长输出和请求正文，返回引用 | put、get、delete |
| create_app_services | 一次性构建并连接服务 | 返回 Runner 等应用入口 |

Settings 不传遍所有模块；组装时只向各依赖传入所需配置。密钥不进入普通日志或记忆。文件写入采用临时文件加原子替换，数据库引用在文件可用后提交。

#### 5.2 模型接口

```python
class ModelClient(Protocol):
    def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]: ...
```

ModelRequest 保存模型标识、有序消息、工具声明、输出预算和生成参数。ModelEvent 至少支持文本增量、最终响应和错误；最终 ModelResponse 包含完整内容、工具调用、停止原因与可用的用量。

流式协议必须明确：正常结束有且仅有一个最终响应；出现错误后不执行未完成的工具调用。Runner 使用最终结构化结果进行工具调度，不从文本增量猜测工具请求。

V1 实现 FakeModelClient，依次返回预设的“调用工具”和“最终回答”，便于无密钥验证。真实模型适配器在 V2 添加。

#### 5.3 工具接口与执行

```python
class Tool(Protocol):
    name: str
    description: str
    parameters: dict

    async def execute(self, arguments: dict, context: ToolContext) -> ToolResult: ...
```

ToolContext 提供 workspace、run_id、取消信号和输出目录等本次调用信息，不在工具实例上保存当前会话。

| 类 | 职责 | 主要方法 |
|---|---|---|
| ToolRegistry | 注册、查询工具，输出模型需要的声明 | register、get、definitions |
| ToolExecutor | 参数校验、权限检查、超时、取消和结果规范化 | execute |

所有模型工具调用经过 ToolExecutor。未知工具、校验失败与执行异常转换为明确结果；存储故障由运行层处理，不能伪装成普通工具错误后继续执行。

V1 先实现无外部副作用的测试工具，验证协议与调用关系。真实文件和 Shell 工具在 V2 接入。

#### 5.4 上下文接口

```python
class ContextBuilder(Protocol):
    async def build(self, inputs: ContextInput) -> BuiltContext: ...
```

ContextInput 包含当前输入、历史、TaskState、候选记忆、工具声明与预算。BuiltContext 返回可发送的有序消息和 ContextSnapshot。

ContextBuilder 不负责修改会话或写入长期记忆；Runner 收集输入，TraceRecorder 保存构建结果。

V1 的 BasicContextBuilder 只实现基础指令、当前输入与近期完整历史的组装，保留工具调用/结果配对。预留摘要和记忆字段，不提前实现排序器或复杂压缩机制。

#### 5.5 记忆接口

```python
class MemoryService(Protocol):
    async def search(self, query: MemoryQuery) -> list[Memory]: ...
    async def remember(self, entry: MemoryDraft) -> Memory: ...
    async def update(self, memory_id: str, change: MemoryChange) -> Memory: ...
    async def forget(self, memory_id: str) -> None: ...
```

MemoryService 管理范围与写入规则，MemoryRepository 只处理数据库访问。MemoryQuery 显式包含当前工作区和可见范围。

V1 实现显式记录的保存、读取和删除，以及最小文本匹配；不实现自动抽取、推断和后台整理。开发脚本可以调用这些能力，无需先做网页。

#### 5.6 持久化与追踪

| 类 | 持有的数据与职责 |
|---|---|
| ConversationRepository | 工作区、会话、消息、任务状态和摘要 |
| RunRepository | Run、模型调用、工具调用、上下文快照和事件 |
| MemoryRepository | 记忆记录及范围过滤 |
| TraceRecorder | 将模型请求、工具活动、快照和用量写入 RunRepository / ArtifactStore |
| RunEventSink | 接收已提交的事件并通知订阅者；V1 可使用进程内队列 |

先写具体 PostgreSQL Repository，不额外设计通用 BaseRepository 或一套 ORM 包装层。需要测试替身的边界使用窄接口即可。

关键状态与对应事件在事务内提交后再通知订阅者。事件序号在单个 Run 内单调递增；它支持网页恢复和调试，不承担持久化任务队列职责。

数据库至少建立工作区/会话归属外键、事件序号唯一约束和消息顺序约束。服务重启将未完成 Run 标为 interrupted。

#### 5.7 AgentRunner

AgentRunner 是运行流程的协调者，通过构造函数接收模型、上下文、记忆、工具、存储和追踪依赖。

```text
run(session_id, input)
  → 创建运行、保存用户消息
  → 加载历史与任务状态、查询记忆
  → ContextBuilder.build
  → TraceRecorder 保存请求快照
  → ModelClient.stream
  → 保存最终模型响应
  → ToolExecutor.execute（如有工具调用）
  → 保存结果，重新构建下一次请求
  → 写入终态并通知订阅者
```

构造函数注入依赖，单次运行数据放在局部状态中。Runner 不包含网页格式、供应商 SDK 对象或检索 SQL。

V1 同会话只允许一个活动 Run，默认全局同时运行一个任务；工具串行。设置轮数、输出和时间上限，支持 cancel(run_id)。取消后停止后续调度，不声称自动撤销已完成操作。

### 6. 依赖关系

```text
bootstrap 创建依赖
  └── AgentRunner
       ├── ModelClient              → FakeModelClient / V2 真实适配
       ├── ContextBuilder           → BasicContextBuilder
       ├── MemoryService            → MemoryRepository → Database
       ├── ToolExecutor             → ToolRegistry → Tool
       ├── ConversationRepository   → Database
       └── TraceRecorder            → RunRepository / ArtifactStore
                                          └── 提交后通知 RunEventSink

V2：HTTP API 和 EvalRunner 分别调用同一个 AgentRunner
```

领域类型不依赖网页、数据库或模型 SDK。上下文和记忆策略可以替换，Runner 的执行协议保持稳定。不要为只有一个简单实现的内部辅助函数建立继承层级。

### 7. 本版交付与验收

交付类型定义、类与接口、迁移文件、依赖组装入口、FakeModelClient、测试工具和端到端开发脚本。

完成以下验证后进入 V2：

- 无真实模型密钥即可跑通“输入 → 工具调用 → 工具结果 → 最终回答”。
- PostgreSQL 可以保存并重新加载完整运行、消息、快照和事件。
- 模型客户端、上下文构建器和工具实现可替换，不改 Runner 的流程。
- 校验失败、模型错误、取消与重启中断具有明确状态。
- 每个工具结果关联原始 call_id，每次模型请求关联上下文快照。
- 记忆查询强制范围过滤，删除后不再返回。
- 基础契约和持久化集成检查通过。

本版不是仅提交空类与 TODO：最小实现要证明这些接口能够合作。验收重点是依赖、数据流和状态正确性，不考核 Agent 的任务智能表现。

## V2：核心能力、网页与基础评测

### 1. 本版目标与开发顺序

在 V1 已验证的接口上补充真实能力，先记录基础策略的评测基线，再逐步改善上下文和记忆。

1. 实现真实 ModelClient、文件与 Shell 工具。
2. 增强 ContextBuilder，完成预算、任务状态与摘要。
3. 完成显式记忆检索和纠正。
4. 接入网页与 HTTP / SSE。
5. 实现 EvalRunner，形成基础策略对比。
6. 根据基线失败补充候选记忆和生命周期。

### 2. 本版新增与扩展的类

| 类或模块 | 本版实现 |
|---|---|
| ProviderModelClient | 一个实际使用的模型供应商，统一流式响应和用量 |
| ReadFileTool / WriteFileTool / EditFileTool / ShellTool | 本地任务所需工具 |
| BudgetedContextBuilder | 预算计算、信息选择、合法裁剪与快照 |
| ContextSummarizer | 阶段摘要生成，记录覆盖边界及来源 |
| BasicMemoryService 扩展 | 检索、纠正、修订、失效与候选处理 |
| RunService | 网页层提交、查询、取消和会话并发控制，调用 Runner |
| HTTP / SSE 路由 | 输入校验与事件投递，不承载执行策略 |
| EvalRunner | 创建隔离环境并复用 AgentRunner 执行用例 |
| RuleScorer | 检查回答、文件、记忆状态和工具行为 |

RunService 到本版出现：用于管理活动任务句柄和 API 请求，避免在 V1 提前增加一层只转发调用的服务。

### 3. 上下文实现

每次请求优先保留当前输入、必要指令和用户约束，然后加入近期完整交互、历史摘要及相关记忆。最终消息顺序遵循模型协议。

```text
可用输入预算 = 模型上下文上限 - 输出预留 - 安全余量
```

预算计算包含工具定义和附件。使用兼容计数方式或保守估算，并在快照中注明。长工具输出保留完整 artifact，模型接收片段与查阅入口。

TaskState 按会话维护目标、约束、决定与待办。用户约束保留原文来源，模型派生状态单独标识。

接近预算时调用 ContextSummarizer，压缩较早的完整交互。摘要记录覆盖范围，保留目标、约束、决定、待办和证据位置；原始历史不删除。摘要失败且剩余上下文不足以继续时，明确停止并报告。

ContextSnapshot 展示选入与排除的信息、理由和 token。构建器不静默丢弃必要约束，也不留下孤立工具结果。

### 4. 记忆实现

先完成显式记忆：用户要求保存时写入，默认当前工作区；编辑和删除后立即影响检索。当前明确指令优先于旧记忆。

检索先过滤范围与状态，再用 PostgreSQL 文本匹配与简单排序。中文提供子串或字符片段匹配回退。候选数量和注入 token 有上限。

建立基线后再增加：

```text
原始证据 → candidate → active → superseded / expired / deleted
```

新增 origin、revision、supersedes_id、expires_at 等字段。任务结束后可以抽取候选，模型推断默认待审；用户明确保存或纠正的信息可直接更新，保留修订记录。

同义重复允许合并，条件或范围不同的信息分别保留。无法消解的冲突呈现给用户。删除时记录禁止同源重新抽取的标记；删除记忆不等于删除原始会话。

本版先不默认启用 embedding 或后台自动整理。

### 5. 工具与网页

- 工具具备参数校验、超时、输出上限、取消和统一结果。
- 文件访问限制在授权工作区；Shell 使用隔离后端或用户明确启用的宿主执行模式，工作目录检查不等于进程沙箱。
- 网页支持工作区、会话列表、流式对话、工具进度、取消、记忆管理与请求检查。
- 页面刷新后读取后端状态；通过事件序号补取保留事件，超出范围则读取快照。
- 密钥由后端持有。完整快照保存在本机，支持保留期限和清理。

先保证提交与取消可靠，运行中追加指令和后续排队作为本版后续增量，不阻塞基本交付。

### 6. 基础评测

EvalRunner 接收用例和策略配置，创建隔离数据库或 schema、临时工作区与工具环境，通过同一个 Runner 执行，再由 RuleScorer 检查结果。

用例记录初始文件、历史、记忆、多会话输入步骤和预期检查。至少包含基础任务、早期约束、摘要续接、记忆召回、纠正、项目隔离、中文不同表达和干扰信息。

比较三组基线：

| 配置 | 上下文策略 |
|---|---|
| B0 | 近期合法历史窗口 |
| B1 | B0 + 阶段摘要 |
| B2 | B1 + 显式记忆检索 |

报告任务成功率、约束遵守率、召回、错误记忆使用、摘要完整性、总 token 与耗时。后台抽取和摘要成本计入总成本。

保存代码、用例、配置、模型和工具环境信息。开发集用于调试，冻结验收集不用于调参或记忆学习。固定模型、工具和输出预算，关键比较重复运行并报告波动。

规则评分优先；关键任务检查实际工具行为和最终文件状态，不能只检查回答中的关键词。组件、模拟工具和真实本地任务结果分开报告。评测不能访问日常工作区、个人记忆或真实发送账号。

### 7. 本版交付与验收

- 可通过网页完成“读取文件 → 修改 → 验证 → 汇报”。
- 刷新和重启后会话可读取，中断任务有明确标记。
- 长对话摘要后能继续任务，必要约束与待办保留。
- 同工作区新会话可检索记忆，其他工作区记录不混入。
- 记忆纠正、失效与删除有明确行为，推断候选不会直接变成事实。
- 可一条命令运行建议不少于 30 个审阅用例，生成基线、成本和失败轨迹报告。
- 每个失败可以定位到请求上下文、记忆与工具结果。

本版先生成本地评测报告，再增加网页结果查看。完整网页评测调度器不作为验收前提。

## V3：评测驱动的优化与长任务续接

### 1. 本版目标

针对 V2 已经观察到的失败，修改必要策略，并通过对照评测验证。以下类是按需新增项，不能仅因规划中出现就全部实现。

### 2. 按问题扩展类

| 实际问题 | 可新增或扩展的类 | 边界 |
|---|---|---|
| 固定上下文配额不适合不同任务 | ContextPolicy | 为少量任务类型返回预算与选择配置 |
| 检索候选多且干扰严重 | MemoryRanker | 对已过滤候选排序，返回可解释理由 |
| 摘要丢失约束 | ContextSummarizer | 强化必要事实保留，先修改现有实现 |
| 来源内容变化导致记忆失效 | MemoryService | 校验来源版本，标记待更新 |
| 重复成功流程值得复用 | MemoryConsolidator | 生成带适用条件和证据的经验候选 |
| 长任务中断难续接 | CheckpointService | 保存阶段状态与已知操作结果 |
| 操作完成情况未知 | RecoveryCoordinator | 验证结果后决定重用、重试或等待用户 |

只有需要独立配置、替换或承担明确状态的职责才拆成类；简单计算保持为函数，已有类可以承担的能力先在原类内扩展。

### 3. 开发顺序

1. 将人工审阅、脱敏后的真实失败轨迹转成回归用例。
2. 定位失败来自检索、摘要、信息选择还是工具执行。
3. 单独改变一个策略，保存对应配置。
4. 对照运行并做消融，确认收益和新增成本。
5. 达标后启用，否则回退到 V2 的基础路径。

### 4. 上下文与记忆优化

- 用少量规则区分短问答、文件任务和长任务续接，设置历史、摘要、原文和记忆的配额。
- 摘要缺证据时按来源查阅原始片段，约束丢失时加强原文保留。
- 增加上下文选择回放，在固定候选上比较策略；回放不能替代端到端重跑。
- 保持稳定指令与工具声明顺序，缓存收益依据实际用量判断。
- 只有语义召回问题已被评测确认时，才增加 embedding 索引。
- 经验候选保留适用条件和验证证据，回归验证后再决定是否激活。
- 后台整理有单次和每日预算；低价值记忆先停止自动注入，不自动删除用户明确保存的信息。

### 5. 检查点与恢复

阶段检查点保存目标、约束、证据、已完成操作、待办和结果未知的操作。

| 操作状态 | 恢复方式 |
|---|---|
| 已确认完成 | 重用结果 |
| 尚未执行 | 检查当前任务与权限后调度 |
| 失败 | 在次数和预算内重试 |
| 结果未知 | 先查询或验证，不直接假定未执行 |

只读或可证明幂等的工具可以自动恢复。文件修改与外部副作用需要结果验证、幂等键，或由用户确认后继续。检查点本身不保证操作只发生一次，恢复会话也不等于回滚工作区文件。

### 6. 持续评测与验收

增加中断、记忆规模增长、源文件变化等场景。日常运行快速、无真实外部副作用的回归集，完整任务集按需运行。

验收要求：

- 每项默认启用的优化对应明确失败用例、对照结果与成本。
- 记忆来源、修订、范围和选入依据均可检查。
- 任务中断后可恢复必要状态，结果未知的操作不会盲目重试。
- 数据增长时，检索延迟、上下文 token 和后台成本处于已测量预算内。
- 新策略可以关闭，基础类和执行流程不依赖全部优化组件才能运行。

不预设压缩率、召回提升或 token 降幅。新增类与机制由实际问题决定，避免基础设施持续膨胀。
