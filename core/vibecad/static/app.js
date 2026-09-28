import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { createSketchEditor, setSketchTheme, TOOLS as SKETCH_TOOLS, CONSTRAINTS as SKETCH_CONSTRAINTS } from "./sketch.js";
import { ICON } from "./icons.js";

const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const fmt = (v, d = 2) => {
  if (v == null) return "–";
  const t = (+v).toFixed(d);
  return t.includes(".") ? t.replace(/\.?0+$/, "") : t;  // strip zeros after the decimal point only
};

let S = null;          // part state from the server
let selected = null;   // selected feature id
let busy = false;
let runStart = null, runTimer = null, toolCount = 0, phase = null;
const toolRows = {}, lastToolByName = {};

const ICONS = {
  sketch: '<path d="M4 20h16"/><path d="M14.5 4.5l5 5L10 19H5v-5z"/>',
  extrude: '<path d="M4 15l8 4 8-4-8-4z"/><path d="M12 11V3"/><path d="M9 6l3-3 3 3"/>',
  revolve: '<ellipse cx="12" cy="13" rx="8" ry="3.5"/><path d="M12 3v17"/><path d="M17 7.5l2.5.5-.5 2.5"/>',
  fillet: '<path d="M5 20v-8a7 7 0 0 1 7-7h7"/>',
  chamfer: '<path d="M5 20v-9l6-6h8"/>',
  shell: '<rect x="3.5" y="5" width="17" height="15" rx="1"/><path d="M7.5 20V9h9v11"/>',
  linear_pattern: '<rect x="2.5" y="9" width="5" height="6"/><rect x="9.5" y="9" width="5" height="6"/><rect x="16.5" y="9" width="5" height="6"/>',
  circular_pattern: '<circle cx="12" cy="12" r="7.5"/><circle cx="12" cy="4.5" r="1.6"/><circle cx="18.5" cy="15.8" r="1.6"/><circle cx="5.5" cy="15.8" r="1.6"/>',
  mirror: '<path d="M12 3v18" stroke-dasharray="2 2"/><path d="M9 7l-6 5 6 5z"/><path d="M15 7l6 5-6 5z"/>',
  import: '<path d="M4 15v4a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-4"/><path d="M12 3v11M7.5 9.5L12 14l4.5-4.5"/>',
  hole: '<ellipse cx="12" cy="6.5" rx="6" ry="2.5"/><path d="M6 6.5V17M18 6.5V17"/><path d="M6 17a6 2.5 0 0 0 12 0"/>',
  text: '<path d="M5 6V4h14v2M12 4v16M9 20h6"/>',
  boolean: '<rect x="3" y="9" width="18" height="11" rx="1.5"/><path d="M8 9V6.5a4 4 0 0 1 8 0V9" stroke-dasharray="2 1.6"/><path d="M9 9v3a3 3 0 0 0 6 0V9"/>',
  loft: '<path d="M4 20h10l6-4H10z"/><ellipse cx="13" cy="5.5" rx="4" ry="2"/><path d="M6.5 18.5L9 5.5M17.5 17L17 5.5"/>',
  sweep: '<circle cx="6" cy="18.5" r="2.5"/><path d="M8.5 18.5V12a5 5 0 0 1 5-5H21M3.5 18.5V12a10 10 0 0 1 10-10H21"/>',
};
const icon = (t) => `<svg viewBox="0 0 24 24">${ICONS[t] || '<circle cx="12" cy="12" r="6"/>'}</svg>`;

// ── server ────────────────────────────────────────────────────────
async function api(path, body) {
  const r = await fetch(path, body === undefined ? {} : { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body) });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) {
    note(`Error: ${j.detail || r.statusText}`, "err");
    throw Object.assign(new Error(j.detail || r.statusText), { shown: true });
  }
  return j;
}
// errors api() already showed in the log need no further handling by every button handler
window.addEventListener("unhandledrejection", (ev) => { if (ev.reason?.shown) ev.preventDefault(); });
let sock;
function connect() {
  sock = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
  sock.onopen = () => { $("#conn").textContent = "connected"; $("#conn").className = "conn live"; };
  sock.onclose = () => { $("#conn").textContent = "reconnecting…"; $("#conn").className = "conn"; setTimeout(connect, 1500); };
  sock.onmessage = (m) => onEvent(JSON.parse(m.data));
}
const send = (o) => sock.send(JSON.stringify(o));

function onEvent(e) {
  switch (e.type) {
    case "hello":
      $("#model").value = e.model; if (e.effort) $("#effort").value = e.effort;
      resetLog();
      e.transcript.forEach(onEvent);
      setBusy(e.busy);
      if (e.state) setState(e.state, { fit: true });
      loadParts();
      break;
    case "part_changed": {
      const fresh = e.state && (!S || S.path !== e.state.path);
      if (fresh) { exitSketch(); selected = null; }
      setState(e.state, { fit: fresh, flash: e.author === "agent" });
      if (fresh) loadParts();
      break;
    }
    case "user_prompt": addUser(e.text, e.selection, e.scope, e.entities, e.face, e.marks, e.attachments); break;
    case "run_start": setBusy(true, e.t); break;
    case "agent_text": addAgent(e.text); break;
    case "agent_thinking": addThinking(e.text); break;
    case "agent_phase": phase = e.phase ? e : null; updateLive(); break;
    case "tool_call": addTool(e); break;
    case "tool_result": toolResult(e); break;
    case "tool_image": toolImage(e); break;
    case "run_done": phase = null; setBusy(false); showMetrics(e.metrics); break;
    case "note": note(e.text, "note"); break;
    case "error": note(e.message, "err"); setBusy(false); break;
    case "conversation_reset": resetLog(); break;
    case "conversation":  // another part was opened: show its own conversation
      resetLog();
      e.transcript.forEach(onEvent);
      setBusy(false);
      if (e.transcript.length) note(`conversation for ${e.part}`, "note");
      break;
    case "regen": onRegen(e); break;
  }
}

// ── part state ────────────────────────────────────────────────────
const rollIndex = () => (S && S.rollback != null ? S.rollback : S ? S.features.length : 0);

function setState(state, { fit = false, flash = false } = {}) {
  const before = S ? new Map(S.features.map((f) => [f.id, f.status + (f.warnings || []).join()])) : null;
  S = state;
  welcomeUpdate();
  if (!S) { $("#tree").innerHTML = ""; return; }
  $("#partName").textContent = S.rel;
  const bb = S.bbox ? S.bbox.map((v) => fmt(v, 1)).join(" × ") : "–";
  const hasErr = S.features.some((f) => f.status === "error"), hasWarn = S.features.some((f) => f.warnings?.length);
  $("#partStatus").innerHTML = `${hasErr ? '<span class="err">errors</span>' : hasWarn ? '<span class="warn">warnings</span>' : '<span class="ok">ok</span>'}
     · ${fmt(S.volume, 1)} mm³ · ${bb} mm${S.rollback != null ? ' · <span class="roll">rolled back</span>' : ""}`;
  $("#undoBtn").disabled = !S.can_undo;
  $("#redoBtn").disabled = !S.can_redo;
  if (selected && !S.features.some((f) => f.id === selected)) { selected = null; exitSketch(); }
  const rb = $("#rollbackNote");
  rb.hidden = S.rollback == null;
  if (S.rollback != null) rb.textContent = `Showing the part up to ${S.features[S.rollback - 1]?.id ?? "the start"} (${S.rollback} of ${S.features.length} features). Drag the bar to the bottom to see all.`;
  renderTree(flash ? before : null);
  modelToolsUpdate();
  renderParams();
  renderDetails();
  lastMesh = loadMesh(fit);
  if (inSketch()) SK.refresh();
}

let expanded = new Set();  // features whose parameters are shown in the tree
try { expanded = new Set(JSON.parse(localStorage.getItem("vibecad.expanded") || "[]")); } catch {}
const saveExpanded = () => { try { localStorage.setItem("vibecad.expanded", JSON.stringify([...expanded])); } catch {} };
const isBare = (e) => typeof e === "string" && /^[A-Za-z_]\w*$/.test(e.trim());
// a feature's numbers, as the rows its tree entry expands to: its own params, key fields, sketch dimensions
function featureRows(f) {
  const P = Object.fromEntries(S.params.map((p) => [p.name, p])), rows = [], shown = new Set();
  const viaParam = (label, name, what) => {
    shown.add(name);
    const p = P[name];
    rows.push({ label, expr: p.expr, value: p.value, param: name, shared: p.users.length > 1 ? p.users : null, what,
      ops: (v) => [{ op: "set_param", name, value: v }] });
  };
  for (const fl of f.fields || []) {
    if (isBare(fl.expr) && P[fl.expr]) viaParam(fl.key, fl.expr.trim(), fl.key);
    else rows.push({ label: fl.key, expr: String(fl.expr), value: fl.value, what: fl.key,
      ops: (v) => [{ op: "update_feature", id: f.id, set: { [fl.key]: numOrExpr(v) } }] });
  }
  for (const d of f.dims || []) {
    if (isBare(d.expr) && P[d.expr]) { if (!shown.has(d.expr.trim())) viaParam(d.name, d.expr.trim(), "dimension"); }
    else rows.push({ label: d.name, expr: String(d.expr), value: d.value, what: "dimension",
      ops: (v) => [{ op: "set_dimension", sketch: f.id, name: d.name, value: numOrExpr(v) }] });
  }
  for (const name of f.params || []) if (!shown.has(name)) viaParam(name, name, "parameter");
  return rows;
}
function paramRow(r, fid) {
  const li = document.createElement("li");
  li.className = "fparam" + (r.shared ? " shared" : "");
  const tag = r.param ? (r.param === r.label ? "ƒ" : `ƒ ${r.param}`) : "";
  li.title = r.param ? (r.shared ? `drives parameter ${r.param}, shared by ${r.shared.join(", ")}` : `parameter ${r.param} (only ${fid} uses it)`)
    : `${r.what} of ${fid}, written into the feature`;
  li.innerHTML = `<span class="pl">${esc(r.label)}${tag ? ` <i>${esc(tag)}</i>` : ""}</span><input value="${esc(r.expr)}"><span class="val">${fmt(r.value, 3)}</span>`;
  if (r.param) li.dataset.param = r.param;
  const inp = li.querySelector("input");
  inp.onclick = (ev) => ev.stopPropagation();
  inp.onkeydown = (ev) => { if (ev.key === "Enter") inp.blur(); if (ev.key === "Escape") { inp.value = r.expr; inp.blur(); } };
  inp.onchange = async () => {
    const v = inp.value.trim();
    if (!v || !(await edit(r.ops(v), `set ${fid} ${r.label} = ${v}`))) inp.value = r.expr;
  };
  return li;
}
function renderTree(before) {
  const ol = $("#tree");
  ol.innerHTML = "";
  const n = S.features.length, rb = rollIndex();
  S.features.forEach((f, i) => {
    if (i === rb) ol.appendChild(rollbar());
    const li = document.createElement("li");
    const st = f.status === "error" ? "error" : f.warnings?.length ? "warn" : f.status === "suppressed" ? "suppressed" : "ok";
    li.className = `feat ${st}` + (f.id === selected ? " selected" : "") + (i >= rb ? " rolled" : "");
    const meta = f.status === "error" ? "error" : f.warnings?.length ? "warning" : f.status === "suppressed" ? "suppressed"
      : f.type === "sketch" ? `${f.dof ?? "?"} DOF` : "";
    const rows = featureRows(f), open = expanded.has(f.id) && rows.length;
    const isRef = f.type === "import" && f.mode === "reference";
    li.innerHTML = `<span class="caret" title="${rows.length ? `${open ? "Hide" : "Show"} this feature's parameters` : ""}">${rows.length ? (open ? "▾" : "▸") : ""}</span>`
      + `<span class="ico">${icon(f.type)}</span><span class="fid" title="${esc(f.type)}: ${esc(f.intent || "")}">${esc(f.id)}`
      + (isRef ? `<span class="refsw" style="background:#${refColor(f.id).getHexString()}" title="reference body: shown ghosted, never part of the solid"></span>` : "")
      + `</span><span class="meta ${st}">${esc(meta)}`
      + (isRef ? `<button class="eye ${hiddenRefs.has(f.id) ? "off" : ""}" title="${hiddenRefs.has(f.id) ? "Show" : "Hide"} this reference body">${ICON[hiddenRefs.has(f.id) ? "eyeoff" : "eye"]}</button>` : "")
      + `</span>`
      + (f.status === "error" ? `<span class="err-msg">${esc(f.message)}</span>` : "")
      + (f.warnings || []).map((w) => `<span class="warn-msg">${esc(w)}</span>`).join("");
    if (before && before.get(f.id) !== f.status + (f.warnings || []).join()) li.classList.add("flash");
    li.dataset.index = i;
    li.dataset.id = f.id;
    li.onclick = (ev) => { if (ev.detail > 1) return; select(f.id === selected ? null : f.id); };
    if (f.type === "fillet" || f.type === "chamfer") {  // double-click: change which edges it rounds
      li.title = "Double-click to change which edges it acts on";
    }
    const eye = li.querySelector(".eye");
    if (eye) eye.onclick = (ev) => {
      ev.stopPropagation();
      hiddenRefs.has(f.id) ? hiddenRefs.delete(f.id) : hiddenRefs.add(f.id);
      for (const m of refMeshes) if (m.userData.ref === f.id) m.visible = !hiddenRefs.has(f.id);
      for (const o of fitGroup.children) if (o.userData.ref === f.id) o.visible = !hiddenRefs.has(f.id);
      renderTree(null);
    };
    const caret = li.querySelector(".caret");
    if (rows.length) caret.onclick = (ev) => {
      ev.stopPropagation();
      expanded.has(f.id) ? expanded.delete(f.id) : expanded.add(f.id);
      saveExpanded();
      renderTree(null);
    };
    ol.appendChild(li);
    if (open) {
      const sub = document.createElement("ol");
      sub.className = "fparams" + (i >= rb ? " rolled" : "");
      sub.dataset.feature = f.id;
      for (const r of rows) sub.appendChild(paramRow(r, f.id));
      const wrap = Object.assign(document.createElement("li"), { className: "fparams-wrap" });
      wrap.appendChild(sub);
      ol.appendChild(wrap);
    }
  });
  if (rb >= n) ol.appendChild(rollbar());
  ol.querySelector("li.feat.selected")?.scrollIntoView({ block: "nearest" });  // picked in the view: show it in the tree
}

// the splitter between the tree and the parameters / details: drag to resize, remembered per browser
(function splitter() {
  const sp = $("#splitter"), left = $("#left");
  try { const h = localStorage.getItem("vibecad.treeH"); if (h) left.style.setProperty("--tree-h", h); } catch {}
  sp.onpointerdown = (ev) => {
    ev.preventDefault();
    sp.setPointerCapture(ev.pointerId);
    sp.classList.add("dragging");
    const top = $("#treePane").getBoundingClientRect().top, total = left.getBoundingClientRect().bottom - top;
    sp.onpointermove = (mv) => left.style.setProperty("--tree-h", `${Math.max(12, Math.min(85, ((mv.clientY - top) / total) * 100)).toFixed(1)}%`);
    sp.onpointerup = () => {
      sp.onpointermove = sp.onpointerup = null;
      sp.classList.remove("dragging");
      try { localStorage.setItem("vibecad.treeH", left.style.getPropertyValue("--tree-h")); } catch {}
    };
  };
})();

// double-click a feature to edit its settings (on the list: the first click re-renders the row). Fillets and
// chamfers have their own double-click: picking their edges
$("#tree").addEventListener("dblclick", (ev) => {
  const li = ev.target.closest("li.feat"), f = li && S?.features.find((x) => x.id === li.dataset.id);
  if (!f || !EDITABLE.includes(f.type)) return;
  ev.preventDefault();
  if (selected !== f.id) select(f.id);
  if (f.type === "fillet" || f.type === "chamfer") {  // double-click: change which edges it acts on
    if (S.features.indexOf(f) < rollIndex() || S.rollback == null) startEdgeEdit(f.id);
    return;
  }
  editFeature(f.id, $(`#tree li.feat[data-id="${f.id}"]`) || li);
});
function rollbar() {
  const bar = document.createElement("li");
  bar.className = "rollbar";
  bar.title = "Rollback bar: drag to show the part at an earlier point in the tree";
  bar.onpointerdown = (ev) => {
    ev.preventDefault();
    bar.setPointerCapture(ev.pointerId);
    bar.classList.add("dragging");
    const hint = Object.assign(document.createElement("li"), { className: "drop-hint" });
    let target = rollIndex();
    const feats = [...document.querySelectorAll("#tree li.feat")];
    bar.onpointermove = (mv) => {
      target = feats.length;
      for (const li of feats) {
        const r = li.getBoundingClientRect();
        if (mv.clientY < r.top + r.height / 2) { target = +li.dataset.index; break; }
      }
      hint.remove();
      if (target < feats.length) feats[target].before(hint); else feats[feats.length - 1]?.after(hint);
    };
    bar.onpointerup = async () => {
      bar.onpointermove = bar.onpointerup = null;
      hint.remove();
      bar.classList.remove("dragging");
      const r = await api("/api/rollback", { index: target >= S.features.length ? null : target });
      setState(r.state);
    };
  };
  return bar;
}

// global parameters (shared by several features, or not used yet) first, then the ones only one feature uses
function renderParams() {
  const tb = $("#params tbody");
  tb.innerHTML = "";
  const row = (p) => {
    const tr = document.createElement("tr");
    const own = p.users.length === 1 ? p.users[0] : null;
    tr.dataset.param = p.name;
    tr.title = own ? `only ${own} uses it` : p.users.length ? `used by ${p.users.join(", ")}` : "not used by any feature yet";
    tr.innerHTML = `<td>${esc(p.name)}${own ? `<span class="owner">${esc(own)}</span>` : ""}</td><td><input value="${esc(p.expr)}"></td><td class="val">${fmt(p.value, 3)}</td>`;
    const inp = tr.querySelector("input");
    inp.onkeydown = (ev) => { if (ev.key === "Enter") inp.blur(); if (ev.key === "Escape") { inp.value = p.expr; inp.blur(); } };
    inp.onchange = async () => {
      if (!(await edit([{ op: "set_param", name: p.name, value: inp.value }], `set ${p.name} = ${inp.value}`))) inp.value = p.expr;
    };
    tb.appendChild(tr);
  };
  const global = S.params.filter((p) => p.users.length !== 1), local = S.params.filter((p) => p.users.length === 1);
  global.forEach(row);
  if (local.length) {
    const g = document.createElement("tr");
    g.className = "grp";
    g.innerHTML = `<td colspan="3">Feature parameters <span class="muted">(each used by one feature; also under ▸ in the tree)</span></td>`;
    tb.appendChild(g);
    local.forEach(row);
  }
}

