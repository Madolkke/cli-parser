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

function reduceAgentEvents(events) {
  const entries = [];
  const byKey = new Map();
  for (const event of events || []) {
    const type = event.type || "";
    const kind = eventKind(type);
    const isDelta = type.endsWith("_delta");
    const isEnd = type.endsWith("_completed");
    const detail = event.detail || {};
    const key = eventKey(event, kind);
    let entry = byKey.get(key);
    if (!entry || (!isDelta && !isEnd && !type.endsWith("_started") && kind === "fact")) {
      entry = { key, kind, phase: event.phase, elapsed: event.elapsed_seconds || 0, text: "", detail: null, complete: false, title: "" };
      byKey.set(key, entry); entries.push(entry);
    }
    entry.phase = event.phase || entry.phase;
    entry.elapsed = event.elapsed_seconds || entry.elapsed;
    if (kind === "thinking") entry.title = "思考";
    else if (kind === "text") entry.title = "模型输出";
    else if (kind === "tool_call") entry.title = detail.tool_name ? "工具调用 · " + detail.tool_name : "工具调用";
    else if (kind === "tool_result") entry.title = detail.tool_name ? "工具结果 · " + detail.tool_name : "工具结果";
    else if (kind === "model") entry.title = "模型请求";
    else entry.title = describeEvent(event);
    if (typeof detail.text === "string") entry.text += detail.text;
    if (kind === "model" || kind === "fact") entry.detail = detail;
    if (isEnd || type === "run.finished" || type === "cli_parser.phase.completed") entry.complete = true;
  }
  return entries.slice(-120);
}

const timelineApi = { eventKind, reduceAgentEvents };
if (typeof window !== "undefined") window.AgentTimeline = timelineApi;
if (typeof module !== "undefined") module.exports = timelineApi;
