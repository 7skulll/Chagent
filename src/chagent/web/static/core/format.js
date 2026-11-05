/* Pure formatting and classification: no DOM, no fetch, no module state.
 *
 * Everything here is a function of its arguments alone, which is what lets
 * tests/js/pure.test.mjs check it under plain `node --test` without a DOM stub.
 * "Which string for this status" is a product decision, so it lives in one
 * place instead of being spelled out again at each call site.
 */

/* ---------- numbers ---------- */

export function formatTokens(value) {
  if (value < 1000) return String(value);
  if (value < 1e6) return `${(value / 1000).toFixed(1)}k`;
  return `${(value / 1e6).toFixed(1)}M`;
}

// A missing denominator is not a 0% hit rate; it is "no fresh input to serve".
export function formatRate(rate) {
  return rate === null || rate === undefined ? "—" : `${Math.round(rate * 100)}%`;
}

export function formatSize(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

/* ---------- paths ---------- */
// The full path lives in the title. The sidebar shows the last segment in ink
// with its parent dimmed before it, so a deep checkout never truncates to a
// slash the way a left-anchored path does.

export function workspaceName(path) {
  const parts = String(path || "").split("/").filter(Boolean);
  return parts.length ? parts[parts.length - 1] : String(path || "");
}

export function workspaceParent(path) {
  const parts = String(path || "").split("/").filter(Boolean).slice(0, -1);
  return parts.length ? `${parts[parts.length - 1]}/` : "";
}

export function hostOf(url) {
  try { return new globalThis.URL(url).host; } catch { return ""; }
}

// "第 2、3 页" for a PDF, the heading path for a Word file, the sheet name for
// Excel — whichever locator the parser actually produced.
export function locatorLabel(citations) {
  const pages = [...new Set(citations.map(item => item.page).filter(page => page != null))];
  if (pages.length) return `第 ${pages.join("、")} 页`;
  const sections = [...new Set(citations.map(item => item.section).filter(Boolean))];
  return sections.slice(0, 3).join(" · ");
}

/* ---------- messages ---------- */

export function plainText(message) {
  const content = message.role === "toolResult" && message.artifact
    ? [{type: "text", text: message.artifact.preview}] : message.content;
  if (typeof content === "string") return content;
  return (content || []).filter(block => block.type === "text").map(block => block.text).join("\n");
}

// A file name is a path the tool can open, so the sent sentence lists it where
// the user can still edit or drop it.
export function attachmentPrompt(paths) {
  return paths.length ? `请阅读 ${paths.join("、")}` : "";
}

/* ---------- runs ---------- */
// A live run and a stored one describe the same thing with different words: the
// poll returns `status`, the transcript returns `end_reason`. One table, both.

const END_STATUS = {completed: "已完成", failed: "运行失败", cancelled: "已停止", interrupted: "运行已中断"};
const END_REASON = {
  tool_requested_stop: "工具已请求正常停止",
  explicit_memory: "已写入长期记忆",
  turn_limit: "已达到模型轮数上限",
  repeated_tool_call: "重复调用同一工具且没有进展，已停止",
  run_timeout: "已达到运行时间上限",
  context_budget_exhausted: "上下文预算不足",
  output_limit: "模型输出达到上限",
  model_aborted: "模型已停止生成",
  unwatched: "无人查看审批，操作未执行",
};

export function runStatusText(run) {
  if (!run) return "";
  return END_REASON[run.end_reason] || END_STATUS[run.status] || run.status;
}

export function toolStateText(type) {
  if (type === "tool_queued") return "等待执行";
  if (type === "tool_completed") return "完成";
  if (type === "tool_cancelled") return "未执行，已取消";
  if (type === "tool_interrupted") return "结果未知";
  return "正在执行";
}

const TERMINAL = new Set(["completed", "failed", "cancelled", "interrupted"]);
export const isTerminal = status => TERMINAL.has(status);

/* ---------- uploads ---------- */

export const MAX_UPLOAD_BYTES = 20 * 1024 * 1024;

//: What read_file can actually open. Anything else is refused here rather than
//: uploaded and then reported as unreadable three tool calls later.
export const READABLE = /\.(pdf|docx|xlsx|txt|md|markdown|rst|json|jsonl|ndjson|yaml|yml|toml|csv|tsv|log|py|js|mjs|cjs|jsx|ts|tsx|go|rs|java|kt|rb|php|c|h|cc|cpp|hpp|cs|swift|m|mm|sh|bash|zsh|fish|sql|html|htm|css|scss|sass|less|vue|svelte|xml|ini|cfg|conf|env|properties|gradle|lock|mod|sum|proto|graphql|gql)$/i;
export const IMAGE = /\.(png|jpe?g|gif|webp|bmp|svg|heic|avif)$/i;

export function readableName(name) {
  if (!name.includes(".")) return true;   // README, Makefile, LICENSE
  return READABLE.test(name);
}

const UPLOAD_STATUS = item => ({
  queued: "等待上传",
  uploading: `上传中 ${Math.round(item.progress * 100)}%`,
  pending: "等待同名输出确认",
  done: `已上传 ${formatSize(item.size)}`,
  failed: item.error || "上传失败",
})[item.status];

export const uploadStatusLabel = item => UPLOAD_STATUS(item) || item.status;

export const PENDING_UPLOAD = ["queued", "uploading", "pending"];
export const isPendingUpload = item => PENDING_UPLOAD.includes(item.status);

/* ---------- approvals ---------- */
// A write is not a command: the card has to say what it would replace, not name
// the tool and leave the user reading JSON.

const WRITE_LABELS = {write_file: "写入文件", edit_file: "修改文件", apply_patch: "应用补丁", create_directory: "新建目录"};

export const approvalTitle = toolName => WRITE_LABELS[toolName] || toolName;

/* ---------- runtime configuration ---------- */
// The same facts the CLI prints as `Behavior:`.

export const CONFIG_LABELS = [
  ["provider", "供应商"], ["model", "模型"], ["tools", "工具数"],
  ["controller", "主控位置"],
  ["run_timeout", "单次运行上限"], ["boundary", "执行边界"], ["capability", "执行能力"], ["gate", "门禁"], ["approval", "审批"],
  ["host_commands", "命令执行"], ["workspace_writable", "工作区写入"], ["process_stdin", "交互输入"], ["web_access", "网页搜索"], ["compaction", "上下文压缩"],
  ["tokenizer", "token 计数"], ["max_turns", "最大轮数"], ["max_processes", "会话进程"],
];

export function configValue(key, c) {
  switch (key) {
    case "tools": return `${c.tools} 个`;
    case "run_timeout": return `${Math.round(c.run_timeout)} 秒`;
    case "boundary": return c.boundary === "container" ? "容器（命令可联网）" : "无命令边界";
    case "capability": return c.capability === "full" ? "可执行命令" : "只读，不执行命令";
    case "gate": return c.controller === "host-controller" ? "仅宿主原生操作逐次审批" : c.gate === "approval" ? "逐次审批" : "无需审批";
    case "controller": return c.controller === "host-controller" ? "本机；每工作区独立 Docker 执行容器" : c.controller;
    case "approval": return `${c.approval}，等待 ${Math.round(c.approval_timeout)} 秒，` +
      (c.approval_grace ? `无人查看 ${Math.round(c.approval_grace)} 秒后放弃` : "无人查看也等到超时");
    case "workspace_writable": return c.workspace_writable ? "允许" : "关闭";
    case "process_stdin": return c.process_stdin ? "允许" : "关闭";
    case "host_commands": return c.host_commands ? "开启" : "关闭";
    case "web_access": return c.web_access === "off" ? "关闭" : `${c.web_access}，密钥${c.web_key === "set" ? "已配置" : "缺失"}`;
    case "compaction": return c.compaction ? "开启" : "关闭";
    case "max_turns": return `${c.max_turns} 轮`;
    case "max_processes": return `${c.max_processes} 个，单个 ${Math.round(c.process_timeout)} 秒`;
    default: return String(c[key]);
  }
}

/* ---------- import/memory notices ---------- */
// One sentence each, because these appear once per run and must not be
// recombined by the caller in two slightly different ways.

export const MEMORY_JOB = {
  queued: "等待后台整理记忆",
  running: "正在后台整理记忆",
  completed: "后台记忆整理完成",
  failed: "后台记忆整理未完成，回答已保留",
};

/* ---------- page constants shared by more than one view ---------- */
//: An archived session is still readable; only new input is refused. The server
//: enforces the same rule, this is the part the user can see.
export const ARCHIVED_HINT = "已归档，恢复后才能继续对话";
//: How close to the bottom still counts as "following the stream".
export const NEAR_BOTTOM = 96;