let detailsToken = 0;  // only the newest render may build the panel (several can be in flight after an edit)
async function renderDetails() {
  const tok = ++detailsToken;
  const d = $("#details");
  $("#selName").textContent = selected || "";
  if (!selected) { d.innerHTML = "Click a feature in the tree or a face in the view."; d.className = "muted small"; return; }
  const f = S.features.find((x) => x.id === selected);
  if (!f) return;
  const json = await api(`/api/feature/${encodeURIComponent(selected)}`);
  if (tok !== detailsToken || json.id !== selected) return;  // a newer render or selection took over
  const i = S.features.indexOf(f), off = f.status === "suppressed";
  d.className = "";
  d.innerHTML = `<div class="factions">
      <button data-a="rename" title="Rename; every reference to it is updated">Rename</button>
      <button data-a="suppress" title="${off ? "Build this feature again" : "Skip this feature when building (keeps it in the tree)"}">${off ? "Unsuppress" : "Suppress"}</button>
      <button data-a="up" title="Move up the tree" ${i === 0 ? "disabled" : ""}>↑</button>
      <button data-a="down" title="Move down the tree" ${i === S.features.length - 1 ? "disabled" : ""}>↓</button>
      <span class="grow"></span><button data-a="delete" class="danger" title="Delete this feature (undo brings it back)">Delete</button></div>
    ${EDITABLE.includes(f.type) ? `<button class="editbtn" data-a="edit" title="Change this ${f.type.replace("_", " ")}'s settings (or double-click it in the tree)">Edit ${esc(f.type.replace("_", " "))}…</button>` : ""}
    ${f.type === "import" && f.mode === "reference" && S.volume != null ? `<button class="editbtn" data-a="nest" title="Cut this body out of the part, grown by a clearance: a nest, cradle or case for it">Cut a nest for it…</button>` : ""}
    <div class="intent small" title="Click to edit: one line on why this feature exists">${esc(f.intent || "No intent written. Click to add one.")}</div>
    ${f.type === "fillet" || f.type === "chamfer" ? `<div class="edgelist"><div class="row"><b>Edges</b><span class="grow"></span>
      <button data-a="edges" title="Show the part just before this ${f.type} and click edges to add or remove them">Edit edges…</button></div>
      ${(json.edges || []).map((r) => `<span class="e" title="${esc(JSON.stringify(r))}">${esc(refText(r))}</span>`).join("")}</div>` : ""}
    <details class="json"><summary>JSON</summary><textarea spellcheck="false"></textarea>
    <div class="row"><button id="applyFeat">Apply edit</button></div></details>
    <div class="row"><span class="grow"></span><button id="askAbout" title="Ask the agent about this feature">Ask agent</button></div>`;
  d.querySelector("textarea").value = JSON.stringify(json, null, 1);
  const act = {
    rename: async () => {
      const to = prompt(`Rename ${f.id} to (letters, digits, underscores):`, f.id);
      if (!to || to === f.id) return;
      const wasSketch = inSketch() && SK.active() === f.id;
      if (await edit([{ op: "rename_feature", id: f.id, to }], `rename ${f.id} to ${to}`)) {
        if (wasSketch) exitSketch();
        select(to);
      }
    },
    suppress: () => edit([{ op: "update_feature", id: f.id, set: { suppressed: !off } }], `${off ? "unsuppress" : "suppress"} ${f.id}`),
    up: () => {  // positions from the tree as it is now, not as it was when the panel was drawn
      const k = S.features.findIndex((x) => x.id === f.id);
      if (k > 0) edit([{ op: "move_feature", id: f.id, before: S.features[k - 1].id }], `move ${f.id} up`);
    },
    down: () => {
      const k = S.features.findIndex((x) => x.id === f.id);
      if (k >= 0 && k < S.features.length - 1) edit([{ op: "move_feature", id: f.id, after: S.features[k + 1].id }], `move ${f.id} down`);
    },
    delete: async () => {
      if (await edit([{ op: "remove_feature", id: f.id }], `delete ${f.id}`)) select(null);
    },
    edges: () => startEdgeEdit(f.id),
    edit: () => editFeature(f.id, d.querySelector(".editbtn")),
    nest: () => combineForm(f.id, d.querySelector("[data-a=nest]")),
  };
  d.querySelectorAll(".factions [data-a], .edgelist [data-a], .editbtn[data-a]").forEach((b) => (b.onclick = act[b.dataset.a]));
  const intent = d.querySelector(".intent");
  intent.onclick = () => {
    const inp = Object.assign(document.createElement("input"), { value: f.intent || "", placeholder: "why this feature exists", className: "intent-edit" });
    intent.replaceWith(inp);
    inp.focus();
    let done = false;
    const finish = (save) => {
      if (done) return;
      done = true;
      if (save && inp.value.trim() !== (f.intent || "")) edit([{ op: "update_feature", id: f.id, set: { intent: inp.value.trim() || null } }], `intent of ${f.id}`);
      else renderDetails();
    };
    inp.onkeydown = (ev) => { if (ev.key === "Enter") finish(true); if (ev.key === "Escape") finish(false); };
    inp.onblur = () => finish(true);
  };
  d.querySelector("#applyFeat").onclick = () => {
    let obj;
    try { obj = JSON.parse(d.querySelector("textarea").value); } catch (err) { note(`JSON error: ${err.message}`, "err"); return; }
    const set = {};
    for (const k of new Set([...Object.keys(obj), ...Object.keys(json)])) {
      if (k === "id" || k === "type") continue;
      if (JSON.stringify(obj[k]) !== JSON.stringify(json[k])) set[k] = obj[k] ?? null;
    }
    if (Object.keys(set).length) edit([{ op: "update_feature", id: selected, set }], `edit ${selected} in GUI`);
  };
  d.querySelector("#askAbout").onclick = () => { $("#prompt").focus(); $("#prompt").dataset.placeholder = `Ask about or change ${selected}…`; };
}

function select(id) {
  selected = id;
  modelToolsUpdate();
  const f = id && S.features.find((x) => x.id === id);
  if (f && f.type === "sketch" && S.features.indexOf(f) < rollIndex()) enterSketch(id);
  else exitSketch();
  renderTree(null);
  renderDetails();
  colorFaces();
}

// ── rebuild indicator: an edit on a big part takes seconds (regenerate, then re-mesh); say what is happening ──
let lastMesh = Promise.resolve();
let editsInFlight = 0, rebuildT0 = 0, rebuildDelay = null, rebuildTick = null, rebuildPhase = "", regenInfo = null, agentRegen = false;
function rebuildShow() {
  if (rebuildTick) return;
  document.body.classList.add("rebuilding");
  $("#rebuild").hidden = false;
  rebuildTick = setInterval(rebuildText, 100);
  rebuildText();
}
function rebuildHide() {
  clearTimeout(rebuildDelay); clearInterval(rebuildTick);
  rebuildDelay = rebuildTick = null;
  document.body.classList.remove("rebuilding");
  $("#rebuild").hidden = true;
  document.querySelectorAll("#tree li.building").forEach((li) => li.classList.remove("building"));
}
function rebuildText() {
  const t = ((performance.now() - rebuildT0) / 1000).toFixed(1);
  const what = regenInfo?.feature ? `${agentRegen && !editsInFlight ? "Agent edit: rebuilding" : "Rebuilding"} ${regenInfo.feature} (${regenInfo.i + 1} of ${regenInfo.n})`
    : rebuildPhase || "Rebuilding";
  $("#rebuildText").textContent = `${what}… ${t} s`;
}
function rebuildStart(phase = "Applying edit") {
  rebuildPhase = phase;
  if (editsInFlight++ === 0) {
    regenInfo = null;
    if (!rebuildTick) { rebuildT0 = performance.now(); rebuildDelay = setTimeout(rebuildShow, 150); }  // quick edits never flash it
  }
  if (rebuildTick) rebuildText();
}
function rebuildEnd() {
  if (--editsInFlight > 0) return;
  editsInFlight = 0;
  if (!agentRegen) rebuildHide();
}
function onRegen(e) {  // the server rebuilds features one by one (for the GUI's edits and the agent's)
  if (S && e.part !== S.name) return;
  if (e.feature == null) {  // that regeneration finished; the view still has to be re-meshed
    regenInfo = null;
    if (agentRegen && !editsInFlight) { agentRegen = false; rebuildHide(); }
    else rebuildPhase = "Updating the 3D view";
    return;
  }
  regenInfo = e;
  if (!editsInFlight && !rebuildTick) { agentRegen = true; rebuildT0 = performance.now(); clearTimeout(rebuildDelay); rebuildDelay = setTimeout(rebuildShow, 300); }
  document.querySelectorAll("#tree li.building").forEach((li) => li.classList.remove("building"));
  document.querySelector(`#tree li.feat[data-id="${CSS.escape(e.feature)}"]`)?.classList.add("building");
  if (rebuildTick) rebuildText();
}
async function busyDo(phase, fn) {  // run fn with the indicator up until its result is on screen
  rebuildStart(phase);
  try { return await fn(); }
  finally { await lastMesh.catch(() => {}); rebuildEnd(); }
}

async function edit(ops, message) {  // true if the batch was applied
  return busyDo("Applying edit", async () => {
    let r;
    try { r = await api("/api/ops", { ops, message }); } catch { return false; }
    const rep = r.report;
    rebuildPhase = "Updating the 3D view";
    if (r.state) setState(r.state);
    if (!rep.applied) note(rep.error || "edit rejected", "err");
    else if (rep.errors || rep.warnings) note([...(rep.errors || []), ...(rep.warnings || [])].join("\n"), "err");
    return !!rep.applied;
  });
}

async function loadParts() {
  const r = await api("/api/parts");
  $("#partSelect").innerHTML = `<option value="">— open a part —</option>` + r.parts.map((p) => `<option ${p === r.active ? "selected" : ""}>${esc(p)}</option>`).join("");
  welcomeUpdate(r.parts);
}

// ── 3D view ───────────────────────────────────────────────────────
const host = $("#viewer");
const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
renderer.setPixelRatio(devicePixelRatio);
renderer.setClearColor(0x000000, 0);  // the viewport's CSS gradient shows through
renderer.localClippingEnabled = true;  // section view
host.appendChild(renderer.domElement);
const scene = new THREE.Scene();
const persp = new THREE.PerspectiveCamera(35, 1, 0.1, 100000);
const ortho = new THREE.OrthographicCamera(-50, 50, 50, -50, -100000, 100000);
let camera = ortho, viewRadius = 50;
for (const c of [persp, ortho]) c.up.set(0, 0, 1);
let controls = makeControls(camera);
function makeControls(cam, target) {
  const c = new OrbitControls(cam, renderer.domElement);
  c.enableDamping = true;
  if (target) c.target.copy(target);
  return c;
}
// studio lighting: sky/ground fill, a key light that follows the camera, and a soft rim from behind
scene.add(new THREE.HemisphereLight(0xffffff, 0x7d8698, 1.35));
const key = new THREE.DirectionalLight(0xffffff, 1.55);
scene.add(key);
const rim = new THREE.DirectionalLight(0xdfe8ff, 0.55);
scene.add(rim);
const partGroup = new THREE.Group();
scene.add(partGroup);
const refGroup = new THREE.Group();  // reference imports: ghosted, never part of the solid
scene.add(refGroup);
const axes = new THREE.AxesHelper(10);  // a short triad at the origin
axes.material.transparent = true; axes.material.opacity = 0.7;
scene.add(axes);
let grid = null, gridBox = null;  // ground grid under the part, resized to it on each fit
const isDark = () => document.documentElement.dataset.theme === "dark";
function setGrid(box) {
  gridBox = box;
  if (grid) { scene.remove(grid); grid.geometry.dispose(); }
  const size = box.getSize(new THREE.Vector3());
  const span = Math.max(size.x, size.y, 10) * 2.2;
  const step = 10 ** Math.floor(Math.log10(span / 8));
  const n = Math.max(2, Math.ceil(span / step / 2) * 2);
  grid = isDark() ? new THREE.GridHelper(n * step, n, 0x334052, 0x222b37) : new THREE.GridHelper(n * step, n, 0xcfd5de, 0xe2e6ec);
  grid.rotation.x = Math.PI / 2;  // three's grid lies in XZ; ours is the XY plane (Z up)
  const c = box.getCenter(new THREE.Vector3());
  grid.position.set(Math.round(c.x / step) * step, Math.round(c.y / step) * step, Math.min(box.min.z, 0) - 1e-3 * span);
  grid.material.transparent = true; grid.material.opacity = 0.8; grid.material.depthWrite = false;
  grid.renderOrder = -1;
  scene.add(grid);
}
const BASE = new THREE.Color(0xb7c2d1), HI = new THREE.Color(0xf08a24), HOVER = new THREE.Color(0x7aa2e8), PICKED = new THREE.Color(0xc2410c);
let pickedFaces = [];  // faces clicked in the 3D view, shift-click adds: {mesh, labels, point}
const lastPicked = () => pickedFaces.at(-1) || null;
let faceMeshes = [], edgeObjs = [], refMeshes = [], meshRev = null, hovered = null, hoveredEdge = null;
// reference bodies each get their own colour (in tree order), so several imports stay apart in the view and the tree
const REF_PALETTE = [0x8b5cf6, 0x0ea5a4, 0xf59e0b, 0xec4899, 0x3b82f6, 0x84cc16].map((c) => new THREE.Color(c));
const refIds = () => (S ? S.features.filter((f) => f.type === "import" && f.mode === "reference").map((f) => f.id) : []);
const refColor = (id) => REF_PALETTE[Math.max(0, refIds().indexOf(id)) % REF_PALETTE.length];
const hiddenRefs = new Set();
const clipPlanes = [];  // the section view's plane, when on (shared by every part material)
let pickedEdges = [];  // edges clicked in the 3D view: {i, ref, label, point}; fillet/chamfer act on them
const EDGE = new THREE.Color(0x1b1f27), EDGE_HOVER = new THREE.Color(0x2f6fe0), EDGE_PICKED = new THREE.Color(0xea580c);
const edgeMarks = new THREE.Group();  // thick tubes over hovered and picked edges (WebGL lines are 1px)
scene.add(edgeMarks);

function resize() {
  const w = host.clientWidth, h = host.clientHeight;
  if (!w || !h) return;
  renderer.setSize(w, h);
  persp.aspect = w / h; persp.updateProjectionMatrix();
  setOrthoFrustum();
}
function setOrthoFrustum() {
  const w = host.clientWidth || 1, h = host.clientHeight || 1, a = w / h, s = viewRadius * 1.15;
  Object.assign(ortho, { left: -s * a, right: s * a, top: s, bottom: -s });
  ortho.updateProjectionMatrix();
}
new ResizeObserver(resize).observe(host);
function loop() {
  requestAnimationFrame(loop);
  controls.update();
  key.position.copy(camera.position);
  rim.position.copy(controls.target).sub(camera.position).add(controls.target).add(new THREE.Vector3(0, 0, viewRadius));
  renderer.render(scene, camera);
  drawGizmo();
  SK.placeLabels();
}

$("#projBtn").onclick = () => {
  const next = camera === ortho ? persp : ortho;
  // keep the same view direction and distance
  next.position.copy(camera.position); next.up.copy(camera.up);
  const t = controls.target.clone();
  controls.dispose();
  camera = next;
  controls = makeControls(camera, t);
  if (inSketch()) controls.mouseButtons.LEFT = null;
  $("#projBtn").textContent = camera === ortho ? "Orthographic" : "Perspective";
  resize();
};

let pendingFit = false;  // a new part is empty when first shown: fit once its first solid arrives
let meshReq = null;  // the mesh request in flight: a second request for the same revision waits for it
async function loadMesh(fit) {
  if (!S) return;
  if (!fit && meshRev === S.rev) return;
  fit = fit || pendingFit;
  if (meshReq && meshReq.rev === S.rev) {
    await meshReq.p.catch(() => {});
    if (fit) pendingFit = !fitView("iso");
    return;
  }
  const req = { rev: S.rev, p: api("/api/mesh") };
  meshReq = req;
  let m;
  try { m = await req.p; } finally { if (meshReq === req) meshReq = null; }
  if (S && m.rev !== S.rev && meshReq) return;  // an older revision arrived while a newer one is loading
  meshRev = m.rev;
  if (fitOn) setTimeout(runFit, 0);  // the part or a reference changed: check the fit again
  partGroup.clear();
  faceMeshes = [];
  for (const f of m.faces) {
    const g = new THREE.BufferGeometry();
    g.setAttribute("position", new THREE.Float32BufferAttribute(f.p, 3));
    g.setIndex(f.i);
    g.computeVertexNormals();
    const mat = new THREE.MeshStandardMaterial({ color: BASE.clone(), metalness: 0.12, roughness: 0.52, side: THREE.DoubleSide,
      polygonOffset: true, polygonOffsetFactor: 1, polygonOffsetUnits: 1, clippingPlanes: clipPlanes });
    const mesh = new THREE.Mesh(g, mat);
    mesh.userData = { features: f.features, labels: f.labels };
    partGroup.add(mesh);
    faceMeshes.push(mesh);
  }
  refGroup.clear();
  refMeshes = [];
  for (const rb of m.refs || []) {
    for (const f of rb.faces) {
      const g = new THREE.BufferGeometry();
      g.setAttribute("position", new THREE.Float32BufferAttribute(f.p, 3));
      g.setIndex(f.i);
      g.computeVertexNormals();
      const mesh = new THREE.Mesh(g, new THREE.MeshStandardMaterial({ color: refColor(rb.id).clone(), transparent: true, opacity: 0.32,
        depthWrite: false, side: THREE.DoubleSide, roughness: 0.8, clippingPlanes: clipPlanes }));
      mesh.userData = { features: f.features, labels: f.labels, ref: rb.id };
      mesh.visible = !hiddenRefs.has(rb.id);
      mesh.renderOrder = 2;
      refGroup.add(mesh);
      refMeshes.push(mesh);
    }
  }
  edgeObjs = [];
  m.edges.forEach((e, i) => {  // one pickable line per edge; the index is the server's edge index
    if (m.edge_seam?.[i]) return;
    const g = new THREE.BufferGeometry();
    g.setAttribute("position", new THREE.Float32BufferAttribute(e, 3));
    const line = new THREE.Line(g, new THREE.LineBasicMaterial({ color: EDGE.clone(), transparent: true, opacity: 0.85, clippingPlanes: clipPlanes }));
    line.userData = { i };
    partGroup.add(line);
    edgeObjs.push(line);
  });
  // edge indices are only stable for the same body: keep picks whose edge sits where it did
  pickedEdges = pickedEdges.filter((p) => { const o = edgeObjs.find((x) => x.userData.i === p.i); return o && nearLine(o, p.point); });
  hoveredEdge = null;
  pickedFaces = pickedFaces.map((p) => {  // the new mesh has new face objects: keep picks whose labelled face still exists
    const same = [...faceMeshes, ...refMeshes].find((x) => x.userData.labels.join() === p.labels.join());
    return same && { ...p, mesh: same };
  }).filter(Boolean);
  pickUpdate();
  colorFaces();
  if (inSketch() || fitOn) ghostPart(true);
  if (fit) pendingFit = !fitView("iso");
  if (section) applySection();
  if (measuring) {  // the new mesh has new face objects: keep face picks whose label still exists (edge indices may change)
    mPicks = mPicks.filter((p) => p.edge == null).map((p) => {
      const same = [...faceMeshes, ...refMeshes].find((x) => x.userData.labels.includes(p.label));
      return same && { ...p, mesh: same };
    }).filter(Boolean);
    runMeasure();
  }
}

function nearLine(o, pt) {  // does the polyline pass through pt (the edge's midpoint from the server)?
  const a = o.geometry.attributes.position, v = new THREE.Vector3(...pt), seg = new THREE.Line3(), q = new THREE.Vector3();
  const tol = 1e-4 * (viewRadius || 10) + 1e-3;
  for (let k = 0; k + 1 < a.count; k++) {
    seg.start.fromBufferAttribute(a, k); seg.end.fromBufferAttribute(a, k + 1);
    if (seg.closestPointToPoint(v, true, q).distanceTo(v) < Math.max(tol, seg.distance() * 0.01)) return true;
  }
  return false;
}
function colorEdges() {
  edgeMarks.clear();
  const r = (viewRadius || 10) * 0.006;
  for (const o of edgeObjs) {
    const picked = EE ? eeIdx().has(o.userData.i) : pickedEdges.some((p) => p.i === o.userData.i);
    o.material.color.copy(picked ? EDGE_PICKED : o === hoveredEdge ? EDGE_HOVER : EDGE);
    if (!picked && o !== hoveredEdge) continue;
    const a = o.geometry.attributes.position, pts = [];
    for (let k = 0; k < a.count; k++) pts.push(new THREE.Vector3().fromBufferAttribute(a, k));
    const curve = new THREE.CatmullRomCurve3(pts, false, "centripetal");
    const tube = new THREE.Mesh(new THREE.TubeGeometry(curve, Math.max(2, pts.length * 2), r, 6, false),
      new THREE.MeshBasicMaterial({ color: picked ? EDGE_PICKED : EDGE_HOVER, depthTest: false, transparent: true, opacity: 0.9 }));
    tube.renderOrder = 5;
    edgeMarks.add(tube);
  }
}
function colorFaces() {
  colorEdges();
  for (const m of faceMeshes) m.material.color.copy(m === hovered ? HOVER : pickedFaces.some((p) => p.mesh === m) ? PICKED
    : selected && m.userData.features.includes(selected) ? HI : BASE);
  for (const m of refMeshes) {
    const on = m === hovered || pickedFaces.some((p) => p.mesh === m) || (selected && m.userData.ref === selected);
    const c = refColor(m.userData.ref);
    m.material.color.copy(on ? c.clone().offsetHSL(0, 0, -0.14) : c);
    m.material.opacity = on ? 0.5 : 0.32;
  }
}

