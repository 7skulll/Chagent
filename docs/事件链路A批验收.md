# A 批验收：文本合并、提交后通知与 SSE

状态：已实施，核心验收通过。对应[下一阶段统一实施方案](下一阶段实施方案.md)的 A 批。没有实现 B／E／C／D 批、`/remember` 短路或新的数据库迁移，也没有提交／推送 Git。

## 1. 实现与职责

| 文件 | 改动 |
|---|---|
| `agent/stream_writer.py` | 每次模型响应一个 TextStreamWriter，首片优先、20ms／4KiB 合并、64KiB 容量、flush／close 与故障传播 |
| `agent/runner.py` | 消费供应商流与写库分开；同时监控 writer，写库失败能打断等待中的供应商；模型完成前排空文本 |
| `events/notifications.py` | 按 run 的版本广播，无正文和客户端事件队列；最后一个订阅释放后移除条目 |
| `storage/database.py` | 同一个数据库实例持有一份进程内通知服务，供仓储与 Web 共享 |
| `storage/repositories.py` | 关键事务提交后通知；增加运行终态事件序号查询 |
| `storage/summaries.py`、`memory/worker.py` | 摘要事务、后台记忆外层事务成功后通知，避免在嵌套事务内提前发布 |
| `web/app.py` | 先订阅再追赶持久事件、版本检查防止漏唤醒、1 秒无通知兜底；终态事件处关闭回答流 |
| `web/schemas.py`、`web/static/app.js` | 独立的 memory-job 状态接口和后台状态展示，不让回答流等待记忆提取 |

数据流：

```text
供应商文本 → 有界缓冲 → 合并事务提交 → 版本通知
                                        ↓
                         SSE 按已提交 sequence 查询／发送
```

`RunState` 仍在最终模型响应或工具结果的事务成功后更新。文本事件没有成为第二份活动历史；工具意图、工具结果与运行终态保持原有事务边界。

## 2. 实际保证

- 首个非空片段不等待合并定时器；后续文本达到时间／容量阈值或 flush 时保存。
- 定时器独立运行，供应商暂停输出时也会刷新已有文本。
- 超大增量先按字符分片再编码；64KiB 额度包含正在写库的内容，慢数据库会对生产方形成有界背压。
- 单次响应一个 writer、一个流消费任务，不按增量创建任务。数据库等待不持有 Condition 锁。
- 非文本事件到达时排空已有文本；模型完成和后续工具调度之前，必须确认全部已接收文本已提交。
- 写库失败会立即让消费方停止并关闭供应商流，不等待下一个 token 或等缓冲耗尽才报错。
- 取消／模型异常时最多额外给文本 writer 2 秒清理预算；无法保存的未提交尾部不伪装成成功。达到清理期限后取消并回收 writer。
- 只有事务成功才发通知；通知不保存正文。断线补齐仍依赖 PostgreSQL，因此通知遗漏和独立进程写入都能由兜底查询恢复。
- 多个 SSE 订阅者收到同一份持久事件，不竞争消费。慢客户端不持有数据库连接，不阻塞生产者。
- 回答 SSE 截止于该 run 的首个终态事件，包括重连游标已经越过终态的情况。后台记忆事件仍保留在审计表，但不会在历史回放中越过回答终态发送。

合并事件增加 `count` 元数据，表示合并的内部文本片段数（超大供应商片段可能被拆分），不把它当作供应商 token 数。消费方继续按 delta 拼接。

### 后台记忆状态

新增 `GET /api/runs/{run_id}/memory-job`：返回 `job=null` 或该运行的后台任务状态，包括 queued／running／completed／failed、尝试数与时间；不暴露内部 last_error、源消息内容等字段。访问沿用运行的工作区检查。

网页回答完成后加载最终消息并恢复输入，再独立查询记忆状态；活动 job 最多轮询 2 分钟，切换会话后旧轮询停止。提取尚未结束不会让回答保持“生成中”。没有把 job 失败转成 run 失败。

## 3. 自动验收

改动前完整测试：154 项通过。改动后：**174 项全部通过，约 23.07 秒**。

新增 20 项事件链路验收，覆盖：

- 首片不等待 deadline；供应商无新输出时按时刷新；空片段、中文、emoji 与超大输入正确拼接。
- 执行中的写入计入容量；慢写形成背压；失败释放容量及 flush 等待者。
- 通知广播、多监听者、查询与等待交界的通知、无通知时兜底、取消后注册释放。
- 合并正文在模型结果保存之前提交，序号连续；终态后记忆事件不会污染回答回放。
- 使用 PostgreSQL 的 deferred constraint trigger 在 COMMIT 阶段拒绝 text_delta：未提交事件不唤醒订阅者，供应商停止，run 以 storage_error 失败。
- 取消时保存已接收尾部；写入永久阻塞时在清理期限内退出，无成功终态，无遗留 writer／消费任务。
- 慢 SSE 读取不拖住提交；空闲订阅 1.15 秒最多两个查询轮次（初始追赶＋一次兜底）。
- 后台 job 保持 queued 时，回答 SSE 仍结束；独立接口不暴露内部字段。

原有真实回环 HTTP 测试同时通过：生成未结束就能收到增量、断开 SSE 不取消任务、重复停止、停止后续聊。数据库测试使用临时 schema，不改业务会话。

