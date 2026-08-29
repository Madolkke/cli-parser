"use strict";

const PHASE_LABELS = { generation: "准备", schema: "Schema 阶段", ttp: "TTP 阶段", acceptance: "最终验收" };
const TOOL_RESULT_DIAGNOSTIC = "cli_parser.tool.result";
const MAX_SCHEMA_SUMMARY_FIELDS = 20;
const MAX_TOOL_ERROR_CHARS = 240;

function displayToolName(toolName) {
  return {
    submit_result_schema: "提交 Schema",
    submit_ttp_template: "提交 TTP 模板",
    test_ttp_template: "测试 TTP 模板",
    finish_generation: "确认生成完成",
  }[toolName] || "执行工具操作";
}

function parseJsonObject(text) {
  if (typeof text !== "string" || !text.trim()) return null;
  try {
    const parsed = JSON.parse(text);
    return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed : null;
  } catch {
    return null;
  }
}

function schemaNodeType(node) {
  if (!node || typeof node !== "object") return "unknown";
  if (typeof node.type === "string") return node.type;
  if (node.properties && typeof node.properties === "object") return "object";
  return "unknown";
}

function collectSchemaFields(node, path, depth, fields) {
  if (!node || typeof node !== "object" || depth > 4) return;
  const properties = node.properties && typeof node.properties === "object" && !Array.isArray(node.properties)
    ? node.properties : null;
  if (properties) {
    const required = new Set(Array.isArray(node.required) ? node.required : []);
    for (const [name, child] of Object.entries(properties)) {
      const childPath = path ? path + "." + name : name;
      fields.push({ path: childPath, type: schemaNodeType(child), required: required.has(name) });
      collectSchemaFields(child, childPath, depth + 1, fields);
    }
  }
  if (node.items && typeof node.items === "object") collectSchemaFields(node.items, path ? path + "[]" : "[]", depth + 1, fields);
}

function summarizeSchema(schemaPayload) {
  const schema = schemaPayload && schemaPayload.result_schema && typeof schemaPayload.result_schema === "object"
    ? schemaPayload.result_schema : schemaPayload;
  if (!schema || typeof schema !== "object" || Array.isArray(schema)) return "正在准备 Schema…";
  const fields = [];
  collectSchemaFields(schema, "", 0, fields);
  const shown = fields.slice(0, MAX_SCHEMA_SUMMARY_FIELDS);
  const requiredCount = fields.filter((field) => field.required).length;
  const details = shown.map((field) => field.path + ": " + field.type + (field.required ? "（必填）" : "")).join("、");
  const overflow = fields.length > shown.length ? "、另有 " + (fields.length - shown.length) + " 个字段" : "";
  return "根类型 " + schemaNodeType(schema) + " · 字段 " + fields.length + " 个 · 必填 " + requiredCount + " 个"
    + (details ? " · " + details : "") + overflow;
}

function summarizeTtpTemplate(template) {
  if (typeof template !== "string" || !template.trim()) return "正在准备模板…";
  const groups = (template.match(/<group(?:\s|>)/g) || []).length;
  const fields = [...template.matchAll(/\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\|/g)]
    .map((match) => match[1]).filter((name, index, all) => all.indexOf(name) === index);
  const shown = fields.slice(0, MAX_SCHEMA_SUMMARY_FIELDS);
  const details = shown.length ? " · 字段：" + shown.join("、") : "";
  const overflow = fields.length > shown.length ? "、另有 " + (fields.length - shown.length) + " 个字段" : "";
  return "分组 " + groups + " 个 · 字段 " + fields.length + " 个" + details + overflow;
}

function summarizeToolCall(toolName, callText) {
  const payload = parseJsonObject(callText);
  if (!payload) return toolName === "finish_generation" ? "等待确认" : "正在准备参数…";
  if (toolName === "submit_result_schema") return summarizeSchema(payload.result_schema || payload);
  if (toolName === "submit_ttp_template") return summarizeTtpTemplate(payload.ttp_template);
  if (toolName === "test_ttp_template") return summarizeTtpTemplate(payload.ttp_template);
  if (toolName === "finish_generation") return "请求结束生成";
  return "参数已准备";
}

