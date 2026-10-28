# 结束控制与 RunState：前两项改造验收

日期：2026-10-28（北京时间）。状态：已实现，验收通过。

本批范围是上一轮实施表中的前两项：明确结束原因和返回契约；使用 RunState 并在提交成功后推进活动状态。工具批次调度、事件缓冲与订阅、租约接管、本地命令路由、后台记忆提取没有混入本批。

## 1. 结束控制

`runs.end_reason`、终态事件的 `payload.end_reason`、成功返回的 `RunResult.end_reason` 使用相同原因。异常调用方可读取 `RunFailedError.end_reason`。

| 情形 | status | end_reason |
|---|---|---|
| 模型正常结束且无工具调用 | completed | model_stop |
| 整批工具都要求 terminate | completed | tool_requested_stop |
| 用户／宿主取消 asyncio Task | cancelled | user_cancelled |
| 模型返回 aborted | cancelled | model_aborted |
| 达到模型循环上限 | failed | turn_limit |
| 全局运行超时 | failed | run_timeout |
| 必须保留的上下文超预算 | failed | context_budget_exhausted |
| 模型输出截断 | failed | output_limit |
| 供应商返回错误 | failed | model_error |
| 缺少终结事件等流协议错误 | failed | model_protocol_error |
| 可识别的数据库／连接池错误 | failed | storage_error |
| 其他未分类执行异常 | failed | execution_error |

`owner_lost` 供显式 interrupted 终态使用，但本批没有实现所有者租约或自动失联扫描。

内部成功分支通过 FinishDecision 区分 continue／complete，失败分支使用携带 end_reason 的异常；没有为了统一外形而吞掉取消或存储异常。`completed` 表示运行正常停止，不代表用户的整个现实任务一定已经完成。

### 工具正常停止的返回契约

```python
RunResult(
    run_id=...,
    session_id=...,
    message=None,
    end_reason="tool_requested_stop",
    tool_results=[实际保存的工具结果消息],
)
```

不再抛出“Tool batch requested termination”导致失败，也不额外调用模型生成收尾回答。没有伪造助手消息。

继续沿用本地 Pi 的规则：必须整批结果都 terminate=True 才正常停止；混合批次继续。单工具错误仍作为工具结果交回模型。本批对截断只明确原因并保持不执行工具，未新增自动重试。

CLI 已处理 message=None；网页 RunView 和 latest_run 暴露 end_reason，状态栏能区分工具正常停止、轮数／时间上限、上下文预算和输出限制。API／SSE 已自动验证；网页 JavaScript 做了语法检查，没有执行人工浏览器视觉验收。

## 2. RunState 的实际数据流

替换此前未接入主循环的 AgentContext／AgentState 预留定义，采用运行实际持有的 RunState：

```text
run_id、session
history（已提交、可进入上下文的消息）
task_state、summary
iteration、active_model_call_id
context_revision
tools、tool_revision
```

开始运行时加载一次会话信息、历史、任务状态和可用摘要。下一轮直接使用活动视图，不再从数据库重新加载整段会话。

模型响应和工具结果的保存方法使用 INSERT RETURNING 获取真实消息 ID／sequence；先完成事务，再返回 StoredMessage，Runner 收到后才 append_committed。消息内容通过 codec 建立独立快照，避免与适配器的可变 partial 对象共享。

摘要同样在保存事务成功后更新 state.summary。工具注册表增加只读 revision，注册集合变化时下一轮重新生成声明。任务状态在活动运行中原本就禁止修改，因此本批按运行复用；记忆库仍可每轮查询，不缓存可能被用户审核／编辑的数据。

运行结束、失败、超时和取消都会清理 `_states`、`_tasks` 和上下文管理器的运行缓存；不同会话并发运行分别持有独立 RunState。

大工具输出在数据库保留完整正文与产物，RunState 只保留引用及图片，与 `messages(project_artifacts=True)` 的重建结果一致。原始审计历史不被裁掉。本批没有新增按摘要边界读取数据库尾部的优化，运行开始仍需加载可重放历史。

## 3. 事务与故障验收

新增测试不是只在方法入口抛异常，而是用 PostgreSQL 的 DEFERRABLE INITIALLY DEFERRED 约束触发器，在 COMMIT 阶段拒绝事务：

- 模型响应提交失败：消息和 model_completed 事件回滚，RunState 仍只有此前已提交历史，下一轮不继续。
- 工具结果提交失败：工具消息、产物与 tool_completed 事件回滚；RunState 不加入不存在的结果；未确认调用最终为 unknown。
- 运行完成提交失败：run_completed 事件回滚，调用方不收到成功结果，随后以 storage_error 保存失败终态。
- 大产物重建：运行内投影与数据库重新加载的投影相等。
- 多次运行内压缩：已提交摘要在同一 RunState 中复用，历史与已有摘要均不按轮次重新读取。
- 新 Runner 续聊：从数据库重建，保留已完成工具批次和历史内容，不依赖旧 Runner 的内存。
- 并发会话：同一 Runner 的不同运行没有状态串用，结束后缓存释放。

