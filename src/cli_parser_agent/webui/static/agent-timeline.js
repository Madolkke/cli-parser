"use strict";

function eventKind(type) {
  if (type.includes("thinking")) return "thinking";
  if (type.includes("tool_call")) return "tool_call";
  if (type.includes("tool_result")) return "tool_result";
  if (type.includes("text")) return "text";
  if (type.includes("model_call")) return "model";
  return "fact";
}

function describeEvent(event) {
  if (event.type === "run.finished") return "结束：" + ({ success: "成功", failed: "失败", cancelled: "已取消" }[event.status] || event.status || "未知");
  const detail = event.detail || {};
  const extra = detail.termination_reason || detail.reason || detail.status || "";
  return (event.type || "").replace("cli_parser.", "") + (extra ? " · " + extra : "");
}

function eventKey(event, kind) {
  const detail = event.detail || {};
  const id = event.block_id || event.tool_call_id || detail.reply_id;
  return kind + ":" + (id || event.sequence || "event");
}

function createTimelineState(maxEntries = 120) {
  return { entries: [], byKey: new Map(), maxEntries };
}

function appendAgentEvent(timeline, event) {
  const type = event.type || "";
  const kind = eventKind(type);
  const isDelta = type.endsWith("_delta");
  const isEnd = type.endsWith("_completed");
  const detail = event.detail || {};
  const key = eventKey(event, kind);
  let entry = timeline.byKey.get(key);
  let created = false;
  let removedKey = null;
  if (!entry) {
    entry = {
      key, kind, phase: event.phase, elapsed: event.elapsed_seconds || 0,
      text: "", detail: null, complete: false, title: "", manualOpen: null,
    };
    timeline.byKey.set(key, entry);
    timeline.entries.push(entry);
    created = true;
    if (timeline.entries.length > timeline.maxEntries) {
      const removed = timeline.entries.shift();
      removedKey = removed.key;
      timeline.byKey.delete(removed.key);
    }
  }
  entry.phase = event.phase || entry.phase;
  entry.elapsed = event.elapsed_seconds || entry.elapsed;
  if (kind === "thinking") entry.title = "思考";
  else if (kind === "text") entry.title = "模型输出";
  else if (kind === "tool_call") entry.title = detail.tool_name ? "工具调用 · " + detail.tool_name : (entry.title || "工具调用");
  else if (kind === "tool_result") entry.title = detail.tool_name ? "工具结果 · " + detail.tool_name : (entry.title || "工具结果");
  else if (kind === "model") entry.title = "模型请求";
  else entry.title = describeEvent(event);
  if (typeof detail.text === "string") entry.text += detail.text;
  const structuredDetail = { ...detail };
  delete structuredDetail.text;
  if (Object.keys(structuredDetail).length) entry.detail = { ...(entry.detail || {}), ...structuredDetail };
  if (isEnd || type === "run.finished" || type === "cli_parser.phase.completed") entry.complete = true;
  return { entry, created, removedKey, isDelta };
}

function buildTimeline(events, maxEntries = 120) {
  const timeline = createTimelineState(maxEntries);
  for (const event of events || []) appendAgentEvent(timeline, event);
  return timeline;
}

function reduceAgentEvents(events) {
  return buildTimeline(events).entries;
}

function createSequenceTracker(events = []) {
  const seen = new Set();
  let highest = 0;
  function accept(event) {
    const sequence = Number(event && event.sequence || 0);
    if (sequence > 0) {
      if (seen.has(sequence)) return false;
      seen.add(sequence);
      highest = Math.max(highest, sequence);
    }
    return true;
  }
  for (const event of events) accept(event);
  return { accept, highest: () => highest, size: () => seen.size };
}

function createRenderScheduler(render, requestFrame, cancelFrame) {
  let handle = null;
  return {
    schedule() {
      if (handle !== null) return;
      handle = requestFrame(() => {
        handle = null;
        render();
      });
    },
    cancel() {
      if (handle === null) return;
      cancelFrame(handle);
      handle = null;
    },
    pending: () => handle !== null,
  };
}

const timelineApi = {
  appendAgentEvent,
  buildTimeline,
  createRenderScheduler,
  createSequenceTracker,
  createTimelineState,
  eventKind,
  reduceAgentEvents,
};
if (typeof window !== "undefined") window.AgentTimeline = timelineApi;
if (typeof module !== "undefined") module.exports = timelineApi;
