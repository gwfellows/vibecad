// Sketch editor: draws the open sketch on its plane in the 3D view and edits it through the same ops as
// everything else (add_entity, add_constraint, set_dimension, update_entity, remove_*). Coordinates are
// sketch-local (u, v); the server returns them with the plane's frame and we map them into the scene.
import * as THREE from "three";

const COLORS = { fixed: 0x15803d, free: 0x1d4ed8, sel: 0xf08a24, hover: 0x60a5fa, bad: 0xdc2626, cons: 0x64748b, point: 0x334155 };
const GLYPH = { horizontal: "H", vertical: "V", parallel: "∥", perpendicular: "⊥", equal: "=", tangent: "tan", concentric: "◎",
  midpoint: "mid", symmetric: "sym", point_on: "on", fix: "fix", coincident: "•" };
const DIMS = new Set(["distance", "distance_x", "distance_y", "radius", "diameter", "angle"]);
const PICK_PX = 8;

export const TOOLS = [
  { id: "select", label: "Select", key: "s", title: "Select, drag to move free geometry, drag on empty space to box-select (S)" },
  { id: "line", label: "Line", key: "l", title: "Line: click start, click end; keeps chaining until Esc or right-click (L)" },
  { id: "rect", label: "Rect", key: "r", title: "Rectangle: click two opposite corners (R)" },
  { id: "circle", label: "Circle", key: "c", title: "Circle: click centre, click a point on the rim (C)" },
  { id: "arc", label: "Arc", key: "a", title: "Arc: click centre, start, end (counterclockwise) (A)" },
];
export const CONSTRAINTS = [
  { id: "coincident", label: "•", title: "Coincident: two points, or a point and a curve (point on curve)" },
  { id: "horizontal", label: "H", key: "h", title: "Horizontal: lines, or two points (H)" },
  { id: "vertical", label: "V", key: "v", title: "Vertical: lines, or two points (V)" },
  { id: "parallel", label: "∥", title: "Parallel: two lines" },
  { id: "perpendicular", label: "⊥", title: "Perpendicular: two lines" },
  { id: "equal", label: "=", key: "e", title: "Equal: two lines (length) or two circles/arcs (radius) (E)" },
  { id: "tangent", label: "tan", key: "t", title: "Tangent: a line and a circle/arc, or two circles/arcs (T)" },
  { id: "concentric", label: "◎", title: "Concentric: two circles/arcs" },
  { id: "midpoint", label: "mid", title: "Midpoint: a point and a line" },
  { id: "symmetric", label: "sym", title: "Symmetric: two points and a line (or point) to mirror about" },
  { id: "dim", label: "Dim", key: "d", title: "Dimension: line length, circle diameter, arc radius, distance between points or point-line, angle between lines (D)" },
  { id: "dx", label: "Dx", title: "Horizontal distance between two points, or a line's width" },
  { id: "dy", label: "Dy", title: "Vertical distance between two points, or a line's height" },
];

