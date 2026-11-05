/* The run stream as a pure function.
 *
 * Every SSE event the server sends for a running turn becomes one branch here,
 * and the DOM is never touched: that is what lets tests/js/replay.test.mjs feed
 * the same events twice and compare the two results, with no stub and no
 * waiting on a paint.
 *
 * `state.items` is ordered by arrival, because that is the order the reader
 * watched them appear: a compaction notice belongs where it happened, not at
 * the bottom under the running tool. Tool rows are updated in place by
 * `callId`; the answer text of one model turn is one item, so a second turn
 * starts a new paragraph instead of appending to the previous one.
 */

export function initialRunState(runId = null) {
  return {
    runId,
    //: Empty until an event says otherwise. A placeholder here would be painted
    //: over the "就绪" the page writes when a finished run's transcript reloads.
    status: "",
    items: [],
    //: Monotonic id source. Part of the state, not module scope, so replaying
    //: the same events produces the same ids and therefore the same DOM.
    seq: 0,
    openTextId: null,
    approvalId: null,
  };
}

const copy = state => ({...state, items: state.items.map(item => ({...item}))});

function findItem(state, predicate) {
  return state.items.find(predicate) || null;
}

function notice(state, text, failed = false) {
  const next = copy(state);
  next.items.push({kind: "notice", id: ++next.seq, text, failed});
  return next;
}

function upsertTool(state, payload, change) {
  const next = copy(state);
  let tool = findItem(next, item => item.kind === "tool" && item.callId === payload.tool_call_id);
  if (!tool) {
    tool = {kind: "tool", id: ++next.seq, callId: payload.tool_call_id, name: payload.name, state: "正在执行", failed: false};
    next.items.push(tool);
  }
  change(tool);
  return next;
}

export function reduce(state, event) {
  const {type, payload} = event || {};
  switch (type) {
    case "model_started":
      // A new model turn: its answer is a new paragraph, and the previous one
      // is no longer the one receiving deltas.
      return {...state, openTextId: null, status: "正在思考…"};

    case "text_delta": {
      const next = copy(state);
      let item = next.openTextId
        ? findItem(next, entry => entry.kind === "text" && entry.id === next.openTextId) : null;
      if (!item) {
        item = {kind: "text", id: ++next.seq, text: ""};
        next.items.push(item);
        next.openTextId = item.id;
      }
      item.text += payload.delta;
      next.status = "正在生成…";
      return next;
    }

    case "tool_queued":
    case "tool_started":
      return upsertTool(state, payload, tool => {
        tool.state = type === "tool_queued" ? "等待执行" : "正在执行";
      });

    case "tool_progress":
      return upsertTool(state, payload, tool => { tool.state = payload.text; });

    case "tool_completed":
      return upsertTool(state, payload, tool => {
        tool.state = payload.outcome_unknown ? "结果未知" : payload.is_error ? "失败" : "完成";
        tool.failed = Boolean(payload.is_error || payload.outcome_unknown);
      });

    case "tool_interrupted":
    case "tool_cancelled":
      return upsertTool(state, payload, tool => {
        tool.state = type === "tool_cancelled" ? "未执行，已取消" : "结果未知";
        tool.failed = true;
      });

    case "approval_requested": {
      const next = copy(state);
      const item = {kind: "approval", id: ++next.seq, state: "loading", request: null};
      next.items.push(item);
      next.approvalId = item.id;
      next.status = "等待确认操作…";
      return next;
    }

    // Not an SSE event: the page fetches the pending request after it sees
    // `approval_requested`, and reports the answer back through the same fold.
    case "approval_loaded": {
      const next = copy(state);
      const item = next.approvalId
        ? findItem(next, entry => entry.kind === "approval" && entry.id === next.approvalId)
        : findItem(next, entry => entry.kind === "approval");
      if (!item) return state;
      item.request = payload.request;
      item.state = payload.request ? "pending" : "failed";
      item.error = payload.error || "";
      return next;
    }

    case "approval_decided":
    case "approval_expired":
    case "approval_cancelled": {
      if (state.approvalId === null) return state;
      const next = copy(state);
      next.items = next.items.filter(item => item.id !== next.approvalId);
      next.approvalId = null;
      return next;
    }

    case "compaction_started":
      return {...state, status: "正在压缩历史上下文…"};

    case "compaction_completed":
      return notice(state,
        `历史已压缩，摘要第 ${payload.revision} 版，${payload.tokens_before} → ${payload.tokens_after} 估算 token`);

    case "compaction_failed":
      return notice(state, "压缩未完成，继续使用原有摘要和近期完整轮次。", true);

    case "memory_extraction_started":
      return {...state, status: "正在整理记忆候选…"};

    case "memory_saved": {
      const active = payload.items.filter(item => item.status === "active").length;
      const pending = payload.items.filter(item => item.status === "candidate").length;
      return notice(state, `记忆已处理，${active} 条已生效，${pending} 条待审核`);
    }

    case "memory_extraction_failed":
    case "memory_write_failed":
      return notice(state, "本次记忆整理未完成，回答已保留。", true);

    case "workspace_manifest_after": {
      const changes = payload?.diff;
      if (!changes) return notice(state, "工作区结束清单已记录；起始清单不可用，无法计算差异。", true);
      const {added, modified, deleted} = changes.counts;
      const paths = [...changes.added, ...changes.modified, ...changes.deleted]
        .slice(0, 12).map(item => item.path).join("、");
      const suffix = paths ? `：${paths}${added + modified + deleted > 12 ? " 等" : ""}` : "";
      return notice(state,
        `工作区文件差异（已记录）：新增 ${added}，修改 ${modified}，删除 ${deleted}${suffix}${changes.complete ? "" : "；扫描不完整，请查看运行记录"}`,
        !changes.complete);
    }

    case "workspace_manifest_error":
      return notice(state, "工作区文件清单未能完整记录，请查看运行记录。", true);

    case "network_observed": {
      if (!payload.complete) return notice(state, "本次命令网络观测不完整，请查看运行记录。", true);
      const destinations = payload.targets.slice(0, 12).map(item => `${item.ip}:${item.port}`).join("、");
      const more = payload.targets.length > 12 ? ` 等 ${payload.targets.length} 个目标` : "";
      return notice(state, `工作区容器出网：${payload.sent_bytes} 字节；目标 ${destinations || "无"}${more}`);
    }

    default:
      // Unknown event types are ignored rather than fatal: the server owns the
      // event vocabulary, and a page that throws on a new one loses the run.
      return state;
  }
}
