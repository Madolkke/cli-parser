"use strict";

const $ = (id) => document.getElementById(id);
const model = window.SchemaModel;
const timelineModule = window.AgentTimeline;
const ui = window.UI;
const highlighter = window.Highlight;
const MAX_INPUT_BYTES = 1024 * 1024;
const state = {
  runId: null, stream: null, activeTab: "template", outputs: [{ text: "", scrollTop: 0 }],
  activeOutput: 0, schemaDraft: null, savedSchema: null, schemaDirty: false,
  schemaMode: "visual", schemaErrors: [], schemaExpanded: Object.create(null),
  drawerOpen: false, events: [], followProgress: true,
  timelinePhaseFilter: "all", timelineUnread: { schema: 0, ttp: 0, acceptance: 0 },
  timeline: timelineModule.createTimelineState(),
  eventTracker: timelineModule.createSequenceTracker(),
  timelineNodes: new Map(), timelineDirty: new Set(), timelineRemoved: new Set(),
  timelineScheduler: null, rerunAvailable: false,
  runtimeBaseline: null, runtimeReady: false, runtimeError: null,
  runs: [], historyFilter: "all", historyQuery: "",
  recordsText: "", inputsText: "",
};

/* ------------------------------------------------------------- routing */

function currentRoute() {
  const hash = location.hash.replace(/^#/, "");
  const match = /^\/runs\/([^/]+)$/.exec(hash);
  if (match) return { view: "run", runId: decodeURIComponent(match[1]) };
  return { view: "new", runId: null };
}

let suppressRoute = false;

async function handleRoute() {
  if (suppressRoute) { suppressRoute = false; return; }
  const route = currentRoute();
  if (route.view === "run" && route.runId === state.runId) return;
  if (route.view === "new" && state.runId === null) return;
  if (!await confirmDiscard()) {
    suppressRoute = true;
    location.hash = state.runId ? "#/runs/" + encodeURIComponent(state.runId) : "#/new";
    return;
  }
  if (route.view === "run") openRun(route.runId);
  else showNew();
}

function navigateToNew() {
  const route = currentRoute();
  if (route.view === "new") showNew();
  else location.hash = "#/new";
}

function navigateToRun(runId) {
  const route = currentRoute();
  if (route.view === "run" && route.runId === runId) return;
  location.hash = "#/runs/" + encodeURIComponent(runId);
}

window.addEventListener("hashchange", handleRoute);

/* ------------------------------------------------------ runtime overrides */

const RUNTIME_FIELDS = [
  { group: "settings", name: "model_name", label: "模型名", type: "text" },
  { group: "settings", name: "base_url", label: "API 地址", type: "text" },
  { group: "settings", name: "api_key", label: "API Key", type: "password", secret: true },
  { group: "settings", name: "verify_tls", label: "校验 TLS 证书", type: "boolean" },
  { group: "settings", name: "stream", label: "模型流式请求", type: "boolean" },
  { group: "settings", name: "temperature", label: "Temperature", type: "number", step: "0.1", min: "0", max: "2" },
  { group: "settings", name: "thinking_enable", label: "启用 Thinking", type: "tri-boolean" },
  { group: "settings", name: "reasoning_effort", label: "推理强度", type: "reasoning" },
  { group: "settings", name: "max_tokens", label: "最大输出 Token", type: "number", min: "1" },
  { group: "settings", name: "context_size", label: "上下文长度", type: "number", min: "1" },
  { group: "settings", name: "model_max_retries", label: "模型重试次数", type: "number", min: "0" },
  { group: "settings", name: "model_timeout_seconds", label: "单次模型超时（秒）", type: "number", min: "0.1", step: "0.1" },
  { group: "policy", name: "total_timeout_seconds", label: "总运行超时（秒）", type: "number", min: "0.1", step: "0.1" },
  { group: "policy", name: "max_agent_rounds", label: "最大 Agent 轮次", type: "number", min: "1" },
  { group: "policy", name: "max_ttp_submissions", label: "最大 TTP 提交次数", type: "number", min: "1" },
  { group: "policy", name: "max_schema_no_tool_retries", label: "Schema 无工具重试", type: "number", min: "0" },
  { group: "policy", name: "max_ttp_no_tool_retries", label: "TTP 无工具重试", type: "number", min: "0" },
  { group: "policy", name: "ttp_validation_timeout_seconds", label: "TTP 解析超时（秒）", type: "number", min: "0.1", step: "0.1" },
  { group: "policy", name: "model_input_char_budget", label: "模型输入字符预算", type: "number", min: "1", max: "240000" },
  { group: "policy", name: "max_ttp_template_bytes", label: "模板大小上限（字节）", type: "number", min: "1", max: "65536" },
  { group: "policy", name: "max_ttp_group_depth", label: "TTP Group 深度", type: "number", min: "1", max: "16" },
  { group: "policy", name: "max_ttp_regex_chars", label: "正则字符上限", type: "number", min: "1", max: "2048" },
  { group: "policy", name: "max_ttp_argument_chars", label: "参数字符上限", type: "number", min: "1", max: "4096" },
  { group: "policy", name: "max_parse_result_bytes", label: "解析结果上限（字节）", type: "number", min: "1", max: "8388608" },
  { group: "policy", name: "max_schema_bytes", label: "Schema 大小上限（字节）", type: "number", min: "1", max: "65536" },
  { group: "policy", name: "max_schema_depth", label: "Schema 深度上限", type: "number", min: "1", max: "16" },
  { group: "policy", name: "max_schema_properties", label: "Schema 字段上限", type: "number", min: "1", max: "256" },
];

function runtimeBaselineValue(field) {
  const group = state.runtimeBaseline && state.runtimeBaseline[field.group];
  return group ? group[field.name] : undefined;
}

function runtimeControl(field) {
  let input;
  if (field.type === "boolean") {
    input = document.createElement("input"); input.type = "checkbox";
    input.checked = Boolean(runtimeBaselineValue(field));
  } else if (field.type === "tri-boolean") {
    input = document.createElement("select");
    input.append(new Option("继承服务默认", ""), new Option("启用", "true"), new Option("关闭", "false"));
    const value = runtimeBaselineValue(field);
    input.value = value === null || value === undefined ? "" : String(value);
  } else if (field.type === "reasoning") {
    input = document.createElement("select");
    input.append(new Option("继承服务默认", ""));
    ["none", "minimal", "low", "medium", "high", "xhigh"].forEach((value) => input.append(new Option(value, value)));
    input.value = runtimeBaselineValue(field) || "";
  } else {
    input = document.createElement("input"); input.type = field.type;
    if (field.type === "number") { input.min = field.min || ""; input.max = field.max || ""; input.step = field.step || "1"; }
    const value = field.secret ? "" : runtimeBaselineValue(field);
    input.value = value === null || value === undefined ? "" : String(value);
    if (field.secret) input.placeholder = state.runtimeBaseline?.settings?.api_key_configured ? "已配置，留空表示继承" : "留空表示继承服务默认";
  }
  input.dataset.runtimeGroup = field.group;
  input.dataset.runtimeName = field.name;
  input.dataset.runtimeType = field.type;
  input.autocomplete = field.secret ? "new-password" : "off";
  return input;
}

function renderRuntimeEditors() {
  for (const hostId of ["new-runtime-editor", "rerun-runtime-editor"]) {
    const host = $(hostId); if (!host) continue;
    host.replaceChildren();
    if (!state.runtimeReady) { host.textContent = state.runtimeError || "正在加载服务默认配置…"; continue; }
    for (const group of ["settings", "policy"]) {
      const section = document.createElement("section"); section.className = "runtime-section";
      const heading = document.createElement("h4"); heading.textContent = group === "settings" ? "模型请求" : "生成预算"; section.append(heading);
      const grid = document.createElement("div"); grid.className = "runtime-grid";
      RUNTIME_FIELDS.filter((field) => field.group === group).forEach((field) => {
        const label = document.createElement("label"); label.className = "runtime-field";
        const caption = document.createElement("span"); caption.textContent = field.label; label.append(caption, runtimeControl(field));
        grid.append(label);
      });
      section.append(grid); host.append(section);
    }
    const note = document.createElement("p"); note.className = "hint runtime-note";
    note.textContent = "extra_body 继续使用服务环境配置；parallel_tool_calls 固定为 false。空白字段表示沿用服务默认。";
    host.append(note);
    const error = document.createElement("p"); error.className = "error runtime-error"; error.hidden = true;
    host.append(error);
    host.querySelectorAll("[data-runtime-name]").forEach((input) => {
      input.addEventListener("input", () => validateRuntimeEditor(host));
      input.addEventListener("change", () => validateRuntimeEditor(host));
    });
    const reset = document.createElement("button"); reset.type = "button"; reset.className = "btn btn-ghost btn-sm runtime-reset"; reset.textContent = "恢复服务默认";
    reset.onclick = () => renderRuntimeEditors(); host.append(reset);
  }
}

function validateRuntimeEditor(host) {
  const invalid = [...host.querySelectorAll("[data-runtime-name]")].find((input) => !input.checkValidity());
  const error = host.querySelector(".runtime-error");
  if (!error) return !invalid;
  error.hidden = !invalid;
  error.textContent = invalid ? "请修正运行参数：" + (invalid.validationMessage || invalid.dataset.runtimeName) : "";
  return !invalid;
}

function collectRuntimeParameters(hostId) {
  if (!state.runtimeReady) return null;
  const parameters = { settings: {}, policy: {} };
  $(hostId).querySelectorAll("[data-runtime-name]").forEach((input) => {
    const group = input.dataset.runtimeGroup; const name = input.dataset.runtimeName; const type = input.dataset.runtimeType;
    let value = input.type === "checkbox" ? input.checked : input.value;
    if (type === "password") { if (!String(value).trim()) return; value = String(value).trim(); }
    else if (value === "") return;
    else if (type === "number") value = input.step && input.step.includes(".") ? Number.parseFloat(value) : Number.parseInt(value, 10);
    else if (type === "tri-boolean") value = value === "true";
    const baseline = runtimeBaselineValue({ group, name });
    if (type !== "password" && String(value) === String(baseline)) return;
    if (type === "boolean" && Boolean(value) === Boolean(baseline)) return;
    if (type === "tri-boolean" && value === baseline) return;
    parameters[group][name] = value;
  });
  if (!Object.keys(parameters.settings).length) delete parameters.settings;
  if (!Object.keys(parameters.policy).length) delete parameters.policy;
  return Object.keys(parameters).length ? parameters : null;
}

async function loadRuntimeConfig() {
  try { state.runtimeBaseline = await api("/api/runtime-config"); state.runtimeReady = true; }
  catch (error) { state.runtimeError = "运行参数加载失败：" + error.message; }
  renderRuntimeEditors();
}

function renderRuntimeSummary(config, configError) {
  const panel = $("run-runtime-panel");
  panel.hidden = !config && !configError;
  if (!config) {
    $("run-runtime-view").textContent = configError || "没有保存运行配置快照（旧运行记录）";
    $("run-runtime-summary").textContent = configError || "旧运行记录没有运行配置快照";
    return;
  }
  const host = $("run-runtime-view"); host.replaceChildren();
  const settings = config.settings || {}; const policy = config.policy || {};
  const text = document.createElement("p"); text.className = "runtime-summary-line";
  text.textContent = "模型：" + (settings.model_name || "未配置") + " · API Key：" + (settings.api_key_configured ? "已配置" : "未配置") + " · 指纹：" + (config.configuration_fingerprint || "-"); host.append(text);
  const details = document.createElement("p"); details.className = "runtime-summary-line";
  details.textContent = "总超时 " + (policy.total_timeout_seconds ?? "-") + "s · Agent " + (policy.max_agent_rounds ?? "-") + " 轮 · TTP 提交 " + (policy.max_ttp_submissions ?? "-") + " 次"; host.append(details);
  $("run-runtime-summary").textContent = config.source === "env_baseline" ? "本次实际使用服务默认配置" : "本次实际使用服务默认配置 + 运行覆盖";
}

/* ---------------------------------------------------------------- shared */

async function api(path, options) {
  const response = await fetch(path, { headers: { "Content-Type": "application/json" }, ...options });
  let body = null;
  try { body = await response.json(); } catch { body = null; }
  if (!response.ok) {
    const detail = body && body.detail ? body.detail : "HTTP " + response.status;
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return body;
}

function setHidden(id, hidden) { $(id).hidden = hidden; }
function formatBytes(bytes) {
  if (bytes < 1024) return bytes + " B";
  return (bytes / 1024).toFixed(bytes < 10240 ? 1 : 0) + " KiB";
}

/* ---------------------------------------------------- new run inputs */

function persistActiveOutput() {
  const current = state.outputs[state.activeOutput];
  if (!current) return;
  current.text = $("output-editor").value;
  current.scrollTop = $("output-editor").scrollTop;
}

function selectOutput(index) {
  persistActiveOutput();
  state.activeOutput = Math.max(0, Math.min(index, state.outputs.length - 1));
  const current = state.outputs[state.activeOutput];
  $("output-editor").value = current.text;
  requestAnimationFrame(() => { $("output-editor").scrollTop = current.scrollTop; });
  renderOutputTabs();
  validateOutputs();
}

function renderOutputTabs() {
  const host = $("output-tabs");
  host.replaceChildren();
  state.outputs.forEach((output, index) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "input-tab" + (index === state.activeOutput ? " is-active" : "");
    button.setAttribute("role", "tab");
    button.setAttribute("aria-selected", String(index === state.activeOutput));
    const bytes = model.utf8Bytes(output.text);
    const label = document.createElement("strong"); label.textContent = "输入 " + (index + 1);
    const size = document.createElement("small"); size.textContent = formatBytes(bytes);
    button.append(label, size);
    button.onclick = () => selectOutput(index);
    host.append(button);
  });
  $("output-count").textContent = state.outputs.length + " / 5";
  $("add-output").disabled = state.outputs.length >= 5;
  $("remove-output").hidden = state.outputs.length === 1;
}

function validateOutputs() {
  persistActiveOutput();
  const invalid = model.validateInputs(state.outputs.map((item) => item.text), MAX_INPUT_BYTES);
  const current = state.outputs[state.activeOutput];
  const bytes = model.utf8Bytes(current.text);
  $("output-size").textContent = current.text.length + " 字符 · " + formatBytes(bytes);
  $("output-editor").classList.toggle("is-invalid", !current.text.trim() || bytes > MAX_INPUT_BYTES);
  $("output-error").hidden = invalid.length === 0;
  $("output-error").textContent = invalid.map((item) => item.message).join("；");
  $("start").disabled = invalid.length > 0;
  renderOutputTabs();
  return invalid.length === 0;
}

$("output-editor").addEventListener("input", validateOutputs);
$("output-editor").addEventListener("scroll", persistActiveOutput);
$("add-output").onclick = () => {
  if (state.outputs.length >= 5) return;
  persistActiveOutput();
  state.outputs.push({ text: "", scrollTop: 0 });
  selectOutput(state.outputs.length - 1);
  $("output-editor").focus();
};
$("remove-output").onclick = async () => {
  if (state.outputs.length === 1) return;
  if (!await ui.confirmDialog({
    title: "删除当前输入",
    body: "将删除输入 " + (state.activeOutput + 1) + " 的全部内容。",
    confirmLabel: "删除",
    danger: true,
  })) return;
  state.outputs.splice(state.activeOutput, 1);
  state.activeOutput = Math.min(state.activeOutput, state.outputs.length - 1);
  $("output-editor").value = state.outputs[state.activeOutput].text;
  renderOutputTabs();
  validateOutputs();
};

/* ---------------------------------------------------------- history list */

const statusLabel = (status) => ({ running: "运行中", success: "成功", failed: "失败", cancelled: "已取消" }[status] || status);
function formatTime(value) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "" : date.toLocaleString(undefined, { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
}

function historySkeleton() {
  const fragment = document.createDocumentFragment();
  for (let index = 0; index < 5; index += 1) {
    const item = document.createElement("li");
    item.className = "h-skeleton";
    const line = document.createElement("div"); line.className = "sk-line";
    const short = document.createElement("div"); short.className = "sk-line short";
    item.append(line, short);
    fragment.append(item);
  }
  return fragment;
}

function renderHistory() {
  const list = $("history");
  const note = $("history-state");
  list.replaceChildren();
  const query = state.historyQuery.trim().toLowerCase();
  const runs = state.runs.filter((run) =>
    (state.historyFilter === "all" || run.status === state.historyFilter)
    && (!query
      || String(run.title || "").toLowerCase().includes(query)
      || String(run.run_id).toLowerCase().includes(query)));
  note.hidden = runs.length > 0;
  note.textContent = state.runs.length ? "没有匹配的运行记录" : "暂无运行记录";
  for (const run of runs) {
    const item = document.createElement("li");
    if (run.run_id === state.runId) item.classList.add("is-active");
    const button = document.createElement("button");
    button.type = "button";
    const title = document.createElement("span"); title.className = "h-title";
    title.textContent = run.title || run.run_id;
    const meta = document.createElement("span"); meta.className = "h-meta";
    const badge = document.createElement("span"); badge.className = "badge " + run.status;
    badge.textContent = statusLabel(run.status);
    const time = document.createElement("span"); time.className = "h-time";
    time.textContent = ui.relativeTime(run.created_at);
    time.title = formatTime(run.created_at);
    meta.append(badge, time);
    button.append(title, meta);
    button.onclick = () => navigateToRun(run.run_id);
    item.append(button);
    list.append(item);
  }
}

async function loadHistory() {
  const note = $("history-state");
  const list = $("history");
  list.replaceChildren(historySkeleton());
  note.hidden = true;
  try {
    const data = await api("/api/runs");
    state.runs = data.runs || [];
    renderHistory();
  } catch (error) {
    list.replaceChildren();
    note.hidden = false;
    note.textContent = "历史记录加载失败：" + error.message;
  }
}

$("history-search").addEventListener("input", (event) => {
  state.historyQuery = event.target.value;
  renderHistory();
});
$("history-filters").addEventListener("click", (event) => {
  const chip = event.target.closest("[data-filter]");
  if (!chip) return;
  state.historyFilter = chip.dataset.filter;
  $("history-filters").querySelectorAll(".filter-chip").forEach((item) => {
    item.classList.toggle("is-active", item === chip);
  });
  renderHistory();
});

/* --------------------------------------------------------- view control */

async function confirmDiscard() {
  if (!state.schemaDirty) return true;
  return await ui.confirmDialog({
    title: "放弃未保存的修改？",
    body: "Schema 有未保存的修改，离开当前视图后将丢失这些修改。",
    confirmLabel: "放弃修改",
    danger: true,
  });
}
function closeDrawer() {
  state.drawerOpen = false;
  $("sidebar").classList.remove("is-open");
  $("drawer-overlay").hidden = true;
  document.body.classList.remove("drawer-open");
}
function openDrawer() {
  state.drawerOpen = true;
  $("sidebar").classList.add("is-open");
  $("drawer-overlay").hidden = false;
  document.body.classList.add("drawer-open");
}
function showNew() {
  closeStream();
  stopElapsedTicker();
  setStreamState(null);
  state.runId = null;
  state.schemaDirty = false; state.schemaErrors = []; state.schemaDraft = null; state.savedSchema = null;
  state.rerunAvailable = false;
  setHidden("view-new", false);
  setHidden("view-run", true);
  closeDrawer();
  loadHistory();
}
async function openRun(runId) {
  closeStream();
  stopElapsedTicker();
  setStreamState(null);
  state.runId = runId;
  state.schemaDirty = false; state.schemaErrors = []; state.schemaDraft = null; state.savedSchema = null;
  state.rerunAvailable = false;
  setHidden("view-new", true);
  setHidden("view-run", false);
  $("run-loading").hidden = false;
  $("run-body").hidden = true;
  closeDrawer();
  try {
    const data = await refreshRun();
    await loadHistory();
    if (data.meta.status === "running") openStream(runId);
  } catch (error) {
    ui.toast("无法加载运行：" + error.message, "error");
    navigateToNew();
  } finally {
    $("run-loading").hidden = true;
    $("run-body").hidden = false;
  }
}

/* ---------------------------------------------------------- schema editor */

function initialiseSchema(schema) {
  state.schemaDraft = model.normalise(schema);
  state.savedSchema = model.clone(state.schemaDraft);
  state.schemaDirty = false;
  state.schemaErrors = [];
  state.schemaExpanded = Object.create(null);
  state.schemaMode = "visual";
  $("schema-editor").value = JSON.stringify(state.schemaDraft, null, 2);
  setSchemaMode("visual", true);
  renderSchema();
  updateSchemaState();
}
function markSchemaDirty() {
  state.schemaDirty = JSON.stringify(state.schemaDraft) !== JSON.stringify(state.savedSchema);
  state.schemaErrors = model.validate(state.schemaDraft);
  $("schema-editor").value = JSON.stringify(state.schemaDraft, null, 2);
  clearSchemaMessage();
  updateSchemaState();
  renderSchemaErrors();
}
function updateSchemaState() {
  const badge = $("schema-state");
  badge.textContent = state.schemaDirty ? "未保存" : "已保存";
  badge.className = "save-state " + (state.schemaDirty ? "dirty" : "saved");
  $("save-schema").disabled = !state.schemaDirty || state.schemaErrors.length > 0;
  updateRerunAction();
}
function updateRerunAction() {
  const button = $("rerun-schema");
  button.hidden = !state.rerunAvailable;
  button.disabled = !state.rerunAvailable || state.schemaDirty || state.schemaErrors.length > 0;
  button.title = state.schemaDirty || state.schemaErrors.length
    ? "请先保存没有错误的 Schema"
    : "以当前已保存的 Schema 创建独立的 TTP 生成任务";
}
function clearSchemaMessage() { $("schema-message").hidden = true; }
function showSchemaMessage(text, kind) {
  const message = $("schema-message");
  message.textContent = text;
  message.className = "message " + (kind || "error");
  message.hidden = false;
}
function renderSchemaErrors() {
  const host = $("schema-errors");
  host.replaceChildren();
  host.hidden = state.schemaErrors.length === 0;
  for (const error of state.schemaErrors) {
    const row = document.createElement("p");
    const path = document.createElement("code");
    path.textContent = error.path || "/";
    row.append(path, document.createTextNode(" " + error.message));
    host.append(row);
  }
}
function setSchemaMode(mode, force) {
  if (mode === "visual" && state.schemaMode === "json" && !force) {
    try {
      state.schemaDraft = model.normalise(JSON.parse($("schema-editor").value));
      state.schemaErrors = model.validate(state.schemaDraft);
    } catch (error) {
      showSchemaMessage("JSON 无法转换为可视化结构：" + error.message, "error");
      return;
    }
  }
  if (mode === "json") $("schema-editor").value = JSON.stringify(state.schemaDraft, null, 2);
  state.schemaMode = mode;
  setHidden("schema-visual", mode !== "visual");
  setHidden("schema-json", mode !== "json");
  $("schema-mode-visual").classList.toggle("is-active", mode === "visual");
  $("schema-mode-json").classList.toggle("is-active", mode === "json");
  $("schema-mode-visual").setAttribute("aria-selected", String(mode === "visual"));
  $("schema-mode-json").setAttribute("aria-selected", String(mode === "json"));
  $("add-root-field").hidden = mode !== "visual";
  if (mode === "visual") renderSchema();
}

function control(label, input) {
  const wrap = document.createElement("label");
  wrap.className = "schema-control";
  const caption = document.createElement("span");
  caption.textContent = label;
  wrap.append(caption, input);
  return wrap;
}
function textInput(value, placeholder) {
  const input = document.createElement("input");
  input.type = "text";
  input.value = value == null ? "" : value;
  input.placeholder = placeholder || "";
  return input;
}
function numberInput(value) {
  const input = document.createElement("input");
  input.type = "number";
  input.value = value == null ? "" : value;
  input.step = "any";
  return input;
}
function assignOptional(node, key, raw, numeric) {
  if (raw === "") delete node[key];
  else node[key] = numeric ? Number(raw) : raw;
  markSchemaDirty();
}

function renderSchema() {
  const host = $("schema-visual");
  host.replaceChildren();
  if (!state.schemaDraft) return;
  const rootMeta = document.createElement("div");
  rootMeta.className = "schema-root-meta";
  const title = textInput(state.schemaDraft.title, "Schema 标题");
  title.oninput = () => assignOptional(state.schemaDraft, "title", title.value, false);
  const description = textInput(state.schemaDraft.description, "Schema 描述");
  description.oninput = () => assignOptional(state.schemaDraft, "description", description.value, false);
  rootMeta.append(control("根节点", Object.assign(document.createElement("span"), { className: "type-chip", textContent: "object" })), control("标题", title), control("描述", description));
  host.append(rootMeta);
  renderObjectFields(state.schemaDraft, host, 0, "");
  renderSchemaErrors();
}

function escapePointer(value) { return String(value).replaceAll("~", "~0").replaceAll("/", "~1"); }
function fieldPointer(parentPath, name) { return parentPath + "/properties/" + escapePointer(name); }
function migrateSchemaExpansion(oldPath, newPath) {
  for (const key of Object.keys(state.schemaExpanded)) {
    if (key !== oldPath && !key.startsWith(oldPath + "/")) continue;
    const next = newPath + key.slice(oldPath.length);
    state.schemaExpanded[next] = state.schemaExpanded[key];
    delete state.schemaExpanded[key];
  }
}
function forgetSchemaExpansion(path) {
  for (const key of Object.keys(state.schemaExpanded)) {
    if (key === path || key.startsWith(path + "/")) delete state.schemaExpanded[key];
  }
}
function renderObjectFields(node, host, depth, path) {
  const properties = node.properties || {};
  if (!Object.keys(properties).length) {
    const empty = document.createElement("p");
    empty.className = "schema-empty";
    empty.textContent = "暂无字段";
    host.append(empty);
  }
  for (const [name, child] of Object.entries(properties)) {
    host.append(renderField(node, name, child, depth, fieldPointer(path, name)));
  }
}
function addField(parent) {
  let index = Object.keys(parent.properties || {}).length + 1;
  let name = "field_" + index;
  while (Object.prototype.hasOwnProperty.call(parent.properties, name)) name = "field_" + (++index);
  parent.properties[name] = model.createNode("string");
  markSchemaDirty();
  renderSchema();
}
function renameField(parent, oldName, newName, oldPath) {
  const renamed = model.renameProperty(parent.properties || {}, oldName, newName);
  if (!renamed.ok) return renamed.reason;
  const newPath = oldPath.slice(0, oldPath.lastIndexOf("/")) + "/" + escapePointer(newName);
  parent.properties = renamed.properties;
  if (parent.required) parent.required = parent.required.map((item) => item === oldName ? newName : item);
  migrateSchemaExpansion(oldPath, newPath);
  markSchemaDirty();
  return null;
}
function renderField(parent, name, node, depth, path) {
  const card = document.createElement("div");
  card.className = "schema-field";
  const header = document.createElement("div");
  header.className = "schema-field-head";
  const nameInput = textInput(name, "field_name");
  nameInput.className = "field-name-input";
  nameInput.onchange = () => {
    const nextName = nameInput.value.trim();
    const renameError = renameField(parent, name, nextName, path);
    if (renameError) {
      nameInput.value = name;
      if (renameError === "duplicate") ui.toast("同级字段名不能重复。", "error");
    }
    renderSchema();
  };
  const type = document.createElement("select");
  for (const value of model.TYPES) { const option = document.createElement("option"); option.value = value; option.textContent = value; option.selected = value === node.type; type.append(option); }
  type.onchange = async () => {
    const destructive = node.type === "object" || node.type === "array" || Object.keys(node).some((key) => !["type", "title", "description", "enum"].includes(key));
    if (destructive && !await ui.confirmDialog({
      title: "更改字段类型",
      body: "更改类型会移除不兼容的子字段或约束。",
      confirmLabel: "继续",
      danger: true,
    })) { type.value = node.type; return; }
    parent.properties[name] = model.changeType(node, type.value);
    markSchemaDirty(); renderSchema();
  };
  const requiredLabel = document.createElement("label");
  requiredLabel.className = "required-toggle";
  const required = document.createElement("input"); required.type = "checkbox"; required.checked = (parent.required || []).includes(name);
  required.onchange = () => {
    const values = new Set(parent.required || []);
    if (required.checked) values.add(name); else values.delete(name);
    if (values.size) parent.required = [...values]; else delete parent.required;
    markSchemaDirty();
  };
  requiredLabel.append(required, document.createTextNode("必填"));
  const remove = document.createElement("button"); remove.type = "button"; remove.className = "icon-btn subtle-danger"; remove.title = "删除字段"; remove.setAttribute("aria-label", "删除字段 " + name);
  remove.append(ui.svgIcon("trash", 14));
  remove.onclick = async () => {
    if (!await ui.confirmDialog({
      title: "删除字段",
      body: "将删除字段 " + name + " 及其全部子字段和约束。",
      confirmLabel: "删除",
      danger: true,
    })) return;
    delete parent.properties[name];
    forgetSchemaExpansion(path);
    if (parent.required) {
      parent.required = parent.required.filter((item) => item !== name);
      if (!parent.required.length) delete parent.required;
    }
    markSchemaDirty(); renderSchema();
  };
  const collapsible = node.type === "object" || node.type === "array";
  let toggle = null;
  let expanded = true;
  if (collapsible) {
    expanded = state.schemaExpanded[path] === true;
    toggle = document.createElement("button");
    toggle.type = "button";
    toggle.className = "schema-toggle";
    toggle.append(ui.svgIcon(expanded ? "chevronDown" : "chevron", 14));
    toggle.title = expanded ? "折叠节点" : "展开节点";
    toggle.setAttribute("aria-label", (expanded ? "折叠" : "展开") + "字段 " + name);
    toggle.setAttribute("aria-expanded", String(expanded));
    toggle.onclick = () => {
      state.schemaExpanded[path] = !expanded;
      renderSchema();
    };
  }
  header.append(toggle || document.createElement("span"), nameInput, type, requiredLabel, remove);
  card.append(header);
  const body = document.createElement("div");
  body.className = "schema-node-body";
  body.hidden = collapsible && !expanded;
  const options = renderNodeOptions(node);
  if (options.childElementCount) body.append(options);
  if (node.type === "object") {
    const children = document.createElement("div"); children.className = "schema-children";
    renderObjectFields(node, children, depth + 1, path);
    const add = document.createElement("button"); add.type = "button"; add.className = "btn btn-ghost btn-sm add-child"; add.textContent = "＋ 添加子字段"; add.onclick = () => addField(node);
    children.append(add);
    body.append(children);
  } else if (node.type === "array") {
    const itemWrap = document.createElement("div"); itemWrap.className = "schema-array-item";
    const label = document.createElement("span"); label.className = "array-label"; label.textContent = "数组元素";
    const fakeParent = { properties: { items: node.items } };
    const item = renderField(fakeParent, "items", node.items, depth + 1, path + "/items");
    item.querySelector(".field-name-input").replaceWith(Object.assign(document.createElement("span"), { className: "field-name-static", textContent: "items" }));
    item.querySelector(".required-toggle").remove(); item.querySelector(".subtle-danger").remove();
    item.querySelector("select").onchange = async (event) => {
      const old = node.items; const destructive = ["object", "array"].includes(old.type);
      if (destructive && !await ui.confirmDialog({
        title: "更改数组元素类型",
        body: "更改数组元素类型会移除其子字段。",
        confirmLabel: "继续",
        danger: true,
      })) { event.target.value = old.type; return; }
      node.items = model.changeType(old, event.target.value); markSchemaDirty(); renderSchema();
    };
    itemWrap.append(label, item); body.append(itemWrap);
  }
  card.append(body);
  return card;
}

function renderNodeOptions(node) {
  const options = document.createElement("details");
  options.className = "schema-options";
  const summary = document.createElement("summary"); summary.textContent = "约束与说明"; options.append(summary);
  const grid = document.createElement("div"); grid.className = "schema-options-grid";
  const title = textInput(node.title, "可选"); title.oninput = () => assignOptional(node, "title", title.value, false); grid.append(control("标题", title));
  const description = textInput(node.description, "可选"); description.oninput = () => assignOptional(node, "description", description.value, false); grid.append(control("描述", description));
  if (!["object", "array"].includes(node.type)) {
    const enumeration = textInput(node.enum ? node.enum.join(", ") : "", "逗号分隔");
    enumeration.onchange = () => {
      if (!enumeration.value.trim()) delete node.enum;
      else {
        const parts = enumeration.value.split(",").map((item) => item.trim());
        if (node.type === "boolean") node.enum = parts.map((item) => item === "true");
        else if (["integer", "number"].includes(node.type)) node.enum = parts.map(Number);
        else node.enum = parts;
      }
      markSchemaDirty(); renderSchemaErrors();
    };
    grid.append(control("枚举", enumeration));
  }
  for (const key of model.TYPE_KEYS[node.type].filter((value) => !["properties", "required", "additionalProperties", "items"].includes(value))) {
    const input = numberInput(node[key]); input.oninput = () => assignOptional(node, key, input.value, true); grid.append(control(key, input));
  }
  options.append(grid);
  if (Object.keys(node).some((key) => ["title", "description", "enum"].includes(key) || model.TYPE_KEYS[node.type].includes(key) && !["properties", "required", "additionalProperties", "items"].includes(key))) options.open = true;
  return options;
}

$("schema-mode-visual").onclick = () => setSchemaMode("visual");
$("schema-mode-json").onclick = () => setSchemaMode("json");
$("add-root-field").onclick = () => addField(state.schemaDraft);
$("schema-editor").addEventListener("input", () => {
  state.schemaDirty = $("schema-editor").value !== JSON.stringify(state.savedSchema, null, 2);
  try { state.schemaDraft = JSON.parse($("schema-editor").value); state.schemaErrors = model.validate(state.schemaDraft); clearSchemaMessage(); }
  catch (error) { state.schemaErrors = [{ path: "/", message: "JSON 语法错误：" + error.message }]; }
  updateSchemaState(); renderSchemaErrors();
});
$("format-schema").onclick = () => {
  try { const parsed = JSON.parse($("schema-editor").value); $("schema-editor").value = JSON.stringify(parsed, null, 2); $("schema-editor").dispatchEvent(new Event("input")); }
  catch (error) { showSchemaMessage("JSON 语法错误：" + error.message, "error"); }
};

async function currentSchema() {
  if (state.schemaMode === "json") {
    try { state.schemaDraft = model.normalise(JSON.parse($("schema-editor").value)); }
    catch (error) { showSchemaMessage("JSON 无法保存：" + error.message, "error"); return null; }
  }
  state.schemaErrors = model.validate(state.schemaDraft);
  renderSchemaErrors(); updateSchemaState();
  return state.schemaErrors.length ? null : state.schemaDraft;
}
async function saveSchema() {
  const schema = await currentSchema();
  if (!schema) return false;
  $("save-schema").disabled = true;
  try {
    const response = await api("/api/runs/" + state.runId + "/schema", { method: "PUT", body: JSON.stringify({ result_schema: schema }) });
    if (!response.saved) {
      state.schemaErrors = response.issues.map((issue) => ({ path: issue.path || "/", message: issue.code + " " + issue.message }));
      renderSchemaErrors(); showSchemaMessage("Schema 未通过服务端校验。", "error"); updateSchemaState(); return false;
    }
    state.savedSchema = model.clone(schema); state.schemaDirty = false; state.schemaErrors = [];
    $("schema-editor").value = JSON.stringify(schema, null, 2);
    showSchemaMessage("Schema 已保存。", "success"); ui.toast("Schema 已保存", "success"); updateSchemaState(); renderSchemaErrors(); return true;
  } catch (error) { showSchemaMessage(error.message, "error"); ui.toast(error.message, "error"); return false; }
  finally { updateSchemaState(); }
}
$("save-schema").onclick = saveSchema;
async function rerunFromSchema() {
  if (state.schemaDirty || state.schemaErrors.length) return;
  $("rerun-schema").disabled = true;
  try {
    const response = await api("/api/runs/" + state.runId + "/rerun", { method: "POST", body: JSON.stringify({ parameters: collectRuntimeParameters("rerun-runtime-editor") }) });
    ui.toast("已创建 Schema 重执行任务", "success");
    navigateToRun(response.run_id);
  }
  catch (error) { showSchemaMessage(error.message, "error"); ui.toast(error.message, "error"); updateSchemaState(); }
}
$("rerun-schema").onclick = rerunFromSchema;

/* ------------------------------------------------------------- run detail */

const TERMINAL_STATUSES = ["success", "failed", "cancelled"];
const PHASE_TEXT = { generation: "准备", schema: "Schema 阶段", ttp: "TTP 阶段", acceptance: "最终验收" };
const PHASE_STEP = { generation: 0, schema: 1, ttp: 2, acceptance: 3 };

async function refreshRun() {
  const data = await api("/api/runs/" + state.runId);
  const { meta, result, schema, inputs, events, config } = data;
  const timelineEvents = Array.isArray(events) ? events : [];
  $("run-title").textContent = meta.title || "运行";
  const runKind = meta.execution_kind === "schema_rerun"
    ? "基于 Schema 重新生成 · 来源 " + meta.source_run_id
    : (meta.mode === "propose" ? "Schema 提案" : "完整生成");
  $("run-sub").textContent = meta.run_id + " · " + runKind;
  const badge = $("run-status"); badge.className = "badge " + meta.status; badge.textContent = statusLabel(meta.status);
  const running = meta.status === "running";
  $("cancel").hidden = !running; $("delete").hidden = running;
  const artifactSchema = result && result.artifact && result.artifact.result_schema;
  state.rerunAvailable = !running && Boolean(schema || artifactSchema);
  renderRuntimeSummary(config, data.config_error);
  renderRunSummary(meta, result, config, Array.isArray(inputs) ? inputs : []);
  $("progress").hidden = timelineEvents.length === 0 && !running; $("progress").open = running;
  $("bar-fill").classList.toggle("is-done", !running); renderLog(timelineEvents);
  if (!running) stopElapsedTicker(meta.elapsed_seconds);
  const showSchema = Boolean(schema) && meta.mode === "propose" && !(result && result.artifact);
  $("schema-panel").hidden = !showSchema;
  if (showSchema && (!state.schemaDraft || !state.schemaDirty)) { initialiseSchema(schema); }
  updateRerunAction();
  renderResult(result, Array.isArray(inputs) ? inputs : [], schema); renderIssues(result ? result.issues : []);
  return data;
}

function renderRunSummary(meta, result, config, inputs) {
  const panel = $("run-summary");
  const terminal = TERMINAL_STATUSES.includes(meta.status);
  panel.hidden = !terminal;
  if (!terminal) return;
  const grid = $("summary-grid"); grid.replaceChildren();
  const metadata = (result && result.metadata) || {};
  const items = [
    ["状态", statusLabel(meta.status)],
    ["终止原因", meta.termination_reason || metadata.termination_reason || "—"],
    ["总耗时", meta.elapsed_seconds != null ? ui.formatDuration(meta.elapsed_seconds) : "—"],
    ["模型", meta.runtime_model_name || "—"],
    ["运行模式", meta.execution_kind === "schema_rerun" ? "Schema 重执行" : (meta.mode === "propose" ? "Schema 提案" : "完整生成")],
    ["输入数量", String(inputs.length)],
    ["创建时间", formatTime(meta.created_at)],
  ];
  if (config && config.configuration_fingerprint) items.push(["配置指纹", config.configuration_fingerprint]);
  for (const [term, value] of items) {
    const cell = document.createElement("div");
    const dt = document.createElement("dt"); dt.textContent = term;
    const dd = document.createElement("dd"); dd.textContent = value;
    if (["配置指纹"].includes(term)) dd.classList.add("mono");
    cell.append(dt, dd);
    grid.append(cell);
  }
}

/* ------------------------------------------------------- progress & steps */

let elapsedTimer = null;
let elapsedBaseSeconds = 0;
let elapsedBaseAt = 0;

function startElapsedTicker(baseSeconds) {
  const now = Date.now();
  if (elapsedTimer && Math.abs(elapsedBaseSeconds + (now - elapsedBaseAt) / 1000 - baseSeconds) < 1.5) return;
  stopElapsedTicker();
  elapsedBaseSeconds = Math.max(0, Number(baseSeconds) || 0);
  elapsedBaseAt = now;
  const update = () => {
    $("progress-elapsed").textContent = ui.formatDuration(elapsedBaseSeconds + (Date.now() - elapsedBaseAt) / 1000);
  };
  update();
  elapsedTimer = setInterval(update, 1000);
}

function stopElapsedTicker(finalSeconds) {
  if (elapsedTimer) { clearInterval(elapsedTimer); elapsedTimer = null; }
  if (finalSeconds != null) $("progress-elapsed").textContent = ui.formatDuration(finalSeconds);
}

const STEP_FINAL_INDEX = 4;
let currentStepIndex = 0;
let seenStepIndexes = new Set([0]);

function updateSteps(phase, finished) {
  if (phase in PHASE_STEP) {
    currentStepIndex = PHASE_STEP[phase];
    seenStepIndexes.add(currentStepIndex);
  }
  $("progress-steps").querySelectorAll(".step").forEach((step, index) => {
    const isFinalStep = index === STEP_FINAL_INDEX;
    const done = finished ? seenStepIndexes.has(index) || isFinalStep : seenStepIndexes.has(index) && index < currentStepIndex;
    const skipped = finished && !done;
    step.classList.toggle("is-done", done);
    step.classList.toggle("is-skipped", skipped);
    step.classList.toggle("is-running", !finished && !isFinalStep && index === currentStepIndex);
  });
}

function phaseLabel(event) {
  if (event.type === "run.finished") return "已结束";
  return { schema: "Schema 阶段", ttp: "TTP 阶段", generation: "准备中", acceptance: "最终验收" }[event.phase] || "进行中";
}

function renderLog(events) {
  resetAgentTimeline(events);
  currentStepIndex = 0;
  seenStepIndexes = new Set([0]);
  for (const event of events) {
    if (event.phase in PHASE_STEP) seenStepIndexes.add(PHASE_STEP[event.phase]);
  }
  const last = events[events.length - 1];
  if (last) updateProgressSummary(last);
  else if ($("progress").hidden === false) updateSteps("generation", false);
}

function updateProgressSummary(event) {
  $("progress-phase").textContent = phaseLabel(event);
  const finished = event.type === "run.finished";
  updateSteps(event.phase, finished);
  if (!finished) startElapsedTicker(event.elapsed_seconds || 0);
  else if (elapsedTimer) stopElapsedTicker();
}

/* --------------------------------------------------------------- timeline */

const TIMELINE_ICONS = { thinking: "brain", text: "chat", tool: "wrench", retry: "refresh", status: "close", terminal: "check" };

function preferredEntryOpen(entry) {
  return timelineModule.shouldOpenEntry(state.timeline, entry);
}

function createTimelineNode(entry) {
  if (entry.kind === "phase") {
    const node = document.createElement("div");
    node.className = "agent-phase";
    const title = document.createElement("span"); title.className = "agent-phase-title";
    const meta = document.createElement("span"); meta.className = "agent-phase-meta";
    node.append(title, meta);
    const refs = { node, title, meta };
    state.timelineNodes.set(entry.key, refs);
    return refs;
  }
  const details = document.createElement("details");
  details.className = "agent-entry " + entry.kind;
  const summary = document.createElement("summary");
  const icon = document.createElement("span");
  icon.className = "agent-entry-icon";
  icon.append(ui.svgIcon(TIMELINE_ICONS[entry.kind] || "clock", 13));
  const title = document.createElement("span");
  title.className = "agent-entry-title";
  const status = document.createElement("span");
  status.className = "agent-entry-status";
  const meta = document.createElement("span");
  meta.className = "agent-entry-meta";
  summary.append(icon, title, status, meta);
  details.append(summary);
  const body = document.createElement("div");
  body.className = "agent-entry-body";
  details.append(body);
  summary.addEventListener("click", () => {
    setTimeout(() => {
      const current = state.timeline.byKey.get(entry.key);
      if (current) current.manualOpen = details.open;
    }, 0);
  });
  const refs = {
    node: details, details, title, status, meta, body, text: null, caret: null, detail: null,
    callSummary: null, resultSummary: null, errorSummary: null, statusNode: null,
    rawResultDetails: null, rawResult: null,
  };
  state.timelineNodes.set(entry.key, refs);
  return refs;
}

function streamCaret() {
  const caret = document.createElement("span");
  caret.className = "stream-caret";
  caret.setAttribute("aria-hidden", "true");
  return caret;
}

function updateTimelineNode(entry) {
  let refs = state.timelineNodes.get(entry.key);
  if (!refs) refs = createTimelineNode(entry);
  if (entry.kind === "phase") {
    refs.node.className = "agent-phase";
    refs.title.textContent = entry.title;
    refs.meta.textContent = entry.roundIndex != null ? "第 " + (Number(entry.roundIndex) + 1) + " 轮" : "";
    return refs.node;
  }
  const isStreaming = timelineModule.isLatestStreamingEntry(state.timeline, entry);
  refs.details.className = "agent-entry " + entry.kind + (isStreaming ? " is-streaming" : "");
  refs.details.open = preferredEntryOpen(entry);
  refs.title.textContent = entry.title;
  refs.status.textContent = entry.kind === "tool" || entry.kind === "retry" || entry.kind === "status" || entry.kind === "terminal" ? entry.status : "";
  refs.status.className = "agent-entry-status" + (entry.status === "成功" ? " is-success" : entry.status === "失败" ? " is-error" : ["调用中", "等待结果", "处理中"].includes(entry.status) ? " is-active" : "");
  refs.meta.textContent = (PHASE_TEXT[entry.phase] || entry.phase || "") + " · " + Number(entry.elapsed || 0).toFixed(1) + "s";
  const children = [];
  if (entry.kind === "tool") {
    const summaries = [
      ["callSummary", entry.callSummary, "call"],
      ["resultSummary", entry.resultSummary, "result"],
      ["errorSummary", entry.errorSummary, "error"],
    ];
    for (const [refName, value, kind] of summaries) {
      if (!value) continue;
      if (!refs[refName]) {
        refs[refName] = document.createElement("p");
        refs[refName].className = "agent-tool-summary agent-tool-summary-" + kind;
      }
      refs[refName].textContent = value;
      children.push(refs[refName]);
    }
    const rawText = entry.toolName === "submit_ttp_template" && !entry.errorSummary
      ? timelineModule.rawTtpResultText(entry.rawResultText) : "";
    if (rawText) {
      if (!refs.rawResultDetails) {
        refs.rawResultDetails = document.createElement("details");
        refs.rawResultDetails.className = "agent-raw-result";
        const rawSummary = document.createElement("summary");
        rawSummary.textContent = "查看原始解析结果";
        refs.rawResult = document.createElement("pre");
        refs.rawResult.className = "agent-tool-raw-result";
        refs.rawResultDetails.append(rawSummary, refs.rawResult);
        rawSummary.addEventListener("click", () => {
          setTimeout(() => {
            entry.rawResultManualOpen = refs.rawResultDetails.open;
          }, 0);
        });
      }
      refs.rawResult.textContent = rawText;
      if (entry.resultComplete) {
        refs.rawResultDetails.open = entry.rawResultManualOpen === true;
      } else {
        refs.rawResultDetails.open = true;
      }
      children.push(refs.rawResultDetails);
    }
  }
  if (entry.kind !== "tool" && entry.text) {
    if (!refs.text) {
      refs.text = document.createElement("pre");
      refs.text.className = "agent-stream-text";
    }
    refs.text.textContent = entry.text;
    if (!entry.complete) {
      if (!refs.caret) refs.caret = streamCaret();
      refs.text.append(refs.caret);
    } else if (refs.caret) {
      refs.caret.remove();
    }
    children.push(refs.text);
  }
  const displayDetail = timelineModule.sanitizeDisplayDetail(entry.detail);
  if (entry.kind !== "tool" && Object.keys(displayDetail).length) {
    if (!refs.detail) {
      refs.detail = document.createElement("pre");
      refs.detail.className = "agent-stream-json";
    }
    refs.detail.textContent = JSON.stringify(displayDetail, null, 2);
    children.push(refs.detail);
  }
  if (!children.length) {
    if (!refs.statusNode) {
      const statusNode = document.createElement("span");
      statusNode.className = "mono";
      refs.statusNode = statusNode;
    }
    refs.statusNode.textContent = entry.complete ? (entry.status === "失败" ? "执行失败" : "已完成") : "进行中…";
    children.push(refs.statusNode);
  }
  for (const child of [...refs.body.children]) {
    if (!children.includes(child)) child.remove();
  }
  for (const child of children) refs.body.append(child);
  if (refs.text && timelineModule.isLatestStreamingEntry(state.timeline, entry)) {
    refs.text.scrollTop = refs.text.scrollHeight;
  }
  if (refs.rawResult && !entry.resultComplete && timelineModule.isLatestStreamingEntry(state.timeline, entry)) {
    refs.rawResult.scrollTop = refs.rawResult.scrollHeight;
  }
  return refs.details;
}

function isTimelineEntryVisible(entry) {
  return state.timelinePhaseFilter === "all" || entry.phase === state.timelinePhaseFilter;
}

function timelineCount(phase) {
  return state.timeline.entries.filter((entry) => entry.kind !== "phase" && (!phase || entry.phase === phase)).length;
}

function renderTimelineFilters() {
  const labels = { all: "全部", schema: "Schema 阶段", ttp: "TTP 阶段", acceptance: "最终验收" };
  document.querySelectorAll("#timeline-filters .timeline-filter").forEach((button) => {
    const phase = button.dataset.phase;
    button.replaceChildren(document.createTextNode(labels[phase] || phase));
    const count = timelineCount(phase === "all" ? "" : phase);
    if (count) button.append(document.createTextNode(" · " + count));
    const unread = phase === "all" ? 0 : state.timelineUnread[phase] || 0;
    if (unread && phase !== state.timelinePhaseFilter) {
      const badge = document.createElement("span");
      badge.className = "timeline-filter-unread";
      badge.textContent = "未读 " + unread;
      button.append(badge);
    }
    button.classList.toggle("is-active", phase === state.timelinePhaseFilter);
    button.setAttribute("aria-pressed", String(phase === state.timelinePhaseFilter));
  });
}

function applyTimelineFilter(scrollToPhase = false) {
  const host = $("agent-timeline");
  for (const entry of state.timeline.entries) {
    let refs = state.timelineNodes.get(entry.key);
    if (!refs) refs = createTimelineNode(entry);
    const node = updateTimelineNode(entry);
    if (isTimelineEntryVisible(entry)) {
      if (!node.isConnected) host.append(node);
    } else if (node.isConnected) {
      node.remove();
    }
  }
  renderTimelineFilters();
  if (scrollToPhase) {
    const first = state.timeline.entries.find((entry) => entry.phase === state.timelinePhaseFilter);
    const refs = first && state.timelineNodes.get(first.key);
    if (refs && refs.node.isConnected) refs.node.scrollIntoView({ block: "start" });
  }
}

function renderTimelineFull() {
  const host = $("agent-timeline");
  host.replaceChildren();
  state.timelineNodes.clear();
  const entries = state.timeline.entries;
  if (!entries.length) {
    const empty = document.createElement("p"); empty.className = "timeline-empty"; empty.textContent = "等待 Agent 事件…"; host.append(empty); return;
  }
  for (const entry of entries) {
    const node = updateTimelineNode(entry);
    if (isTimelineEntryVisible(entry)) host.append(node);
  }
  renderTimelineFilters();
  if (state.followProgress) host.scrollTop = host.scrollHeight;
}

function flushTimelineChanges() {
  const host = $("agent-timeline");
  const empty = host.querySelector(".timeline-empty");
  if (empty) empty.remove();
  for (const key of state.timelineRemoved) {
    const refs = state.timelineNodes.get(key);
    if (refs) refs.node.remove();
    state.timelineNodes.delete(key);
  }
  for (const key of state.timelineDirty) {
    const entry = state.timeline.byKey.get(key);
    if (!entry) continue;
    const node = updateTimelineNode(entry);
    if (isTimelineEntryVisible(entry)) {
      if (!node.isConnected) host.append(node);
    } else if (node.isConnected) {
      node.remove();
    }
  }
  state.timelineRemoved.clear();
  state.timelineDirty.clear();
  renderTimelineFilters();
  if (state.followProgress) host.scrollTop = host.scrollHeight;
}

state.timelineScheduler = timelineModule.createRenderScheduler(
  flushTimelineChanges,
  (callback) => requestAnimationFrame(callback),
  (handle) => cancelAnimationFrame(handle),
);

function resetAgentTimeline(events) {
  state.timelineScheduler.cancel();
  state.events = events.slice();
  state.eventTracker = timelineModule.createSequenceTracker(events);
  state.timeline = timelineModule.buildTimeline(events);
  state.timelinePhaseFilter = "all";
  state.timelineUnread = { schema: 0, ttp: 0, acceptance: 0 };
  state.timelineDirty.clear();
  state.timelineRemoved.clear();
  renderTimelineFull();
}

function appendTimelineEvent(event) {
  if (!state.eventTracker.accept(event)) return false;
  state.events.push(event);
  const change = timelineModule.appendAgentEvent(state.timeline, event);
  if (change.removedKey) {
    state.timelineRemoved.add(change.removedKey);
    state.timelineDirty.delete(change.removedKey);
  }
  if (change.isDelta && change.previousLatestStreamingKey && change.previousLatestStreamingKey !== change.entry.key) {
    state.timelineDirty.add(change.previousLatestStreamingKey);
  }
  if (!change.entry) {
    renderTimelineFilters();
    updateProgressSummary(event);
    return true;
  }
  if (change.created && change.entry.kind !== "phase" && change.entry.phase !== state.timelinePhaseFilter
    && Object.prototype.hasOwnProperty.call(state.timelineUnread, change.entry.phase)) {
    state.timelineUnread[change.entry.phase] += 1;
  }
  state.timelineDirty.add(change.entry.key);
  updateProgressSummary(event);
  state.timelineScheduler.schedule();
  return true;
}

/* --------------------------------------------------------------- artifacts */

function renderResult(result, inputs, schema) {
  const artifact = result && result.artifact;
  $("result-panel").hidden = !artifact && !inputs.length;
  const templateText = artifact ? artifact.ttp_template : "（尚无模板）";
  highlighter.fillCodeElement($("out-template"), templateText, "ttp");
  const schemaJson = JSON.stringify(artifact ? artifact.result_schema : schema, null, 2);
  highlighter.fillCodeElement($("out-schema"), schemaJson || "（尚无 Schema）", "json");
  state.inputsText = inputs.map((value, index) => "输入 " + (index + 1) + "\n" + value).join("\n\n───────────────\n\n");
  $("out-inputs").textContent = state.inputsText;
  const records = artifact ? artifact.records : [];
  state.recordsText = JSON.stringify(records, null, 2);
  renderRecords(records);
}

const isScalar = (value) => value === null || ["string", "number", "boolean"].includes(typeof value);
const isRecordObject = (value) => value && typeof value === "object" && !Array.isArray(value);

function renderRecords(records) {
  const host = $("out-records"); host.replaceChildren();
  if (!records || !records.length) {
    const empty = document.createElement("p"); empty.className = "empty"; empty.textContent = "（尚无 records）"; host.append(empty);
    return;
  }
  records.forEach((record, index) => {
    const group = document.createElement("details");
    group.className = "record-group";
    group.open = true;
    const summary = document.createElement("summary");
    const label = document.createElement("span");
    label.textContent = "输入 " + (index + 1) + " · input_index=" + index;
    const count = document.createElement("span");
    count.className = "record-count";
    count.textContent = (Array.isArray(record) ? record.length : 1) + " 条";
    summary.append(label, count);
    const body = document.createElement("div");
    body.className = "record-group-body";
    body.append(renderValue(record));
    group.append(summary, body);
    host.append(group);
  });
}

function renderValue(value) {
  if (Array.isArray(value) && value.length && value.every(isRecordObject)) return recordsTable(value);
  if (Array.isArray(value) && value.length && value.every(isScalar)) return chipList(value);
  if (isRecordObject(value) && Object.keys(value).length && Object.values(value).every(isScalar)) return recordsTable([value]);
  const pre = document.createElement("pre"); pre.className = "code";
  highlighter.fillCodeElement(pre, JSON.stringify(value, null, 2) || "{}", "json");
  return pre;
}

function chipList(values) {
  const wrap = document.createElement("div");
  wrap.className = "chip-list";
  for (const value of values) {
    const chip = document.createElement("span");
    chip.className = "chip";
    chip.textContent = String(value);
    wrap.append(chip);
  }
  return wrap;
}

function cellContent(value) {
  if (value === undefined) return document.createTextNode("—");
  if (isScalar(value)) return document.createTextNode(String(value));
  if (Array.isArray(value) && value.length && value.every(isScalar)) return chipList(value);
  const span = document.createElement("span");
  span.className = "cell-json";
  span.textContent = JSON.stringify(value);
  return span;
}

function recordsTable(rows) {
  const columns = [...new Set(rows.flatMap((row) => Object.keys(row)))];
  const wrap = document.createElement("div"); wrap.className = "table-wrap";
  const table = document.createElement("table"); table.className = "records";
  const head = document.createElement("tr");
  for (const column of columns) { const th = document.createElement("th"); th.textContent = column; head.append(th); }
  table.append(head);
  for (const row of rows) {
    const tr = document.createElement("tr");
    for (const column of columns) { const td = document.createElement("td"); td.append(cellContent(row[column])); tr.append(td); }
    table.append(tr);
  }
  wrap.append(table);
  return wrap;
}

function renderIssues(issues) {
  $("issues-panel").hidden = !issues || !issues.length; const list = $("issues"); list.replaceChildren();
  for (const issue of issues || []) { const li = document.createElement("li"); if (issue.severity === "warning") li.className = "warning"; const code = document.createElement("code"); code.textContent = issue.code; li.append(code, document.createTextNode(" " + issue.message)); list.append(li); }
}

/* ------------------------------------------------------- events & actions */

function setStreamState(value) {
  const badge = $("stream-state");
  if (!value) { badge.hidden = true; return; }
  badge.hidden = false;
  badge.className = "stream-state" + (value === "live" ? " live" : value === "reconnecting" ? " reconnecting" : "");
  badge.textContent = value === "live" ? "实时" : value === "reconnecting" ? "重连中" : "连接中";
}

function openStream(runId) {
  closeStream();
  const after = state.eventTracker.highest();
  const stream = new EventSource("/api/runs/" + runId + "/events?after_sequence=" + after);
  state.stream = stream;
  setStreamState("connecting");
  stream.onopen = () => setStreamState("live");
  stream.onmessage = (message) => {
    let event;
    try { event = JSON.parse(message.data); }
    catch { return; }
    if (!appendTimelineEvent(event)) return;
    $("progress").hidden = false; $("progress").open = true;
    if (event.type === "run.finished") {
      setStreamState(null);
      closeStream();
      refreshRun().then(() => { loadHistory(); ui.toast("运行已结束", "info"); });
    }
  };
  stream.onerror = () => {
    // Keep EventSource alive during transient disconnects so it can reconnect
    // with Last-Event-ID; close only after the browser marks it terminal.
    if (stream.readyState === EventSource.CLOSED) { setStreamState(null); closeStream(); }
    else setStreamState("reconnecting");
  };
}
function closeStream() {
  if (state.stream) { state.stream.close(); state.stream = null; }
  state.timelineScheduler.cancel();
}
$("progress-follow").onclick = () => {
  state.followProgress = true;
  state.timelinePhaseFilter = "all";
  applyTimelineFilter();
  const host = $("agent-timeline");
  host.scrollTop = host.scrollHeight;
};
$("agent-timeline").addEventListener("scroll", () => {
  const host = $("agent-timeline");
  state.followProgress = host.scrollHeight - host.scrollTop - host.clientHeight < 24;
});
document.querySelectorAll("#timeline-filters .timeline-filter").forEach((button) => {
  button.addEventListener("click", () => {
    state.timelinePhaseFilter = button.dataset.phase || "all";
    if (state.timelinePhaseFilter !== "all") state.timelineUnread[state.timelinePhaseFilter] = 0;
    applyTimelineFilter(true);
  });
});
$("new-run").onclick = () => navigateToNew(); $("refresh").onclick = loadHistory; $("history-toggle").onclick = openDrawer; $("history-close").onclick = closeDrawer; $("drawer-overlay").onclick = closeDrawer;
$("start").onclick = async () => {
  if (!validateOutputs() || !state.runtimeReady || !validateRuntimeEditor($("new-runtime-editor"))) return;
  const error = $("new-error"); error.hidden = true; $("start").disabled = true;
  try {
    const mode = document.querySelector('input[name="mode"]:checked').value;
    const created = await api("/api/runs", { method: "POST", body: JSON.stringify({ mode, title: $("title").value, command_outputs: state.outputs.map((item) => item.text), parameters: collectRuntimeParameters("new-runtime-editor") }) });
    ui.toast("已创建生成任务", "success");
    navigateToRun(created.run_id);
  }
  catch (failure) { error.textContent = failure.message; error.hidden = false; ui.toast(failure.message, "error"); }
  finally { validateOutputs(); }
};
$("cancel").onclick = async () => {
  try {
    await api("/api/runs/" + state.runId + "/cancel", { method: "POST" });
    ui.toast("已请求取消，等待运行结束…", "info");
    await refreshRun(); await loadHistory();
  } catch (error) { ui.toast(error.message, "error"); }
};
$("delete").onclick = async () => {
  if (!await ui.confirmDialog({
    title: "删除运行记录",
    body: "将删除这次运行的全部文件，包括输入、事件与产物。此操作不可恢复。",
    confirmLabel: "删除",
    danger: true,
  })) return;
  try {
    await api("/api/runs/" + state.runId, { method: "DELETE" });
    ui.toast("运行已删除", "success");
    state.schemaDirty = false;
    navigateToNew();
  } catch (error) { ui.toast(error.message, "error"); }
};
document.querySelectorAll(".tab").forEach((tab) => { tab.onclick = () => { document.querySelectorAll(".tab").forEach((other) => { other.classList.remove("is-active"); other.setAttribute("aria-selected", "false"); }); tab.classList.add("is-active"); tab.setAttribute("aria-selected", "true"); state.activeTab = tab.dataset.tab; for (const name of ["template", "records", "schema", "inputs"]) $("tab-" + name).hidden = name !== state.activeTab; }; });
document.querySelectorAll(".copy").forEach((button) => {
  button.onclick = async () => {
    const key = button.dataset.copy;
    const text = key === "records" ? state.recordsText : key === "inputs" ? state.inputsText : $("out-" + key).textContent;
    try {
      await navigator.clipboard.writeText(text);
      const original = button.textContent;
      button.classList.add("is-copied");
      button.textContent = "已复制";
      setTimeout(() => { button.classList.remove("is-copied"); button.replaceChildren(ui.svgIcon("copy", 13), document.createTextNode(original)); }, 1200);
    } catch (error) { ui.toast("复制失败：" + error.message, "error"); }
  };
});
window.addEventListener("beforeunload", (event) => { if (!state.schemaDirty) return; event.preventDefault(); event.returnValue = ""; });

/* --------------------------------------------------------------- bootstrap */

ui.hydrateIcons();
renderOutputTabs(); validateOutputs(); loadRuntimeConfig(); loadHistory();
handleRoute();