function frame(center, radius, dir, up) {
  viewRadius = radius;
  const dist = radius * 4;
  camera.position.copy(center).add(dir.clone().normalize().multiplyScalar(camera === persp ? radius / Math.sin((persp.fov * Math.PI) / 360) * 1.1 : dist));
  camera.up.copy(up);
  controls.target.copy(center);
  camera.near = camera === persp ? radius / 100 : -dist * 10; camera.far = radius * 100;
  if (camera === ortho) { ortho.zoom = 1; setOrthoFrustum(); } else camera.updateProjectionMatrix();
  controls.update();
}
function fitView(dir) {  // false if there is nothing to fit yet
  const box = new THREE.Box3().setFromObject(partGroup).union(new THREE.Box3().setFromObject(refGroup));
  if (box.isEmpty()) return false;
  const c = box.getCenter(new THREE.Vector3()), r = box.getSize(new THREE.Vector3()).length() / 2 || 10;
  axes.scale.setScalar(r * 0.02);
  setGrid(box);
  const d = { iso: [1, -1, 0.8], front: [0, -1, 0], top: [0, 0, 1], right: [1, 0, 0] }[dir] || [1, -1, 0.8];
  frame(c, r, new THREE.Vector3(...d), dir === "top" ? new THREE.Vector3(0, 1, 0) : new THREE.Vector3(0, 0, 1));
  return true;
}
document.querySelectorAll(".vtools [data-view]").forEach((b) => (b.onclick = () => { exitSketch(); fitView(b.dataset.view); }));
$("#fitBtn").onclick = () => (inSketch() ? viewSketch() : fitView("iso"));

// view gizmo (bottom left): the world axes as the camera sees them; click one to look along it
const GZ = [["X", [1, 0, 0], "#e5484d"], ["Y", [0, 1, 0], "#30a46c"], ["Z", [0, 0, 1], "#3e63dd"]];
const gzItems = [];  // built once; each frame only moves them (a node rebuilt every frame can't be clicked)
(function buildGizmo() {
  const svg = $("#gizmo"), NS = "http://www.w3.org/2000/svg";
  const el = (tag, attrs, parent) => { const e = document.createElementNS(NS, tag); for (const k in attrs) e.setAttribute(k, attrs[k]); parent.appendChild(e); return e; };
  el("circle", { class: "ring", r: 46 }, svg);
  for (const [n, v, col] of GZ) for (const sgn of [1, -1]) {
    const g = el("g", { class: `ax${sgn < 0 ? " neg" : ""}`, "data-ax": n, "data-sgn": sgn }, svg);
    el("title", {}, g).textContent = `Look ${sgn > 0 ? "down" : "up"} the ${n} axis`;
    const line = sgn > 0 ? el("line", { x1: 0, y1: 0, stroke: col, "stroke-width": 2.2 }, g) : null;
    const dot = el("circle", { r: sgn > 0 ? 9 : 6.5, fill: col }, g);
    const label = sgn > 0 ? el("text", {}, g) : null;
    if (label) label.textContent = n;
    gzItems.push({ v: new THREE.Vector3(...v).multiplyScalar(sgn), g, line, dot, label, z: 0 });
  }
})();
let gzOrder = "";
function drawGizmo() {
  const q = camera.quaternion.clone().invert();
  for (const it of gzItems) {
    const p = it.v.clone().applyQuaternion(q), x = (p.x * 34).toFixed(1), y = (-p.y * 34).toFixed(1);
    it.z = p.z;
    it.dot.setAttribute("cx", x); it.dot.setAttribute("cy", y);
    if (it.line) { it.line.setAttribute("x2", x); it.line.setAttribute("y2", y); }
    if (it.label) { it.label.setAttribute("x", x); it.label.setAttribute("y", y); }
  }
  const order = [...gzItems].sort((a, b) => a.z - b.z);  // far axes first
  const key = order.map((it) => gzItems.indexOf(it)).join();
  if (key !== gzOrder) { gzOrder = key; for (const it of order) $("#gizmo").appendChild(it.g); }
}
$("#gizmo").addEventListener("click", (ev) => {
  const g = ev.target.closest(".ax");
  if (!g || inSketch()) return;
  const v = { X: [1, 0, 0], Y: [0, 1, 0], Z: [0, 0, 1] }[g.dataset.ax].map((c) => c * +g.dataset.sgn);
  const box = new THREE.Box3().setFromObject(partGroup).union(new THREE.Box3().setFromObject(refGroup));
  if (box.isEmpty()) return;
  const up = g.dataset.ax === "Z" ? new THREE.Vector3(0, 1, 0) : new THREE.Vector3(0, 0, 1);
  frame(box.getCenter(new THREE.Vector3()), box.getSize(new THREE.Vector3()).length() / 2 || 10, new THREE.Vector3(...v), up);
});

// hover + pick
const ray = new THREE.Raycaster(), ptr = new THREE.Vector2();
const tip = Object.assign(document.createElement("div"), { className: "tip" });
document.body.appendChild(tip);
function pickHit(ev) {
  const r = renderer.domElement.getBoundingClientRect();
  ptr.set(((ev.clientX - r.left) / r.width) * 2 - 1, -((ev.clientY - r.top) / r.height) * 2 + 1);
  ray.setFromCamera(ptr, camera);
  return ray.intersectObjects([...faceMeshes, ...refMeshes.filter((m) => m.visible)]).find((h) => !clipped(h.point)) || null;
}
const clipped = (p) => clipPlanes.some((pl) => pl.distanceToPoint(p) < 0);
function unitsPerPixel() {
  const h = renderer.domElement.clientHeight || 1;
  if (camera === ortho) return (ortho.top - ortho.bottom) / ortho.zoom / h;
  return camera.position.distanceTo(controls.target) * 2 * Math.tan((persp.fov * Math.PI) / 360) / h;
}
function edgeHit(ev) {  // the edge within a few pixels of the pointer, unless a face hides it
  const face = pickHit(ev);
  ray.params.Line.threshold = unitsPerPixel() * 6;
  const hits = ray.intersectObjects(edgeObjs);
  if (!hits.length) return null;
  // the ray's closest approach to the edge, not the face behind it: allow for the threshold
  const e = hits.reduce((a, b) => (a.distanceToRay < b.distanceToRay - 1e-9 ? a : b));
  if (face && e.distance > face.distance + ray.params.Line.threshold * 2) return null;
  return e;
}
const pick = (ev) => pickHit(ev)?.object || null;
renderer.domElement.addEventListener("pointermove", (ev) => {
  if (inSketch()) return;
  const eh = edgeHit(ev), eo = eh?.object || null;
  const m = eo ? null : pick(ev);
  if (m !== hovered || eo !== hoveredEdge) { hovered = m; hoveredEdge = eo; colorFaces(); }
  if (eo) { tip.style.display = "block"; tip.style.left = ev.clientX + 12 + "px"; tip.style.top = ev.clientY + 12 + "px"; tip.textContent = `edge ${eo.userData.i}: click to pick, shift-click to add`; }
  else if (m) { tip.style.display = "block"; tip.style.left = ev.clientX + 12 + "px"; tip.style.top = ev.clientY + 12 + "px"; tip.textContent = m.userData.labels.join("  ·  "); }
  else tip.style.display = "none";
});
renderer.domElement.addEventListener("pointerleave", () => { hovered = null; hoveredEdge = null; tip.style.display = "none"; colorFaces(); });
let downAt = null;
renderer.domElement.addEventListener("pointerdown", (ev) => (downAt = [ev.clientX, ev.clientY]));
renderer.domElement.addEventListener("pointerup", (ev) => {
  if (inSketch() || !downAt || Math.hypot(ev.clientX - downAt[0], ev.clientY - downAt[1]) > 4) return;
  if (refPick) return void refFromView(ev);
  if (measuring) return void measurePick(ev);
  if (EE) { const h = edgeHit(ev); if (h) eeToggle(h.object.userData.i); return; }
  const eh = edgeHit(ev);
  if (eh) return pickEdge(eh.object.userData.i, ev.shiftKey);
  if (!ev.shiftKey) pickedEdges = [];
  const hit = pickHit(ev), m = hit?.object;
  const face = m && { mesh: m, labels: m.userData.labels, point: hit.point.toArray() };
  if (ev.shiftKey && m) {  // shift-click: add or remove a face, keep the feature selection
    pickedFaces = pickedFaces.some((p) => p.mesh === m) ? pickedFaces.filter((p) => p.mesh !== m) : [...pickedFaces, face];
    pickUpdate();
    return colorFaces();
  }
  pickedFaces = m ? [face] : [];
  pickUpdate();
  if (m) { const fs = m.userData.features, i = fs.indexOf(selected); select(fs[(i + 1) % fs.length]); }
  else select(null);
});

async function pickEdge(i, add) {
  if (add && pickedEdges.some((p) => p.i === i)) {
    pickedEdges = pickedEdges.filter((p) => p.i !== i);
    pickUpdate(); return colorFaces();
  }
  let r;
  try { r = await api(`/api/edge/${i}`); } catch (e) { return note(`That edge can't be referenced: ${e.message}`, "err"); }
  const rec = { i, ref: r.ref, label: r.label, point: r.point };
  if (add) pickedEdges = [...pickedEdges, rec];
  else { pickedEdges = [rec]; pickedFaces = []; }
  pickUpdate();
  colorFaces();
}

// ── sketch mode: the sketch editor (static/sketch.js) on the sketch's plane, viewed straight on ──
let savedView = null;
const SK = createSketchEditor({
  scene, renderer, host, labelsBox: $("#labels"), dimEdit: $("#dimEdit"), box: $("#boxSel"),
  camera: () => camera, api, edit, note,
  tip: (t) => {
    if (!t) { tip.style.display = "none"; return; }
    Object.assign(tip.style, { display: "block", left: t.x + 12 + "px", top: t.y + 12 + "px" });
    tip.textContent = t.text;
  },
  hint: (t) => { $("#sketchHint").textContent = t; },
  ask: (q, def) => prompt(q, def),
  onChange: () => { sketchBarUpdate(); if (refPick && SK.selection().length) refSketchSelection(); },
  onLost: () => exitSketch(),
});
const inSketch = () => !!SK.active();
window.vibecadSketch = SK;  // for browser tests and the devtools console

async function enterSketch(id) {
  const reframe = SK.active() !== id;
  if (!inSketch()) savedView = { pos: camera.position.clone(), up: camera.up.clone(), target: controls.target.clone(), radius: viewRadius,
                                 persp: camera === persp, empty: new THREE.Box3().setFromObject(partGroup).isEmpty() };
  $("#sketchBar").hidden = $("#sketchTools").hidden = $("#sketchHint").hidden = false;
  $("#modelTools").hidden = true;
  $("#sketchName").textContent = id;
  if (camera === persp) $("#projBtn").click();  // sketches are always viewed orthographic
  controls.mouseButtons.LEFT = null;  // in a sketch the left button selects and draws; right-drag pans, wheel zooms
  ghostPart(true);
  await SK.enter(id);
  if (reframe) viewSketch();
}
function viewSketch() {
  const F = SK.frame();
  if (!F) return;
  const box = SK.bounds();
  let c, r;
  if (!box.isEmpty()) {
    c = box.getCenter(new THREE.Vector3());
    r = Math.max(box.getSize(new THREE.Vector3()).length() / 2, 5) * 1.35;
  } else {  // a new, empty sketch: frame the part as seen on this plane
    const pb = new THREE.Box3().setFromObject(partGroup).union(new THREE.Box3().setFromObject(refGroup));
    const pc = pb.isEmpty() ? F.o.clone() : pb.getCenter(new THREE.Vector3());
    c = pc.clone().addScaledVector(F.n, -pc.clone().sub(F.o).dot(F.n));
    r = pb.isEmpty() ? 30 : Math.max(pb.getSize(new THREE.Vector3()).length() / 2, 10);
  }
  // keep the sketch clear of the status bar (top) and tool palette (left): fit it to the free area
  const h = host.clientHeight || 1, w = host.clientWidth || 1;
  const top = $("#sketchBar").getBoundingClientRect().bottom - host.getBoundingClientRect().top + 8;
  const left = $("#sketchTools").getBoundingClientRect().right - host.getBoundingClientRect().left + 8;
  r *= Math.max(h / Math.max(h - top, h / 2), w / Math.max(w - left, w / 2));
  frame(c, r, F.n, F.y);
  const upp = (ortho.top - ortho.bottom) / ortho.zoom / h;
  const right = new THREE.Vector3().crossVectors(F.y, F.n);
  const shift = F.y.clone().multiplyScalar((top / 2) * upp).addScaledVector(right, -(left / 2) * upp);
  camera.position.add(shift);
  controls.target.add(shift);
  controls.update();
}
function exitSketch() {
  if (!inSketch()) return;
  SK.exit();
  $("#sketchBar").hidden = $("#sketchTools").hidden = $("#sketchHint").hidden = true;
  $("#modelTools").hidden = false;
  controls.mouseButtons.LEFT = THREE.MOUSE.ROTATE;
  ghostPart(false);
  if (savedView?.empty) {  // the part was empty when the sketch opened: show whatever it has become
    if (!fitView("iso")) pendingFit = true;
  } else if (savedView) {
    if (savedView.persp && camera === ortho) $("#projBtn").click();
    viewRadius = savedView.radius;
    camera.position.copy(savedView.pos); camera.up.copy(savedView.up); controls.target.copy(savedView.target);
    if (camera === ortho) setOrthoFrustum();
    controls.update();
  }
}
$("#exitSketch").onclick = () => { selected = null; exitSketch(); renderTree(null); renderDetails(); colorFaces(); };
function ghostPart(on) {
  for (const m of faceMeshes) { m.material.transparent = on; m.material.opacity = on ? 0.28 : 1; m.material.depthWrite = !on; }
  for (const o of edgeObjs) o.material.opacity = on ? 0.45 : 0.85;
  edgeMarks.visible = !on;
  for (const m of refMeshes) m.material.opacity = on ? 0.12 : 0.32;
}

// sketch toolbar: drawing tools, constraints (enabled when the selection fits), edit actions
(function buildSketchTools() {
  const bar = $("#sketchTools");
  const btn = (icon, title, onclick, data) => {
    const b = Object.assign(document.createElement("button"), { className: "ico", innerHTML: ICON[icon] || icon, title, onclick });
    Object.assign(b.dataset, data);
    return b;
  };
  const group = (items) => { const g = document.createElement("span"); g.className = "skgroup"; items.forEach((i) => g.appendChild(i)); bar.appendChild(g); };
  group(SKETCH_TOOLS.map((t) => btn(t.id, t.title, () => SK.setTool(t.id), { tool: t.id })));
  group(SKETCH_CONSTRAINTS.map((c) => btn(c.id, c.title, () => SK.constrain(c.id), { con: c.id })));
  group([btn("project", "Project the outline of this sketch's face as reference geometry to constrain to (it follows the part)", () => SK.projectOutline(), { act: "project" }),
         btn("construction", "Toggle construction geometry for the selected curves (G)", () => SK.toggleConstruction(), { act: "construction" }),
         btn("roundcorner", "Round corner: select the point where two lines meet, then give the radius (F)", () => SK.roundCorner(), { act: "roundcorner" }),
         btn("offset", "Offset: select a line, arc or circle; its whole outline is copied at a distance (− for inside). Follows the original (K)", () => SK.offsetSel(), { act: "offset" }),
         btn("mirror", "Mirror: select curves (and a construction line as the axis, or choose X or Y next); the copies follow the originals (I)", () => SK.mirrorSel(), { act: "mirror" }),
         btn("rename", "Rename the selected entity or dimension; references are updated", () => SK.rename(), { act: "rename" }),
         btn("param", "Drive the selected dimension from a new part parameter", () => SK.toParam(), { act: "param" }),
         btn("clearmarks", "Remove your freehand marks", () => SK.clearMarks(), { act: "clearmarks" }),
         btn("delete", "Delete the selected entities and constraints (Del)", () => SK.del(), { act: "delete" }),
         btn("ask", "Ask the agent about, or to change, the selected sketch entities", askAboutSketch, { act: "ask" })]);
  for (const b of document.querySelectorAll("button.ico[data-icon]")) b.innerHTML = ICON[b.dataset.icon];
  for (const b of document.querySelectorAll("button.rb[data-icon]")) b.innerHTML = `${ICON[b.dataset.icon]}<span>${b.dataset.label}</span>`;
  for (const b of document.querySelectorAll("button.hb[data-icon]")) b.insertAdjacentHTML("afterbegin", ICON[b.dataset.icon]);
  for (const b of document.querySelectorAll("button.refbtn[data-icon]")) b.insertAdjacentHTML("afterbegin", ICON[b.dataset.icon]);
})();
function askAboutSketch() {
  const sel = SK.selection();
  $("#prompt").dataset.placeholder = sel.length ? `Ask about or change ${sel.join(", ")} in ${SK.active()}…` : `Ask about or change sketch ${SK.active()}…`;
  $("#prompt").focus();
}
const HINTS = {
  select: () => "Click to select (Shift adds) · drag free geometry · drag on empty space to box-select · double-click a dimension to change it",
  line: (n) => (n ? "Click the end point (snaps to points and curves; near-horizontal/vertical lines get a constraint) · Esc ends the chain" : "Click the start point"),
  rect: (n) => (n ? "Click the opposite corner" : "Click the first corner"),
  circle: (n) => (n ? "Click a point on the rim" : "Click the centre"),
  arc: (n) => ["Click the centre", "Click the start point", "Click the end point (counterclockwise)"][n] || "",
  slot: (n) => ["Click the centre of one end", "Click the centre of the other end (snaps level / plumb)", "Click to set the width"][n] || "",
  polygon: (n) => (n ? "Click a corner (you'll be asked how many sides)" : "Click the centre"),
  point: () => "Click to place a point (snaps to points and curves) · a Hole on this sketch drills at every point",
  mark: () => `Draw on the sketch to show the agent what you mean (${SK.marks().length} mark(s)); they go with your next prompt and are never saved to the part`,
};
function sketchBarUpdate() {
  const d = SK.data();
  if (!d) return;
  const names = (list) => list.map((x) => x.split(" ")[0]).join(", ");  // "slot_w distance(...)=6" -> "slot_w"
  const conf = d.conflicting?.length ? ` · conflicting: ${names(d.conflicting)}` : d.redundant?.length ? ` · redundant: ${names(d.redundant)}` : "";
  const info = $("#sketchInfo");
  info.textContent = `· ${d.dof} DOF · ${d.dof === 0 && !conf ? "fully constrained" : d.status}${conf}`;
  info.className = conf ? "bad" : d.dof === 0 ? "okc" : "muted";
  const tool = SK.tool();
  for (const b of $("#sketchTools").querySelectorAll("button")) {
    if (b.dataset.tool) b.classList.toggle("on", b.dataset.tool === tool);
    if (b.dataset.con) b.disabled = !SK.available(b.dataset.con);
  }
  const sel = SK.selection();
  $("#sketchTools [data-act=delete]").disabled = !sel.some((k) => k.startsWith("#") || !k.includes(".") && k !== "origin");
  $("#sketchTools [data-act=construction]").disabled = !sel.some((k) => !k.startsWith("#") && !k.includes(".") && k !== "origin");
  $("#sketchTools [data-act=rename]").disabled = !SK.canRename();
  $("#sketchTools [data-act=clearmarks]").disabled = !SK.marks().length;
  $("#sketchTools [data-act=project]").disabled = !d.on_face;
  $("#sketchTools [data-act=param]").disabled = !SK.canParam();
  $("#sketchTools [data-act=roundcorner]").disabled = !SK.canRound();
  $("#sketchTools [data-act=offset]").disabled = !SK.canOffset();
  $("#sketchTools [data-act=mirror]").disabled = !SK.canMirror();
  const sub = tool === "select" && sel.length ? `Selected: ${sel.join(", ")}` : HINTS[tool](SK.pendingCount());
  $("#sketchHint").textContent = sub;
}