export function createSketchEditor(ctx) {
  const group = new THREE.Group();
  ctx.scene.add(group);
  const ray = new THREE.Raycaster();
  let sid = null, D = null;          // open sketch id, sketch data from the server
  let F = null;                      // frame vectors
  let tool = "select", pending = [], cursor = null, snapInfo = null;
  let sel = new Set(), hover = null;
  let dragS = null, boxS = null, committing = false;
  let labels = [];
  const made = new Set();            // ids created this session but maybe not yet in D

  // ── coordinates ───────────────────────────────────────────────
  const W = (u, v) => F.o.clone().addScaledVector(F.x, u).addScaledVector(F.y, v);
  function toUV(ev) {
    const r = ctx.renderer.domElement.getBoundingClientRect();
    ray.setFromCamera(new THREE.Vector2(((ev.clientX - r.left) / r.width) * 2 - 1, -((ev.clientY - r.top) / r.height) * 2 + 1), ctx.camera());
    const p = new THREE.Vector3();
    if (!ray.ray.intersectPlane(new THREE.Plane().setFromNormalAndCoplanarPoint(F.n, F.o), p)) return null;
    p.sub(F.o);
    return [p.dot(F.x), p.dot(F.y)];
  }
  function unitsPerPx() {
    const cam = ctx.camera(), h = ctx.renderer.domElement.clientHeight || 1;
    if (cam.isOrthographicCamera) return (cam.top - cam.bottom) / cam.zoom / h;
    return (2 * cam.position.distanceTo(F.o) * Math.tan((cam.fov * Math.PI) / 360)) / h;
  }
  const dist = (a, b) => Math.hypot(a[0] - b[0], a[1] - b[1]);
  function segDist(p, a, b) {
    const dx = b[0] - a[0], dy = b[1] - a[1], L2 = dx * dx + dy * dy || 1e-30;
    const t = Math.max(0, Math.min(1, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / L2));
    return [Math.hypot(p[0] - a[0] - t * dx, p[1] - a[1] - t * dy), [a[0] + t * dx, a[1] + t * dy]];
  }
  const angOf = (c, p) => (Math.atan2(p[1] - c[1], p[0] - c[0]) * 180) / Math.PI;
  function onArc(e, ang) {
    const sweep = (((e.end_angle - e.start_angle) % 360) + 360) % 360 || 360;
    return ((((ang - e.start_angle) % 360) + 360) % 360) <= sweep + 1e-6;
  }
  function curveDist(e, p) {  // [distance, nearest point on the curve]
    if (e.type === "line") return segDist(p, e.p1, e.p2);
    const a = angOf(e.center, p), rad = (a * Math.PI) / 180;
    const q = [e.center[0] + e.r * Math.cos(rad), e.center[1] + e.r * Math.sin(rad)];
    if (e.type === "arc" && !onArc(e, a)) return dist(p, e.p1) < dist(p, e.p2) ? [dist(p, e.p1), e.p1] : [dist(p, e.p2), e.p2];
    return [dist(p, q), q];
  }
  const ent = (id) => D?.entities.find((e) => e.id === id);
  const isPointKey = (k) => k === "origin" || k.includes(".") || ent(k)?.type === "point";

  // ── picking ───────────────────────────────────────────────────
  function hitTest(uv, { points = true, curves = true, exclude = new Set() } = {}) {
    const tol = PICK_PX * unitsPerPx();
    let best = null;
    if (points) for (const p of D.points) {
      if (exclude.has(p.ref)) continue;
      const d = dist(uv, p.at);
      if (d < tol && (!best || d < best.d)) best = { kind: "point", key: p.ref, at: p.at, d };
    }
    if (best) return best;
    if (curves) for (const e of D.entities) {
      if (e.type === "point" || exclude.has(e.id)) continue;
      const [d, q] = curveDist(e, uv);
      if (d < tol && (!best || d < best.d)) best = { kind: "curve", key: e.id, at: q, d };
    }
    return best;
  }

  // ── drawing ───────────────────────────────────────────────────
  function polyline(e) {
    if (e.type === "line") return [e.p1, e.p2];
    const [a0, a1] = e.type === "circle" ? [0, 360] : [e.start_angle, e.end_angle < e.start_angle ? e.end_angle + 360 : e.end_angle];
    const n = e.type === "circle" ? 72 : Math.max(8, Math.ceil(Math.abs(a1 - a0) / 5));
    return Array.from({ length: n + 1 }, (_, k) => {
      const t = ((a0 + ((a1 - a0) * k) / n) * Math.PI) / 180;
      return [e.center[0] + e.r * Math.cos(t), e.center[1] + e.r * Math.sin(t)];
    });
  }
  function addLine(pts, color, dashed = false, order = 10) {
    const g = new THREE.BufferGeometry().setFromPoints(pts.map(([u, v]) => W(u, v)));
    const mat = dashed ? new THREE.LineDashedMaterial({ color, dashSize: 1.2, gapSize: 0.8, depthTest: false })
                       : new THREE.LineBasicMaterial({ color, depthTest: false });
    if (dashed) { mat.dashSize = 6 * unitsPerPx(); mat.gapSize = 4 * unitsPerPx(); }
    const l = new THREE.Line(g, mat);
    l.renderOrder = order;
    if (dashed) l.computeLineDistances();
    group.add(l);
  }
  function addPoints(list, color, size) {
    if (!list.length) return;
    const g = new THREE.BufferGeometry().setFromPoints(list.map(([u, v]) => W(u, v)));
    const p = new THREE.Points(g, new THREE.PointsMaterial({ color, size, sizeAttenuation: false, depthTest: false }));
    p.renderOrder = 12;
    group.add(p);
  }
  function badEntities() {
    const bad = new Set();
    for (const i of D.conflicting_idx || []) for (const r of D.constraints[i]?.on || []) bad.add(r.split(".")[0]);
    return bad;
  }
  function render() {
    group.clear();
    if (!D) return;
    const bad = badEntities();
    for (const e of D.entities) {
      if (e.type === "point") continue;
      const color = sel.has(e.id) ? COLORS.sel : hover === e.id ? COLORS.hover : bad.has(e.id) ? COLORS.bad
        : e.construction ? COLORS.cons : e.fixed ? COLORS.fixed : COLORS.free;
      addLine(polyline(e), color, e.construction);
    }
    const plain = [], hi = [], hov = [];
    for (const p of D.points) (sel.has(p.ref) ? hi : hover === p.ref ? hov : plain).push(p.at);
    addPoints(plain, COLORS.point, 5);
    addPoints(hov, COLORS.hover, 8);
    addPoints(hi, COLORS.sel, 8);
    renderPreview();
    styleLabels();
  }
  function renderPreview() {
    if (!cursor || tool === "select") return;
    const c = snapInfo?.uv || cursor, pv = COLORS.hover;
    if (tool === "line" && pending.length) addLine([pending[0].uv, c], pv, false, 14);
    if (tool === "rect" && pending.length) {
      const a = pending[0].uv;
      addLine([a, [c[0], a[1]], c, [a[0], c[1]], a], pv, false, 14);
    }
    if ((tool === "circle" || tool === "arc") && pending.length) {
      const ctr = pending[0].uv, r = dist(ctr, pending[1]?.uv || c);
      if (tool === "circle" || pending.length === 1) addLine(polyline({ type: "circle", center: ctr, r }), pv, pending.length === 1 && tool === "arc", 14);
      else addLine(polyline({ type: "arc", center: ctr, r, start_angle: angOf(ctr, pending[1].uv), end_angle: angOf(ctr, c) }), pv, false, 14);
    }
    addPoints([c], snapInfo?.ref || snapInfo?.on ? COLORS.sel : COLORS.hover, 7);
  }

  // ── labels: dimensions, constraint glyphs, entity ids ─────────
  // Elements are built when the sketch data changes and only restyled on selection/hover, so a label
  // survives between the two clicks of a double-click.
  let labelsFor = null;
  function buildLabels() {
    const box = ctx.labelsBox;
    box.innerHTML = "";
    labels = [];
    labelsFor = D;
    if (!D) return;
    const bad = new Set(D.conflicting_idx || []), red = new Set(D.redundant_idx || []);
    const stack = new Map();
    for (const c of D.constraints) {
      const key = `#${c.index}`;
      const el = document.createElement("div");
      el.dataset.key = key;
      el.dataset.on = c.on.map((r) => r.split(".")[0]).join(" ");
      if (DIMS.has(c.type)) {
        const val = c.value == null ? "?" : `${+c.value.toFixed(4)}${c.type === "angle" ? "°" : ""}`;
        const via = c.param && c.param !== c.name ? ` (${c.param})` : !c.param && c.expr && isNaN(+c.expr) ? ` (${c.expr})` : "";
        el.className = "lbl dim pick";
        el.textContent = `${c.name ? c.name + " = " : ""}${val}${via}`;
        el.title = "Click to select, double-click to change";
        el.ondblclick = (ev) => { ev.stopPropagation(); editDim(c, el); };
      } else {
        el.className = "lbl glyph pick" + (c.type === "coincident" ? " coinc" : "");
        el.textContent = GLYPH[c.type] || c.type;
        el.title = `${c.type}(${c.on.join(", ")})`;
      }
      if (bad.has(c.index)) el.classList.add("bad");
      else if (red.has(c.index)) el.classList.add("redundant");
      el.onpointerdown = (ev) => ev.stopPropagation();
      el.onclick = (ev) => { ev.stopPropagation(); pick(key, ev.shiftKey || ev.metaKey || ev.ctrlKey); };
      const k = c.at.map((v) => v.toFixed(3)).join(",");
      const n = stack.get(k) || 0;
      stack.set(k, n + 1);
      const off = DIMS.has(c.type) ? [0, n * 16] : [12 + n * 18, -12];
      labels.push([W(...c.at), el, off]);
      box.appendChild(el);
    }
    for (const e of D.entities) {
      const el = Object.assign(document.createElement("div"), { className: "lbl ent", textContent: e.id });
      el.dataset.ent = e.id;
      const at = e.type === "line" ? [(e.p1[0] + e.p2[0]) / 2, (e.p1[1] + e.p2[1]) / 2] : e.type === "point" ? e.p1 : [e.center[0], e.center[1] + e.r];
      labels.push([W(...at), el, [0, 14]]);
      box.appendChild(el);
    }
  }
  function styleLabels() {
    if (labelsFor !== D) buildLabels();
    const near = new Set([...sel, ...(hover ? [hover] : [])].map((k) => k.split(".")[0]));
    for (const [, el] of labels) {
      if (el.dataset.ent) { el.hidden = !near.has(el.dataset.ent); continue; }
      el.classList.toggle("sel", sel.has(el.dataset.key));
      if (el.classList.contains("coinc")) el.hidden = !sel.has(el.dataset.key) && !el.dataset.on.split(" ").some((r) => near.has(r));
    }
  }
  const tmp = new THREE.Vector3();
  function placeLabels() {
    if (!labels.length) return;
    const w = ctx.host.clientWidth, h = ctx.host.clientHeight, cam = ctx.camera();
    for (const [p, el, off] of labels) {
      tmp.copy(p).project(cam);
      el.style.left = ((tmp.x + 1) / 2) * w + off[0] + "px";
      el.style.top = ((1 - tmp.y) / 2) * h + off[1] + "px";
    }
  }

  // ── selection ─────────────────────────────────────────────────
  function pick(key, add) {
    if (!add) sel = new Set(key ? [key] : []);
    else if (key) sel.has(key) ? sel.delete(key) : sel.add(key);
    changed();
  }
  function changed() {
    render();
    ctx.onChange?.();
  }
  function selected() {
    const points = [], curves = [], cons = [];
    for (const k of sel) {
      if (k.startsWith("#")) cons.push(D.constraints[+k.slice(1)]);
      else if (isPointKey(k)) points.push(k);
      else curves.push(ent(k));
    }
    return { points, curves: curves.filter(Boolean), cons: cons.filter(Boolean) };
  }

  // ── ops ───────────────────────────────────────────────────────
  function newId(prefix) {
    const taken = new Set([...D.entities.map((e) => e.id), ...made]);
    let n = 1;
    while ([...taken].some((t) => t === `${prefix}${n}` || t.startsWith(`${prefix}${n}_`))) n++;
    const id = `${prefix}${n}`;
    made.add(id);
    return id;
  }
  async function commit(ops, message) {
    committing = true;
    try {
      const ok = await ctx.edit(ops, message);
      await refresh();
      return ok;
    } finally { committing = false; }
  }
  const C = (type, on, extra = {}) => ({ op: "add_constraint", sketch: sid, constraint: { type, on, ...extra } });
  const E = (entity) => ({ op: "add_entity", sketch: sid, entity });
  const r4 = (p) => p.map((v) => +v.toFixed(4));
  function attach(pt, ref) {  // constraints joining a new point to what it was snapped to
    if (pt.ref) return [C("coincident", [ref, pt.ref])];
    if (pt.on) return [C("point_on", [ref, pt.on])];
    return [];
  }

  // snapping for drawing tools: points first, then curves, then horizontal / vertical from the previous point
  function snap(uv, from = null) {
    const hit = hitTest(uv, { exclude: new Set(pending.map((p) => p.ref).filter(Boolean)) });
    if (hit?.kind === "point") return { uv: hit.at, ref: hit.key };
    if (hit?.kind === "curve") return { uv: hit.at, on: hit.key };
    if (from) {
      const dx = uv[0] - from[0], dy = uv[1] - from[1], tol = Math.tan((4 * Math.PI) / 180);
      if (Math.abs(dy) <= tol * Math.abs(dx) && Math.abs(dx) > 0) return { uv: [uv[0], from[1]], hv: "horizontal" };
      if (Math.abs(dx) <= tol * Math.abs(dy) && Math.abs(dy) > 0) return { uv: [from[0], uv[1]], hv: "vertical" };
    }
    return { uv };
  }

  async function drawClick(uv) {
    const s = snap(uv, (tool === "line" || tool === "rect") && pending.length ? pending[0].uv : null);
    if (tool === "line") {
      if (!pending.length) { pending = [s]; return render(); }
      const a = pending[0];
      if (dist(a.uv, s.uv) < 1e-9) return;
      const id = newId("line");
      const ops = [E({ id, type: "line", p1: r4(a.uv), p2: r4(s.uv) }), ...attach(a, `${id}.p1`), ...attach(s, `${id}.p2`)];
      if (s.hv) ops.push(C(s.hv, [id]));
      const closes = s.ref && s.ref === chainStart;
      await commit(ops, `draw ${id} in ${sid}`);
      if (closes) { pending = []; chainStart = null; }
      else { pending = [{ uv: s.uv, ref: `${id}.p2` }]; if (!chainStart) chainStart = `${id}.p1`; }
      return render();
    }
    if (tool === "rect") {
      if (!pending.length) { pending = [snap(uv)]; return render(); }
      const a = pending[0], b = snap(uv);
      const [x0, x1] = [Math.min(a.uv[0], b.uv[0]), Math.max(a.uv[0], b.uv[0])];
      const [y0, y1] = [Math.min(a.uv[1], b.uv[1]), Math.max(a.uv[1], b.uv[1])];
      if (x1 - x0 < 1e-9 || y1 - y0 < 1e-9) return;
      const id = newId("rect"), c = [[x0, y0], [x1, y0], [x1, y1], [x0, y1]];
      const n = ["bottom", "right", "top", "left"].map((s) => `${id}_${s}`);
      const ops = n.map((nm, i) => E({ id: nm, type: "line", p1: r4(c[i]), p2: r4(c[(i + 1) % 4]) }));
      n.forEach((nm, i) => ops.push(C("coincident", [`${nm}.p2`, `${n[(i + 1) % 4]}.p1`])));
      ops.push(C("horizontal", [n[0]]), C("horizontal", [n[2]]), C("vertical", [n[1]]), C("vertical", [n[3]]));
      for (const p of [a, b]) {
        const i = c.findIndex((q) => dist(q, p.uv) < 1e-9);
        if (i >= 0) ops.push(...attach(p, `${n[i]}.p1`));
      }
      pending = [];
      await commit(ops, `draw rectangle ${id} in ${sid}`);
      return render();
    }
    if (tool === "circle") {
      if (!pending.length) { pending = [s]; return render(); }
      const ctr = pending[0], r = dist(ctr.uv, s.uv);
      if (r < 1e-9) return;
      const id = newId("circle");
      pending = [];
      await commit([E({ id, type: "circle", center: r4(ctr.uv), r: +r.toFixed(4) }), ...attach(ctr, `${id}.center`)], `draw ${id} in ${sid}`);
      return render();
    }
    if (tool === "arc") {
      if (pending.length < 2) {
        if (pending.length === 1 && dist(pending[0].uv, s.uv) < 1e-9) return;
        pending.push(s);
        return render();
      }
      const [ctr, st] = pending, r = dist(ctr.uv, st.uv);
      const id = newId("arc");
      const a0 = angOf(ctr.uv, st.uv), a1 = angOf(ctr.uv, s.uv);
      pending = [];
      await commit([E({ id, type: "arc", center: r4(ctr.uv), r: +r.toFixed(4), start_angle: +a0.toFixed(4), end_angle: +a1.toFixed(4) }),
        ...attach(ctr, `${id}.center`), ...attach(st, `${id}.start`), ...attach(s, `${id}.end`)], `draw ${id} in ${sid}`);
      return render();
    }
  }
  let chainStart = null;

  // constraint tools on the selection: [ops] or an error string
  function constraintOps(kind) {
    const { points: P, curves: K } = selected();
    const lines = K.filter((e) => e.type === "line"), round = K.filter((e) => e.type !== "line");
    const n = sel.size;
    const need = (msg) => msg;
    switch (kind) {
      case "coincident":
        if (P.length === 2 && n === 2) return [C("coincident", P)];
        if (P.length === 1 && K.length === 1 && n === 2) return [C("point_on", [P[0], K[0].id])];
        return need("select two points, or a point and a curve");
      case "horizontal": case "vertical":
        if (lines.length && lines.length === n) return lines.map((l) => C(kind, [l.id]));
        if (P.length === 2 && n === 2) return [C(kind, P)];
        return need("select lines, or two points");
      case "parallel": case "perpendicular":
        return lines.length === 2 && n === 2 ? [C(kind, [lines[0].id, lines[1].id])] : need("select two lines");
      case "equal":
        if (n === 2 && (lines.length === 2 || round.length === 2)) return [C("equal", K.map((e) => e.id))];
        return need("select two lines, or two circles/arcs");
      case "concentric":
        return round.length === 2 && n === 2 ? [C("concentric", round.map((e) => e.id))] : need("select two circles/arcs");
      case "midpoint":
        return P.length === 1 && lines.length === 1 && n === 2 ? [C("midpoint", [P[0], lines[0].id])] : need("select a point and a line");
      case "symmetric":
        if (P.length === 2 && lines.length === 1 && n === 3) return [C("symmetric", [...P, lines[0].id])];
        if (P.length === 3 && n === 3) return [C("symmetric", P)];
        return need("select two points and a line (or a third point) to mirror about");
      case "tangent": {
        if (K.length !== 2 || n !== 2 || lines.length === 2) return need("select a line and a circle/arc, or two circles/arcs");
        const shared = sharedEnd(K[0], K[1]);
        return [C("tangent", shared ? [K[0].id, K[1].id, shared] : [K[0].id, K[1].id])];
      }
    }
    return need("unknown tool");
  }
  function ends(e) {
    return e.type === "line" ? [[`${e.id}.p1`, e.p1], [`${e.id}.p2`, e.p2]] : e.type === "arc" ? [[`${e.id}.start`, e.p1], [`${e.id}.end`, e.p2]] : [];
  }
  function sharedEnd(a, b) {
    const tol = 1e-6 + unitsPerPx();
    for (const [ra, pa] of ends(a)) for (const [, pb] of ends(b)) if (dist(pa, pb) < tol) return ra;
    return null;
  }

  // dimension tools: what to measure, and its current value (so adding it doesn't move anything)
  function dimSpec(kind) {
    const { points: P, curves: K } = selected();
    const n = sel.size, pt = (r) => D.points.find((p) => p.ref === r).at;
    const base = (r) => r.split(".")[0];
    if (kind === "dim") {
      if (n === 1 && K.length === 1) {
        const e = K[0];
        if (e.type === "line") return { type: "distance", on: [e.id], value: dist(e.p1, e.p2), name: `${e.id}_len` };
        if (e.type === "circle") return { type: "diameter", on: [e.id], value: 2 * e.r, name: `${e.id}_d` };
        return { type: "radius", on: [e.id], value: e.r, name: `${e.id}_r` };
      }
      if (n === 2 && P.length === 2) return { type: "distance", on: P, value: dist(pt(P[0]), pt(P[1])), name: `${base(P[0])}_${base(P[1])}_dist` };
      if (n === 2 && P.length === 1 && K.length === 1 && K[0].type === "line")
        return { type: "distance", on: [P[0], K[0].id], value: segLineDist(pt(P[0]), K[0]), name: `${base(P[0])}_${K[0].id}_dist` };
      if (n === 2 && K.length === 2 && K.every((e) => e.type === "line")) {
        const a = K.map((e) => Math.atan2(e.p2[1] - e.p1[1], e.p2[0] - e.p1[0]));
        return { type: "angle", on: K.map((e) => e.id), value: ((((a[1] - a[0]) * 180) / Math.PI) % 360 + 360) % 360, name: `${K[0].id}_${K[1].id}_angle` };
      }
      return "select a line, circle or arc; two points; a point and a line; or two lines";
    }
    const k = kind === "dx" ? 0 : 1, type = kind === "dx" ? "distance_x" : "distance_y", sfx = kind === "dx" ? "dx" : "dy";
    if (n === 2 && P.length === 2) return { type, on: P, value: pt(P[1])[k] - pt(P[0])[k], name: `${base(P[0])}_${base(P[1])}_${sfx}` };
    if (n === 1 && P.length === 1) return { type, on: ["origin", P[0]], value: pt(P[0])[k], name: `${base(P[0])}_${sfx}` };
    if (n === 1 && K.length === 1 && K[0].type === "line") return { type, on: [K[0].id], value: K[0].p2[k] - K[0].p1[k], name: `${K[0].id}_${sfx}` };
    return "select two points, one point (measured from the origin), or a line";
  }
  function segLineDist(p, l) {
    const dx = l.p2[0] - l.p1[0], dy = l.p2[1] - l.p1[1];
    return Math.abs((p[0] - l.p1[0]) * dy - (p[1] - l.p1[1]) * dx) / (Math.hypot(dx, dy) || 1);
  }
  function uniqueName(name) {
    const taken = new Set(D.constraints.map((c) => c.name).filter(Boolean));
    let out = name, i = 2;
    while (taken.has(out) || D.params.includes(out)) out = `${name}_${i++}`;
    return out;
  }
  function parseValue(text) {
    const t = text.trim();
    if (!t) return { error: "empty value" };
    if (/^[-+]?(\d+\.?\d*|\.\d+)([eE][-+]?\d+)?$/.test(t)) return { value: +t };
    const funcs = new Set(["sqrt", "sin", "cos", "tan", "atan2", "min", "max", "abs", "round", "floor", "ceil", "pi", "mm", "cm", "m", "in", "inch", "ft", "deg", "rad"]);
    const unknown = (t.match(/[A-Za-z_]\w*/g) || []).filter((w) => !funcs.has(w) && !D.params.includes(w));
    if (unknown.length) return { error: `unknown name ${unknown[0]}; params: ${D.params.join(", ") || "none"}` };
    return { value: t };
  }

  // inline value editor, shared by new and existing dimensions
  function askValue(el, initial, label) {
    return new Promise((resolve) => {
      const box = ctx.dimEdit, inp = box.querySelector("input");
      box.querySelector(".what").textContent = label;
      const r = el ? el.getBoundingClientRect() : null, host = ctx.host.getBoundingClientRect();
      box.style.left = (r ? r.left - host.left : host.width / 2 - 80) + "px";
      box.style.top = (r ? r.bottom - host.top + 4 : 90) + "px";
      box.hidden = false;
      inp.value = initial;
      inp.focus();
      inp.select();
      const done = (v) => { box.hidden = true; inp.onkeydown = inp.onblur = null; resolve(v); };
      inp.onkeydown = (ev) => {
        ev.stopPropagation();
        if (ev.key === "Enter") done(inp.value);
        if (ev.key === "Escape") done(null);
      };
      inp.onblur = () => done(null);
    });
  }
  async function editDim(c, el) {
    const shown = c.param || (c.expr && isNaN(+c.expr) ? c.expr : String(+(+c.value).toFixed(6)));
    const text = await askValue(el, shown, `${c.name || c.type} =`);
    if (text == null) return;
    const v = parseValue(text);
    if (v.error) return ctx.note(v.error, "err");
    const op = c.name ? { op: "set_dimension", sketch: sid, name: c.name, value: v.value }
                      : { op: "update_constraint", sketch: sid, match: { index: c.index }, set: { value: v.value } };
    await commit([op], `set ${c.name || c.type} = ${text} in ${sid}`);
  }
  async function addDim(kind) {
    const spec = dimSpec(kind);
    if (typeof spec === "string") return ctx.note(`${kind === "dim" ? "Dimension" : kind}: ${spec}`, "err");
    const name = uniqueName(spec.name);
    const text = await askValue(null, String(+spec.value.toFixed(4)), `${name} =`);
    if (text == null) return;
    const v = parseValue(text);
    if (v.error) return ctx.note(v.error, "err");
    sel = new Set();
    await commit([C(spec.type, spec.on, { value: v.value, name })], `dimension ${name} = ${text} in ${sid}`);
  }
  async function constrain(kind) {
    if (kind === "dim" || kind === "dx" || kind === "dy") return addDim(kind);
    const ops = constraintOps(kind);
    if (typeof ops === "string") return ctx.note(`${kind}: ${ops}`, "err");
    sel = new Set();
    await commit(ops, `${kind} in ${sid}`);
  }
  function available(kind) {
    if (!D || !sel.size) return false;
    if (kind === "dim" || kind === "dx" || kind === "dy") return typeof dimSpec(kind) !== "string";
    return typeof constraintOps(kind) !== "string";
  }
  async function del() {
    const { curves, cons } = selected();
    const pts = [...sel].filter((k) => ent(k)?.type === "point");
    if (!cons.length && !curves.length && !pts.length) return;
    const ops = cons.map((c) => c.index).sort((a, b) => b - a).map((i) => ({ op: "remove_constraint", sketch: sid, match: { index: i } }));
    for (const id of [...curves.map((e) => e.id), ...pts]) ops.push({ op: "remove_entity", sketch: sid, id });
    sel = new Set();
    await commit(ops, `delete ${ops.length} item(s) in ${sid}`);
  }
  async function toggleConstruction() {
    const { curves } = selected();
    if (!curves.length) return ctx.note("Construction: select lines, circles or arcs", "err");
    await commit(curves.map((e) => ({ op: "update_entity", sketch: sid, id: e.id, set: { construction: !e.construction } })),
      `toggle construction in ${sid}`);
  }

  // ── live drag ─────────────────────────────────────────────────
  async function dragTo(uv) {
    dragS.target = uv;
    if (dragS.inflight) return;
    dragS.inflight = true;
    try {
      while (dragS && dragS.target) {
        const to = dragS.target;
        dragS.target = null;
        const r = await ctx.api(`/api/sketch/${encodeURIComponent(sid)}/drag`, { ref: dragS.ref, to, grab: dragS.grab, guess: dragS.ir });
        if (!dragS) break;
        if (r.moved) {
          dragS.ir = r.ir;
          dragS.preview = true;
          const fixed = new Map(D.entities.map((e) => [e.id, e.fixed]));
          D = { ...D, entities: r.entities.map((e) => ({ ...e, fixed: fixed.get(e.id) })), points: pointsOf(r.entities) };
          render();
        } else if (!dragS.warned) {
          dragS.warned = true;
          ctx.hint("That geometry is fully constrained here: change its dimensions instead.");
        }
      }
    } finally { if (dragS) dragS.inflight = false; }
  }
  function pointsOf(ents) {
    const pts = [{ ref: "origin", at: [0, 0] }];
    for (const e of ents) {
      if (e.type === "line") pts.push({ ref: `${e.id}.p1`, at: e.p1 }, { ref: `${e.id}.p2`, at: e.p2 });
      else if (e.type === "point") pts.push({ ref: e.id, at: e.p1 });
      else {
        pts.push({ ref: `${e.id}.center`, at: e.center });
        if (e.type === "arc") pts.push({ ref: `${e.id}.start`, at: e.p1 }, { ref: `${e.id}.end`, at: e.p2 });
      }
    }
    return pts;
  }
  function sameGeom(a, b) {
    const eq = (p, q) => (p == null && q == null) || (p && q && Math.abs(p[0] - q[0]) < 1e-7 && Math.abs(p[1] - q[1]) < 1e-7);
    return eq(a.p1, b.p1) && eq(a.p2, b.p2) && eq(a.center, b.center) && Math.abs((a.r ?? 0) - (b.r ?? 0)) < 1e-7;
  }
  async function endDrag() {
    const s = dragS;
    while (s.inflight) await new Promise((r) => setTimeout(r, 10));
    dragS = null;
    if (!s.preview) return;
    const ops = [];
    for (const g of s.ir) {
      const now = D.entities.find((e) => e.id === g.id), was = s.orig.get(g.id);
      if (!now || !was || sameGeom(now, was)) continue;
      const { id, ...set } = g;
      ops.push({ op: "update_entity", sketch: sid, id, set });
    }
    if (ops.length) await commit(ops, `drag ${s.ref} in ${sid}`);
    else await refresh();
  }

  // ── pointer + keys ────────────────────────────────────────────
  const dom = ctx.renderer.domElement;
  let down = null;
  dom.addEventListener("pointerdown", (ev) => {
    if (!sid || ev.button !== 0) return;
    const uv = toUV(ev);
    if (!uv) return;
    down = { x: ev.clientX, y: ev.clientY, uv };
    if (tool !== "select" || committing) return;
    const hit = hitTest(uv);
    const add = ev.shiftKey || ev.metaKey || ev.ctrlKey;
    if (hit) {
      if (add) pick(hit.key, true);
      else if (!sel.has(hit.key)) pick(hit.key, false);
      dragS = { ref: hit.key, grab: uv, ir: null, moved: false, orig: new Map(D.entities.map((e) => [e.id, e])) };
    } else {
      boxS = { uv, x: ev.clientX, y: ev.clientY, add };
    }
    dom.setPointerCapture(ev.pointerId);
  });
  dom.addEventListener("pointermove", (ev) => {
    if (!sid) return;
    const uv = toUV(ev);
    if (!uv) return;
    cursor = uv;
    const far = down && Math.hypot(ev.clientX - down.x, ev.clientY - down.y) > 4;
    if (dragS && far) { dragS.moved = true; ctx.tip(null); return void dragTo(uv); }
    if (boxS && far) return showBox(ev);
    if (tool !== "select") {
      const from = (tool === "line" || tool === "rect") && pending.length ? pending[0].uv : null;
      snapInfo = snap(uv, from);
      return render();
    }
    const h = hitTest(uv)?.key || null;
    if (h !== hover) { hover = h; render(); }
    ctx.tip(h ? { x: ev.clientX, y: ev.clientY, text: h } : null);
  });
  dom.addEventListener("pointerup", async (ev) => {
    if (!sid || ev.button !== 0) return;
    const wasClick = down && Math.hypot(ev.clientX - down.x, ev.clientY - down.y) <= 4;
    const uv = toUV(ev);
    down = null;
    if (dragS) { if (dragS.moved) return endDrag(); dragS = null; return; }
    if (boxS) {
      const b = boxS;
      boxS = null;
      hideBox();
      if (wasClick) { if (!b.add) pick(null, false); return; }
      return boxSelect(b.uv, uv, b.add);
    }
    if (wasClick && tool !== "select" && !committing && uv) await drawClick(uv);
  });
  dom.addEventListener("contextmenu", (ev) => {
    if (!sid) return;
    if (tool !== "select" && pending.length) { ev.preventDefault(); cancelDrawing(); }
  });
  function showBox(ev) {
    const b = ctx.box, host = ctx.host.getBoundingClientRect();
    const x0 = Math.min(boxS.x, ev.clientX) - host.left, y0 = Math.min(boxS.y, ev.clientY) - host.top;
    Object.assign(b.style, { left: x0 + "px", top: y0 + "px", width: Math.abs(ev.clientX - boxS.x) + "px", height: Math.abs(ev.clientY - boxS.y) + "px" });
    b.hidden = false;
  }
  function hideBox() { ctx.box.hidden = true; }
  function boxSelect(a, b, add) {
    if (!a || !b) return;
    const [u0, u1] = [Math.min(a[0], b[0]), Math.max(a[0], b[0])], [v0, v1] = [Math.min(a[1], b[1]), Math.max(a[1], b[1])];
    const inside = (p) => p[0] >= u0 && p[0] <= u1 && p[1] >= v0 && p[1] <= v1;
    if (!add) sel = new Set();
    for (const e of D.entities) {
      const pts = e.type === "point" ? [e.p1] : e.type === "line" ? [e.p1, e.p2] : polyline(e);
      if (pts.every(inside)) sel.add(e.id);
    }
    changed();
  }
  function cancelDrawing() {
    pending = [];
    chainStart = null;
    render();
  }
  function setTool(t) {
    tool = t;
    pending = [];
    chainStart = null;
    snapInfo = null;
    hover = null;
    ctx.onChange?.();
    render();
  }
  function key(ev) {  // returns true if handled
    if (!sid) return false;
    const k = ev.key.toLowerCase();
    if (ev.key === "Escape") {
      if (pending.length) { cancelDrawing(); return true; }
      if (tool !== "select") { setTool("select"); return true; }
      if (sel.size) { pick(null, false); return true; }
      return false;  // caller exits the sketch
    }
    if (ev.key === "Delete" || ev.key === "Backspace") { del(); return true; }
    if (ev.metaKey || ev.ctrlKey || ev.altKey) return false;
    const t = TOOLS.find((x) => x.key === k);
    if (t) { setTool(t.id); return true; }
    const c = CONSTRAINTS.find((x) => x.key === k);
    if (c) { constrain(c.id); return true; }
    if (k === "g") { toggleConstruction(); return true; }
    return false;
  }

  // ── lifecycle ─────────────────────────────────────────────────
  async function refresh() {
    if (!sid) return;
    const want = sid;
    let d;
    try { d = await ctx.api(`/api/sketch/${encodeURIComponent(want)}.json`); } catch { return sid === want ? ctx.onLost?.() : undefined; }
    if (sid !== want) return;  // the sketch was closed or switched while this was loading
    D = d;
    const f = D.frame;
    F = { o: new THREE.Vector3(...f.origin), x: new THREE.Vector3(...f.x_dir), y: new THREE.Vector3(...f.y_dir), n: new THREE.Vector3(...f.normal) };
    const keys = new Set([...D.entities.map((e) => e.id), ...D.points.map((p) => p.ref), ...D.constraints.map((c) => `#${c.index}`)]);
    sel = new Set([...sel].filter((k) => keys.has(k)));
    if (hover && !keys.has(hover)) hover = null;
    render();
    ctx.onChange?.();
    return D;
  }
  async function enter(id) {
    sid = id;
    sel = new Set();
    made.clear();
    setTool("select");
    return refresh();
  }
  function exit() {
    sid = null;
    D = null;
    dragS = boxS = null;
    pending = [];
    sel = new Set();
    group.clear();
    ctx.labelsBox.innerHTML = "";
    labels = [];
    labelsFor = null;
    hideBox();
    ctx.dimEdit.hidden = true;
  }
  function bounds() {  // sketch extent in world space, for framing the view
    const box = new THREE.Box3();
    for (const e of D?.entities || []) for (const p of e.type === "point" ? [e.p1] : polyline(e)) box.expandByPoint(W(...p));
    return box;
  }

  return {
    enter, exit, refresh, key, setTool, constrain, del, toggleConstruction, placeLabels, bounds, available,
    active: () => sid,
    data: () => D,
    tool: () => tool,
    pendingCount: () => pending.length,
    selection: () => [...sel],
    select: (keys) => { sel = new Set(keys); changed(); },
    frame: () => F,
    toScreen: (u, v) => {  // client pixel position of a sketch point (browser tests use it to click geometry)
      const p = W(u, v).project(ctx.camera()), r = ctx.renderer.domElement.getBoundingClientRect();
      return [r.left + ((p.x + 1) / 2) * r.width, r.top + ((1 - p.y) / 2) * r.height];
    },
  };
}
