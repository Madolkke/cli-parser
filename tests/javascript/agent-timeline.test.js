"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const timeline = require("../../src/cli_parser_agent/webui/static/agent-timeline.js");

test("reduces streamed thinking, text, tool call, and result blocks", () => {
  const entries = timeline.reduceAgentEvents([
    { type: "agent.thinking_started", block_id: "think", phase: "ttp", elapsed_seconds: 1 },
    { type: "agent.thinking_delta", block_id: "think", detail: { text: "先检查" }, phase: "ttp", elapsed_seconds: 2 },
    { type: "agent.thinking_completed", block_id: "think", phase: "ttp", elapsed_seconds: 3 },
    { type: "agent.text_started", block_id: "text", phase: "ttp", elapsed_seconds: 4 },
    { type: "agent.text_delta", block_id: "text", detail: { text: "提交" }, phase: "ttp", elapsed_seconds: 5 },
    { type: "agent.text_completed", block_id: "text", phase: "ttp", elapsed_seconds: 6 },
    { type: "agent.tool_call_started", tool_call_id: "call", detail: { tool_name: "submit_ttp_template" }, phase: "ttp", elapsed_seconds: 7 },
    { type: "agent.tool_call_delta", tool_call_id: "call", detail: { text: "{\"ttp_template\":\"<group>{{ x | WORD }}</group>\"}" }, phase: "ttp", elapsed_seconds: 8 },
    { type: "agent.tool_call_completed", tool_call_id: "call", phase: "ttp", elapsed_seconds: 9 },
    { type: "agent.tool_result_started", tool_call_id: "call", detail: { tool_name: "submit_ttp_template" }, phase: "ttp", elapsed_seconds: 10 },
    { type: "agent.tool_result_delta", tool_call_id: "call", detail: { text: "{\"accepted\":true}" }, phase: "ttp", elapsed_seconds: 11 },
    { type: "agent.tool_result_completed", tool_call_id: "call", detail: { state: "success" }, phase: "ttp", elapsed_seconds: 12 },
  ]);

  assert.deepEqual(entries.map((entry) => entry.kind), ["thinking", "text", "tool"]);
  assert.equal(entries[0].text, "先检查");
  assert.equal(entries[1].text, "提交");
  assert.equal(entries[2].toolName, "submit_ttp_template");
  assert.equal(entries[2].title, "提交 TTP 模板");
  assert.equal(entries[2].callText, '{"ttp_template":"<group>{{ x | WORD }}</group>"}');
  assert.equal(entries[2].rawResultText, '{"accepted":true}');
  assert.equal(entries[2].callSummary, "分组 1 个 · 字段 1 个 · 字段：x");
  assert.equal(entries[2].resultSummary, "解析结果已返回");
  assert.equal("tool_call_id" in entries[2], false);
  assert.equal(entries[2].status, "成功");
  assert.equal(entries.every((entry) => entry.complete), true);
});

test("projects lifecycle noise into phase separators and hides duplicate diagnostics", () => {
  const entries = timeline.reduceAgentEvents([
    { type: "cli_parser.generation.started", phase: "generation", detail: { request: { command_outputs: ["secret"] } } },
    { type: "cli_parser.phase.started", phase: "ttp", round_index: 1 },
    { type: "agent.model_call_started", phase: "ttp", detail: { model_name: "model" } },
    { type: "agent.tool_call_started", phase: "ttp", tool_call_id: "call-1", detail: { tool_name: "submit_ttp_template" } },
    { type: "agent.tool_result_completed", phase: "ttp", tool_call_id: "call-1", detail: { tool_name: "submit_ttp_template", text: "结果" } },
    { type: "cli_parser.tool.result", phase: "ttp", detail: { tool_name: "submit_ttp_template", output: { accepted: true } } },
    { type: "cli_parser.phase.completed", phase: "ttp" },
  ]);

  assert.deepEqual(entries.map((entry) => entry.kind), ["phase", "tool"]);
  assert.equal(entries[0].title, "TTP 阶段");
  assert.equal(entries[1].rawResultText, "结果");
  assert.equal(entries.filter((entry) => entry.kind === "tool").length, 1);
  assert.equal(entries.some((entry) => JSON.stringify(entry).includes("secret")), false);
});

test("shows a compact diagnostic tool fallback only without an agent tool projection", () => {
  const entries = timeline.reduceAgentEvents([
    { type: "cli_parser.tool.result", phase: "ttp", sequence: 1, detail: { tool_name: "submit_ttp_template", input: { secret: "no" }, output: { state: "success", capture: { records: [{ secret: "no" }] } } } },
  ]);

  assert.deepEqual(entries.map((entry) => entry.kind), ["tool"]);
  assert.equal(entries[0].title, "提交 TTP 模板");
  assert.equal(entries[0].status, "成功");
  assert.equal(JSON.stringify(entries[0]).includes("secret"), false);
});