// ── modelling by hand: new sketches, extrude, revolve ─────────────
function nextId(prefix) {
  const ids = new Set(S.features.map((f) => f.id));
  let n = 1;
  while (ids.has(`${prefix}${n}`)) n++;
  return `${prefix}${n}`;
}
async function addFeature(feature, message, after) {
  // new features go at the rollback bar if it is up, and the bar moves past them
  const rb = S.rollback, anchor = after ? { after } : rb != null && rb > 0 ? { after: S.features[rb - 1].id } : rb === 0 ? { before: S.features[0].id } : {};
  const pos = after ? S.features.findIndex((f) => f.id === after) + 1 : rb;
  const ok = await edit([{ op: "add_feature", ...anchor, feature }], message);
  if (ok && rb != null && pos <= rb) setState((await api("/api/rollback", { index: rb + 1 })).state);
  return ok;
}
function faceRef(label, point) {
  const m = label.match(/^([^.]+)\.([a-z_]+)(?:\[([^\]]+)\])?(?:@(.+))?$/);
  if (!m) return null;
  const ref = { feature: m[1], role: m[2] };
  if (m[3]) ref.entity = m[3];
  if (m[4]) ref.instance = m[4];
  if (faceMeshes.filter((x) => x.userData.labels.includes(label)).length > 1)  // label on several faces: take the clicked one
    Object.assign(ref, { pick: "nearest", near: point.map((v) => +v.toFixed(4)) });
  ref.note = `face ${label}, picked in the GUI`;
  return ref;
}
function popup(el, anchor) {
  const a = anchor.getBoundingClientRect(), h = $("#center").getBoundingClientRect();
  el.hidden = false;  // shown first, so its real width is known: keep it inside the view
  el.style.left = Math.max(8, Math.min(a.left - h.left, h.width - el.offsetWidth - 8)) + "px";
  el.style.top = a.bottom - h.top + 6 + "px";
  const close = (ev) => { if (!el.contains(ev.target) && ev.target !== anchor) { el.hidden = true; document.removeEventListener("pointerdown", close, true); } };
  document.addEventListener("pointerdown", close, true);
}
const numOrExpr = (t) => (/^[-+]?(\d+\.?\d*|\.\d+)$/.test(t.trim()) ? +t : t.trim());

$("#newSketchBtn").onclick = () => {
  if (!S) return note("Open or create a part first.", "err");
  const m = $("#newMenu"), pf = lastPicked(), face = pf?.labels?.[0];
  m.innerHTML = `<div class="ttl">New sketch on…</div>
    <button data-d="XY">XY plane (top, normal +Z)</button><button data-d="XZ">XZ plane (front, normal −Y)</button>
    <button data-d="YZ">YZ plane (right, normal +X)</button>
    <div class="row"><label>offset</label><input id="nsOffset" value="0"></div>
    <button data-face ${face ? "" : "disabled"}>${face ? `Picked face: ${esc(face)}` : "Picked face (click a face first)"}</button>`;
  popup(m, $("#newSketchBtn"));
  const make = async (plane) => {
    m.hidden = true;
    const id = nextId("sketch");
    const off = numOrExpr($("#nsOffset").value || "0");
    if (off !== 0) plane.offset = off;
    if (await addFeature({ id, type: "sketch", plane }, `new ${id}`)) select(id);
  };
  m.querySelectorAll("[data-d]").forEach((b) => (b.onclick = () => make({ datum: b.dataset.d })));
  m.querySelector("[data-face]").onclick = () => {
    const ref = faceRef(face, pf.point);
    if (!ref) return note(`can't make a face reference from ${face}`, "err");
    pickedFaces = [];
    pickUpdate();
    make({ face: ref });
  };
};

// extrude extent rows, shared by the new-extrude and edit forms: distance, through all, or up to a face
// parallel to the sketch (listed by the server, nearest first), plus a draft angle
function extentRows(p, j = {}) {
  const ext = j.extent || "blind";
  return `<div class="row"><label>extent</label><select id="${p}Ext">${[["blind", "distance"], ["through_all", "through all"], ["up_to_face", "up to face"]]
      .map(([v, t]) => `<option value="${v}" ${v === ext ? "selected" : ""}>${t}</option>`).join("")}</select></div>
    <div class="row" id="${p}FaceRow" hidden><label>face</label><select id="${p}Face"></select></div>
    <div class="row" id="${p}DistRow"><label id="${p}DistLbl">distance</label><input id="${p}Dist" value="${esc(String(j.distance ?? (ext === "up_to_face" ? 0 : 10)))}"></div>
    <div class="row"><label>draft °</label><input id="${p}Draft" value="${esc(String(j.draft ?? 0))}" title="taper the walls: positive leans them inward along the extrusion"></div>`;
}
function wireExtent(p, sid, j = {}, before = null) {
  let faces = null;
  const sync = async () => {
    const e = $(`#${p}Ext`).value;
    $(`#${p}FaceRow`).hidden = e !== "up_to_face";
    $(`#${p}DistRow`).hidden = e === "through_all";
    $(`#${p}DistLbl`).textContent = e === "up_to_face" ? "past it by" : "distance";
    const dir = $(`#${p === "ff" ? "ffDir" : "efDir"}`)?.closest(".row");
    if (dir) dir.hidden = e === "up_to_face";  // it turns toward the face by itself
    if (e === "up_to_face" && !faces) {
      const sel = $(`#${p}Face`);
      sel.innerHTML = `<option>loading…</option>`;
      try { faces = await api(`/api/parallel_faces/${encodeURIComponent(sid)}${before ? `?before=${encodeURIComponent(before)}` : ""}`); } catch { faces = []; }
      const cur = j.to_face ? JSON.stringify([j.to_face.feature, j.to_face.role, j.to_face.entity || null]) : null;
      sel.innerHTML = faces.length ? faces.map((f, k) => `<option value="${k}" ${cur === JSON.stringify([f.ref.feature, f.ref.role, f.ref.entity || null]) ? "selected" : ""}>${esc(f.label)} · ${Math.abs(f.distance)} ${f.distance > 0 ? "above" : "below"}</option>`).join("")
        : `<option value="">no parallel faces</option>`;
      if (j.to_face && !faces.some((f) => cur === JSON.stringify([f.ref.feature, f.ref.role, f.ref.entity || null]))) {
        sel.insertAdjacentHTML("afterbegin", `<option value="keep" selected>${esc(j.to_face.feature)}.${esc(j.to_face.role)}${j.to_face.entity ? `[${esc(j.to_face.entity)}]` : ""}</option>`);
      }
      if ($(`#${p}Dist`).value === "10" && !j.extent) $(`#${p}Dist`).value = "0";
    }
  };
  $(`#${p}Ext`).onchange = sync;
  sync();
  return () => {  // the extent fields of the feature, or throws with a message
    const e = $(`#${p}Ext`).value, out = { extent: e, draft: numOrExpr($(`#${p}Draft`).value || "0") };
    if (e !== "through_all") out.distance = numOrExpr($(`#${p}Dist`).value || "0");
    if (e === "up_to_face") {
      const v = $(`#${p}Face`).value;
      if (v === "keep") out.to_face = j.to_face;
      else if (v === "" || !faces?.[+v]) throw new Error("pick a face parallel to the sketch");
      else out.to_face = faces[+v].ref;
    } else out.to_face = null;
    return out;
  };
}

function featureForm(kind) {
  const sid = SK.active(), d = SK.data();
  if (!sid || !d) return;
  const m = $("#featMenu"), hasBody = S.volume != null;
  const lines = d.entities.filter((e) => e.type === "line").map((e) => e.id);
  const mode = (def) => `<div class="row"><label>mode</label><select id="ffMode">${["add", "cut", "new", "intersect"].map((x) =>
    `<option ${x === def ? "selected" : ""}>${x}</option>`).join("")}</select></div>`;
  m.innerHTML = kind === "extrude"
    ? `<div class="ttl">Extrude ${esc(sid)}</div>
       ${extentRows("ff")}
       <div class="row"><label>direction</label><select id="ffDir"><option>normal</option><option>reverse</option><option>symmetric</option></select></div>
       ${mode(hasBody ? "add" : "new")}<button class="go" id="ffGo">Extrude</button><div class="err" id="ffErr"></div>`
    : `<div class="ttl">Revolve ${esc(sid)}</div>
       <div class="row"><label>axis</label><select id="ffAxis">${["x_axis", "y_axis", ...lines].map((a) => `<option>${esc(a)}</option>`).join("")}</select></div>
       <div class="row"><label>angle</label><input id="ffAngle" value="360"></div>
       ${mode(hasBody ? "add" : "new")}<button class="go" id="ffGo">Revolve</button><div class="err" id="ffErr"></div>`;
  popup(m, $(kind === "extrude" ? "#extrudeBtn" : "#revolveBtn"));
  const readExtent = kind === "extrude" ? wireExtent("ff", sid) : null;
  $("#ffGo").onclick = async () => {
    const id = nextId(kind), f = { id, type: kind, profile: { sketch: sid }, mode: $("#ffMode").value };
    if (kind === "extrude") {
      let ex;
      try { ex = readExtent(); } catch (e) { $("#ffErr").textContent = e.message; return; }
      if (ex.extent !== "blind") f.extent = ex.extent;
      if (ex.distance !== undefined) f.distance = ex.distance;
      if (ex.to_face) f.to_face = ex.to_face;
      if (ex.draft !== 0) f.draft = ex.draft;
      if ($("#ffDir").value !== "normal") f.direction = $("#ffDir").value;
    } else {
      f.axis = $("#ffAxis").value;
      f.angle = numOrExpr($("#ffAngle").value);
    }
    m.hidden = true;
    if (await addFeature(f, `${kind} ${sid}`, sid)) {
      selected = null;
      exitSketch();
      select(id);
    }
  };
}
$("#extrudeBtn").onclick = () => featureForm("extrude");

// fillet / chamfer from picks: picked edges (each exactly), plus picked faces. Faces alone: two faces -> the edge
// between them; one face -> its edges
function edgeTargets() {  // [{ref, what}] or null when the picks don't make an edge set
  const nf = pickedFaces.length, ne = pickedEdges.length;
  if (ne) {
    const faces = pickedFaces.map((p) => ({ of: faceRef(p.labels[0], p.point), note: `edges of ${p.labels[0]}, picked in the GUI` }));
    return [...pickedEdges.map((p) => ({ ref: p.ref, what: `edge ${p.label}` })), ...faces.map((r, k) => ({ ref: r, what: `edges of ${pickedFaces[k].labels[0]}` }))];
  }
  if (nf === 2) {
    const [a, b] = pickedFaces.map((p) => faceRef(p.labels[0], p.point));
    return [{ ref: { between: [a, b], note: `edge where ${pickedFaces[0].labels[0]} meets ${pickedFaces[1].labels[0]}, picked in the GUI` },
      what: `edge between ${pickedFaces[0].labels[0]} and ${pickedFaces[1].labels[0]}` }];
  }
  if (nf === 1) return [{ ref: { of: faceRef(pickedFaces[0].labels[0], pickedFaces[0].point), note: `edges of ${pickedFaces[0].labels[0]}, picked in the GUI` },
    what: `edges of ${pickedFaces[0].labels[0]}`, face: true }];
  return null;
}
function pickUpdate() {
  const t = edgeTargets();
  for (const b of [$("#filletBtn"), $("#chamferBtn")]) {
    b.disabled = !t;
    b.title = t ? `${b.dataset.kind} the ${t.map((x) => x.what).join(", ")}`
      : `Click an edge (shift-click for more), or a face for all its edges, or two faces for the edge between them`;
  }
}
function edgeForm(kind) {
  const t = edgeTargets();
  if (!t) return note(`${kind}: click an edge, a face (its edges) or two faces (the edge between them)`, "err");
  if (t.some((x) => !x.ref || (x.ref.of === null) || (x.ref.between && x.ref.between.some((r) => !r)))) return note(`${kind}: can't reference one of the picked faces`, "err");
  const m = $("#featMenu"), size = kind === "fillet" ? "radius" : "distance";
  const what = t.length === 1 ? t[0].what : `${t.length} picked edges`;
  m.innerHTML = `<div class="ttl">${kind === "fillet" ? "Fillet" : "Chamfer"} the ${esc(what)}</div>
    <div class="row"><label>${size}</label><input id="ffSize" value="1"></div>
    ${t.length === 1 && t[0].face ? `<div class="row"><label>edges</label><select id="ffFilter"><option value="any">all</option><option value="line">straight only</option><option value="circle">round only</option></select></div>` : ""}
    <button class="go" id="ffGo">${kind === "fillet" ? "Fillet" : "Chamfer"}</button>`;
  popup(m, $(kind === "fillet" ? "#filletBtn" : "#chamferBtn"));
  $("#ffGo").onclick = async () => {
    const edges = t.map((x) => ({ ...x.ref }));
    if ($("#ffFilter") && $("#ffFilter").value !== "any") edges[0].filter = { type: $("#ffFilter").value };
    const id = nextId(kind);
    m.hidden = true;
    if (await addFeature({ id, type: kind, edges, [size]: numOrExpr($("#ffSize").value) }, `${kind} ${what}`)) {
      pickedFaces = []; pickedEdges = [];
      pickUpdate();
      select(id);
    }
  };
}
// ── changing an existing fillet / chamfer's edges: roll back to just before it, then click edges on and off ──
let EE = null;  // {fid, kind, prev (rollback to restore), items: [{ref, idx: [edge indices], label}]}
const faceTxt = (f) => (f ? `${f.feature}.${f.role}${f.entity ? `[${f.entity}]` : ""}${f.instance ? `@${f.instance}` : ""}` : "?");
function refText(r) {
  if (r.note) return r.note.replace(/, picked in the GUI$/, "");
  const flt = r.filter?.type ? ` (${r.filter.type === "line" ? "straight" : "round"} only)` : "";
  return (r.between ? `between ${faceTxt(r.between[0])} and ${faceTxt(r.between[1])}` : `edges of ${faceTxt(r.of)}`) + flt;
}
const eeIdx = () => new Set(EE ? EE.items.flatMap((x) => x.idx) : []);
async function startEdgeEdit(fid) {
  if (EE) return;
  if (inSketch()) exitSketch();
  const k = S.features.findIndex((f) => f.id === fid), prev = S.rollback;
  let r;
  try {
    r = await busyDo("Rolling back to before " + fid, async () => {
      setState((await api("/api/rollback", { index: k })).state);
      await lastMesh;
      return api(`/api/feature/${encodeURIComponent(fid)}/edges`);
    });
  } catch { setState((await api("/api/rollback", { index: prev ?? null })).state); return; }
  EE = { fid, kind: r.type, prev, items: r.items.map((it) => ({ ref: it.ref, idx: it.idx, label: refText(it.ref), error: it.error })) };
  const bad = EE.items.filter((x) => !x.idx.length);
  if (bad.length) note(`${bad.length} of ${fid}'s edge references match no edge now; they are dropped when you click Done`, "err");
  pickedEdges = []; pickedFaces = []; pickUpdate();
  $("#edgeEdit").hidden = false;
  eeUpdate();
  colorFaces();
}
function eeUpdate() {
  const n = eeIdx().size;
  $("#edgeEditText").textContent = `${EE.kind} ${EE.fid}: ${n} edge${n === 1 ? "" : "s"} · click edges to add or remove · Esc cancels`;
  $("#edgeEditDone").disabled = !n;
}
async function eeToggle(i) {
  const hit = EE.items.find((x) => x.idx.includes(i));
  const one = async (j) => { const r = await api(`/api/edge/${j}`); return { ref: r.ref, idx: [j], label: r.label }; };
  if (hit) {  // a reference covering several edges (all edges of a face) is split into one per edge, minus this one
    EE.items = EE.items.filter((x) => x !== hit);
    for (const j of hit.idx) if (j !== i) { try { EE.items.push(await one(j)); } catch { /* shown by api() */ } }
  } else {
    try { EE.items.push(await one(i)); } catch { return; }
  }
  eeUpdate();
  colorFaces();
}
async function endEdgeEdit(save) {
  const ee = EE;
  if (!ee) return;
  EE = null;
  $("#edgeEdit").hidden = true;
  const edges = ee.items.filter((x) => x.idx.length).map((x) => x.ref);
  await busyDo("Rebuilding", async () => {
    if (save && edges.length) await edit([{ op: "update_feature", id: ee.fid, set: { edges } }], `edges of ${ee.fid}, picked in the GUI`);
    setState((await api("/api/rollback", { index: ee.prev ?? null })).state);
  });
  colorFaces();
  if (S.features.some((f) => f.id === ee.fid)) select(ee.fid);
}
$("#edgeEditDone").onclick = () => endEdgeEdit(true);
$("#edgeEditCancel").onclick = () => endEdgeEdit(false);
document.addEventListener("keydown", (ev) => { if (EE && ev.key === "Escape") { ev.stopPropagation(); endEdgeEdit(false); } }, true);

