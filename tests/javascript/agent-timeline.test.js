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
    { type: "agent.tool_call_delta", tool_call_id: "call", detail: { text: "{\"x\":1}" }, phase: "ttp", elapsed_seconds: 8 },
    { type: "agent.tool_call_completed", tool_call_id: "call", phase: "ttp", elapsed_seconds: 9 },
    { type: "agent.tool_result_started", tool_call_id: "call", detail: { tool_name: "submit_ttp_template" }, phase: "ttp", elapsed_seconds: 10 },
    { type: "agent.tool_result_delta", tool_call_id: "call", detail: { text: "{\"accepted\":true}" }, phase: "ttp", elapsed_seconds: 11 },
    { type: "agent.tool_result_completed", tool_call_id: "call", phase: "ttp", elapsed_seconds: 12 },
  ]);

  assert.deepEqual(entries.map((entry) => entry.kind), ["thinking", "text", "tool_call", "tool_result"]);
  assert.equal(entries[0].text, "先检查");
  assert.equal(entries[1].text, "提交");
  assert.equal(entries[2].text, '{"x":1}');
  assert.equal(entries[3].text, '{"accepted":true}');
  assert.equal(entries.every((entry) => entry.complete), true);
});

test("keeps facts and caps the visible timeline", () => {
  const events = Array.from({ length: 130 }, (_, index) => ({
    type: "cli_parser.phase.started",
    sequence: index + 1,
    phase: "schema",
    elapsed_seconds: index,
    detail: { status: "ok" },
  }));
  const entries = timeline.reduceAgentEvents(events);
  assert.equal(entries.length, 120);
  assert.equal(entries[0].detail.status, "ok");
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
