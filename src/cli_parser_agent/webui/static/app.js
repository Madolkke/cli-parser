"use strict";

const $ = (id) => document.getElementById(id);
const api = async (path, options) => {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  let body = null;
  try {
    body = await response.json();
  } catch {
    body = null;
  }
  if (!response.ok) {
    const detail = body && body.detail ? body.detail : `HTTP ${response.status}`;
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return body;
};

const state = { runId: null, stream: null, activeTab: "template" };

/* ---------- new-run form ---------- */

function addOutput(value = "") {
  const row = document.createElement("div");
  row.className = "output-row";
  const area = document.createElement("textarea");
  area.placeholder = "粘贴一份完整的命令输出…";
  area.value = value;
  const remove = document.createElement("button");
  remove.className = "remove";
  remove.textContent = "移除";
  remove.onclick = () => {
    row.remove();
    syncOutputControls();
  };
  row.append(area, remove);
  $("outputs").append(row);
  syncOutputControls();
}

function syncOutputControls() {
  const rows = [...document.querySelectorAll(".output-row")];
  rows.forEach((row) => {
    row.querySelector(".remove").hidden = rows.length === 1;
  });
  $("add-output").disabled = rows.length >= 5;
}

function readOutputs() {
  return [...document.querySelectorAll(".output-row textarea")]
    .map((area) => area.value)
    .filter((text) => text.trim() !== "");
}

/* ---------- history ---------- */

async function loadHistory() {
  const data = await api("/api/runs");
  const list = $("history");
  list.innerHTML = "";
  $("history-empty").hidden = data.runs.length > 0;
  for (const run of data.runs) {
    const item = document.createElement("li");
    if (run.run_id === state.runId) item.classList.add("is-active");
    const title = document.createElement("span");
    title.className = "h-title";
    title.textContent = run.title || run.run_id;
    const meta = document.createElement("span");
    meta.className = "h-meta";
    const badge = document.createElement("span");
    badge.className = `badge ${run.status}`;
    badge.textContent = statusLabel(run.status);
    const when = document.createElement("span");
    when.textContent = formatTime(run.created_at);
    meta.append(badge, when);
    item.append(title, meta);
    item.onclick = () => openRun(run.run_id);
    list.append(item);
  }
}

const statusLabel = (status) =>
  ({ running: "运行中", success: "成功", failed: "失败", cancelled: "已取消" }[status] || status);

function formatTime(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "" : d.toLocaleString();
}

/* ---------- views ---------- */

function showNew() {
  closeStream();
  state.runId = null;
  $("view-new").hidden = false;
  $("view-run").hidden = true;
  loadHistory();
}

async function openRun(runId) {
  closeStream();
  state.runId = runId;
  $("view-new").hidden = true;
  $("view-run").hidden = false;
  await refreshRun();
  await loadHistory();
  const meta = (await api(`/api/runs/${runId}`)).meta;
  if (meta.status === "running") openStream(runId);
}

async function refreshRun() {
  const data = await api(`/api/runs/${state.runId}`);
  const { meta, result, schema, inputs, events } = data;

  $("run-title").textContent = meta.title || "运行";
  $("run-sub").textContent = `${meta.run_id} · ${meta.mode === "propose" ? "Schema 提案" : "完整生成"}`;
  const badge = $("run-status");
  badge.className = `badge ${meta.status}`;
  badge.textContent = statusLabel(meta.status) +
    (meta.elapsed_seconds ? ` · ${meta.elapsed_seconds.toFixed(1)}s` : "");

  const running = meta.status === "running";
  $("cancel").hidden = !running;
  $("delete").hidden = running;
  $("progress").hidden = events.length === 0 && !running;
  if (!running) $("bar-fill").classList.add("is-done");
  else $("bar-fill").classList.remove("is-done");
  renderLog(events);

  // Schema review panel: only for a proposal awaiting confirmation.
  const proposal = result && result.proposal;
  const showSchemaPanel = Boolean(schema) && meta.mode === "propose" && !hasArtifact(result);
  $("schema-panel").hidden = !showSchemaPanel;
  if (showSchemaPanel) {
    $("schema-editor").value = JSON.stringify(schema, null, 2);
    renderAssumptions(proposal ? proposal.assumptions : []);
    renderEvidence(proposal ? proposal.evidence : []);
  }

  renderResult(result, inputs, schema);
  renderIssues(result ? result.issues : []);
}

const hasArtifact = (result) => Boolean(result && result.artifact);

function renderLog(events) {
  const log = $("progress-log");
  log.innerHTML = "";
  for (const event of events.slice(-80)) {
    const li = document.createElement("li");
    const time = document.createElement("span");
    time.className = "t";
    time.textContent = `${(event.elapsed_seconds ?? 0).toFixed(1)}s`;
    const text = document.createElement("span");
    text.textContent = describe(event);
    li.append(time, text);
    log.append(li);
  }
  log.scrollTop = log.scrollHeight;
  const last = events[events.length - 1];
  if (last) $("progress-phase").textContent = phaseLabel(last);
}

function describe(event) {
  const type = event.type || "";
  if (type === "model_call") return `${event.phase || ""} 模型调用`;
  if (type === "run.finished") return `结束：${statusLabel(event.status)}`;
  const detail = event.detail || {};
  const extra = detail.termination_reason || detail.reason || detail.status || "";
  return `${type.replace("cli_parser.", "")}${extra ? ` · ${extra}` : ""}`;
}

function phaseLabel(event) {
  if (event.type === "run.finished") return "已结束";
  return { schema: "Schema 阶段", ttp: "TTP 阶段", generation: "准备中", acceptance: "最终验收" }[event.phase] || "进行中";
}

function renderAssumptions(assumptions) {
  const box = $("assumptions");
  if (!assumptions || assumptions.length === 0) {
    box.hidden = true;
    return;
  }
  box.hidden = false;
  box.innerHTML = "<strong>模型假设</strong>";
  const ul = document.createElement("ul");
  for (const item of assumptions) {
    const li = document.createElement("li");
    li.textContent = item;
    ul.append(li);
  }
  box.append(ul);
}

function renderEvidence(evidence) {
  const box = $("evidence-box");
  if (!evidence || evidence.length === 0) {
    box.hidden = true;
    return;
  }
  box.hidden = false;
  const table = document.createElement("table");
  for (const item of evidence) {
    const tr = document.createElement("tr");
    const path = document.createElement("td");
    path.textContent = item.path;
    const excerpt = document.createElement("td");
    excerpt.textContent = item.excerpt;
    tr.append(path, excerpt);
    table.append(tr);
  }
  $("evidence").replaceChildren(table);
}

function renderResult(result, inputs, schema) {
  const artifact = result && result.artifact;
  $("result-panel").hidden = !artifact && !inputs.length;
  $("out-template").textContent = artifact ? artifact.ttp_template : "（尚无模板）";
  $("out-schema").textContent = JSON.stringify(
    artifact ? artifact.result_schema : schema, null, 2,
  ) || "（尚无 Schema）";
  $("out-inputs").textContent = inputs.join("\n\n───────────────\n\n");
  renderRecords(artifact ? artifact.records : []);
}

function renderRecords(records) {
  const host = $("out-records");
  host.innerHTML = "";
  if (!records || records.length === 0) {
    host.innerHTML = '<p class="empty">（尚无 records）</p>';
    return;
  }
  records.forEach((record, index) => {
    const group = document.createElement("div");
    group.className = "record-group";
    if (records.length > 1) {
      const heading = document.createElement("h4");
      heading.textContent = `输入 #${index}`;
      group.append(heading);
    }
    group.append(renderValue(record));
    host.append(group);
  });
}

/** Render arrays of flat objects as tables; fall back to JSON otherwise. */
function renderValue(value) {
  if (Array.isArray(value) && value.length > 0 && value.every(isFlatObject)) {
    const columns = [...new Set(value.flatMap((row) => Object.keys(row)))];
    const wrap = document.createElement("div");
    wrap.className = "table-wrap";
    const table = document.createElement("table");
    table.className = "records";
    const head = document.createElement("tr");
    for (const column of columns) {
      const th = document.createElement("th");
      th.textContent = column;
      head.append(th);
    }
    table.append(head);
    for (const row of value) {
      const tr = document.createElement("tr");
      for (const column of columns) {
        const td = document.createElement("td");
        td.textContent = row[column] === undefined ? "—" : String(row[column]);
        tr.append(td);
      }
      table.append(tr);
    }
    wrap.append(table);
    return wrap;
  }
  if (value && typeof value === "object" && !Array.isArray(value)) {
    const container = document.createElement("div");
    for (const [key, item] of Object.entries(value)) {
      const block = document.createElement("div");
      block.className = "record-group";
      const heading = document.createElement("h4");
      heading.textContent = key;
      block.append(heading, renderValue(item));
      container.append(block);
    }
    return container;
  }
  const pre = document.createElement("pre");
  pre.className = "code";
  pre.textContent = JSON.stringify(value, null, 2);
  return pre;
}

const isFlatObject = (value) =>
  value && typeof value === "object" && !Array.isArray(value) &&
  Object.values(value).every((item) => item === null || typeof item !== "object");

function renderIssues(issues) {
  $("issues-panel").hidden = !issues || issues.length === 0;
  const list = $("issues");
  list.innerHTML = "";
  for (const issue of issues || []) {
    const li = document.createElement("li");
    if (issue.severity === "warning") li.className = "warning";
    const code = document.createElement("code");
    code.textContent = issue.code;
    li.append(code, document.createTextNode(` ${issue.message}`));
    list.append(li);
  }
}

/* ---------- live progress ---------- */

function openStream(runId) {
  closeStream();
  const events = [];
  const stream = new EventSource(`/api/runs/${runId}/events`);
  state.stream = stream;
  stream.onmessage = (message) => {
    const event = JSON.parse(message.data);
    events.push(event);
    $("progress").hidden = false;
    renderLog(events);
    $("progress-elapsed").textContent = `${(event.elapsed_seconds ?? 0).toFixed(1)}s`;
    if (event.type === "run.finished") {
      closeStream();
      refreshRun().then(loadHistory);
    }
  };
  stream.onerror = () => closeStream();
}

function closeStream() {
  if (state.stream) {
    state.stream.close();
    state.stream = null;
  }
}

/* ---------- actions ---------- */

$("new-run").onclick = showNew;
$("refresh").onclick = loadHistory;
$("add-output").onclick = () => addOutput();

$("start").onclick = async () => {
  const outputs = readOutputs();
  const error = $("new-error");
  error.hidden = true;
  if (outputs.length === 0) {
    error.textContent = "至少需要一份非空的命令输出。";
    error.hidden = false;
    return;
  }
  const mode = document.querySelector('input[name="mode"]:checked').value;
  $("start").disabled = true;
  try {
    const created = await api("/api/runs", {
      method: "POST",
      body: JSON.stringify({ mode, title: $("title").value, command_outputs: outputs }),
    });
    await openRun(created.run_id);
    openStream(created.run_id);
  } catch (failure) {
    error.textContent = failure.message;
    error.hidden = false;
  } finally {
    $("start").disabled = false;
  }
};

$("cancel").onclick = async () => {
  await api(`/api/runs/${state.runId}/cancel`, { method: "POST" });
  await refreshRun();
  await loadHistory();
};

$("delete").onclick = async () => {
  if (!confirm("删除这次运行的全部文件？")) return;
  await api(`/api/runs/${state.runId}`, { method: "DELETE" });
  showNew();
};

$("format-schema").onclick = () => {
  try {
    $("schema-editor").value = JSON.stringify(JSON.parse($("schema-editor").value), null, 2);
    $("schema-error").hidden = true;
  } catch (failure) {
    showSchemaError(`JSON 语法错误：${failure.message}`);
  }
};

async function saveSchema() {
  const error = $("schema-error");
  error.hidden = true;
  let parsed;
  try {
    parsed = JSON.parse($("schema-editor").value);
  } catch (failure) {
    showSchemaError(`JSON 语法错误：${failure.message}`);
    return false;
  }
  const response = await api(`/api/runs/${state.runId}/schema`, {
    method: "PUT",
    body: JSON.stringify({ result_schema: parsed }),
  });
  if (!response.saved) {
    showSchemaError(
      "Schema 未通过校验：\n" +
        response.issues.map((issue) => `· ${issue.code} ${issue.message}`).join("\n"),
    );
    return false;
  }
  return true;
}

function showSchemaError(text) {
  const error = $("schema-error");
  error.textContent = text;
  error.hidden = false;
}

$("save-schema").onclick = async () => {
  if (await saveSchema()) showSchemaError("已保存。");
};

$("run-template").onclick = async () => {
  if (!(await saveSchema())) return;
  $("run-template").disabled = true;
  try {
    await api(`/api/runs/${state.runId}/generate`, { method: "POST" });
    await refreshRun();
    openStream(state.runId);
  } catch (failure) {
    showSchemaError(failure.message);
  } finally {
    $("run-template").disabled = false;
  }
};

document.querySelectorAll(".tab").forEach((tab) => {
  tab.onclick = () => {
    document.querySelectorAll(".tab").forEach((other) => other.classList.remove("is-active"));
    tab.classList.add("is-active");
    state.activeTab = tab.dataset.tab;
    for (const name of ["template", "records", "schema", "inputs"]) {
      $(`tab-${name}`).hidden = name !== state.activeTab;
    }
  };
});

document.querySelectorAll(".copy").forEach((button) => {
  button.onclick = async () => {
    const text = $(`out-${button.dataset.copy}`).textContent;
    await navigator.clipboard.writeText(text);
    const original = button.textContent;
    button.textContent = "已复制";
    setTimeout(() => { button.textContent = original; }, 1200);
  };
});

addOutput();
loadHistory();
