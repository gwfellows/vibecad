import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { createSketchEditor, TOOLS as SKETCH_TOOLS, CONSTRAINTS as SKETCH_CONSTRAINTS } from "./sketch.js";
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
    case "user_prompt": addUser(e.text, e.selection, e.scope, e.entities, e.face, e.marks); break;
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
  if (inSketch()) SK.refresh();
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
    <div class="intent small" title="Click to edit: one line on why this feature exists">${esc(f.intent || "No intent written. Click to add one.")}</div>
    <textarea spellcheck="false"></textarea>
    <div class="row"><button id="applyFeat">Apply edit</button><span class="grow"></span>
    <button id="askAbout" title="Ask the agent about this feature">Ask agent</button></div>`;
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
  };
  d.querySelectorAll(".factions [data-a]").forEach((b) => (b.onclick = act[b.dataset.a]));
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
  if (r.state) setState(r.state);
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
const partGroup = new THREE.Group();
scene.add(partGroup);
const axes = new THREE.AxesHelper(10);
scene.add(axes);
const BASE = new THREE.Color(0x9fb0c8), HI = new THREE.Color(0xf08a24), HOVER = new THREE.Color(0x7aa2e8), PICKED = new THREE.Color(0xc2410c);
let pickedFaces = [];  // faces clicked in the 3D view, shift-click adds: {mesh, labels, point}
const lastPicked = () => pickedFaces.at(-1) || null;
let faceMeshes = [], edgeObjs = [], meshRev = null, hovered = null, hoveredEdge = null;
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
  renderer.render(scene, camera);
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
async function loadMesh(fit) {
  if (!S) return;
  if (!fit && meshRev === S.rev) return;
  fit = fit || pendingFit;
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
  edgeObjs = [];
  m.edges.forEach((e, i) => {  // one pickable line per edge; the index is the server's edge index
    if (m.edge_seam?.[i]) return;
    const g = new THREE.BufferGeometry();
    g.setAttribute("position", new THREE.Float32BufferAttribute(e, 3));
    const line = new THREE.Line(g, new THREE.LineBasicMaterial({ color: EDGE.clone(), transparent: true }));
    line.userData = { i };
    partGroup.add(line);
    edgeObjs.push(line);
  });
  // edge indices are only stable for the same body: keep picks whose edge sits where it did
  pickedEdges = pickedEdges.filter((p) => { const o = edgeObjs.find((x) => x.userData.i === p.i); return o && nearLine(o, p.point); });
  hoveredEdge = null;
  pickedFaces = pickedFaces.map((p) => {  // the new mesh has new face objects: keep picks whose labelled face still exists
    const same = faceMeshes.find((x) => x.userData.labels.join() === p.labels.join());
    return same && { ...p, mesh: same };
  }).filter(Boolean);
  pickUpdate();
  colorFaces();
  if (inSketch()) ghostPart(true);
  if (fit) pendingFit = !fitView("iso");
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
    const picked = pickedEdges.some((p) => p.i === o.userData.i);
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
  const box = new THREE.Box3().setFromObject(partGroup);
  if (box.isEmpty()) return false;
  const c = box.getCenter(new THREE.Vector3()), r = box.getSize(new THREE.Vector3()).length() / 2 || 10;
  axes.scale.setScalar(r * 0.35);
  const d = { iso: [1, -1, 0.8], front: [0, -1, 0], top: [0, 0, 1], right: [1, 0, 0] }[dir] || [1, -1, 0.8];
  frame(c, r, new THREE.Vector3(...d), dir === "top" ? new THREE.Vector3(0, 1, 0) : new THREE.Vector3(0, 0, 1));
  return true;
}
document.querySelectorAll(".vtools [data-view]").forEach((b) => (b.onclick = () => { exitSketch(); fitView(b.dataset.view); }));
$("#fitBtn").onclick = () => (inSketch() ? viewSketch() : fitView("iso"));

// hover + pick
const ray = new THREE.Raycaster(), ptr = new THREE.Vector2();
const tip = Object.assign(document.createElement("div"), { className: "tip" });
document.body.appendChild(tip);
function pickHit(ev) {
  const r = renderer.domElement.getBoundingClientRect();
  ptr.set(((ev.clientX - r.left) / r.width) * 2 - 1, -((ev.clientY - r.top) / r.height) * 2 + 1);
  ray.setFromCamera(ptr, camera);
  return ray.intersectObjects(faceMeshes)[0] || null;
}
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
  onChange: () => sketchBarUpdate(),
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
    const pb = new THREE.Box3().setFromObject(partGroup);
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
  for (const o of edgeObjs) o.material.opacity = on ? 0.45 : 1;
  edgeMarks.visible = !on;
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
         btn("rename", "Rename the selected entity or dimension; references are updated", () => SK.rename(), { act: "rename" }),
         btn("param", "Drive the selected dimension from a new part parameter", () => SK.toParam(), { act: "param" }),
         btn("clearmarks", "Remove your freehand marks", () => SK.clearMarks(), { act: "clearmarks" }),
         btn("delete", "Delete the selected entities and constraints (Del)", () => SK.del(), { act: "delete" }),
         btn("ask", "Ask the agent about, or to change, the selected sketch entities", askAboutSketch, { act: "ask" })]);
  for (const b of document.querySelectorAll("button.ico[data-icon]")) b.innerHTML = ICON[b.dataset.icon];
})();
function askAboutSketch() {
  const sel = SK.selection();
  $("#prompt").placeholder = sel.length ? `Ask about or change ${sel.join(", ")} in ${SK.active()}…` : `Ask about or change sketch ${SK.active()}…`;
  $("#prompt").focus();
}
const HINTS = {
  select: () => "Click to select (Shift adds) · drag free geometry · drag on empty space to box-select · double-click a dimension to change it",
  line: (n) => (n ? "Click the end point (snaps to points and curves; near-horizontal/vertical lines get a constraint) · Esc ends the chain" : "Click the start point"),
  rect: (n) => (n ? "Click the opposite corner" : "Click the first corner"),
  circle: (n) => (n ? "Click a point on the rim" : "Click the centre"),
  arc: (n) => ["Click the centre", "Click the start point", "Click the end point (counterclockwise)"][n] || "",
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
  el.style.left = Math.min(a.left - h.left, h.width - 260) + "px";
  el.style.top = a.bottom - h.top + 6 + "px";
  el.hidden = false;
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

function featureForm(kind) {
  const sid = SK.active(), d = SK.data();
  if (!sid || !d) return;
  const m = $("#featMenu"), hasBody = S.volume != null;
  const lines = d.entities.filter((e) => e.type === "line").map((e) => e.id);
  const mode = (def) => `<div class="row"><label>mode</label><select id="ffMode">${["add", "cut", "new", "intersect"].map((x) =>
    `<option ${x === def ? "selected" : ""}>${x}</option>`).join("")}</select></div>`;
  m.innerHTML = kind === "extrude"
    ? `<div class="ttl">Extrude ${esc(sid)}</div>
       <div class="row"><label>distance</label><input id="ffDist" value="10"></div>
       <div class="row"><label>direction</label><select id="ffDir"><option>normal</option><option>reverse</option><option>symmetric</option></select></div>
       <div class="row"><label>through all</label><input id="ffThru" type="checkbox" style="flex:0"></div>
       ${mode(hasBody ? "add" : "new")}<button class="go" id="ffGo">Extrude</button><div class="err" id="ffErr"></div>`
    : `<div class="ttl">Revolve ${esc(sid)}</div>
       <div class="row"><label>axis</label><select id="ffAxis">${["x_axis", "y_axis", ...lines].map((a) => `<option>${esc(a)}</option>`).join("")}</select></div>
       <div class="row"><label>angle</label><input id="ffAngle" value="360"></div>
       ${mode(hasBody ? "add" : "new")}<button class="go" id="ffGo">Revolve</button><div class="err" id="ffErr"></div>`;
  popup(m, $(kind === "extrude" ? "#extrudeBtn" : "#revolveBtn"));
  $("#ffGo").onclick = async () => {
    const id = nextId(kind), f = { id, type: kind, profile: { sketch: sid }, mode: $("#ffMode").value };
    if (kind === "extrude") {
      if ($("#ffThru").checked) f.extent = "through_all"; else f.distance = numOrExpr($("#ffDist").value);
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
$("#filletBtn").onclick = () => edgeForm("fillet");
$("#chamferBtn").onclick = () => edgeForm("chamfer");
pickUpdate();
$("#revolveBtn").onclick = () => featureForm("revolve");
window.vibecadView = {  // for browser tests and the devtools console
  toScreen: (x, y, z) => {
    const p = new THREE.Vector3(x, y, z).project(camera), r = renderer.domElement.getBoundingClientRect();
    return [r.left + ((p.x + 1) / 2) * r.width, r.top + ((1 - p.y) / 2) * r.height];
  },
  pickedFace: () => lastPicked() && { labels: lastPicked().labels, point: lastPicked().point },
  pickedFaces: () => pickedFaces.map((p) => p.labels[0]),
  pickedEdges: () => pickedEdges.map((p) => ({ i: p.i, label: p.label, ref: p.ref })),
};

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
function addUser(text, sel, scope, entities, face, marks) {
  const d = document.createElement("div");
  d.className = "msg user";
  const what = (sel ? esc(sel) + (entities?.length ? `: ${esc(entities.join(", "))}` : "") : "")
    + (face ? `${sel ? " · " : ""}face ${esc(face)}` : "") + (marks ? ` · ${marks} mark${marks > 1 ? "s" : ""}` : "");
  d.innerHTML = esc(text) + (what ? `<span class="sel">selected: ${what}${scope ? " (edits limited to it)" : ""}</span>` : "");
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
  const entities = inSketch() && SK.selection().length ? SK.selection() : null;
  const marks = inSketch() && SK.marks().length ? SK.marks() : null;
  const face = !inSketch() && lastPicked() ? { labels: lastPicked().labels, point: lastPicked().point } : null;
  send({ type: "prompt", text, selection: selected, scope: $("#scope").checked, entities, marks, face });
  if (marks) SK.clearMarks();
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
  if (inSketch() && SK.key(ev)) { ev.preventDefault(); return; }
  if (ev.key === "Escape" && inSketch()) { $("#exitSketch").click(); return; }
  if ((ev.metaKey || ev.ctrlKey) && ev.key.toLowerCase() === "z") { ev.preventDefault(); api(ev.shiftKey ? "/api/redo" : "/api/undo", {}); }
});

connect();
loop();
