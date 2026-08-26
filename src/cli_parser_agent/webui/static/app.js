"use strict";

const $ = (id) => document.getElementById(id);
const model = window.SchemaModel;
const timelineApi = window.AgentTimeline;
const MAX_INPUT_BYTES = 1024 * 1024;
const state = {
  runId: null, stream: null, activeTab: "template", outputs: [{ text: "", scrollTop: 0 }],
  activeOutput: 0, schemaDraft: null, savedSchema: null, schemaDirty: false,
  schemaMode: "visual", schemaErrors: [], schemaExpanded: Object.create(null),
  drawerOpen: false, events: [], followProgress: true,
  timeline: timelineApi.createTimelineState(),
  eventTracker: timelineApi.createSequenceTracker(),
  timelineNodes: new Map(), timelineDirty: new Set(), timelineRemoved: new Set(),
  timelineScheduler: null,
};

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

/* New run inputs */
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
    button.innerHTML = "<strong>输入 " + (index + 1) + "</strong><small>" + formatBytes(bytes) + "</small>";
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
$("remove-output").onclick = () => {
  if (state.outputs.length === 1) return;
  state.outputs.splice(state.activeOutput, 1);
  state.activeOutput = Math.min(state.activeOutput, state.outputs.length - 1);
  $("output-editor").value = state.outputs[state.activeOutput].text;
  renderOutputTabs();
  validateOutputs();
};