另验收自然结束、正常工具停止、混合批次、模型错误／中止、用户取消、总超时、轮数上限、上下文预算、截断和缺失流终态，以及 API／SSE 的结束原因一致性。

## 4. 验收结果

### 回归与静态检查

- 原有 103 项测试保留并通过。
- 新增 19 项：17 项 RunState／结束控制，1 项迁移，1 项网页接口。
- 最终完整测试：122 项全部通过，用时约 11.8 秒。
- 涉及文件的 Ruff 检查通过；Python 编译检查通过；网页 JavaScript 的 node --check 通过。

所有数据库验收在临时 schema 进行，不修改现有业务会话。没有提交 Git commit。

### 调用次数和离线对照

固定模型、真实 PostgreSQL、相同诊断场景，每组 5 个样本。显式 SQL execute 次数不包含隐式事务语句、fetch、池维护与初始化。

| 场景 | 改造前 SQL | 改造后 SQL | 减少 |
|---|---:|---:|---:|
| 一次工具循环 | 44 | 40 | 4 |
| 8 轮工具／9 次任务模型请求 | 163 | 138 | 25（约 15.3%） |
| 同上，开启空记忆库检索 | 191 | 147 | 44（约 23.0%） |
| 128 个文本增量 | 281 | 280 | 1 |

专门的验收还断言：9 次模型调用时，历史、任务状态、已有摘要和未变化的工具声明各加载／生成 1 次，而不是 9 次。

没有把 SQL 减少等同于固定的总耗时提升。以 8 轮工具为例，本次离线中位数约 35.50ms，旧基线约 35.86ms；系统负载和很小的样本量会影响时长。确定的收益是重复数据库操作被消除。

128 增量场景仍有 280 次显式 SQL，说明本批没有假装完成逐增量事件提交优化。Builder 内部的重复计数也还保留。

原始数据：[旧基线](diagnostics/main-path-baseline-2026-10-28.json)、[本批基线](diagnostics/main-path-step1-2-2026-10-28.json)。后者在回归测试结束后单独重跑，避免与测试负载混测。

### 真实 DeepSeek 验收

使用已配置的 deepseek-v4-flash，输出上限 768，真实供应商和临时 schema；共 6 个场景通过：

| 场景 | 请求数 | 耗时 | 关键断言 |
|---|---:|---:|---|
| 简单回复 | 1 | 0.964s | 自然结束原因一致 |
| 文件读取→计算→写入→回答 | 3 | 2.123s | 结果文件 sum=41 |
| 新 Runner 续聊 | 1 | 0.878s | 回忆正确随机 marker，无新工具调用 |
| 真实摘要压缩 | 3 | 2.793s | 摘要提交且保留目标、限制与暗号 |
| 正常工具停止 | 1 | 0.685s | message=None，返回真实工具结果，不额外请求模型 |
| 网页 API／SSE 回放 | 1 | 0.815s | 正文拼接、持久化回放、model_stop 原因正确 |

共 10 次有效请求。供应商报告：非缓存输入 5,704，缓存读取 6,272，输出 345，总计 12,321 tokens；未报告货币费用。

这是单次语义验收，不能当作延迟分布或与上轮真实测试的严格速度比较。网页使用 ASGITransport，未测浏览器可见首字。测试后 schema 和工作区已清理，Key 未写入报告。

原始结果：[真实链路验收 JSON](diagnostics/live-chain-step1-2-2026-10-28.json)。其源码哈希记录真实调用当时版本；之后仅清理未用导入、补充类型标注并增加回归用例，最终版本另通过完整回归与静态检查。

## 5. 迁移与后续边界

新增不可变迁移 `0007_run_end_reasons.sql`，启动时沿用现有迁移机制自动应用。历史终态记录回填 legacy_terminal，避免把无法追溯的旧运行误记为 model_stop。数据库约束拒绝状态与原因不匹配、终态缺少原因，以及活动状态携带终态原因。

本次只在隔离测试库验证迁移，尚未主动修改业务库。重新启动应用时会应用迁移，应使用同一版本后端与网页资源。

后续仍待实施：工具有界并发、事件合并和实时订阅、所有者租约与失联处理、失败运行副作用恢复、/remember 本地路由、自动记忆后台化、阻塞文件操作隔离。结束原因字段的存在不代表这些恢复机制已实现。

复现：

```sh
CHAGENT_TEST_DATABASE_URL=postgresql:///chagent uv run python -m unittest discover -s tests

CHAGENT_TEST_DATABASE_URL=postgresql:///chagent uv run python scripts/diagnose_main_path.py \
  --output docs/diagnostics/main-path-step1-2-local.json --repeats 5

# 付费调用；本项目命名 Key 被继承的通用 Key 遮蔽时，使用本轮已确认的取消覆盖方式。
env -u CHAGENT_API_KEY uv run --env-file .env python scripts/test_live_chain.py \
  --case core --max-calls 12 --output docs/diagnostics/live-core-local.json
```