静态检查：涉及文件的 Ruff E4/E7/E9/F、Python 编译、JavaScript `node --check`、`git diff --check` 通过。

## 4. 性能对照

新增 `scripts/benchmark_event_path.py`，改动前后各采 30 次，真实 PostgreSQL、固定离线模型、相同中文文本与输出节奏；P95 使用 nearest-rank。预热、建库和验收查询不计入样本；显式 SQL 包括测量窗口内 SSE 查询，不包括隐式 BEGIN／COMMIT、fetch 与连接池维护。

| 场景／指标 | 改动前 | 改动后 |
|---|---:|---:|
| 快速突发 128 片：文本事务中位数 | 128 | **2** |
| 同上：完整测量窗口 SQL 中位数 | 286 | 43 |
| 同上：首片提交 P95 | 0.815ms | 0.751ms |
| 同上：首片 SSE 生成 P95（从模型首次文本开始） | 151.351ms | 1.213ms |
| 同上：提交完成→SSE 生成 P95 | 147.402ms | **0.429ms** |
| 每片等待 5ms、32 片：文本事务中位数 | 32 | 9 |
| 同上：提交完成→SSE 生成 P95 | 144.211ms | 1.320ms |
| 写库注入 5ms、128 片：文本事务中位数 | 128 | 2 |
| 同上：模型流消费阶段中位耗时 | 918.027ms | 0.265ms |

最后一行只衡量生产方消费流的时间，不是整次运行耗时；最终文本排空和响应事务仍需要等待数据库，不能把 0.265ms 当作完整回答延迟。

快速突发场景文本事务减少 **98.4%**，达到至少 80% 的目标；各场景提交到 SSE 生成的 P95 均低于 30ms。慢速流不保证相同合并比例，供应商节奏与其他内容事件也会触发刷新。

这些是本机控制实验，系统负载并非固定；不代表浏览器可见首字、供应商网络耗时或与 Pi／Codex 的同机性能比较。

原始样本：[改动前](diagnostics/event-path-a-before.json)、[改动后](diagnostics/event-path-a-after.json)。基线采样时新 writer 文件可能已存在，但旧 Runner 尚未引用它，报告中的 Runner 哈希可区分实际调用路径。

## 5. 真实供应商与浏览器

真实 DeepSeek 核心与事件场景全部通过，共 12 次供应商请求、14,367 tokens（非缓存输入 5,632、缓存读取 8,192、输出 543），未假定货币价格。

- 核心 6 个场景：简单回复、读文件计算写入、新 Runner 历史恢复、真实摘要压缩、正常工具停止、Web API／SSE 回放。
- 事件场景：长文本收到 **199 个供应商文本增量，保存 43 条文本事件**，重建内容与最终回答逐字一致；另重复一次 Web SSE 回放验收。

这属于 8 次场景执行，Web 场景在两个套件中重复，不声称有 8 种独立能力。使用临时合成文件，没有向供应商发送凭证或无关源码；临时 schema 与工作目录均清理。

原始报告：[核心真实链路](diagnostics/live-chain-a-core.json)、[事件真实链路](diagnostics/live-chain-a-events.json)。

真实浏览器另连接临时本地 Uvicorn、临时工作区和固定流模型，验证了发送后显示生成状态、最终正文完整、完成后输入恢复可用；截图保存在本机 `/tmp/chagent-event-a-browser.png`。浏览器页签和服务已关闭，临时 schema 已检查清理。

浏览器本轮只做功能检查，没有采集绘制级首字延迟分布。因此上表的 P95 明确只对应 SSE 服务端生成，不把它描述成浏览器可见延迟。

## 6. 复现与边界

```sh
PYTHONPATH=src CHAGENT_TEST_DATABASE_URL=postgresql:///chagent \
  .venv/bin/python -m unittest discover -s tests -q

PYTHONPATH=src CHAGENT_TEST_DATABASE_URL=postgresql:///chagent \
  .venv/bin/python scripts/benchmark_event_path.py \
  --repeats 30 --output docs/diagnostics/event-path-local.json

# 以下调用真实付费供应商；保留已确认的命名 Key 选择方式
PYTHONPATH=src env -u CHAGENT_API_KEY uv run --env-file .env python \
  scripts/test_live_chain.py --case core --max-calls 16 --output docs/diagnostics/live-a-core-local.json

PYTHONPATH=src env -u CHAGENT_API_KEY uv run --env-file .env python \
  scripts/test_live_chain.py --case events --max-calls 4 --output docs/diagnostics/live-a-events-local.json
```

本批没有迁移。通知只在同一 Database 实例／事件循环内即时广播，其他实例或进程依靠 1 秒兜底；没有引入 LISTEN/NOTIFY 或外部消息服务。20ms 是刷新触发阈值，不是数据库持久化硬期限。

后续更正：本批只合并了"同一个 20ms 窗口内的片段"，每 20ms 仍然是一个事务。慢速流（一次刷新只有一片）因此还是每 20ms 提交一次，[F1 实施与审计](F1实施审计.md)把这一步改成每 100ms 一批提交，1.6 秒回答的事务数从 81 降到 21；本文的测量数字与结论仍按当时的状态保留。

主进程强杀仍可能丢失缓冲内未提交尾部并遗留 running，需要下一批 B1。文件写阻塞、外部副作用、主运行所有权和审批仍保持原边界；本批不会自动恢复或重跑任何工具。