function sanitizeToolError(detail) {
  if (!detail || typeof detail !== "object") return "";
  let message = detail.error || detail.exception || detail.message;
  if (typeof message !== "string" || !message.trim()) return "";
  message = message.replace(/call_[A-Za-z0-9_-]+/g, "[调用标识]");
  message = message.replace(/(?:api[_-]?key|authorization)\s*[:=]\s*\S+/ig, "凭据: [已隐藏]");
  return message.trim().slice(0, MAX_TOOL_ERROR_CHARS);
}

function rawTtpResultText(text) {
  if (typeof text !== "string" || !text.trim()) return "";
  if (/<parsed_record\b/i.test(text)) return text;
  try {
    const parsed = JSON.parse(text);
    if (Array.isArray(parsed)) return JSON.stringify(parsed, null, 2);
    if (parsed && typeof parsed === "object" && Array.isArray(parsed.records)) {
      return JSON.stringify(parsed.records, null, 2);
    }
  } catch {
    return "";
  }
  return "";
}

function summarizeToolResult(toolName, resultText, resultDetail, complete = true) {
  if (!complete) return { summary: "正在处理结果…", error: "" };
  const failed = resultDetail && (["error", "failed"].includes(resultDetail.state) || resultDetail.status === "failed" || resultDetail.error || resultDetail.exception);
  if (failed) {
    return {
      summary: toolName === "submit_result_schema" ? "Schema 提交失败" : toolName === "submit_ttp_template" ? "模板解析失败" : toolName === "test_ttp_template" ? "TTP 测试失败" : toolName === "finish_generation" ? "生成确认失败" : "工具执行失败",
      error: sanitizeToolError(resultDetail),
    };
  }
  if (toolName === "submit_result_schema") return { summary: "Schema 已提交", error: "" };
  if (toolName === "submit_ttp_template") {
    const count = typeof resultText === "string" ? (resultText.match(/<parsed_record\b/g) || []).length : 0;
    return { summary: count ? "已返回 " + count + " 个解析结果块" : "解析结果已返回", error: "" };
  }
  if (toolName === "test_ttp_template") {
    const count = typeof resultText === "string" ? (resultText.match(/<parsed_record\b/g) || []).length : 0;
    return { summary: count ? "测试结果已返回" : "测试未产生结果", error: "" };
  }
  if (toolName === "finish_generation") return { summary: "生成已确认", error: "" };
  return { summary: "工具已完成", error: "" };
}

function isModelFailure(type, detail) {
  if (!type.includes("model_call") || !type.endsWith("_completed")) return false;
  return Boolean(detail && (detail.error || detail.exception || ["failed", "error", "timeout"].includes(detail.status)));
}

function eventKind(type, detail = {}) {
  if (type === "run.finished") return "terminal";
  if (type.includes("retry")) return "retry";
  if (isModelFailure(type, detail)) return "status";
  if (type.includes("thinking")) return "thinking";
  if (type.includes("text")) return "text";
  if (type.includes("tool_call") || type.includes("tool_result")) return "tool";
  if (type === TOOL_RESULT_DIAGNOSTIC) return "hidden";
  if (type === "cli_parser.phase.started" || type === "cli_parser.phase.completed"
    || type === "agent.phase_started" || type === "agent.phase_completed"
    || type === "cli_parser.final_validation.started" || type === "cli_parser.final_validation.completed") return "phase";
  if (type.includes("exception") || type.includes("failed") || type.includes("cancelled")) return "status";
  return "hidden";
}

function sanitizeDisplayDetail(detail) {
  if (!detail || typeof detail !== "object" || Array.isArray(detail)) return {};
  const displayDetail = { ...detail };
  delete displayDetail.text;
  delete displayDetail.coalesced;
  delete displayDetail.input_tokens;
  delete displayDetail.output_tokens;
  delete displayDetail.request;
  delete displayDetail.frozen_result_schema;
  return displayDetail;
}

function timelinePhaseLabel(phase) { return PHASE_LABELS[phase] || phase || "进行中"; }

function phaseForEvent(event) {
  if (event.phase) return event.phase;
  if ((event.type || "").includes("final_validation")) return "acceptance";
  return "";
}