$("#filletBtn").onclick = () => edgeForm("fillet");
$("#chamferBtn").onclick = () => edgeForm("chamfer");
pickUpdate();
$("#revolveBtn").onclick = () => featureForm("revolve");
// Extrude / Revolve in the 3D tool column act on a sketch without opening it first: the selected sketch, else
// the newest sketch above the rollback bar that no extrude or revolve uses yet. They open it and the form.
function profileSketch() {
  const upto = S ? S.features.slice(0, rollIndex()) : [];
  const sel = upto.find((f) => f.id === selected && f.type === "sketch");
  if (sel) return sel.id;
  const used = new Set(upto.flatMap((f) => f.sketches || []));
  return [...upto].reverse().find((f) => f.type === "sketch" && !used.has(f.id) && f.status !== "error")?.id || null;
}
function modelToolsUpdate() {
  const hasBody = !!S && S.volume != null, rep = S ? replayable() : [];
  $("#holeBtn").disabled = $("#shellBtn").disabled = $("#textBtn").disabled = !hasBody;
  $("#textBtn").title = hasBody ? "Text: click a flat face, then type the text to engrave or emboss there" : "Text: the part needs a solid first";
  $("#holeBtn").title = hasBody ? "Hole: click a flat face where it goes (or use a sketch's points), then choose the screw size and type" : "Hole: the part needs a solid first";
  $("#shellBtn").title = hasBody ? "Shell: hollow the part; click the faces to leave open first" : "Shell: the part needs a solid first";
  $("#patternBtn").disabled = $("#mirrorBtn").disabled = !rep.length;
  $("#patternBtn").title = rep.length ? "Pattern: repeat features in a row or around an axis" : "Pattern: make an extrude, revolve or hole to repeat first";
  $("#mirrorBtn").title = rep.length ? "Mirror: copy features across a datum plane" : "Mirror: make an extrude, revolve or hole to mirror first";
  $("#exportBtn").disabled = !hasBody;
  const nref = refIds().length;
  $("#clashBtn").disabled = !hasBody || !nref;
  $("#combineBtn").disabled = !hasBody || !nref;
  $("#combineBtn").title = !nref ? "Combine: import a part as a reference first" : !hasBody ? "Combine: the part needs a solid first"
    : "Combine with a reference body: cut a nest for it with a clearance gap, add it, or keep the overlap";
  $("#clashBtn").title = !nref ? "Clash: import a part as a reference first, then check how this part sits against it"
    : !hasBody ? "Clash: the part needs a solid first" : "Clash and clearance: where the part runs into each reference body (red), and the smallest gap to it";
  if (fitOn && $("#clashBtn").disabled) setFit(false);
  const sks = sketchesOk();
  $("#loftBtn").disabled = $("#sweepBtn").disabled = sks.length < 2;
  $("#loftBtn").title = sks.length < 2 ? "Loft: needs two or more sketches (the sections), on different planes"
    : "Loft: blend a solid through the profiles of two or more sketches (a square duct to a round one)";
  $("#sweepBtn").title = sks.length < 2 ? "Sweep: needs two sketches, a profile and a path"
    : "Sweep: move a profile along a path of lines and arcs (a bent tube, a handle, a frame)";
  const sid = profileSketch();
  for (const [b, kind] of [[$("#extrude3dBtn"), "Extrude"], [$("#revolve3dBtn"), "Revolve"]]) {
    b.disabled = !sid;
    b.title = sid ? `${kind} ${sid} (select another sketch in the tree to ${kind.toLowerCase()} that one)`
      : `${kind}: draw a sketch first (+ Sketch), or select one in the tree`;
  }
}
for (const [id, kind] of [["#extrude3dBtn", "extrude"], ["#revolve3dBtn", "revolve"]]) {
  $(id).onclick = async () => {
    const sid = profileSketch();
    if (!sid) return;
    selected = sid;
    await enterSketch(sid);
    renderTree(null); renderDetails();
    featureForm(kind);
  };
}
// ── hole: at the point clicked on a face (a new face sketch holds it), or at the points of an existing sketch ──
// ISO metric screws: clearance (medium fit), socket-head counterbore, flat-head (90°) countersink, tap drill
const SCREWS = {
  M2: { clear: 2.4, cbore: [4.4, 2.3], csk: 4.4, tap: 1.6, pitch: 0.4 },
  "M2.5": { clear: 2.9, cbore: [5.5, 2.8], csk: 5.5, tap: 2.05, pitch: 0.45 },
  M3: { clear: 3.4, cbore: [6.5, 3.3], csk: 6.5, tap: 2.5, pitch: 0.5 },
  M4: { clear: 4.5, cbore: [8, 4.4], csk: 8.6, tap: 3.3, pitch: 0.7 },
  M5: { clear: 5.5, cbore: [9.5, 5.4], csk: 10.4, tap: 4.2, pitch: 0.8 },
  M6: { clear: 6.6, cbore: [11, 6.5], csk: 12.4, tap: 5.0, pitch: 1.0 },
  M8: { clear: 9.0, cbore: [14.5, 8.6], csk: 16.4, tap: 6.8, pitch: 1.25 },
  M10: { clear: 11.0, cbore: [17.5, 10.8], csk: 20.4, tap: 8.5, pitch: 1.5 },
  M12: { clear: 13.5, cbore: [20, 13], csk: 24.4, tap: 10.2, pitch: 1.75 },
};
function holeSketches() {  // sketches above the rollback bar with points or circles to drill at
  return S.features.slice(0, rollIndex()).filter((f) => f.type === "sketch" && f.status === "ok" && f.n_entities > 0).map((f) => f.id);
}
$("#holeBtn").onclick = () => {
  if (!S || S.volume == null) return note("Hole: the part needs a solid first", "err");
  const pf = lastPicked(), onFace = pf && !pf.mesh?.userData?.ref;
  const sks = holeSketches();
  const m = $("#featMenu");
  const where = [...(onFace ? [["face", `where I clicked on ${pf.labels[0]}`]] : []),
    ...sks.map((id) => [`sk:${id}`, `at every point of ${id}`])];
  if (!where.length) return note("Hole: click a flat face where the hole goes, or draw a sketch with points (Point tool) first", "err");
  m.innerHTML = `<div class="ttl">Hole</div>
    <div class="row"><label>where</label><select id="hoWhere">${where.map(([v, t]) => `<option value="${esc(v)}">${esc(t)}</option>`).join("")}</select></div>
    <div class="row"><label>type</label><select id="hoKind"><option value="simple">Simple</option><option value="counterbore">Counterbore</option>
      <option value="countersink">Countersink</option><option value="tapped">Tapped</option></select></div>
    <div class="row"><label>screw</label><select id="hoScrew"><option value="">custom size</option>${Object.keys(SCREWS).map((k) => `<option ${k === "M4" ? "selected" : ""}>${k}</option>`).join("")}</select></div>
    <div class="row"><label>diameter</label><input id="hoD"></div>
    <div class="row" data-k="counterbore"><label>counterbore</label><input id="hoCbD" title="counterbore diameter" placeholder="⌀"><input id="hoCbH" title="counterbore depth" placeholder="depth"></div>
    <div class="row" data-k="countersink"><label>countersink</label><input id="hoCsD" title="countersink diameter" placeholder="⌀"><input id="hoCsA" value="90" title="countersink angle (degrees)" placeholder="angle"></div>
    <div class="row"><label>depth</label><select id="hoExt"><option value="through_all">through all</option><option value="blind">blind</option></select><input id="hoDepth" value="10" hidden></div>
    <div class="muted" id="hoNote"></div>
    <button class="go" id="hoGo">Add hole</button>`;
  popup(m, $("#holeBtn"));
  const fill = () => {
    const k = $("#hoKind").value, sc = SCREWS[$("#hoScrew").value];
    m.querySelectorAll("[data-k]").forEach((r) => (r.hidden = r.dataset.k !== k));
    $("#hoDepth").hidden = $("#hoExt").value !== "blind";
    if (k === "tapped" && $("#hoExt").value === "through_all" && !fill.touched) { $("#hoExt").value = "blind"; $("#hoDepth").hidden = false; }
    if (!sc) { $("#hoNote").textContent = ""; return; }
    $("#hoD").value = k === "tapped" ? sc.tap : sc.clear;
    $("#hoCbD").value = sc.cbore[0]; $("#hoCbH").value = sc.cbore[1]; $("#hoCsD").value = sc.csk;
    $("#hoNote").textContent = k === "tapped" ? `${$("#hoScrew").value}x${sc.pitch} thread: ${sc.tap} mm tap drill` : `${$("#hoScrew").value} clearance (ISO medium fit)`;
  };
  $("#hoKind").onchange = $("#hoScrew").onchange = () => fill();
  $("#hoExt").onchange = () => { fill.touched = true; fill(); };
  for (const q of ["#hoD", "#hoCbD", "#hoCbH", "#hoCsD"]) $(q).oninput = () => { $("#hoScrew").value = ""; $("#hoNote").textContent = ""; };
  fill();
  $("#hoGo").onclick = async () => {
    const kind = $("#hoKind").value, where = $("#hoWhere").value, screw = $("#hoScrew").value;
    const id = nextId("hole"), f = { id, type: "hole", kind: kind === "tapped" ? "simple" : kind, diameter: numOrExpr($("#hoD").value) };
    if (kind === "counterbore") Object.assign(f, { cbore_diameter: numOrExpr($("#hoCbD").value), cbore_depth: numOrExpr($("#hoCbH").value) });
    if (kind === "countersink") Object.assign(f, { csk_diameter: numOrExpr($("#hoCsD").value), csk_angle: numOrExpr($("#hoCsA").value) });
    if ($("#hoExt").value === "blind") Object.assign(f, { extent: "blind", depth: numOrExpr($("#hoDepth").value) });
    if (kind === "tapped" && screw) f.thread = `${screw}x${SCREWS[screw].pitch}`;
    f.intent = `${screw ? screw + " " : ""}${kind === "simple" ? "clearance hole" : kind === "tapped" ? "tapped hole" : kind + " hole"}${f.extent === "blind" ? `, ${f.depth} deep` : ", through"}`;
    m.hidden = true;
    if (where.startsWith("sk:")) {
      f.sketch = where.slice(3);
      if (await addFeature(f, `hole at the points of ${f.sketch}`)) select(id);
      return;
    }
    const ref = faceRef(pf.labels[0], pf.point);
    let at;
    try { at = await api("/api/face_point", { ref, point: pf.point }); } catch { return; }
    const sid = `${id}_at`, [u, v] = at.uv.map((c) => +c.toFixed(2));
    f.sketch = sid;
    const rb = S.rollback, anchor = rb != null && rb > 0 ? { after: S.features[rb - 1].id } : {};
    const ok = await edit([
      { op: "add_feature", ...anchor, feature: { id: sid, type: "sketch", plane: { face: ref }, intent: `position of ${id}`,
        entities: [{ id: "p1", type: "point", at: [u, v] }],
        constraints: [{ type: "distance_x", on: ["origin", "p1"], value: u, name: `${id}_x` }, { type: "distance_y", on: ["origin", "p1"], value: v, name: `${id}_y` }] } },
      { op: "add_feature", after: sid, feature: f },
    ], `hole on ${pf.labels[0]}`);
    if (ok) {
      if (rb != null) setState((await api("/api/rollback", { index: rb + 2 })).state);
      pickedFaces = []; pickUpdate(); select(id);
    }
  };
};

// ── shell, pattern, mirror ──
$("#shellBtn").onclick = () => {
  if (!S || S.volume == null) return note("Shell: the part needs a solid first", "err");
  const faces = pickedFaces.filter((p) => !p.mesh?.userData?.ref);
  const m = $("#featMenu");
  m.innerHTML = `<div class="ttl">Shell</div>
    <div class="muted">${faces.length ? `Opens ${faces.map((p) => esc(p.labels[0])).join(", ")}` : "Hollows the part closed. Click a face first (shift-click more) to leave it open."}</div>
    <div class="row"><label>wall</label><input id="shT" value="2"></div>
    <div class="row"><label>grows</label><select id="shDir"><option value="in">inward (hollow the part)</option><option value="out">outward (a skin around it)</option></select></div>
    <button class="go" id="shGo">Shell</button>`;
  popup(m, $("#shellBtn"));
  $("#shGo").onclick = async () => {
    const id = nextId("shell"), refs = faces.map((p) => faceRef(p.labels[0], p.point));
    m.hidden = true;
    const out = $("#shDir").value === "out";
    if (await addFeature({ id, type: "shell", remove_faces: refs, thickness: numOrExpr($("#shT").value), ...(out ? { outward: true } : {}),
      intent: `${out ? "A skin" : "Hollow to a"} ${$("#shT").value} mm wall${out ? " around it" : ""}${faces.length ? `, open at ${faces.map((p) => p.labels[0]).join(", ")}` : ""}` }, `shell ${id}`)) {
      pickedFaces = []; pickUpdate(); select(id);
    }
  };
};
// sketches above the rollback bar that built
function sketchesOk() {
  return S ? S.features.slice(0, rollIndex()).filter((f) => f.type === "sketch" && f.status !== "error").map((f) => f.id) : [];
}
const sketchOpts = (ids, cur) => ids.map((id) => `<option ${id === cur ? "selected" : ""}>${esc(id)}</option>`).join("");
const modeRow = (idp, cur) => `<div class="row"><label>mode</label><select id="${idp}">${["add", "cut", "new", "intersect"].map((x) =>
  `<option ${x === cur ? "selected" : ""}>${x}</option>`).join("")}</select></div>`;
// loft: the ticked sketches, in tree order; unused sketches are ticked to start with
$("#loftBtn").onclick = () => {
  const ids = sketchesOk();
  if (ids.length < 2) return note("Loft: needs two or more sketches", "err");
  const used = new Set(S.features.flatMap((f) => f.sketches || []));
  let pre = ids.filter((id) => !used.has(id));
  if (pre.length < 2) pre = ids.slice(-2);
  const m = $("#featMenu"), hasBody = S.volume != null;
  m.innerHTML = `<div class="ttl">Loft</div><div class="muted">Blend through these sections, first to last (tree order):</div>
    <div class="checks">${ids.map((id) => `<label><input type="checkbox" value="${esc(id)}" ${pre.includes(id) ? "checked" : ""}> ${esc(id)}</label>`).join("")}</div>
    <div class="row"><label>ruled</label><input id="loRuled" type="checkbox" style="flex:0" title="straight faces between sections instead of a smooth blend"></div>
    ${modeRow("loMode", hasBody ? "add" : "new")}<button class="go" id="loGo">Loft</button><div class="err" id="loErr"></div>`;
  popup(m, $("#loftBtn"));
  $("#loGo").onclick = async () => {
    const secs = pickedIds(m);
    if (secs.length < 2) { $("#loErr").textContent = "tick two or more sketches"; return; }
    const id = nextId("loft");
    m.hidden = true;
    if (await addFeature({ id, type: "loft", sections: secs, ...($("#loRuled").checked ? { ruled: true } : {}), mode: $("#loMode").value,
      intent: `Loft through ${secs.join(", ")}` }, `loft ${id}`)) select(id);
  };
};
// sweep: a profile sketch along a path sketch; guesses the newest unused sketch as the path, the one before as profile
$("#sweepBtn").onclick = () => {
  const ids = sketchesOk();
  if (ids.length < 2) return note("Sweep: needs two sketches, a profile and a path", "err");
  const used = new Set(S.features.flatMap((f) => f.sketches || []));
  const free = ids.filter((id) => !used.has(id));
  const path = free.at(-1) || ids.at(-1), prof = (free.length > 1 ? free.at(-2) : ids.filter((x) => x !== path).at(-1));
  const m = $("#featMenu"), hasBody = S.volume != null;
  m.innerHTML = `<div class="ttl">Sweep</div>
    <div class="row"><label>profile</label><select id="swProf">${sketchOpts(ids, prof)}</select></div>
    <div class="row"><label>path</label><select id="swPath">${sketchOpts(ids, path)}</select></div>
    <div class="muted">The path is the other sketch's lines and arcs, end to end. Sharp corners are mitred; round them for a bend.</div>
    ${modeRow("swMode", hasBody ? "add" : "new")}<button class="go" id="swGo">Sweep</button><div class="err" id="swErr"></div>`;
  popup(m, $("#sweepBtn"));
  $("#swGo").onclick = async () => {
    const pr = $("#swProf").value, pa = $("#swPath").value;
    if (pr === pa) { $("#swErr").textContent = "the profile and the path must be different sketches"; return; }
    const id = nextId("sweep");
    m.hidden = true;
    if (await addFeature({ id, type: "sweep", profile: { sketch: pr }, path: pa, mode: $("#swMode").value, intent: `Sweep ${pr} along ${pa}` }, `sweep ${id}`)) select(id);
  };
};
// combine the part with a reference body: cut a nest for it (with a clearance gap), add it, or keep the overlap
function combineForm(pre, anchor) {
  const refs = refIds();
  if (!refs.length) return note("Combine: import a part as a reference first", "err");
  if (S.volume == null) return note("Combine: the part needs a solid first", "err");
  const m = $("#featMenu");
  m.innerHTML = `<div class="ttl">Combine with a reference body</div>
    <div class="row"><label>body</label><select id="coTool">${refs.map((id) => `<option ${id === pre ? "selected" : ""}>${esc(id)}</option>`).join("")}</select></div>
    <div class="row"><label>mode</label><select id="coMode"><option value="cut">cut it out (a nest)</option><option value="add">add it</option><option value="intersect">keep the overlap</option></select></div>
    <div class="row"><label>clearance</label><input id="coC" value="0.3" title="gap all round, in mm: the body is grown by this much first"></div>
    <div class="muted">The nest follows the body: move or change the import and it updates.</div>
    <button class="go" id="coGo">Combine</button>`;
  popup(m, anchor || $("#combineBtn"));
  $("#coGo").onclick = async () => {
    const tool = $("#coTool").value, mode = $("#coMode").value, c = numOrExpr($("#coC").value || "0");
    const id = nextId(mode === "cut" ? "nest" : "combine");
    m.hidden = true;
    if (await addFeature({ id, type: "boolean", tool, mode, ...(c !== 0 ? { clearance: c } : {}),
      intent: mode === "cut" ? `A nest for ${tool}${c ? ` with ${c} mm clearance` : ""}` : `${mode === "add" ? "Add" : "Keep the overlap with"} ${tool}` }, `${mode} ${tool}`)) select(id);
  };
}
$("#combineBtn").onclick = () => combineForm(selected && refIds().includes(selected) ? selected : refIds().at(-1));
const REPLAYABLE = ["extrude", "revolve", "loft", "sweep", "boolean", "hole", "import"];
function replayable() {
  return S.features.slice(0, rollIndex()).filter((f) => REPLAYABLE.includes(f.type) && f.status === "ok").map((f) => f.id);
}
function featurePicker(ids) {
  const sel = ids.includes(selected) ? selected : ids.at(-1);
  return `<div class="checks">${ids.map((id) => `<label><input type="checkbox" value="${esc(id)}" ${id === sel ? "checked" : ""}> ${esc(id)}</label>`).join("")}</div>`;
}
const pickedIds = (m) => [...m.querySelectorAll(".checks input:checked")].map((x) => x.value);
$("#patternBtn").onclick = () => {
  const ids = S ? replayable() : [];
  if (!ids.length) return note("Pattern: make an extrude, revolve or hole to repeat first", "err");
  const m = $("#featMenu");
  m.innerHTML = `<div class="ttl">Pattern</div><div class="muted">Repeat these features:</div>${featurePicker(ids)}
    <div class="row"><label>kind</label><select id="paKind"><option value="linear">In a row</option><option value="circular">Around an axis</option></select></div>
    <div class="row" data-k="linear"><label>direction</label><select id="paDir">${["X", "Y", "Z", "-X", "-Y", "-Z"].map((d) => `<option>${d}</option>`).join("")}</select></div>
    <div class="row" data-k="linear"><label>spacing</label><input id="paSp" value="10"></div>
    <div class="row" data-k="circular"><label>axis</label><select id="paAx"><option>Z</option><option>X</option><option>Y</option></select></div>
    <div class="row" data-k="circular"><label>through</label><input id="paOx" value="0" title="x"><input id="paOy" value="0" title="y"><input id="paOz" value="0" title="z"></div>
    <div class="row" data-k="circular"><label>angle</label><input id="paAng" value="360" title="360 spaces the copies evenly around"></div>
    <div class="row"><label>count</label><input id="paN" value="4" title="including the original"></div>
    <button class="go" id="paGo">Pattern</button>`;
  popup(m, $("#patternBtn"));
  const kind = () => { m.querySelectorAll("[data-k]").forEach((r) => (r.hidden = r.dataset.k !== $("#paKind").value)); };
  $("#paKind").onchange = kind; kind();
  $("#paGo").onclick = async () => {
    const feats = pickedIds(m);
    if (!feats.length) return note("Pattern: tick at least one feature", "err");
    const id = nextId("pattern"), count = numOrExpr($("#paN").value);
    let f;
    if ($("#paKind").value === "linear") {
      const d = $("#paDir").value, v = { X: [1, 0, 0], Y: [0, 1, 0], Z: [0, 0, 1] }[d.replace("-", "")].map((c) => (d.startsWith("-") ? -c : c));
      f = { id, type: "linear_pattern", features: feats, direction: d.startsWith("-") ? v : d, spacing: numOrExpr($("#paSp").value), count,
        intent: `${count} × ${feats.join(", ")} along ${d}, ${$("#paSp").value} apart` };
    } else {
      f = { id, type: "circular_pattern", features: feats, axis: $("#paAx").value, origin: ["#paOx", "#paOy", "#paOz"].map((q) => numOrExpr($(q).value)),
        count, angle: numOrExpr($("#paAng").value), intent: `${count} × ${feats.join(", ")} around ${$("#paAx").value}` };
    }
    m.hidden = true;
    if (await addFeature(f, `pattern ${feats.join(", ")}`)) select(id);
  };
};
$("#mirrorBtn").onclick = () => {
  const ids = S ? replayable() : [];
  if (!ids.length) return note("Mirror: make an extrude, revolve or hole to mirror first", "err");
  const m = $("#featMenu");
  m.innerHTML = `<div class="ttl">Mirror</div><div class="muted">Mirror these features:</div>${featurePicker(ids)}
    <div class="row"><label>plane</label><select id="miPl"><option value="YZ">YZ (flip X)</option><option value="XZ">XZ (flip Y)</option><option value="XY">XY (flip Z)</option></select></div>
    <div class="row"><label id="miAtL">at X =</label><input id="miOff" value="0" title="where the mirror plane is, in world coordinates"></div>
    <button class="go" id="miGo">Mirror</button>`;
  popup(m, $("#mirrorBtn"));
  $("#miPl").onchange = () => { $("#miAtL").textContent = `at ${{ YZ: "X", XZ: "Y", XY: "Z" }[$("#miPl").value]} =`; };
  $("#miGo").onclick = async () => {
    const feats = pickedIds(m);
    if (!feats.length) return note("Mirror: tick at least one feature", "err");
    // a datum's offset runs along its normal, and XZ's normal is -Y: turn "at Y = 20" into offset -20
    const id = nextId("mirror"), at = numOrExpr($("#miOff").value || "0"), plane = { datum: $("#miPl").value };
    const off = plane.datum !== "XZ" ? at : typeof at === "number" ? -at : `-(${at})`;
    if (off !== 0) plane.offset = off;
    m.hidden = true;
    if (await addFeature({ id, type: "mirror", features: feats, plane, intent: `Mirror of ${feats.join(", ")} across ${plane.datum}` }, `mirror ${feats.join(", ")}`)) select(id);
  };
};