test("summarizes schema structure without exposing schema JSON", () => {
  const schema = {
    type: "object",
    required: ["hostname"],
    properties: {
      hostname: { type: "string" },
      interfaces: { type: "array", items: { type: "object", required: ["name"], properties: { name: { type: "string" } } } },
    },
    description: "secret description",
  };
  const summary = timeline.summarizeToolCall("submit_result_schema", JSON.stringify({ result_schema: schema }));

  assert.equal(summary.includes("根类型 object"), true);
  assert.equal(summary.includes("字段 3 个"), true);
  assert.equal(summary.includes("必填 2 个"), true);
  assert.equal(summary.includes("hostname: string（必填）"), true);
  assert.equal(summary.includes("secret description"), false);
  assert.equal(summary.includes('"properties"'), false);
});

test("summarizes TTP structure and finish semantics without raw protocol text", () => {
  const template = '<template><group><group name="items*">{{ id | DIGIT }} {{ name | WORD }}</group></group></template>';
  const templateSummary = timeline.summarizeToolCall("submit_ttp_template", JSON.stringify({ ttp_template: template }));
  const finishSummary = timeline.summarizeToolCall("finish_generation", "{}");

  assert.equal(templateSummary, "分组 2 个 · 字段 2 个 · 字段：id、name");
  assert.equal(templateSummary.includes("<group"), false);
  assert.equal(finishSummary, "请求结束生成");
  assert.equal(finishSummary.includes("{}"), false);
});

test("shows semantic tool result and bounded sanitized errors", () => {
  const success = timeline.summarizeToolResult("submit_ttp_template", "<parsed_record input_index=\"0\">…</parsed_record>", { state: "success", accepted: true, issues: [] });
  const failure = timeline.summarizeToolResult("submit_ttp_template", "raw result", { state: "error", error: "call_00_secret request failed" });

  assert.deepEqual(success, { summary: "已返回 1 个解析结果块", error: "" });
  assert.equal(failure.summary, "模板解析失败");
  assert.equal(failure.error.includes("call_00_secret"), false);
  assert.equal(failure.error.length <= 240, true);
});

test("hides coalescing diagnostics from display details without mutating events", () => {
  const event = {
    type: "agent.text_delta",
    sequence: 1,
    block_id: "text",
    detail: { text: "内容", coalesced: 9, status: "streaming" },
  };
  const state = timeline.createTimelineState();
  timeline.appendAgentEvent(state, event);

  assert.deepEqual(state.entries[0].detail, null);
  assert.equal(event.detail.coalesced, 9);
  assert.equal(event.detail.text, "内容");
  assert.deepEqual(timeline.sanitizeDisplayDetail({ coalesced: 3 }), {});
});

test("keeps facts and caps the visible timeline", () => {
  const events = Array.from({ length: 130 }, (_, index) => ({
    type: "agent.text_completed",
    sequence: index + 1,
    phase: "schema",
    block_id: "block-" + index,
    elapsed_seconds: index,
    detail: { text: "段落 " + index },
  }));
  const entries = timeline.reduceAgentEvents(events);
  assert.equal(entries.length, 120);
  assert.equal(entries[0].text, "段落 10");
});

test("appends deltas incrementally and deduplicates sequences in O(1)", () => {
  const timelineState = timeline.createTimelineState();
  const tracker = timeline.createSequenceTracker();
  const first = { type: "agent.text_started", sequence: 1, block_id: "text" };
  const delta = { type: "agent.text_delta", sequence: 2, block_id: "text", detail: { text: "流式" } };
  const end = { type: "agent.text_completed", sequence: 3, block_id: "text" };

  assert.equal(tracker.accept(first), true);
  assert.equal(tracker.accept(delta), true);
  assert.equal(tracker.accept(delta), false);
  timeline.appendAgentEvent(timelineState, first);
  timeline.appendAgentEvent(timelineState, delta);
  timeline.appendAgentEvent(timelineState, end);

  assert.equal(tracker.highest(), 2);
  assert.equal(timelineState.entries.length, 1);
  assert.equal(timelineState.entries[0].text, "流式");
  assert.equal(timelineState.entries[0].complete, true);
});

test("tracks the latest streaming block and releases it after completion", () => {
  const state = timeline.createTimelineState();
  const first = { type: "agent.text_delta", sequence: 1, block_id: "text-1", detail: { text: "第一段" } };
  const second = { type: "agent.thinking_delta", sequence: 2, block_id: "think-1", detail: { text: "思考" } };
  const complete = { type: "agent.thinking_completed", sequence: 3, block_id: "think-1" };

  timeline.appendAgentEvent(state, first);
  assert.equal(state.latestStreamingKey, "text:text-1");
  assert.equal(timeline.isLatestStreamingEntry(state, state.entries[0]), true);

  const change = timeline.appendAgentEvent(state, second);
  assert.equal(state.latestStreamingKey, "thinking:think-1");
  assert.equal(change.previousLatestStreamingKey, "text:text-1");
  assert.equal(timeline.isLatestStreamingEntry(state, state.entries[0]), false);
  assert.equal(timeline.isLatestStreamingEntry(state, state.entries[1]), true);

  timeline.appendAgentEvent(state, complete);
  assert.equal(timeline.isLatestStreamingEntry(state, state.entries[1]), false);
  assert.equal(state.entries[1].text, "思考");
});