function eventKey(event, kind) {
  const detail = event.detail || {};
  if (kind === "phase") return "phase:" + (phaseForEvent(event) || "unknown");
  if (kind === "tool") return "tool:" + (event.tool_call_id || event.block_id || detail.tool_call_id || detail.tool_name || event.sequence || "event");
  if (kind === "terminal") return "terminal";
  return kind + ":" + (event.block_id || detail.reply_id || event.sequence || "event");
}

function createTimelineState(maxEntries = 120) {
  return { entries: [], byKey: new Map(), maxEntries, latestStreamingKey: null, currentPhase: null };
}

function isLatestStreamingEntry(timeline, entry) {
  return Boolean(entry && !entry.complete && timeline.latestStreamingKey === entry.key);
}

function shouldOpenEntry(timeline, entry) {
  if (entry.kind === "phase") return false;
  if (isLatestStreamingEntry(timeline, entry)) return true;
  if (entry.manualOpen !== null) return entry.manualOpen;
  return entry.kind === "text";
}

function createEntry(timeline, key, kind, event) {
  const entry = {
    key, kind, phase: phaseForEvent(event), roundIndex: event.round_index ?? null,
    elapsed: event.elapsed_seconds || 0, title: "", text: "", detail: null,
    toolName: "", callText: "", callSummary: "", callComplete: false,
    rawResultText: "", resultSummary: "", errorSummary: "", resultComplete: false, status: "running",
    rawResultManualOpen: null,
    complete: false, manualOpen: null,
  };
  timeline.byKey.set(key, entry);
  timeline.entries.push(entry);
  return entry;
}

function trimEntries(timeline) {
  let removedKey = null;
  while (timeline.entries.length > timeline.maxEntries) {
    const removableIndex = timeline.entries.findIndex((entry) => entry.kind !== "phase");
    if (removableIndex < 0) break;
    const [removed] = timeline.entries.splice(removableIndex, 1);
    removedKey = removed.key;
    timeline.byKey.delete(removed.key);
    if (timeline.latestStreamingKey === removed.key) timeline.latestStreamingKey = null;
  }
  return removedKey;
}

function setCommonFields(entry, event) {
  entry.phase = phaseForEvent(event) || entry.phase;
  entry.roundIndex = event.round_index ?? entry.roundIndex;
  entry.elapsed = event.elapsed_seconds || entry.elapsed;
}

function hasToolProjection(timeline, detail) {
  const toolCallId = detail.tool_call_id;
  const toolName = detail.tool_name;
  return [...timeline.byKey.values()].some((entry) => entry.kind === "tool" && (
    (toolCallId && entry.key === "tool:" + toolCallId)
    || (toolName && entry.toolName === toolName)
  ));
}

function appendDiagnosticToolResult(entry, detail) {
  entry.toolName = detail.tool_name || entry.toolName || "工具";
  entry.title = displayToolName(entry.toolName);
  const output = detail.output && typeof detail.output === "object" ? detail.output : {};
  const failed = ["error", "failed"].includes(output.state) || output.status === "failed" || Boolean(output.error || output.exception);
  entry.status = failed ? "失败" : "成功";
  entry.resultComplete = true;
  entry.complete = true;
  const result = summarizeToolResult(entry.toolName, "", output, true);
  entry.resultSummary = result.summary;
  entry.errorSummary = result.error;
}

