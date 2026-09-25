import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";

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
      $("#log").innerHTML = "";
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
    case "user_prompt": addUser(e.text, e.selection, e.scope); break;
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
    case "conversation_reset": $("#log").innerHTML = ""; $("#metrics").innerHTML = ""; break;
  }
}

// ── part state ────────────────────────────────────────────────────
const rollIndex = () => (S && S.rollback != null ? S.rollback : S ? S.features.length : 0);

function setState(state, { fit = false, flash = false } = {}) {
  const before = S ? new Map(S.features.map((f) => [f.id, f.status + (f.warnings || []).join()])) : null;
  S = state;
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
  renderParams();
  renderDetails();
  loadMesh(fit);
  if (sketchMode) refreshSketch();
}

function renderTree(before) {
  const ol = $("#tree");
  ol.innerHTML = "";
  const n = S.features.length, rb = rollIndex();
  S.features.forEach((f, i) => {
    if (i === rb) ol.appendChild(rollbar());
    const li = document.createElement("li");
    const st = f.status === "error" ? "error" : f.warnings?.length ? "warn" : "ok";
    li.className = `feat ${st}` + (f.id === selected ? " selected" : "") + (i >= rb ? " rolled" : "");
    const meta = f.status === "error" ? "error" : f.warnings?.length ? "warning" : f.type === "sketch" ? `${f.dof ?? "?"} DOF` : "";
    li.innerHTML = `<span class="ico">${icon(f.type)}</span><span class="fid" title="${esc(f.type)}: ${esc(f.intent || "")}">${esc(f.id)}</span><span class="meta ${st}">${esc(meta)}</span>`
      + (f.status === "error" ? `<span class="err-msg">${esc(f.message)}</span>` : "")
      + (f.warnings || []).map((w) => `<span class="warn-msg">${esc(w)}</span>`).join("");
    if (before && before.get(f.id) !== f.status + (f.warnings || []).join()) li.classList.add("flash");
    li.dataset.index = i;
    li.onclick = () => select(f.id === selected ? null : f.id);
    ol.appendChild(li);
  });
  if (rb >= n) ol.appendChild(rollbar());
}

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