/* History and views */
const statusLabel = (status) => ({ running: "运行中", success: "成功", failed: "失败", cancelled: "已取消" }[status] || status);
function formatTime(value) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "" : date.toLocaleString(undefined, { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
}

async function loadHistory() {
  const note = $("history-state");
  note.hidden = false;
  note.textContent = "正在加载…";
  try {
    const data = await api("/api/runs");
    const list = $("history");
    list.replaceChildren();
    note.textContent = data.runs.length ? "" : "暂无运行记录";
    note.hidden = data.runs.length > 0;
    for (const run of data.runs) {
      const item = document.createElement("li");
      if (run.run_id === state.runId) item.classList.add("is-active");
      const button = document.createElement("button");
      button.type = "button";
      button.innerHTML = "<span class=\"h-title\"></span><span class=\"h-meta\"><span class=\"badge " + run.status + "\">" + statusLabel(run.status) + "</span><span>" + formatTime(run.created_at) + "</span></span>";
      button.querySelector(".h-title").textContent = run.title || run.run_id;
      button.onclick = () => openRun(run.run_id);
      item.append(button);
      list.append(item);
    }
  } catch (error) {
    note.hidden = false;
    note.textContent = "历史记录加载失败：" + error.message;
  }
}

function confirmDiscard() {
  return !state.schemaDirty || confirm("Schema 有未保存的修改，确定放弃吗？");
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
  if (!confirmDiscard()) return;
  closeStream();
  state.runId = null;
  state.schemaDirty = false;
  setHidden("view-new", false);
  setHidden("view-run", true);
  closeDrawer();
  loadHistory();
}
async function openRun(runId) {
  if (runId !== state.runId && !confirmDiscard()) return;
  closeStream();
  state.runId = runId;
  state.schemaDirty = false;
  setHidden("view-new", true);
  setHidden("view-run", false);
  closeDrawer();
  const data = await refreshRun();
  await loadHistory();
  if (data.meta.status === "running") openStream(runId);
}

/* Schema editor */
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
  $("run-template").disabled = state.schemaDirty || state.schemaErrors.length > 0;
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
  if (oldName === newName) return;
  if (!newName) return;
  const newPath = oldPath.slice(0, oldPath.lastIndexOf("/")) + "/" + escapePointer(newName);
  const entries = Object.entries(parent.properties);
  parent.properties = Object.fromEntries(entries.map(([key, value]) => [key === oldName ? newName : key, value]));
  if (parent.required) parent.required = parent.required.map((item) => item === oldName ? newName : item);
  migrateSchemaExpansion(oldPath, newPath);
  markSchemaDirty();
}
function renderField(parent, name, node, depth, path) {
  const card = document.createElement("div");
  card.className = "schema-field";
  card.style.setProperty("--depth", depth);
  const header = document.createElement("div");
  header.className = "schema-field-head";
  const nameInput = textInput(name, "field_name");
  nameInput.className = "field-name-input";
  nameInput.onchange = () => {
    const nextName = nameInput.value.trim();
    renameField(parent, name, nextName, path);
    renderSchema();
  };
  const type = document.createElement("select");
  for (const value of model.TYPES) { const option = document.createElement("option"); option.value = value; option.textContent = value; option.selected = value === node.type; type.append(option); }
  type.onchange = () => {
    const destructive = node.type === "object" || node.type === "array" || Object.keys(node).some((key) => !["type", "title", "description", "enum"].includes(key));
    if (destructive && !confirm("更改类型会移除不兼容的子字段或约束，继续吗？")) { type.value = node.type; return; }
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
  const remove = document.createElement("button"); remove.type = "button"; remove.className = "icon-btn subtle-danger"; remove.title = "删除字段"; remove.setAttribute("aria-label", "删除字段 " + name); remove.textContent = "×";
  remove.onclick = () => {
    if (!confirm("删除字段 " + name + "？")) return;
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
    toggle.textContent = expanded ? "⌄" : "›";
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
    item.querySelector("select").onchange = (event) => { const old = node.items; const destructive = ["object", "array"].includes(old.type); if (destructive && !confirm("更改数组元素类型会移除子字段，继续吗？")) { event.target.value = old.type; return; } node.items = model.changeType(old, event.target.value); markSchemaDirty(); renderSchema(); };
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
    showSchemaMessage("Schema 已保存。", "success"); updateSchemaState(); renderSchemaErrors(); return true;
  } catch (error) { showSchemaMessage(error.message, "error"); return false; }
  finally { updateSchemaState(); }
}
$("save-schema").onclick = saveSchema;
$("run-template").onclick = async () => {
  if (state.schemaDirty || state.schemaErrors.length) return;
  $("run-template").disabled = true;
  try { await api("/api/runs/" + state.runId + "/generate", { method: "POST" }); await refreshRun(); openStream(state.runId); }
  catch (error) { showSchemaMessage(error.message, "error"); updateSchemaState(); }
};

/* Run rendering */
async function refreshRun() {
  const data = await api("/api/runs/" + state.runId);
  const { meta, result, schema, inputs, events } = data;
  const timelineEvents = Array.isArray(events) ? events : [];
  $("run-title").textContent = meta.title || "运行";
  $("run-sub").textContent = meta.run_id + " · " + (meta.mode === "propose" ? "Schema 提案" : "完整生成");
  const badge = $("run-status"); badge.className = "badge " + meta.status; badge.textContent = statusLabel(meta.status) + (meta.elapsed_seconds ? " · " + meta.elapsed_seconds.toFixed(1) + "s" : "");
  const running = meta.status === "running";
  $("cancel").hidden = !running; $("delete").hidden = running;
  $("progress").hidden = timelineEvents.length === 0 && !running; $("progress").open = running;
  $("bar-fill").classList.toggle("is-done", !running); renderLog(timelineEvents);
  const proposal = result && result.proposal;
  const showSchema = Boolean(schema) && meta.mode === "propose" && !(result && result.artifact);
  $("schema-panel").hidden = !showSchema;
  if (showSchema && (!state.schemaDraft || !state.schemaDirty)) { initialiseSchema(schema); renderAssumptions(proposal ? proposal.assumptions : []); }
  renderResult(result, inputs, schema); renderIssues(result ? result.issues : []);
  return data;
}
function renderLog(events) {
  resetAgentTimeline(events);
  const last = events[events.length - 1];
  if (last) updateProgressSummary(last);
}

function updateProgressSummary(event) {
  $("progress-phase").textContent = phaseLabel(event);
  $("progress-elapsed").textContent = (event.elapsed_seconds || 0).toFixed(1) + "s";
}

function preferredEntryOpen(entry) {
  if (entry.manualOpen !== null) return entry.manualOpen;
  return !entry.complete || entry.kind === "text";
}

function createTimelineNode(entry) {
  const details = document.createElement("details");
  details.className = "agent-entry " + entry.kind;
  const summary = document.createElement("summary");
  const title = document.createElement("strong");
  const meta = document.createElement("span");
  meta.className = "agent-entry-meta";
  summary.append(title, meta);
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
  const refs = { details, title, meta, body, text: null, detail: null, status: null };
  state.timelineNodes.set(entry.key, refs);
  return refs;
}

function updateTimelineNode(entry) {
  let refs = state.timelineNodes.get(entry.key);
  if (!refs) refs = createTimelineNode(entry);
  refs.details.className = "agent-entry " + entry.kind;
  refs.details.open = preferredEntryOpen(entry);
  refs.title.textContent = entry.title;
  refs.meta.textContent = (entry.phase || "") + " · " + Number(entry.elapsed || 0).toFixed(1) + "s";
  const children = [];
  if (entry.text) {
    if (!refs.text) {
      refs.text = document.createElement("pre");
      refs.text.className = "agent-stream-text";
    }
    refs.text.textContent = entry.text;
    children.push(refs.text);
  }
  if (entry.detail && Object.keys(entry.detail).length) {
    if (!refs.detail) {
      refs.detail = document.createElement("pre");
      refs.detail.className = "agent-stream-json";
    }
    refs.detail.textContent = JSON.stringify(entry.detail, null, 2);
    children.push(refs.detail);
  }
  if (!children.length) {
    if (!refs.status) {
      refs.status = document.createElement("span");
      refs.status.className = "mono";
    }
    refs.status.textContent = entry.complete ? "已完成" : "进行中…";
    children.push(refs.status);
  }
  refs.body.replaceChildren(...children);
  return refs.details;
}

function renderTimelineFull() {
  const host = $("agent-timeline");
  host.replaceChildren();
  state.timelineNodes.clear();
  const entries = state.timeline.entries;
  if (!entries.length) {
    const empty = document.createElement("p"); empty.className = "timeline-empty"; empty.textContent = "等待 Agent 事件…"; host.append(empty); return;
  }
  for (const entry of entries) host.append(updateTimelineNode(entry));
  if (state.followProgress) host.scrollTop = host.scrollHeight;
}

function flushTimelineChanges() {
  const host = $("agent-timeline");
  const empty = host.querySelector(".timeline-empty");
  if (empty) empty.remove();
  for (const key of state.timelineRemoved) {
    const refs = state.timelineNodes.get(key);
    if (refs) refs.details.remove();
    state.timelineNodes.delete(key);
  }
  for (const key of state.timelineDirty) {
    const entry = state.timeline.byKey.get(key);
    if (!entry) continue;
    const node = updateTimelineNode(entry);
    if (!node.isConnected) host.append(node);
  }
  state.timelineRemoved.clear();
  state.timelineDirty.clear();
  if (state.followProgress) host.scrollTop = host.scrollHeight;
}

state.timelineScheduler = timelineApi.createRenderScheduler(
  flushTimelineChanges,
  (callback) => requestAnimationFrame(callback),
  (handle) => cancelAnimationFrame(handle),
);

function resetAgentTimeline(events) {
  state.timelineScheduler.cancel();
  state.events = events.slice();
  state.eventTracker = timelineApi.createSequenceTracker(events);
  state.timeline = timelineApi.buildTimeline(events);
  state.timelineDirty.clear();
  state.timelineRemoved.clear();
  renderTimelineFull();
}

function appendTimelineEvent(event) {
  if (!state.eventTracker.accept(event)) return false;
  state.events.push(event);
  const change = timelineApi.appendAgentEvent(state.timeline, event);
  if (change.removedKey) {
    state.timelineRemoved.add(change.removedKey);
    state.timelineDirty.delete(change.removedKey);
  }
  state.timelineDirty.add(change.entry.key);
  updateProgressSummary(event);
  state.timelineScheduler.schedule();
  return true;
}
function describe(event) {
  if (event.type === "model_call") return (event.phase || "") + " 模型调用";
  if (event.type === "run.finished") return "结束：" + statusLabel(event.status);
  const detail = event.detail || {}; const extra = detail.termination_reason || detail.reason || detail.status || "";
  return (event.type || "").replace("cli_parser.", "") + (extra ? " · " + extra : "");
}
function phaseLabel(event) {
  if (event.type === "run.finished") return "已结束";
  return { schema: "Schema 阶段", ttp: "TTP 阶段", generation: "准备中", acceptance: "最终验收" }[event.phase] || "进行中";
}
function renderAssumptions(items) {
  const box = $("assumptions"); box.hidden = !items || !items.length; box.replaceChildren(); if (!items || !items.length) return;
  const strong = document.createElement("strong"); strong.textContent = "模型假设"; const ul = document.createElement("ul");
  for (const value of items) { const li = document.createElement("li"); li.textContent = value; ul.append(li); } box.append(strong, ul);
}
function renderResult(result, inputs, schema) {
  const artifact = result && result.artifact;
  $("result-panel").hidden = !artifact && !inputs.length;
  $("out-template").textContent = artifact ? artifact.ttp_template : "（尚无模板）";
  $("out-schema").textContent = JSON.stringify(artifact ? artifact.result_schema : schema, null, 2) || "（尚无 Schema）";
  $("out-inputs").textContent = inputs.map((value, index) => "输入 " + (index + 1) + "\n" + value).join("\n\n───────────────\n\n");
  renderRecords(artifact ? artifact.records : []);
}
function renderRecords(records) {
  const host = $("out-records"); host.replaceChildren();
  if (!records || !records.length) { const empty = document.createElement("p"); empty.className = "empty"; empty.textContent = "（尚无 records）"; host.append(empty); return; }
  records.forEach((record, index) => { const group = document.createElement("div"); group.className = "record-group"; const heading = document.createElement("h4"); heading.textContent = "输入 " + (index + 1) + " · input_index=" + index; group.append(heading, renderValue(record)); host.append(group); });
}
function renderValue(value) {
  if (Array.isArray(value) && value.length && value.every(isFlatObject)) {
    const columns = [...new Set(value.flatMap((row) => Object.keys(row)))]; const wrap = document.createElement("div"); wrap.className = "table-wrap"; const table = document.createElement("table"); table.className = "records";
    const head = document.createElement("tr"); for (const column of columns) { const th = document.createElement("th"); th.textContent = column; head.append(th); } table.append(head);
    for (const row of value) { const tr = document.createElement("tr"); for (const column of columns) { const td = document.createElement("td"); td.textContent = row[column] === undefined ? "—" : String(row[column]); tr.append(td); } table.append(tr); } wrap.append(table); return wrap;
  }
  const pre = document.createElement("pre"); pre.className = "code"; pre.textContent = JSON.stringify(value, null, 2); return pre;
}
const isFlatObject = (value) => value && typeof value === "object" && !Array.isArray(value) && Object.values(value).every((item) => item === null || typeof item !== "object");
function renderIssues(issues) {
  $("issues-panel").hidden = !issues || !issues.length; const list = $("issues"); list.replaceChildren();
  for (const issue of issues || []) { const li = document.createElement("li"); if (issue.severity === "warning") li.className = "warning"; const code = document.createElement("code"); code.textContent = issue.code; li.append(code, document.createTextNode(" " + issue.message)); list.append(li); }
}

/* Events and actions */
function openStream(runId) {
  closeStream();
  const after = state.eventTracker.highest();
  const stream = new EventSource("/api/runs/" + runId + "/events?after_sequence=" + after);
  state.stream = stream;
  stream.onmessage = (message) => {
    let event;
    try { event = JSON.parse(message.data); }
    catch { return; }
    if (!appendTimelineEvent(event)) return;
    $("progress").hidden = false; $("progress").open = true;
    if (event.type === "run.finished") { closeStream(); refreshRun().then(loadHistory); }
  };
  stream.onerror = () => {
    // Keep EventSource alive during transient disconnects so it can reconnect
    // with Last-Event-ID; close only after the browser marks it terminal.
    if (stream.readyState === EventSource.CLOSED) closeStream();
  };
}
function closeStream() {
  if (state.stream) { state.stream.close(); state.stream = null; }
  state.timelineScheduler.cancel();
}
$('progress-follow').onclick = () => {
  state.followProgress = true;
  const host = $("agent-timeline");
  host.scrollTop = host.scrollHeight;
};
$("agent-timeline").addEventListener("scroll", () => {
  const host = $("agent-timeline");
  state.followProgress = host.scrollHeight - host.scrollTop - host.clientHeight < 24;
});
$("new-run").onclick = showNew; $("refresh").onclick = loadHistory; $("history-toggle").onclick = openDrawer; $("history-close").onclick = closeDrawer; $("drawer-overlay").onclick = closeDrawer;
$("start").onclick = async () => {
  if (!validateOutputs()) return; const error = $("new-error"); error.hidden = true; $("start").disabled = true;
  try { const mode = document.querySelector('input[name="mode"]:checked').value; const created = await api("/api/runs", { method: "POST", body: JSON.stringify({ mode, title: $("title").value, command_outputs: state.outputs.map((item) => item.text) }) }); await openRun(created.run_id); }
  catch (failure) { error.textContent = failure.message; error.hidden = false; }
  finally { validateOutputs(); }
};
$("cancel").onclick = async () => { await api("/api/runs/" + state.runId + "/cancel", { method: "POST" }); await refreshRun(); await loadHistory(); };
$("delete").onclick = async () => { if (!confirm("删除这次运行的全部文件？")) return; await api("/api/runs/" + state.runId, { method: "DELETE" }); state.schemaDirty = false; showNew(); };
document.querySelectorAll(".tab").forEach((tab) => { tab.onclick = () => { document.querySelectorAll(".tab").forEach((other) => other.classList.remove("is-active")); tab.classList.add("is-active"); state.activeTab = tab.dataset.tab; for (const name of ["template", "records", "schema", "inputs"]) $("tab-" + name).hidden = name !== state.activeTab; }; });
document.querySelectorAll(".copy").forEach((button) => { button.onclick = async () => { await navigator.clipboard.writeText($("out-" + button.dataset.copy).textContent); const original = button.textContent; button.textContent = "已复制"; setTimeout(() => { button.textContent = original; }, 1200); }; });
window.addEventListener("beforeunload", (event) => { if (!state.schemaDirty) return; event.preventDefault(); event.returnValue = ""; });

renderOutputTabs(); validateOutputs(); loadHistory();