// ── fit: how the part sits against each reference body: clashes in red, the smallest gap as a line ──
let fitOn = false, fitSeq = 0;
const fitGroup = new THREE.Group();
scene.add(fitGroup);
const CLASH = new THREE.Color(0xef4444);
function setFit(on) {
  if (on && measuring) setMeasuring(false);  // they share the corner of the view
  if (on && inSketch()) exitSketch();
  fitOn = on;
  $("#clashBtn").classList.toggle("on", on);
  $("#clashPanel").hidden = !on;
  fitGroup.clear();
  ghostPart(on);
  if (on) runFit();
}
$("#clashBtn").onclick = () => setFit(!fitOn);
async function runFit() {
  if (!fitOn) return;
  const seq = ++fitSeq, panel = $("#clashPanel");
  if (!panel.innerHTML) panel.innerHTML = `<h4>Clash</h4><div class="muted">checking…</div>`;
  let r;
  try { r = await api("/api/fit"); } catch { return; }
  if (seq !== fitSeq || !fitOn) return;
  fitGroup.clear();
  const u = unitsPerPixel();
  for (const row of r.refs) {
    const vis = !hiddenRefs.has(row.id);
    for (const f of row.clash || []) {
      const g = new THREE.BufferGeometry();
      g.setAttribute("position", new THREE.Float32BufferAttribute(f.p, 3));
      g.setIndex(f.i);
      g.computeVertexNormals();
      const m = new THREE.Mesh(g, new THREE.MeshStandardMaterial({ color: CLASH, emissive: 0x7f1d1d, roughness: 0.6, side: THREE.DoubleSide,
        polygonOffset: true, polygonOffsetFactor: -2, polygonOffsetUnits: -2, clippingPlanes: clipPlanes }));
      m.renderOrder = 4; m.userData.ref = row.id; m.visible = vis;
      fitGroup.add(m);
    }
    if (row.points && row.gap > 1e-4) {
      const [a, b] = row.points.map((p) => new THREE.Vector3(...p));
      const line = new THREE.Line(new THREE.BufferGeometry().setFromPoints([a, b]), new THREE.LineBasicMaterial({ color: 0x0f766e, depthTest: false }));
      line.renderOrder = 6; line.userData.ref = row.id; line.visible = vis;
      fitGroup.add(line);
      for (const p of [a, b]) {
        const s = new THREE.Mesh(new THREE.SphereGeometry(u * 3.5, 12, 8), new THREE.MeshBasicMaterial({ color: 0x0f766e, depthTest: false }));
        s.position.copy(p); s.renderOrder = 6; s.userData.ref = row.id; s.visible = vis;
        fitGroup.add(s);
      }
    }
  }
  const fmt = (v) => (+v).toFixed(v < 10 ? 3 : 1).replace(/\.?0+$/, "");
  panel.innerHTML = `<h4>Clash <span class="muted">against the reference bodies</span></h4>` + (r.refs.length ? r.refs.map((row) => {
    const [cls, txt] = row.overlap > 1e-6 ? ["bad", `overlaps ${fmt(row.overlap)} mm³`] : row.touching ? ["good", "touching"]
      : row.gap != null ? ["", `clear, ${fmt(row.gap)} mm gap`] : ["", "—"];
    return `<div class="fitrow" data-ref="${esc(row.id)}"><span class="refsw" style="background:#${refColor(row.id).getHexString()}"></span>`
      + `<b>${esc(row.id)}</b><span class="fitst ${cls}">${txt}</span>${row.note ? `<div class="muted">${esc(row.note)}</div>` : ""}</div>`;
  }).join("") + `<div class="muted">Red: where the part runs into a reference. The green line is the smallest gap.</div>`
    : `<div class="muted">No reference bodies. Import a part as a reference to design around it.</div>`);
}

// ── measure: click faces or edges (two for a distance and angle); the panel also shows the part's mass ──
let measuring = false, mPicks = [];  // [{label, point, mesh} | {edge: i}]
const measureGroup = new THREE.Group();
scene.add(measureGroup);
function setMeasuring(on) {
  if (on && inSketch()) exitSketch();
  if (on && fitOn) setFit(false);
  if (on && sectionOn()) {}  // both can be on
  measuring = on;
  mPicks = [];
  $("#measureBtn").classList.toggle("on", on);
  $("#measurePanel").hidden = !on;
  measureGroup.clear();
  if (on) { pickedFaces = []; pickedEdges = []; pickUpdate(); colorFaces(); runMeasure(); }
  else colorFaces();
}
$("#measureBtn").onclick = () => setMeasuring(!measuring);
function mSphere(p, col) {
  const s = new THREE.Mesh(new THREE.SphereGeometry(unitsPerPixel() * 4, 12, 8), new THREE.MeshBasicMaterial({ color: col, depthTest: false }));
  s.position.set(...p); s.renderOrder = 6;
  return s;
}
async function measurePick(ev) {
  const eh = edgeHit(ev);
  let pk = null;
  if (eh) pk = { edge: eh.object.userData.i, obj: eh.object };
  else {
    const h = pickHit(ev);
    if (h) pk = { label: h.object.userData.labels[0], point: h.point.toArray().map((v) => +v.toFixed(4)), mesh: h.object };
  }
  if (!pk) { mPicks = []; return runMeasure(); }
  mPicks = ev.shiftKey || mPicks.length === 1 ? [...mPicks, pk].slice(-2) : [pk];
  runMeasure();
}
const mm = (v, d = 3) => `${fmt(v, d)} mm`;
async function runMeasure() {
  if (!measuring || !S) return;
  measureGroup.clear();
  colorFaces();
  for (const p of mPicks) if (p.mesh) p.mesh.material.color.copy(PICKED);
  let r;
  try { r = await api("/api/measure", { picks: mPicks.map(({ label, point, edge }) => (edge != null ? { edge } : { label, point })) }); } catch { return; }
  if (!measuring) return;
  const desc = (d) => d.kind === "face"
    ? `<b>${esc(d.surface)} face</b>${d.diameter != null ? ` · ⌀ ${mm(d.diameter)}` : ""}${d.radius != null && d.diameter == null ? ` · R ${mm(d.radius)}` : ""}<br><span class="muted">area ${fmt(d.area, 2)} mm²</span>`
    : `<b>${esc(d.curve)} edge</b> · ${mm(d.length)}${d.diameter != null ? ` · ⌀ ${mm(d.diameter)}` : ""}`;
  let html = `<h4>Measure<span class="grow"></span><button id="mClose" title="Close (Esc)">✕</button></h4>`;
  if (!mPicks.length) html += `<div class="hint">Click a face or edge. Click a second one for the distance and angle between them (shift-click to start over with two).</div>`;
  r.picks.forEach((d, i) => { html += `<div class="mp"><span class="tag">${i + 1}</span><div>${desc(d)}</div></div>`; });
  if (r.between) {
    const b = r.between;
    html += `<div class="big">${b.distance != null ? mm(b.distance) : ""}</div><table>`;
    if (b.plane_distance != null) html += `<tr><td>between the planes</td><td>${mm(b.plane_distance)}</td></tr>`;
    if (b.axis_distance != null) html += `<tr><td>between the axes</td><td>${mm(b.axis_distance)}</td></tr>`;
    if (b.distance != null) html += `<tr><td>Δx Δy Δz</td><td>${fmt(b.dx, 3)}, ${fmt(b.dy, 3)}, ${fmt(b.dz, 3)}</td></tr>`;
    if (b.angle != null) html += `<tr><td>angle</td><td>${fmt(b.angle, 3)}°${b.parallel ? " (parallel)" : ""}</td></tr>`;
    html += `</table>`;
    if (b.p && b.distance > 1e-9) {
      const g = new THREE.BufferGeometry().setFromPoints([new THREE.Vector3(...b.p), new THREE.Vector3(...b.q)]);
      const line = new THREE.Line(g, new THREE.LineDashedMaterial({ color: 0x1d4ed8, dashSize: unitsPerPixel() * 6, gapSize: unitsPerPixel() * 4, depthTest: false }));
      line.computeLineDistances(); line.renderOrder = 6;
      measureGroup.add(line, mSphere(b.p, 0x1d4ed8), mSphere(b.q, 0x1d4ed8));
    }
  }
  for (const p of mPicks) if (p.point) measureGroup.add(mSphere(p.point, 0xea580c));
  if (r.part) {
    const P = r.part;
    html += `<div class="sub">Part</div><table>
      <tr><td>volume</td><td>${fmt(P.volume, 1)} mm³</td></tr>
      <tr><td>surface area</td><td>${fmt(P.area, 1)} mm²</td></tr>
      <tr><td>mass</td><td>${P.mass_g != null ? `${fmt(P.mass_g, 1)} g` : "—"}</td></tr>
      <tr><td>size</td><td>${P.bbox.map((v) => fmt(v, 2)).join(" × ")}</td></tr>
      <tr><td>centre of mass</td><td>${P.center_of_mass.map((v) => fmt(v, 2)).join(", ")}</td></tr></table>
      <div class="muted small">${P.mass_g != null ? `${esc(P.material)}: ${P.density} g/cm³` : P.material ? `No density known for “${esc(P.material)}”` : "Set a material (Part, in the header) for the mass"}</div>`;
  }
  $("#measurePanel").innerHTML = html;
  $("#mClose").onclick = () => setMeasuring(false);
}

// ── section view: clip the model with a plane; the cut shows the inside in a contrasting colour ──
let section = null;  // {axis: "X"|"Y"|"Z", t: 0..1, flip}
const sectionOn = () => !!section;
const capMat = new THREE.MeshBasicMaterial({ color: 0xe07b39, side: THREE.BackSide, clippingPlanes: clipPlanes });
let caps = [];
function partBox() { return new THREE.Box3().setFromObject(partGroup).union(new THREE.Box3().setFromObject(refGroup)); }
function applySection() {
  const had = clipPlanes.length;
  clipPlanes.length = 0;
  for (const c of caps) c.removeFromParent();
  caps = [];
  if (section) {
    const box = partBox();
    if (!box.isEmpty()) {
      // keep the half away from the camera, so the cut faces you (flip keeps the other half)
      const i = "XYZ".indexOf(section.axis), cam = camera.position.getComponent(i) - controls.target.getComponent(i);
      const sg = (cam > 0 ? -1 : 1) * (section.flip ? -1 : 1), n = new THREE.Vector3().setComponent(i, sg);
      const lo = box.min.getComponent(i), hi = box.max.getComponent(i), at = lo + (hi - lo) * section.t;
      clipPlanes.push(new THREE.Plane(n, -sg * at));
      $("#secAt") && ($("#secAt").textContent = `${section.axis} = ${fmt(at, 2)} mm`);
      for (const m of faceMeshes) {  // the part's inside, seen through the cut: its faces from behind
        const c = new THREE.Mesh(m.geometry, capMat);
        c.renderOrder = 1;
        partGroup.add(c);
        caps.push(c);
      }
    }
  }
  if (had !== clipPlanes.length) {  // three compiles the plane count into each shader
    const mats = [...faceMeshes, ...refMeshes, ...edgeObjs].map((o) => o.material).concat([capMat]);
    for (const m of mats) m.needsUpdate = true;
  }
  for (const m of faceMeshes) m.material.side = section ? THREE.FrontSide : THREE.DoubleSide;
}
$("#sectionBtn").onclick = () => {
  section = section ? null : { axis: "Y", t: 0.5, flip: false };
  $("#sectionBtn").classList.toggle("on", !!section);
  $("#sectionPanel").hidden = !section;
  if (section) {
    $("#sectionPanel").innerHTML = `<h4>Section<span class="grow"></span><button id="secClose" title="Close">✕</button></h4>
      <div class="row"><label>plane</label><span class="seg">${["X", "Y", "Z"].map((a) => `<button data-ax="${a}" class="${a === section.axis ? "on" : ""}">${a}</button>`).join("")}</span>
        <label style="width:auto"><input type="checkbox" id="secFlip"> flip</label></div>
      <div class="row"><label>position</label><input type="range" id="secT" min="0" max="1" step="0.001" value="${section.t}"></div>
      <div class="muted" id="secAt"></div>`;
    $("#sectionPanel").querySelectorAll("[data-ax]").forEach((b) => (b.onclick = () => {
      section.axis = b.dataset.ax;
      $("#sectionPanel").querySelectorAll("[data-ax]").forEach((x) => x.classList.toggle("on", x === b));
      applySection();
    }));
    $("#secT").oninput = () => { section.t = +$("#secT").value; applySection(); };
    $("#secFlip").onchange = () => { section.flip = $("#secFlip").checked; applySection(); };
    $("#secClose").onclick = () => $("#sectionBtn").click();
  }
  applySection();
};

// ── editing a feature's settings in the form that made it ──
const EDITABLE = ["extrude", "revolve", "loft", "sweep", "boolean", "hole", "text", "fillet", "chamfer", "shell", "linear_pattern", "circular_pattern", "mirror", "import"];
const val = (v) => (v == null ? "" : esc(String(v)));
async function editFeature(fid, anchor) {
  let j;
  try { j = await api(`/api/feature/${encodeURIComponent(fid)}`); } catch { return; }
  const m = $("#featMenu"), opt = (list, cur) => list.map(([v, t]) => `<option value="${esc(v)}" ${String(v) === String(cur) ? "selected" : ""}>${esc(t ?? v)}</option>`).join("");
  const modes = (cur, extra = []) => `<div class="row"><label>mode</label><select id="efMode">${opt([["add"], ["cut"], ["new"], ["intersect"], ...extra], cur)}</select></div>`;
  let body = "", read, after = null;
  if (j.type === "extrude") {
    body = `${extentRows("ex", j)}
      <div class="row"><label>direction</label><select id="efDir">${opt([["normal"], ["reverse"], ["symmetric"]], j.direction || "normal")}</select></div>${modes(j.mode || "add")}`;
    let readExtent;
    after = () => { readExtent = wireExtent("ex", j.profile.sketch, j, fid); };
    read = () => ({ ...readExtent(), direction: $("#efDir").value, mode: $("#efMode").value });
  } else if (j.type === "boolean") {
    body = `<div class="row"><label>body</label><select id="efTool">${refIds().map((id) => `<option ${id === j.tool ? "selected" : ""}>${esc(id)}</option>`).join("")}</select></div>
      <div class="row"><label>mode</label><select id="efMode">${opt([["cut", "cut it out (a nest)"], ["add", "add it"], ["intersect", "keep the overlap"]], j.mode || "cut")}</select></div>
      <div class="row"><label>clearance</label><input id="efClr" value="${val(j.clearance ?? 0)}"></div>`;
    read = () => ({ tool: $("#efTool").value, mode: $("#efMode").value, clearance: numOrExpr($("#efClr").value || "0") });
  } else if (j.type === "loft") {
    const before = S.features.findIndex((f) => f.id === j.id), ids = sketchesOk().filter((id) => S.features.findIndex((f) => f.id === id) < before);
    body = `<div class="muted">Sections, first to last (tree order):</div>
      <div class="checks">${ids.map((id) => `<label><input type="checkbox" value="${esc(id)}" ${j.sections.includes(id) ? "checked" : ""}> ${esc(id)}</label>`).join("")}</div>
      <div class="row"><label>ruled</label><input id="efRuled" type="checkbox" style="flex:0" ${j.ruled ? "checked" : ""}></div>${modes(j.mode || "add")}`;
    read = () => {
      const secs = pickedIds(m);
      if (secs.length < 2) throw new Error("tick two or more sketches");
      return { sections: secs, ruled: $("#efRuled").checked, mode: $("#efMode").value };
    };
  } else if (j.type === "sweep") {
    const before = S.features.findIndex((f) => f.id === j.id), ids = sketchesOk().filter((id) => S.features.findIndex((f) => f.id === id) < before);
    body = `<div class="row"><label>profile</label><select id="efProf">${sketchOpts(ids, j.profile.sketch)}</select></div>
      <div class="row"><label>path</label><select id="efPath">${sketchOpts(ids, j.path)}</select></div>${modes(j.mode || "add")}`;
    read = () => ({ profile: { ...j.profile, sketch: $("#efProf").value }, path: $("#efPath").value, mode: $("#efMode").value });
  } else if (j.type === "revolve") {
    body = `<div class="row"><label>axis</label><input id="efAxis" value="${val(j.axis)}"></div>
      <div class="row"><label>angle</label><input id="efAng" value="${val(j.angle ?? 360)}"></div>${modes(j.mode || "add")}`;
    read = () => ({ axis: $("#efAxis").value.trim(), angle: numOrExpr($("#efAng").value), mode: $("#efMode").value });
  } else if (j.type === "hole") {
    body = `<div class="row"><label>type</label><select id="efKind">${opt([["simple", "Simple"], ["counterbore", "Counterbore"], ["countersink", "Countersink"]], j.kind || "simple")}</select></div>
      <div class="row"><label>diameter</label><input id="efD" value="${val(j.diameter)}"></div>
      <div class="row" data-k="counterbore"><label>counterbore</label><input id="efCbD" placeholder="⌀" value="${val(j.cbore_diameter)}"><input id="efCbH" placeholder="depth" value="${val(j.cbore_depth)}"></div>
      <div class="row" data-k="countersink"><label>countersink</label><input id="efCsD" placeholder="⌀" value="${val(j.csk_diameter)}"><input id="efCsA" placeholder="angle" value="${val(j.csk_angle ?? 90)}"></div>
      <div class="row"><label>depth</label><select id="efExt">${opt([["through_all", "through all"], ["blind", "blind"]], j.extent || "through_all")}</select><input id="efDepth" value="${val(j.depth ?? 10)}"></div>
      <div class="row"><label>thread</label><input id="efThread" value="${val(j.thread)}" placeholder="e.g. M4x0.7 (tapped)"></div>`;
    read = () => {
      const k = $("#efKind").value, out = { kind: k, diameter: numOrExpr($("#efD").value), extent: $("#efExt").value, thread: $("#efThread").value.trim() || null };
      out.depth = out.extent === "blind" ? numOrExpr($("#efDepth").value) : null;
      Object.assign(out, k === "counterbore" ? { cbore_diameter: numOrExpr($("#efCbD").value), cbore_depth: numOrExpr($("#efCbH").value) } : { cbore_diameter: null, cbore_depth: null });
      Object.assign(out, k === "countersink" ? { csk_diameter: numOrExpr($("#efCsD").value), csk_angle: numOrExpr($("#efCsA").value) } : { csk_diameter: null });
      return out;
    };
  } else if (j.type === "text") {
    body = `<div class="row"><label>text</label><input id="efTx" value="${val(j.text)}"></div>
      <div class="row"><label>height</label><input id="efS" value="${val(j.size ?? 5)}"></div>
      <div class="row"><label>depth</label><input id="efD" value="${val(j.depth ?? 0.5)}"></div>
      <div class="row"><label>style</label><select id="efM">${opt([["cut", "engraved"], ["add", "embossed"]], j.mode || "cut")}</select></div>
      <div class="row"><label>angle °</label><input id="efA" value="${val(j.angle ?? 0)}"></div>`;
    read = () => ({ text: $("#efTx").value, size: numOrExpr($("#efS").value), depth: numOrExpr($("#efD").value), mode: $("#efM").value, angle: numOrExpr($("#efA").value || "0") });
  } else if (j.type === "fillet" || j.type === "chamfer") {
    const key = j.type === "fillet" ? "radius" : "distance";
    body = `<div class="row"><label>${key}</label><input id="efSize" value="${val(j[key])}"></div><div class="muted">${(j.edges || []).length} edge reference(s): change them with Edit edges…</div>`;
    read = () => ({ [key]: numOrExpr($("#efSize").value) });
  } else if (j.type === "shell") {
    body = `<div class="row"><label>wall</label><input id="efT" value="${val(j.thickness)}"></div>
      <div class="row"><label>grows</label><select id="efOut">${opt([["false", "inward"], ["true", "outward"]], String(!!j.outward))}</select></div>
      <div class="muted">Open faces: ${(j.remove_faces || []).map((r) => esc(faceTxt(r))).join(", ") || "none"}</div>`;
    read = () => ({ thickness: numOrExpr($("#efT").value), outward: $("#efOut").value === "true" });
  } else if (j.type === "linear_pattern" || j.type === "circular_pattern") {
    const ids = [...new Set([...replayable(), ...j.features])];
    const checks = `<div class="checks">${ids.map((id) => `<label><input type="checkbox" value="${esc(id)}" ${j.features.includes(id) ? "checked" : ""}> ${esc(id)}</label>`).join("")}</div>`;
    if (j.type === "linear_pattern") {
      const dir = Array.isArray(j.direction) ? j.direction.join(", ") : j.direction;
      body = `${checks}<div class="row"><label>direction</label><input id="efDir" value="${esc(dir)}" title="X, Y, Z, or x, y, z"></div>
        <div class="row"><label>spacing</label><input id="efSp" value="${val(j.spacing)}"></div><div class="row"><label>count</label><input id="efN" value="${val(j.count)}"></div>`;
      read = () => {
        const d = $("#efDir").value.trim(), v = d.includes(",") ? d.split(",").map((x) => numOrExpr(x)) : d;
        return { features: pickedIds(m), direction: v, spacing: numOrExpr($("#efSp").value), count: numOrExpr($("#efN").value) };
      };
    } else {
      body = `${checks}<div class="row"><label>axis</label><input id="efAx" value="${esc(Array.isArray(j.axis) ? j.axis.join(", ") : j.axis ?? "Z")}"></div>
        <div class="row"><label>through</label>${(j.origin || [0, 0, 0]).map((v, k) => `<input id="efO${k}" value="${val(v)}">`).join("")}</div>
        <div class="row"><label>count</label><input id="efN" value="${val(j.count)}"></div><div class="row"><label>angle</label><input id="efAng" value="${val(j.angle ?? 360)}"></div>`;
      read = () => {
        const a = $("#efAx").value.trim();
        return { features: pickedIds(m), axis: a.includes(",") ? a.split(",").map((x) => numOrExpr(x)) : a, origin: [0, 1, 2].map((k) => numOrExpr($(`#efO${k}`).value)),
          count: numOrExpr($("#efN").value), angle: numOrExpr($("#efAng").value) };
      };
    }
  } else if (j.type === "mirror") {
    const ids = [...new Set([...replayable(), ...j.features])], dat = j.plane.datum, off = j.plane.offset ?? 0;
    const at = dat !== "XZ" ? off : typeof off === "number" ? -off : `-(${off})`;
    body = `<div class="checks">${ids.map((id) => `<label><input type="checkbox" value="${esc(id)}" ${j.features.includes(id) ? "checked" : ""}> ${esc(id)}</label>`).join("")}</div>
      <div class="row"><label>plane</label><select id="efPl">${opt([["YZ", "YZ (flip X)"], ["XZ", "XZ (flip Y)"], ["XY", "XY (flip Z)"]], dat)}</select></div>
      <div class="row"><label>at</label><input id="efAt" value="${val(at)}" title="the plane's position along its axis, in world coordinates"></div>`;
    read = () => {
      const d = $("#efPl").value, a = numOrExpr($("#efAt").value || "0"), o = d !== "XZ" ? a : typeof a === "number" ? -a : `-(${a})`;
      return { features: pickedIds(m), plane: o === 0 ? { datum: d } : { datum: d, offset: o } };
    };
  } else if (j.type === "import") {
    const sc = j.scale ?? 1, unit = Object.entries(UNITS).find(([, k]) => k === sc)?.[0];
    body = `<div class="muted">${esc(j.file)}</div>${modes(j.mode || "new", [["reference", "reference"]])}
      <div class="row"><label>scale</label><input id="efSc" value="${val(sc)}" title="${unit ? `file in ${unit}` : ""}"></div>
      <div class="row"><label>rotate °</label>${(j.rotate || [0, 0, 0]).map((v, k) => `<input id="efR${k}" value="${val(v)}">`).join("")}</div>
      <div class="row"><label>move</label>${(j.translate || [0, 0, 0]).map((v, k) => `<input id="efT${k}" value="${val(v)}">`).join("")}</div>`;
    read = () => ({ mode: $("#efMode").value, scale: numOrExpr($("#efSc").value), rotate: [0, 1, 2].map((k) => numOrExpr($(`#efR${k}`).value)),
      translate: [0, 1, 2].map((k) => numOrExpr($(`#efT${k}`).value)) });
  } else return;
  m.innerHTML = `<div class="ttl">Edit ${esc(j.id)} <span class="muted">(${esc(j.type.replace("_", " "))})</span></div>${body}<button class="go" id="efGo">Apply</button><div class="err" id="efErr"></div>`;
  m.classList.add("wide");
  popup(m, anchor || $("#center"));
  if (after) after();
  const kind = () => m.querySelectorAll("[data-k]").forEach((r) => (r.hidden = r.dataset.k !== $("#efKind")?.value));
  if ($("#efKind")) { $("#efKind").onchange = kind; kind(); }
  if ($("#efExt")) { const t = () => ($("#efDepth").hidden = $("#efExt").value !== "blind"); $("#efExt").onchange = t; t(); }
  $("#efGo").onclick = async () => {
    let want;
    try { want = read(); } catch (e) { $("#efErr").textContent = e.message; return; }
    const set = {}, dflt = { draft: 0, distance: 0, extent: "blind", direction: "normal", ruled: false, clearance: 0 };
    for (const [k, v] of Object.entries(want)) if (JSON.stringify(v ?? null) !== JSON.stringify(j[k] ?? dflt[k] ?? null)) set[k] = v;
    m.hidden = true;
    if (Object.keys(set).length) await edit([{ op: "update_feature", id: j.id, set }], `edit ${j.id}`);
  };
}