function appendAgentEvent(timeline, event) {
  const type = event.type || "";
  const detail = event.detail && typeof event.detail === "object" ? event.detail : {};
  let kind = eventKind(type, detail);
  if (type === TOOL_RESULT_DIAGNOSTIC) {
    if (hasToolProjection(timeline, detail)) {
      return { entry: null, hidden: true, created: false, removedKey: null, isDelta: false, previousLatestStreamingKey: timeline.latestStreamingKey };
    }
    kind = "tool";
  }
  const key = eventKey(event, kind);
  const previousLatestStreamingKey = timeline.latestStreamingKey;
  if (kind === "hidden") return { entry: null, hidden: true, created: false, removedKey: null, isDelta: false, previousLatestStreamingKey };

  let entry = timeline.byKey.get(key);
  let created = false;
  if (kind === "phase") {
    if (!entry) { entry = createEntry(timeline, key, kind, event); created = true; }
    setCommonFields(entry, event);
    entry.title = timelinePhaseLabel(entry.phase);
    entry.complete = type.endsWith("_completed");
    timeline.currentPhase = entry.phase;
    return { entry, hidden: false, created, removedKey: trimEntries(timeline), isDelta: false, previousLatestStreamingKey };
  }
  if (!entry) { entry = createEntry(timeline, key, kind, event); created = true; }
  setCommonFields(entry, event);

  if (kind === "thinking" || kind === "text") {
    entry.title = kind === "thinking" ? "思考" : "模型输出";
    if (typeof detail.text === "string") entry.text += detail.text;
  } else if (kind === "tool") {
    entry.toolName = detail.tool_name || entry.toolName || "工具";
    entry.title = displayToolName(entry.toolName);
    if (type.includes("tool_call")) {
      if (typeof detail.text === "string") entry.callText += detail.text;
      entry.callSummary = summarizeToolCall(entry.toolName, entry.callText);
      if (type.endsWith("_completed")) entry.callComplete = true;
      entry.status = entry.callComplete ? "等待结果" : "调用中";
    } else if (type.includes("tool_result")) {
      if (typeof detail.text === "string") entry.rawResultText += detail.text;
      const result = summarizeToolResult(entry.toolName, entry.rawResultText, detail, type.endsWith("_completed"));
      entry.resultSummary = result.summary;
      entry.errorSummary = result.error;
      if (type.endsWith("_started")) entry.status = "处理中";
      if (type.endsWith("_completed")) {
        entry.resultComplete = true;
        entry.status = ["error", "failed"].includes(detail.state) || detail.status === "failed" || detail.error || detail.exception ? "失败" : "成功";
        entry.complete = true;
      }
    } else if (type === TOOL_RESULT_DIAGNOSTIC) {
      appendDiagnosticToolResult(entry, detail);
    }
  } else if (kind === "retry") {
    entry.title = "重试";
    entry.status = "等待重试";
    entry.detail = sanitizeDisplayDetail(detail);
    entry.complete = true;
  } else if (kind === "status") {
    entry.title = type.includes("model_call") ? "模型请求失败" : type.includes("cancelled") ? "运行已取消" : "运行异常";
    entry.status = "失败";
    entry.detail = sanitizeDisplayDetail(detail);
    entry.complete = true;
  } else if (kind === "terminal") {
    const status = event.status || detail.status || "unknown";
    entry.title = status === "success" ? "运行完成" : status === "cancelled" ? "运行已取消" : "运行失败";
    entry.status = status === "success" ? "成功" : status === "cancelled" ? "已取消" : "失败";
    entry.detail = status === "success" ? null : sanitizeDisplayDetail(detail);
    entry.complete = true;
  }

  const isDelta = type.endsWith("_delta");
  if (type.endsWith("_completed") && kind !== "tool") entry.complete = true;
  if (isDelta) timeline.latestStreamingKey = key;
  return { entry, hidden: false, created, removedKey: trimEntries(timeline), isDelta, previousLatestStreamingKey };
}

function buildTimeline(events, maxEntries = 120) {
  const timeline = createTimelineState(maxEntries);
  for (const event of events || []) appendAgentEvent(timeline, event);
  return timeline;
}

function reduceAgentEvents(events) { return buildTimeline(events).entries; }

function phaseEntries(timeline) {
  return timeline.entries.filter((entry) => entry.kind === "phase");
}

function entriesForPhase(timeline, phase) {
  if (!phase || phase === "all") return timeline.entries.slice();
  return timeline.entries.filter((entry) => entry.phase === phase);
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
    schedule() { if (handle !== null) return; handle = requestFrame(() => { handle = null; render(); }); },
    cancel() { if (handle === null) return; cancelFrame(handle); handle = null; },
    pending: () => handle !== null,
  };
}

const timelineApi = {
  appendAgentEvent, buildTimeline, createRenderScheduler, createSequenceTracker,
  createTimelineState, displayToolName, entriesForPhase, eventKind, isLatestStreamingEntry, phaseEntries, phaseLabel: timelinePhaseLabel,
  reduceAgentEvents, sanitizeDisplayDetail, sanitizeToolError, shouldOpenEntry,
  summarizeSchema, summarizeToolCall, summarizeToolResult, summarizeTtpTemplate, rawTtpResultText,
};
if (typeof window !== "undefined") window.AgentTimeline = timelineApi;
if (typeof module !== "undefined") module.exports = timelineApi;