function renderParams() {
  const tb = $("#params tbody");
  tb.innerHTML = "";
  for (const p of S.params) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td title="${esc(p.name)}">${esc(p.name)}</td><td><input value="${esc(p.expr)}"></td><td class="val">${fmt(p.value, 3)}</td>`;
    const inp = tr.querySelector("input");
    inp.onkeydown = (ev) => { if (ev.key === "Enter") inp.blur(); if (ev.key === "Escape") { inp.value = p.expr; inp.blur(); } };
    inp.onchange = async () => {
      if (!(await edit([{ op: "set_param", name: p.name, value: inp.value }], `set ${p.name} = ${inp.value}`))) inp.value = p.expr;
    };
    tb.appendChild(tr);
  }
}

async function renderDetails() {
  const d = $("#details");
  $("#selName").textContent = selected || "";
  if (!selected) { d.innerHTML = "Click a feature in the tree or a face in the view."; d.className = "muted small"; return; }
  const f = S.features.find((x) => x.id === selected);
  const json = await api(`/api/feature/${encodeURIComponent(selected)}`);
  d.className = "";
  d.innerHTML = `<div class="intent small">${esc(f.intent || "No intent written.")}</div>
    <textarea spellcheck="false"></textarea>
    <div class="row"><button id="applyFeat">Apply edit</button><span class="grow"></span>
    <button id="askAbout" title="Ask the agent about this feature">Ask agent</button></div>`;
  d.querySelector("textarea").value = JSON.stringify(json, null, 1);
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
  d.querySelector("#askAbout").onclick = () => { $("#prompt").focus(); $("#prompt").placeholder = `Ask about or change ${selected}…`; };
}

function select(id) {
  selected = id;
  const f = id && S.features.find((x) => x.id === id);
  if (f && f.type === "sketch" && S.features.indexOf(f) < rollIndex()) enterSketch(id);
  else exitSketch();
  renderTree(null);
  renderDetails();
  colorFaces();
}

async function edit(ops, message) {  // true if the batch was applied
  let r;
  try { r = await api("/api/ops", { ops, message }); } catch { return false; }
  const rep = r.report;
  if (!rep.applied) note(rep.error || "edit rejected", "err");
  else if (rep.errors || rep.warnings) note([...(rep.errors || []), ...(rep.warnings || [])].join("\n"), "err");
  return !!rep.applied;
}

async function loadParts() {
  const r = await api("/api/parts");
  $("#partSelect").innerHTML = `<option value="">— open a part —</option>` + r.parts.map((p) => `<option ${p === r.active ? "selected" : ""}>${esc(p)}</option>`).join("");
}

// ── 3D view ───────────────────────────────────────────────────────
const host = $("#viewer");
const renderer = new THREE.WebGLRenderer({ antialias: true });
renderer.setPixelRatio(devicePixelRatio);
host.appendChild(renderer.domElement);
const scene = new THREE.Scene();
scene.background = new THREE.Color(0xf6f7f9);
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
scene.add(new THREE.HemisphereLight(0xffffff, 0x8a93a5, 1.6));
const key = new THREE.DirectionalLight(0xffffff, 1.6);
scene.add(key);
const partGroup = new THREE.Group(), sketchGroup = new THREE.Group();
scene.add(partGroup, sketchGroup);
const axes = new THREE.AxesHelper(10);
scene.add(axes);
const BASE = new THREE.Color(0x9fb0c8), HI = new THREE.Color(0xf08a24), HOVER = new THREE.Color(0x7aa2e8);
let faceMeshes = [], edgeLines = null, meshRev = null, hovered = null;

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
  renderer.render(scene, camera);
  placeLabels();
}

$("#projBtn").onclick = () => {
  const next = camera === ortho ? persp : ortho;
  // keep the same view direction and distance
  next.position.copy(camera.position); next.up.copy(camera.up);
  const t = controls.target.clone();
  controls.dispose();
  camera = next;
  controls = makeControls(camera, t);
  $("#projBtn").textContent = camera === ortho ? "Orthographic" : "Perspective";
  resize();
};

async function loadMesh(fit) {
  if (!S) return;
  if (!fit && meshRev === S.rev) return;
  const m = await api("/api/mesh");
  meshRev = m.rev;
  partGroup.clear();
  faceMeshes = [];
  for (const f of m.faces) {
    const g = new THREE.BufferGeometry();
    g.setAttribute("position", new THREE.Float32BufferAttribute(f.p, 3));
    g.setIndex(f.i);
    g.computeVertexNormals();
    const mat = new THREE.MeshStandardMaterial({ color: BASE.clone(), metalness: 0.05, roughness: 0.65, side: THREE.DoubleSide,
      polygonOffset: true, polygonOffsetFactor: 1, polygonOffsetUnits: 1 });
    const mesh = new THREE.Mesh(g, mat);
    mesh.userData = { features: f.features, labels: f.labels };
    partGroup.add(mesh);
    faceMeshes.push(mesh);
  }
  const pts = [];
  for (const e of m.edges) for (let i = 0; i + 5 < e.length; i += 3) pts.push(e[i], e[i + 1], e[i + 2], e[i + 3], e[i + 4], e[i + 5]);
  const eg = new THREE.BufferGeometry();
  eg.setAttribute("position", new THREE.Float32BufferAttribute(pts, 3));
  edgeLines = new THREE.LineSegments(eg, new THREE.LineBasicMaterial({ color: 0x1b1f27, transparent: true }));
  partGroup.add(edgeLines);
  colorFaces();
  if (sketchMode) ghostPart(true);
  if (fit) fitView("iso");
}

function colorFaces() {
  for (const m of faceMeshes) m.material.color.copy(m === hovered ? HOVER : selected && m.userData.features.includes(selected) ? HI : BASE);
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
function fitView(dir) {
  const box = new THREE.Box3().setFromObject(partGroup);
  if (box.isEmpty()) return;
  const c = box.getCenter(new THREE.Vector3()), r = box.getSize(new THREE.Vector3()).length() / 2 || 10;
  axes.scale.setScalar(r * 0.35);
  const d = { iso: [1, -1, 0.8], front: [0, -1, 0], top: [0, 0, 1], right: [1, 0, 0] }[dir] || [1, -1, 0.8];
  frame(c, r, new THREE.Vector3(...d), dir === "top" ? new THREE.Vector3(0, 1, 0) : new THREE.Vector3(0, 0, 1));
}
document.querySelectorAll(".vtools [data-view]").forEach((b) => (b.onclick = () => { exitSketch(); fitView(b.dataset.view); }));
$("#fitBtn").onclick = () => (sketchMode ? viewSketch() : fitView("iso"));

// hover + pick
const ray = new THREE.Raycaster(), ptr = new THREE.Vector2();
const tip = Object.assign(document.createElement("div"), { className: "tip" });
document.body.appendChild(tip);
function pick(ev) {
  const r = renderer.domElement.getBoundingClientRect();
  ptr.set(((ev.clientX - r.left) / r.width) * 2 - 1, -((ev.clientY - r.top) / r.height) * 2 + 1);
  ray.setFromCamera(ptr, camera);
  return ray.intersectObjects(faceMeshes)[0]?.object || null;
}
renderer.domElement.addEventListener("pointermove", (ev) => {
  if (sketchMode) return;
  const m = pick(ev);
  if (m !== hovered) { hovered = m; colorFaces(); }
  if (m) { tip.style.display = "block"; tip.style.left = ev.clientX + 12 + "px"; tip.style.top = ev.clientY + 12 + "px"; tip.textContent = m.userData.labels.join("  ·  "); }
  else tip.style.display = "none";
});
renderer.domElement.addEventListener("pointerleave", () => { hovered = null; tip.style.display = "none"; colorFaces(); });
let downAt = null;
renderer.domElement.addEventListener("pointerdown", (ev) => (downAt = [ev.clientX, ev.clientY]));
renderer.domElement.addEventListener("pointerup", (ev) => {
  if (sketchMode || !downAt || Math.hypot(ev.clientX - downAt[0], ev.clientY - downAt[1]) > 4) return;
  const m = pick(ev);
  if (m) { const fs = m.userData.features, i = fs.indexOf(selected); select(fs[(i + 1) % fs.length]); }
  else select(null);
});

// ── sketch mode: the sketch drawn on its plane, viewed straight on ──
let sketchMode = null, sketchData = null, savedView = null, labelEls = [];
async function enterSketch(id) {
  if (!sketchMode) savedView = { pos: camera.position.clone(), up: camera.up.clone(), target: controls.target.clone(), radius: viewRadius, persp: camera === persp };
  sketchMode = id;
  $("#sketchBar").hidden = false;
  $("#sketchName").textContent = id;
  if (camera === persp) $("#projBtn").click();  // sketches are always viewed orthographic
  ghostPart(true);
  await refreshSketch(true);
}
async function refreshSketch(reframe = false) {
  if (!sketchMode) return;
  let d;
  try { d = await api(`/api/sketch/${encodeURIComponent(sketchMode)}.json`); } catch { exitSketch(); return; }
  sketchData = d;
  $("#sketchInfo").textContent = `· ${d.dof} DOF · ${d.status}` + (d.conflicting?.length ? ` · conflicts: ${d.conflicting.join(", ")}` : "");
  sketchGroup.clear();
  for (const e of d.entities) {
    if (e.pts.length < 2) {
      const g = new THREE.BufferGeometry().setAttribute("position", new THREE.Float32BufferAttribute(e.pts.flat(), 3));
      sketchGroup.add(new THREE.Points(g, new THREE.PointsMaterial({ color: 0x475569, size: 5, sizeAttenuation: false })));
      continue;
    }
    const g = new THREE.BufferGeometry().setFromPoints(e.pts.map((p) => new THREE.Vector3(...p)));
    const mat = e.construction ? new THREE.LineDashedMaterial({ color: 0x94a3b8, dashSize: 1.5, gapSize: 1, depthTest: false })
                               : new THREE.LineBasicMaterial({ color: 0x1d4ed8, depthTest: false });
    const line = new THREE.Line(g, mat);
    line.renderOrder = 10;
    if (e.construction) line.computeLineDistances();
    sketchGroup.add(line);
  }
  buildLabels(d);
  if (reframe) viewSketch();
}
function viewSketch() {
  if (!sketchData) return;
  const box = new THREE.Box3().setFromObject(sketchGroup);
  const c = box.getCenter(new THREE.Vector3()), r = Math.max(box.getSize(new THREE.Vector3()).length() / 2, 5) * 1.2;
  const n = new THREE.Vector3(...sketchData.frame.normal), up = new THREE.Vector3(...sketchData.frame.y_dir);
  frame(c, r, n, up);
}
function exitSketch() {
  if (!sketchMode) return;
  sketchMode = null; sketchData = null;
  sketchGroup.clear();
  buildLabels(null);
  $("#sketchBar").hidden = true;
  ghostPart(false);
  if (savedView) {
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
  if (edgeLines) edgeLines.material.opacity = on ? 0.45 : 1;
}
function buildLabels(d) {
  const box = $("#labels");
  box.innerHTML = "";
  labelEls = [];
  if (!d) return;
  for (const e of d.entities) labelEls.push([e.label_at, Object.assign(document.createElement("div"), { className: "lbl ent", textContent: e.id })]);
  for (const m of d.dims) labelEls.push([m.at, Object.assign(document.createElement("div"), { className: "lbl dim", textContent: m.label })]);
  for (const [, el] of labelEls) box.appendChild(el);
}
const tmpV = new THREE.Vector3();
function placeLabels() {
  if (!labelEls.length) return;
  const w = host.clientWidth, h = host.clientHeight;
  for (const [p, el] of labelEls) {
    tmpV.set(...p).project(camera);
    el.style.left = ((tmpV.x + 1) / 2) * w + "px";
    el.style.top = ((1 - tmpV.y) / 2) * h + "px";
  }
}

// ── dialogs ───────────────────────────────────────────────────────
$("#rendersBtn").onclick = () => {
  if (!S) return;
  $("#rendersImg").src = `/api/render.png?views=iso,iso_back,iso_below,top&highlight=${encodeURIComponent(selected || "")}&v=${encodeURIComponent(S.rev)}`;
  $("#rendersDlg").showModal();
};
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
function scrollDown() { if (log.scrollHeight - log.scrollTop - log.clientHeight < 200) log.scrollTop = log.scrollHeight; }
function add(el) { log.appendChild(el); scrollDown(); return el; }
function md(t) { return esc(t).replace(/`([^`]+)`/g, "<code>$1</code>").replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>"); }
function addUser(text, sel, scope) {
  const d = document.createElement("div");
  d.className = "msg user";
  d.innerHTML = esc(text) + (sel ? `<span class="sel">selected: ${esc(sel)}${scope ? " (edits limited to it)" : ""}</span>` : "");
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
  const text = $("#prompt").value.trim();
  if (!text || busy) return;
  send({ type: "prompt", text, selection: selected, scope: $("#scope").checked });
  $("#prompt").value = "";
};
$("#prompt").onkeydown = (ev) => { if (ev.key === "Enter" && !ev.shiftKey) { ev.preventDefault(); $("#promptForm").requestSubmit(); } };
$("#stopBtn").onclick = () => send({ type: "stop" });
$("#resetBtn").onclick = () => send({ type: "reset" });
$("#model").onchange = (ev) => send({ type: "config", model: ev.target.value });
$("#effort").onchange = (ev) => send({ type: "config", effort: ev.target.value });
$("#undoBtn").onclick = () => api("/api/undo", {});
$("#redoBtn").onclick = () => api("/api/redo", {});
$("#partSelect").onchange = async (ev) => {
  if (!ev.target.value) return;
  exitSketch(); selected = null;
  await api("/api/rollback", { index: null }).catch(() => {});
  const r = await api("/api/open", { path: ev.target.value });
  setState(r.state, { fit: true });
};
$("#newBtn").onclick = async () => {
  const name = prompt("New part name (letters, digits, underscores):", "new_part");
  if (!name) return;
  const r = await api("/api/new", { path: `parts/${name}.vcad.json`, name });
  setState(r.state, { fit: true });
  loadParts();
};
document.addEventListener("keydown", (ev) => {
  if (ev.target.matches("input, textarea")) return;
  if (ev.key === "Escape" && sketchMode) { $("#exitSketch").click(); return; }
  if ((ev.metaKey || ev.ctrlKey) && ev.key.toLowerCase() === "z") { ev.preventDefault(); api(ev.shiftKey ? "/api/redo" : "/api/undo", {}); }
});

connect();
loop();