// ── text: engraved into (or raised from) the face you clicked, centred where you clicked ──
$("#textBtn").onclick = () => {
  if (!S || S.volume == null) return note("Text: the part needs a solid first", "err");
  const pf = lastPicked();
  if (!pf || pf.mesh?.userData?.ref) return note("Text: click the flat face the text goes on first", "err");
  const m = $("#featMenu");
  m.classList.remove("wide");
  m.innerHTML = `<div class="ttl">Text on ${esc(pf.labels[0])}</div>
    <div class="row"><label>text</label><input id="txT" value="" placeholder="e.g. REV A"></div>
    <div class="row"><label>height</label><input id="txS" value="5"></div>
    <div class="row"><label>depth</label><input id="txD" value="0.5"></div>
    <div class="row"><label>style</label><select id="txM"><option value="cut">engraved (cut in)</option><option value="add">embossed (raised)</option></select></div>
    <div class="row"><label>angle °</label><input id="txA" value="0"></div>
    <button class="go" id="txGo">Add text</button>`;
  popup(m, $("#textBtn"));
  $("#txT").focus();
  $("#txGo").onclick = async () => {
    const text = $("#txT").value;
    if (!text.trim()) return note("Text: type something", "err");
    m.hidden = true;
    const ref = faceRef(pf.labels[0], pf.point);
    let at;
    try { at = await api("/api/face_point", { ref, point: pf.point }); } catch { return; }
    const id = nextId("text"), sid = `${id}_at`, [u, v] = at.uv.map((c) => +c.toFixed(2));
    const ang = numOrExpr($("#txA").value || "0");
    const f = { id, type: "text", sketch: sid, at: "p1", text, size: numOrExpr($("#txS").value), depth: numOrExpr($("#txD").value),
      mode: $("#txM").value, intent: `“${text}” ${$("#txM").value === "cut" ? "engraved" : "embossed"} on ${pf.labels[0]}`, ...(ang ? { angle: ang } : {}) };
    const rb = S.rollback, anchor = rb != null && rb > 0 ? { after: S.features[rb - 1].id } : {};
    const ok = await edit([
      { op: "add_feature", ...anchor, feature: { id: sid, type: "sketch", plane: { face: ref }, intent: `position of ${id}`,
        entities: [{ id: "p1", type: "point", at: [u, v] }],
        constraints: [{ type: "distance_x", on: ["origin", "p1"], value: u, name: `${id}_x` }, { type: "distance_y", on: ["origin", "p1"], value: v, name: `${id}_y` }] } },
      { op: "add_feature", after: sid, feature: f },
    ], `text “${text}” on ${pf.labels[0]}`);
    if (ok) {
      if (rb != null) setState((await api("/api/rollback", { index: rb + 2 })).state);
      pickedFaces = []; pickUpdate(); select(id);
    }
  };
};

// ── import a CAD file: upload it next to the part, then choose how it joins the model ──
const UNITS = { mm: 1, cm: 10, m: 1000, in: 25.4, ft: 304.8, "µm": 0.001 };
$("#importBtn").onclick = async () => {
  if (!S) return note("Open or create a part first.", "err");
  const others = (await api("/api/parts")).parts.filter((p) => p !== S.rel);
  const m = $("#featMenu");
  m.classList.remove("wide");
  m.innerHTML = `<div class="ttl">Import</div>
    <a class="menuitem" id="imFromFile"><b>A CAD file…</b><span>STEP, IGES, BREP or STL</span></a>
    ${others.length ? `<div class="muted" style="margin-top:4px">Another part of this folder (it stays live):</div>
      <div class="checks partlist">${others.map((p) => `<a class="menuitem" data-part="${esc(p)}"><b>${esc(p)}</b></a>`).join("")}</div>` : ""}`;
  popup(m, $("#importBtn"));
  $("#imFromFile").onclick = () => { m.hidden = true; $("#importFile").value = ""; $("#importFile").click(); };
  m.querySelectorAll("[data-part]").forEach((a) => (a.onclick = async () => {
    m.hidden = true;
    let info;
    try { info = await busyDo(`Reading ${a.dataset.part}`, () => api("/api/import_part", { path: a.dataset.part })); } catch { return; }
    importForm(info);
  }));
};
$("#importFile").onchange = async () => {
  const f = $("#importFile").files[0];
  $("#importFile").value = "";  // choosing the same file again must still import it
  if (f) await importFlow(f);
};
async function importFlow(file) {
  let info;
  rebuildStart(`Reading ${file.name}`);
  try {
    const r = await fetch(`/api/import?name=${encodeURIComponent(file.name)}`, { method: "POST", body: file });
    info = await r.json().catch(() => ({}));
    if (!r.ok) return note(`Import: ${info.detail || r.statusText}`, "err");
  } finally { rebuildEnd(); }
  importForm(info);
  return info;
}
function importForm(info) {
  const m = $("#featMenu"), hasBody = S.volume != null, solid = info.solids > 0;
  const modes = [["reference", "Reference: design around it (not part of the solid)"], ["new", hasBody ? "New body" : "Base solid of this part"],
    ["add", "Add to the part"], ["cut", "Cut from the part"], ["intersect", "Keep only the overlap"]];
  const def = !solid || info.part ? "reference" : hasBody ? "reference" : "new";
  const sz = info.bbox_size.map((v) => fmt(v, 2)).join(" × ");
  const stem = (info.name.replace(/\.vcad\.json$/, "").replace(/\.[^.]+$/, "").replace(/[^A-Za-z0-9_]+/g, "_").replace(/^(\d)/, "_$1").toLowerCase() || "imported").slice(0, 28);
  m.innerHTML = `<div class="ttl">Import ${esc(info.name)}</div>
    <div class="muted">${info.part ? "VibeCAD part, kept live: its changes show here · " : ""}${esc(info.format.toUpperCase())} · ${info.mesh ? `mesh, ${info.triangles} triangles` : `${info.faces} faces`} · ${info.solids ? `${info.solids} solid${info.solids === 1 ? "" : "s"}${info.sewn ? " (sewn from its surfaces)" : ""}` : "surfaces only: import it as a reference"} · ${sz} (file units)</div>
    <div class="row"><label>name</label><input id="imId" value="${esc(nextId(stem))}"></div>
    <div class="row"><label>as</label><select id="imMode">${modes.map(([v, t]) => `<option value="${v}" ${v === def ? "selected" : ""} ${!solid && v !== "reference" ? "disabled" : ""}>${esc(t)}</option>`).join("")}</select></div>
    <div class="row"><label>units</label><select id="imUnits">${Object.keys(UNITS).map((u) => `<option ${u === info.units_hint ? "selected" : ""}>${u}</option>`).join("")}</select>
      <input id="imScale" title="scale factor to mm (type your own)" value="${UNITS[info.units_hint] ?? 1}"></div>
    <div class="row"><label>place</label><select id="imPlace"><option value="keep">where the file puts it</option><option value="origin">centred on the origin, on XY</option></select></div>
    <div class="row"><label>rotate °</label><input id="imRx" value="0" title="about X"><input id="imRy" value="0" title="about Y"><input id="imRz" value="0" title="about Z"></div>
    <div class="muted" id="imSize"></div>
    <button class="go" id="imGo">Import</button>`;
  popup(m, $("#importBtn"));
  const scale = () => { const k = parseFloat($("#imScale").value); return k > 0 ? k : 1; };
  const upd = () => { $("#imSize").textContent = `Size in the part: ${info.bbox_size.map((v) => fmt(v * scale(), 2)).join(" × ")} mm`; };
  $("#imUnits").onchange = () => { $("#imScale").value = UNITS[$("#imUnits").value]; upd(); };
  $("#imScale").oninput = upd;
  upd();
  $("#imGo").onclick = async () => {
    const id = $("#imId").value.trim(), mode = $("#imMode").value, k = scale();
    if (!/^[A-Za-z_]\w*$/.test(id)) return note("Import: the name must be letters, digits and underscores", "err");
    const f = { id, type: "import", file: info.file, mode, intent: `${info.name}, imported ${{ reference: "as a reference body (not part of the solid)", new: hasBody ? "as a new body" : "as the base solid", add: "and added", cut: "and cut away", intersect: "and intersected" }[mode]}` };
    if (k !== 1) f.scale = k;
    const rot = ["#imRx", "#imRy", "#imRz"].map((q) => numOrExpr($(q).value || "0"));
    if (rot.some((v) => v !== 0)) f.rotate = rot;
    if ($("#imPlace").value === "origin") {  // centre in X and Y, sit on XY (only exact for no rotation)
      const c = info.bbox_min.map((v, i) => (v + info.bbox_size[i] / (i < 2 ? 2 : 1e9)) * k);
      f.translate = [-c[0], -c[1], -info.bbox_min[2] * k].map((v) => +v.toFixed(4));
    }
    m.hidden = true;
    if (await addFeature(f, `import ${info.name}`)) { select(id); fitView("iso"); }
  };
}
// drop a CAD file anywhere on the 3D view to import it
$("#center").addEventListener("dragover", (ev) => { if ([...ev.dataTransfer.items].some((i) => i.kind === "file")) ev.preventDefault(); });
$("#center").addEventListener("drop", (ev) => {
  const f = [...ev.dataTransfer.files].find((x) => /\.(step|stp|iges|igs|brep|brp|stl)$/i.test(x.name));
  if (!f) return;
  ev.preventDefault();
  if (!S) return note("Open or create a part first.", "err");
  importFlow(f);
});

// ── export: download the shown part ──
$("#exportBtn").onclick = () => {
  if (!S) return;
  const m = $("#featMenu");
  const opts = [["step", "STEP", "exact solid for other CAD tools"], ["stl", "STL", "mesh for 3D printing"], ["3mf", "3MF", "mesh with units, for slicers"],
    ["brep", "BREP", "OpenCascade's own format"], ["glb", "glTF (GLB)", "for viewers and the web"],
    ["svg", "Drawing (SVG)", "dimensioned 2D views with hole callouts"]];
  m.innerHTML = `<div class="ttl">Export ${esc(S.name)}${S.rollback != null ? " (as rolled back)" : ""}</div>` +
    opts.map(([f, n, d]) => `<a class="menuitem" data-fmt="${f}" href="/api/export?fmt=${f}" download><b>${n}</b><span>${d}</span></a>`).join("");
  popup(m, $("#exportBtn"));
  m.querySelectorAll("a").forEach((a) => (a.onclick = () => { m.hidden = true; }));
};

window.vibecadView = {  // for browser tests and the devtools console
  toScreen: (x, y, z) => {
    const p = new THREE.Vector3(x, y, z).project(camera), r = renderer.domElement.getBoundingClientRect();
    return [r.left + ((p.x + 1) / 2) * r.width, r.top + ((1 - p.y) / 2) * r.height];
  },
  pickedFace: () => lastPicked() && { labels: lastPicked().labels, point: lastPicked().point },
  pickedFaces: () => pickedFaces.map((p) => p.labels[0]),
  pickedEdges: () => pickedEdges.map((p) => ({ i: p.i, label: p.label, ref: p.ref })),
  edgeEdit: () => EE && { fid: EE.fid, edges: [...eeIdx()], refs: EE.items.map((x) => x.ref) },
  edgeScreen: (i) => {  // screen point of the middle of edge i's polyline, and whether a face hides it there
    const o = edgeObjs.find((x) => x.userData.i === i);
    if (!o) return null;
    const a = o.geometry.attributes.position, k = Math.floor((a.count - 1) / 2);
    const p = new THREE.Vector3().fromBufferAttribute(a, k).lerp(new THREE.Vector3().fromBufferAttribute(a, k + 1), 0.5);
    const q = p.clone().project(camera), r = renderer.domElement.getBoundingClientRect();
    return [r.left + ((q.x + 1) / 2) * r.width, r.top + ((1 - q.y) / 2) * r.height];
  },
  edgeIds: () => edgeObjs.map((o) => o.userData.i),
  refs: () => [...new Set(refMeshes.map((m) => m.userData.ref))],
  refVisible: (id) => refMeshes.filter((m) => m.userData.ref === id).every((m) => m.visible),
  refColorOf: (id) => "#" + refColor(id).getHexString(),
  fitObjects: () => fitGroup.children.map((o) => ({ ref: o.userData.ref, type: o.type, visible: o.visible })),
  clip: () => clipPlanes.map((p) => ({ normal: p.normal.toArray(), constant: p.constant })),
  viewDir: () => controls.target.clone().sub(camera.position).normalize().toArray().map((v) => +v.toFixed(4)),
  measuring: () => measuring,
  measurePicks: () => mPicks.map((p) => p.label || `edge ${p.edge}`),
};

// ── dialogs ───────────────────────────────────────────────────────
$("#rendersBtn").onclick = () => {
  if (!S) return;
  $("#rendersImg").src = `/api/render.png?views=iso,iso_back,iso_below,top&highlight=${encodeURIComponent(selected || "")}&v=${encodeURIComponent(S.rev)}`;
  $("#rendersDlg").showModal();
};
// part properties: name, material (the Measure panel's mass), process, notes
const DENSITY_HINTS = [[/titanium|ti-?6al/i, 4.43], [/stainless|304|316/i, 8.0], [/steel/i, 7.85], [/alumin/i, 2.70], [/brass/i, 8.5], [/bronze/i, 8.8],
  [/copper/i, 8.96], [/\bpla\b/i, 1.24], [/petg|\bpet\b/i, 1.27], [/\babs\b/i, 1.04], [/\basa\b/i, 1.07], [/nylon|\bpa\d*\b|polyamide/i, 1.14],
  [/\btpu\b/i, 1.21], [/polycarbonate|\bpc\b/i, 1.20], [/acetal|delrin|\bpom\b/i, 1.41], [/resin/i, 1.15], [/plywood|wood|mdf/i, 0.65]];
$("#propsBtn").onclick = () => {
  if (!S) return note("Open or create a part first.", "err");
  $("#ppName").value = S.name || ""; $("#ppMat").value = S.material || ""; $("#ppProc").value = S.process || ""; $("#ppNotes").value = S.design_notes || "";
  const dens = () => {
    const hit = DENSITY_HINTS.find(([re]) => re.test($("#ppMat").value));
    $("#ppDensity").textContent = hit ? `Density ${hit[1]} g/cm³: Measure shows the mass` : $("#ppMat").value ? "Unknown density: Measure can't show a mass for this material" : "";
  };
  $("#ppMat").oninput = dens; dens();
  $("#propsDlg").showModal();
};
$("#propsDlg").onclose = async () => {
  if ($("#propsDlg").returnValue !== "save" || !S) return;
  const set = {}, now = { name: $("#ppName").value.trim(), material: $("#ppMat").value.trim() || null, process: $("#ppProc").value.trim() || null,
    design_notes: $("#ppNotes").value.trim() || null };
  for (const [k, v] of Object.entries(now)) if ((v || null) !== (S[k] || null) && !(k === "name" && !v)) set[k] = v;
  if (Object.keys(set).length) await edit([{ op: "set_meta", set }], "part properties");
};

// light / dark theme (the page sets it from localStorage or the system before it paints)
function themeUpdate() {
  const dark = isDark();
  $("#themeBtn").innerHTML = ICON[dark ? "sun" : "moon"];
  $("#themeBtn").title = dark ? "Light theme" : "Dark theme";
  setSketchTheme(dark);
  if (gridBox) setGrid(gridBox);
  if (inSketch()) SK.refresh();
}
$("#themeBtn").onclick = () => {
  document.documentElement.dataset.theme = isDark() ? "light" : "dark";
  try { localStorage.setItem("vibecad.theme", document.documentElement.dataset.theme); } catch {}
  themeUpdate();
};
themeUpdate();

// keyboard shortcuts: ? shows them
const KEYS = [["Model", ""], ["M", "Measure"], ["F", "Fit the part in view"], ["Ctrl/⌘ Z", "Undo"], ["Ctrl/⌘ Shift Z", "Redo"], ["Double-click a feature", "Edit it"],
  ["Shift-click", "Pick more faces or edges"], ["Esc", "Leave measure / reference picking"], ["Sketch", ""], ["S L R C A", "Select, Line, Rectangle, Circle, Arc"], ["O N P", "Slot, Polygon, Point"],
  ["M", "Mark (freehand, for the agent)"], ["F", "Round the selected corner"], ["K", "Offset the selected outline"], ["H V E T D", "Horizontal, Vertical, Equal, Tangent, Dimension"], ["G", "Construction on/off"], ["Delete", "Delete the selection"],
  ["Esc", "Cancel the tool, clear the selection, then leave the sketch"], ["", ""], ["?", "This list"]];