test("opens active conversation blocks while keeping phase separators collapsed", () => {
  const state = timeline.createTimelineState();
  const phase = timeline.appendAgentEvent(state, {
    type: "cli_parser.phase.started",
    phase: "ttp",
    sequence: 1,
  }).entry;
  const text = timeline.appendAgentEvent(state, {
    type: "agent.text_completed",
    sequence: 2,
    block_id: "text",
    detail: { text: "完成输出" },
  }).entry;
  const thinking = timeline.appendAgentEvent(state, {
    type: "agent.thinking_delta",
    sequence: 3,
    block_id: "thinking",
    detail: { text: "持续更新" },
  }).entry;

  assert.equal(timeline.shouldOpenEntry(state, phase), false);
  assert.equal(timeline.shouldOpenEntry(state, text), true);
  thinking.manualOpen = false;
  assert.equal(timeline.shouldOpenEntry(state, thinking), true);
  thinking.complete = true;
  assert.equal(timeline.shouldOpenEntry(state, thinking), false);
});

test("keeps retry and failure events as semantic status paragraphs", () => {
  const entries = timeline.reduceAgentEvents([
    { type: "cli_parser.no_tool.retry", phase: "schema", detail: { reason: "no tool", retry_index: 1 } },
    { type: "agent.model_call_completed", phase: "schema", detail: { status: "timeout", error: "request timed out" } },
    { type: "run.finished", status: "failed", detail: { reason: "generation_timeout" } },
  ]);

  assert.deepEqual(entries.map((entry) => entry.kind), ["retry", "status", "terminal"]);
  assert.equal(entries[0].title, "重试");
  assert.equal(entries[1].title, "模型请求失败");
  assert.equal(entries[2].title, "运行失败");
});

test("does not expose terminal success diagnostics", () => {
  const entry = timeline.reduceAgentEvents([
    { type: "run.finished", status: "success", detail: { reason: "success", internal: "hidden" } },
  ])[0];
  assert.equal(entry.status, "成功");
  assert.equal(entry.detail, null);
});

test("keeps schema, TTP, and final acceptance phase entries", () => {
  const state = timeline.buildTimeline([
    { type: "cli_parser.phase.started", phase: "schema", sequence: 1 },
    { type: "agent.text_completed", phase: "schema", block_id: "schema-text", sequence: 2, detail: { text: "Schema" } },
    { type: "cli_parser.phase.started", phase: "ttp", sequence: 3 },
    { type: "agent.text_completed", phase: "ttp", block_id: "ttp-text", sequence: 4, detail: { text: "TTP" } },
    { type: "cli_parser.final_validation.started", sequence: 5 },
    { type: "cli_parser.final_validation.completed", sequence: 6 },
  ]);

  assert.deepEqual(timeline.phaseEntries(state).map((entry) => entry.phase), ["schema", "ttp", "acceptance"]);
  assert.deepEqual(timeline.entriesForPhase(state, "schema").map((entry) => entry.kind), ["phase", "text"]);
  assert.deepEqual(timeline.entriesForPhase(state, "ttp").map((entry) => entry.kind), ["phase", "text"]);
  assert.equal(timeline.entriesForPhase(state, "acceptance")[0].title, "最终验收");
});

test("extracts only TTP parsed result text for display", () => {
  assert.equal(timeline.rawTtpResultText("<parsed_record input_index=\"0\">{\"hostname\":\"r1\"}</parsed_record>"), "<parsed_record input_index=\"0\">{\"hostname\":\"r1\"}</parsed_record>");
  assert.equal(timeline.rawTtpResultText(JSON.stringify({ accepted: true, issues: [], records: [{ hostname: "r1" }] })), '[\n  {\n    "hostname": "r1"\n  }\n]');
  assert.equal(timeline.rawTtpResultText(JSON.stringify({ accepted: true, issues: [] })), "");
});

test("coalesces render scheduling to one frame", () => {
  let callbacks = [];
  let renders = 0;
  const scheduler = timeline.createRenderScheduler(
    () => { renders += 1; },
    (callback) => { callbacks.push(callback); return callbacks.length; },
    () => {},
  );

  scheduler.schedule();
  scheduler.schedule();
  assert.equal(callbacks.length, 1);
  callbacks[0]();
  assert.equal(renders, 1);
  scheduler.schedule();
  callbacks[1]();
  assert.equal(renders, 2);
});