function showKeys() {
  $("#keysList").innerHTML = KEYS.map(([k, d]) => (!d ? (k ? `<div class="h">${k}</div>` : "") : `<div>${k.split(" / ").map((x) => `<kbd>${esc(x)}</kbd>`).join(" / ")}</div><div>${esc(d)}</div>`)).join("");
  $("#keysDlg").showModal();
}

$("#historyBtn").onclick = async () => {
  if (!S) return;
  const r = await api("/api/history");
  $("#history").innerHTML = r.history.slice().reverse().map((h) =>
    `<li><span class="muted">${esc(h.time.slice(11))}</span> <span class="who">${esc(h.author)}</span> ${esc(h.message || "")}
     ${h.rejected ? `<span class="rej">rejected: ${esc(h.rejected.slice(0, 200))}</span>` : `<span class="muted">(${h.n_ops} ops)</span>`}</li>`).join("");
  $("#historyDlg").showModal();
};

// ── agent log ─────────────────────────────────────────────────────
const log = $("#log");
function resetLog() {
  log.innerHTML = ""; $("#metrics").innerHTML = "";
  for (const k of Object.keys(toolRows)) delete toolRows[k];
  for (const k of Object.keys(lastToolByName)) delete lastToolByName[k];
}
function scrollDown() { if (log.scrollHeight - log.scrollTop - log.clientHeight < 200) log.scrollTop = log.scrollHeight; }
function add(el) { log.appendChild(el); scrollDown(); return el; }
function md(t) { return esc(t).replace(/`([^`]+)`/g, "<code>$1</code>").replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>"); }
function addUser(text, sel, scope, entities, face, marks, atts) {
  const d = document.createElement("div");
  d.className = "msg user";
  const what = (sel ? esc(sel) + (entities?.length ? `: ${esc(entities.join(", "))}` : "") : "")
    + (face ? `${sel ? " · " : ""}face ${esc(face)}` : "") + (marks ? ` · ${marks} mark${marks > 1 ? "s" : ""}` : "");
  const chips = esc(text).replace(/@(face|edge|sketch):(\S+)/g, (_, k, v) => `<span class="ref ${k}" title="@${k}:${v}">${k === "edge" ? v.replace("|", " | ") : v}</span>`);
  d.innerHTML = chips + (what ? `<span class="sel">selected: ${what}${scope ? " (edits limited to it)" : ""}</span>` : "")
    + (atts?.length ? `<div class="atts">${atts.map((a) => /\.(png|jpe?g|gif|webp)$/i.test(a.name)
      ? `<img src="/api/upload/${a.id.split("/").map(encodeURIComponent).join("/")}" alt="${esc(a.name)}" title="${esc(a.name)}">`
      : `<span class="att"><span class="fi">${esc((a.name.split(".").pop() || "file").slice(0, 4).toUpperCase())}</span><span class="nm">${esc(a.name)}</span></span>`).join("")}</div>` : "");
  d.querySelectorAll(".atts img").forEach((img) => (img.onclick = () => { $("#imgBig").src = img.src; $("#imgDialog").showModal(); }));
  add(d);
}
function addAgent(text) { const d = document.createElement("div"); d.className = "msg agent"; d.innerHTML = md(text); add(d); }
function addThinking(text) {
  if (!text) return;
  const d = document.createElement("details");
  d.className = "think";
  d.innerHTML = `<summary>thinking (${text.length.toLocaleString()} chars)</summary><div></div>`;
  d.querySelector("div").textContent = text;
  add(d);
}
function note(text, cls = "") { const d = document.createElement("div"); d.className = `msg ${cls}`; d.textContent = text; add(d); }
function toolDesc(name, input) {
  if (name === "apply_ops") return `${(input.ops || []).length} ops · ${input.message || ""}`;
  if (name === "render") return (input.views || ["default views"]).join(", ") + (input.highlight?.length ? ` · highlight ${input.highlight.join(", ")}` : "");
  return Object.keys(input || {}).length ? JSON.stringify(input).slice(0, 120) : "";
}
function addTool(e) {
  toolCount++;
  const d = document.createElement("details");
  d.className = "tool";
  d.innerHTML = `<summary><span class="name">${esc(e.name)}</span><span class="desc">${esc(toolDesc(e.name, e.input))}</span><span class="res pending">…</span></summary>
    <pre>${esc(JSON.stringify(e.input, null, 1))}</pre>`;
  toolRows[e.id] = d;
  lastToolByName[e.name] = d;
  add(d);
  updateLive();
}
function toolResult(e) {
  const d = toolRows[e.id];
  if (!d) return;
  const res = d.querySelector(".res");
  let cls = e.is_error ? "err" : "ok", label = e.is_error ? "error" : "ok";
  try {
    const j = JSON.parse(e.text);
    if (j.error) { cls = "err"; label = "rejected"; }
    else if (j.errors || j.warnings) { cls = "warn"; label = j.errors ? "applied, errors" : "applied, warnings"; }
    else if (j.change && j.change.volume_mm3) { const [a, b] = j.change.volume_mm3; label = `ok · ${fmt(a, 0)} → ${fmt(b, 0)} mm³`; }
  } catch { /* plain text */ }
  res.className = `res ${cls}`;
  res.textContent = label;
  if (e.text) { const pre = document.createElement("pre"); pre.textContent = e.text; d.appendChild(pre); }
}
function toolImage(e) {
  const d = lastToolByName[e.name];
  if (!d) return;
  const img = document.createElement("img");
  img.src = `data:image/png;base64,${e.png_b64}`;
  img.onclick = (ev) => { ev.preventDefault(); $("#imgBig").src = img.src; $("#imgDialog").showModal(); };
  d.appendChild(img);
  d.open = true;
  scrollDown();
}
function setBusy(b, startedAt) {
  busy = b;
  $("#stopBtn").disabled = !b;
  $("#sendBtn").disabled = b;
  clearInterval(runTimer);
  if (b) {
    if (startedAt) { runStart = startedAt * 1000; toolCount = 0; }
    else if (!runStart) runStart = Date.now();
    runTimer = setInterval(updateLive, 500); updateLive();
  }
}
function updateLive() {
  if (!busy || !runStart) return;
  let now = "";
  if (phase) {
    const what = { thinking: "thinking", text: "writing a reply", tool_input: `writing ${phase.tool || "tool"} arguments` }[phase.phase] || phase.phase;
    now = ` · <span class="now">${what}${phase.chars ? ` (${(phase.chars / 1000).toFixed(1)}k chars, ${phase.seconds ?? 0} s)` : ""}</span>`;
  } else if (Object.values(toolRows).some((d) => d.querySelector(".res.pending"))) now = ` · <span class="now">running tool</span>`;
  $("#metrics").innerHTML = `working · <b>${((Date.now() - runStart) / 1000).toFixed(0)} s</b> · ${toolCount} tool calls${now}`;
}
function showMetrics(m) {
  if (!m) return;
  const tools = Object.entries(m.tool_counts || {}).map(([k, v]) => `${k}×${v}`).join(", ");
  $("#metrics").innerHTML = `last run: <b>${fmt(m.wall_s, 0)} s</b> (thinking ${fmt(m.thinking_s, 0)} s, writing tool args ${fmt(m.tool_input_s, 0)} s, tools ${fmt(m.tool_s, 0)} s) · ${m.turns} turns ·
    ${m.n_tool_calls} tool calls${m.ops_rejected ? `, ${m.ops_rejected} rejected` : ""} · ${m.output_tokens} out tok · $${fmt(m.cost_usd, 2)} · ${esc(m.model)}<br><span>${esc(tools)}</span>`;
}

// ── inputs ────────────────────────────────────────────────────────
$("#promptForm").onsubmit = (ev) => {
  ev.preventDefault();
  let { text, refs } = promptContent();
  if (busy || (!text && !attachments.length)) return;
  if (attachments.some((a) => !a.id)) return note("Wait for the attachments to finish uploading.", "err");
  if (!text) text = "(see the attached files)";
  const entities = inSketch() && SK.selection().length ? SK.selection() : null;
  const marks = inSketch() && SK.marks().length ? SK.marks() : null;
  const face = !inSketch() && lastPicked() ? { labels: lastPicked().labels, point: lastPicked().point } : null;
  send({ type: "prompt", text, selection: selected, scope: $("#scope").checked, entities, marks, face, refs: refs.length ? refs : null,
         attachments: attachments.length ? attachments.map((a) => a.id) : null });
  if (marks) SK.clearMarks();
  attachments.forEach((a) => a.url && URL.revokeObjectURL(a.url));
  attachments = [];
  renderAttach();
  $("#prompt").innerHTML = "";
};
$("#prompt").onkeydown = (ev) => { if (ev.key === "Enter" && !ev.shiftKey) { ev.preventDefault(); $("#promptForm").requestSubmit(); } };
$("#prompt").onpaste = (ev) => {  // plain text only: pasted markup would become part of the message; pasted files attach
  ev.preventDefault();
  if (ev.clipboardData.files?.length) return void addFiles(ev.clipboardData.files);
  document.execCommand("insertText", false, ev.clipboardData.getData("text/plain"));
};

// ── attachments: files for the agent (images and PDFs it reads directly, text files inline) ──
let attachments = [];  // {id (once uploaded), name, url (image preview)}
function renderAttach() {
  const box = $("#attachList");
  box.hidden = !attachments.length;
  box.innerHTML = "";
  attachments.forEach((a) => {
    const el = Object.assign(document.createElement("span"), { className: "att" + (a.id ? "" : " up"), title: a.id ? a.name : `uploading ${a.name}…` });
    el.innerHTML = (a.url ? `<img src="${a.url}" alt="">` : `<span class="fi">${esc((a.name.split(".").pop() || "file").slice(0, 4).toUpperCase())}</span>`)
      + `<span class="nm">${esc(a.name)}</span><button type="button" class="x" title="Remove">×</button>`;
    el.querySelector(".x").onclick = () => { attachments = attachments.filter((x) => x !== a); if (a.url) URL.revokeObjectURL(a.url); renderAttach(); };
    box.appendChild(el);
  });
}
async function addFiles(files) {
  await Promise.all([...files].map(async (f) => {
    const name = f.name && f.name !== "image.png" ? f.name : `pasted-${Date.now()}.${(f.type.split("/")[1] || "png").replace("jpeg", "jpg")}`;
    const a = { name, url: f.type.startsWith("image/") ? URL.createObjectURL(f) : null };
    attachments.push(a);
    renderAttach();
    try {
      const r = await fetch(`/api/upload?name=${encodeURIComponent(name)}`, { method: "POST", body: f });
      const j = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(j.detail || r.statusText);
      a.id = j.id;
    } catch (e) {
      note(`Couldn't attach ${name}: ${e.message}`, "err");
      attachments = attachments.filter((x) => x !== a);
    }
    renderAttach();
  }));
}
$("#attachBtn").onclick = () => $("#attachInput").click();
$("#attachInput").onchange = (ev) => { addFiles(ev.target.files); ev.target.value = ""; };
{
  const right = $("#right");
  const has = (ev) => [...(ev.dataTransfer?.types || [])].includes("Files");
  right.addEventListener("dragover", (ev) => { if (has(ev)) { ev.preventDefault(); right.classList.add("drop"); } });
  right.addEventListener("dragleave", (ev) => { if (!right.contains(ev.relatedTarget)) right.classList.remove("drop"); });
  right.addEventListener("drop", (ev) => { if (!has(ev)) return; ev.preventDefault(); right.classList.remove("drop"); addFiles(ev.dataTransfer.files); });
}

// ── references in the message: ⌖ Reference, then click a face / edge (or sketch entity) → a chip in the text ──
const refData = new Map();  // chip id -> {token, kind, label, ref, point, sketch, key}
let refSeq = 0, refPick = false, promptRange = null;
function promptContent() {  // the message text with each chip as its @token, and the references it contains
  const refs = [];
  const walk = (n) => {
    if (n.nodeType === 3) return n.textContent;
    if (n.classList?.contains("ref")) {
      const r = refData.get(n.dataset.rid);
      if (!r) return n.textContent;
      if (!refs.includes(r)) refs.push(r);
      return r.token;
    }
    if (n.nodeName === "BR") return "\n";
    const inner = [...n.childNodes].map(walk).join("");
    return n !== $("#prompt") && n.nodeName === "DIV" ? "\n" + inner : inner;
  };
  return { text: walk($("#prompt")).replace(/\u00a0/g, " ").trim(), refs };
}
function saveCaret() {
  const s = window.getSelection();
  if (s.rangeCount && $("#prompt").contains(s.getRangeAt(0).startContainer)) promptRange = s.getRangeAt(0).cloneRange();
}
document.addEventListener("selectionchange", saveCaret);
function insertRef(r) {
  const id = String(++refSeq);
  refData.set(id, r);
  const chip = Object.assign(document.createElement("span"), { className: `ref ${r.kind}`, contentEditable: "false", textContent: r.short || r.label });
  chip.dataset.rid = id;
  chip.title = r.token;
  const box = $("#prompt");
  let range = promptRange && box.contains(promptRange.startContainer) ? promptRange : null;
  if (!range) { range = document.createRange(); range.selectNodeContents(box); range.collapse(false); }
  const space = document.createTextNode("\u00a0");
  range.deleteContents();
  range.insertNode(space);
  range.insertNode(chip);
  const after = document.createRange();
  after.setStartAfter(space); after.collapse(true);
  promptRange = after.cloneRange();
  const caret = () => { box.focus(); const s = window.getSelection(); s.removeAllRanges(); s.addRange(promptRange); };
  caret();
  setTimeout(caret, 0);  // a pick made on pointerdown: the click's own focus change comes after, so restore the caret then
}
function startRefPick() {
  if (!S) return note("Open a part first.", "err");
  if (inSketch() && SK.selection().length) return refSketchSelection();  // already selected in the sketch: use that
  if (inSketch() && SK.tool() !== "select") SK.setTool("select");
  refPick = true;
  document.body.classList.add("refpick");
  $("#refBtn").classList.add("on");
  $("#refHint").textContent = inSketch() ? "Click a sketch entity or dimension to reference it · Esc cancels"
    : "Click a face or edge to reference it · Esc cancels";
  $("#refHint").hidden = false;
}
function endRefPick() {
  refPick = false;
  document.body.classList.remove("refpick");
  $("#refBtn").classList.remove("on");
  $("#refHint").hidden = true;
}
function refSketchSelection() {
  const sid = SK.active(), d = SK.data();
  for (const key of SK.selection()) {
    const c = key.startsWith("#") ? d?.constraints?.find((x) => `#${x.index}` === key) : null;
    const label = c ? (c.name || `${c.type} ${key}`) : key;
    insertRef({ token: `@sketch:${sid}/${c?.name || key}`, kind: "sketch", label, short: label, sketch: sid, key });
  }
  endRefPick();
}
$("#refBtn").onpointerdown = (ev) => ev.preventDefault();  // keep the caret in the message
$("#refBtn").onclick = () => (refPick ? endRefPick() : startRefPick());
document.addEventListener("keydown", (ev) => { if (refPick && ev.key === "Escape") { ev.stopPropagation(); endRefPick(); } }, true);
async function refFromView(ev) {  // a click in the 3D view while picking a reference
  const eh = edgeHit(ev);
  if (eh) {
    try {
      const r = await api(`/api/edge/${eh.object.userData.i}`);
      const [a, b] = r.label.split(" | ");
      insertRef({ token: `@edge:${a}|${b}`, kind: "edge", label: r.label, short: `${a} | ${b}`, ref: r.ref, point: r.point });
    } catch (e) { note(`That edge can't be referenced: ${e.message}`, "err"); }
    return endRefPick();
  }
  const hit = pickHit(ev);
  if (!hit) return;  // missed: stay in pick mode
  const label = hit.object.userData.labels[0], point = hit.point.toArray();
  const ref = faceRef(label, point);
  if (!ref) { note(`can't make a face reference from ${label}`, "err"); return endRefPick(); }
  insertRef({ token: `@face:${label}`, kind: "face", label, ref, point: point.map((v) => +v.toFixed(3)) });
  endRefPick();
}
$("#stopBtn").onclick = () => send({ type: "stop" });
$("#resetBtn").onclick = () => send({ type: "reset" });
$("#model").onchange = (ev) => send({ type: "config", model: ev.target.value });
$("#effort").onchange = (ev) => send({ type: "config", effort: ev.target.value });
const undoRedo = (which) => busyDo(which === "undo" ? "Undoing" : "Redoing", async () => {
  rebuildPhase = which === "undo" ? "Undoing" : "Redoing";
  const r = await api(`/api/${which}`, {}).catch(() => null);
  if (r?.state) setState(r.state);
});
$("#undoBtn").onclick = () => undoRedo("undo");
$("#redoBtn").onclick = () => undoRedo("redo");
$("#partSelect").onchange = async (ev) => {
  if (!ev.target.value) return;
  if (busy) { note("The agent is working on this part: stop it (or wait) before switching parts.", "err"); return loadParts(); }
  if (EE) await endEdgeEdit(false);
  exitSketch(); selected = null;
  await api("/api/rollback", { index: null }).catch(() => {});
  const r = await busyDo("Opening " + ev.target.value, () => api("/api/open", { path: ev.target.value })).catch(() => null);
  if (!r) return loadParts();
  setState(r.state, { fit: true });
};
$("#newBtn").onclick = () => {
  if (busy) return note("The agent is working on this part: stop it (or wait) before starting another.", "err");
  $("#npName").value = ""; $("#npMat").value = "";
  $("#newForm").querySelector("input[value=empty]").checked = true;
  $("#npWhere").textContent = "Saved as parts/<name>.vcad.json";
  $("#npName").oninput = () => { $("#npWhere").textContent = `Saved as parts/${$("#npName").value.trim() || "<name>"}.vcad.json`; };
  $("#newDlg").showModal();
  $("#npName").focus();
};
$("#newDlg").onclose = async () => {
  if ($("#newDlg").returnValue !== "create") return;
  const name = $("#npName").value.trim(), mat = $("#npMat").value.trim();
  const start = $("#newForm").querySelector("input[name=npStart]:checked").value;
  if (!/^[A-Za-z0-9_-]+$/.test(name)) return note("New part: use letters, digits, - and _ in the name", "err");
  if (EE) await endEdgeEdit(false);
  exitSketch(); selected = null;
  await api("/api/rollback", { index: null }).catch(() => {});
  const r = await api("/api/new", { path: `parts/${name}.vcad.json`, name });
  setState(r.state, { fit: true });
  loadParts();
  if (mat) await edit([{ op: "set_meta", set: { material: mat } }], "material");
  if (start === "import") $("#importBtn").click();
};
function welcomeUpdate(parts) {  // no part open: a start card instead of an empty view, and nothing to act on
  $("#welcome").hidden = !!S;
  for (const b of document.querySelectorAll("#modelTools button.rb, #propsBtn, #historyBtn, #rendersBtn")) b.disabled = !S;
  if (!S) { $("#undoBtn").disabled = $("#redoBtn").disabled = true; }
  else { modelToolsUpdate(); pickUpdate(); }
  if (S || !parts) return;
  $("#wParts").innerHTML = parts.length ? parts.map((p) => `<button data-p="${esc(p)}">${esc(p)}</button>`).join("") : `<span class="muted small">none yet</span>`;
  $("#wParts").querySelectorAll("button").forEach((b) => (b.onclick = () => { $("#partSelect").value = b.dataset.p; $("#partSelect").dispatchEvent(new Event("change")); }));
}
$("#wNew").onclick = () => $("#newBtn").click();
$("#wImport").onclick = () => { $("#newBtn").click(); $("#newForm").querySelector("input[value=import]").checked = true; };
document.addEventListener("keydown", (ev) => {
  if (ev.target.matches("input, textarea, [contenteditable=true]")) return;
  if (inSketch() && SK.key(ev)) { ev.preventDefault(); return; }
  if (ev.key === "Escape" && inSketch()) { $("#exitSketch").click(); return; }
  if (ev.key === "Escape" && measuring) { setMeasuring(false); return; }
  if (ev.key === "?") { showKeys(); return; }
  if (!inSketch() && !ev.metaKey && !ev.ctrlKey && !ev.altKey) {
    if (ev.key === "m") { setMeasuring(!measuring); return; }
    if (ev.key === "f") { fitView("iso"); return; }
  }
  if ((ev.metaKey || ev.ctrlKey) && ev.key.toLowerCase() === "z") { ev.preventDefault(); undoRedo(ev.shiftKey ? "redo" : "undo"); }
});

connect();
loop();
