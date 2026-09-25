"""VibeCAD desktop GUI (local web app).

  vibecad-app [--root DIR] [--port 8765] [--model sonnet]   then open http://127.0.0.1:8765

One process holds the Workspace (open parts) and one agent conversation. Every agent step and every part
change is pushed to the browser over a WebSocket, so you can watch the agent work: its messages and tool
calls, the feature tree changing, the 3D view and renders updating.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from .workspace import ToolError, Workspace

STATIC = Path(__file__).parent / "static"
SKIP_DIRS = {".venv", "node_modules", ".git", "results", "out", "__pycache__"}


class App:
    def __init__(self, root: Path, model: str, effort: str = "low"):
        self.ws = Workspace(root)
        self.model = model
        self.effort = effort
        self.clients: set[WebSocket] = set()
        self.queue: asyncio.Queue | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.runner = None
        self.run_task: asyncio.Task | None = None
        self.transcript: list[dict] = []  # replayed to browsers that connect mid-run
        self.rollback: int | None = None  # show the part as of features[:rollback]; None = end
        self.session_id: str | None = None  # agent conversation, kept across model/effort changes
        self.mesh_cache: dict[str, Any] = {}
        self.ws.listeners.append(self.publish)

    # events can come from worker threads (tools) or the loop (agent stream)
    def publish(self, event: dict) -> None:
        if self.loop is None:
            return
        event = {**event, "t": event.get("t", time.time())}
        if event["type"] != "part_changed":
            self.transcript.append(event)
            self.transcript = self.transcript[-600:]
        self.loop.call_soon_threadsafe(self.queue.put_nowait, event)

    async def broadcaster(self) -> None:
        while True:
            ev = await self.queue.get()
            if ev["type"] == "part_changed":
                ev = {**ev, "state": self.state()}
            msg = json.dumps(ev, default=str)
            for c in list(self.clients):
                try:
                    await c.send_text(msg)
                except Exception:
                    self.clients.discard(c)

    # ── part state for the UI ──────────────────────────────────────
    def view_result(self):
        """The regen result the UI shows: the whole part, or the part rolled back to a feature."""
        s = self.ws.session()
        if self.rollback is None or self.rollback >= len(s.doc.features):
            return s.result
        doc = s.doc.model_copy(update={"features": s.doc.features[: self.rollback]})
        return s.regen.run(doc)

    def state(self) -> dict | None:
        if self.ws.active is None:
            return None
        s = self.ws.session()
        if self.rollback is not None and self.rollback >= len(s.doc.features):
            self.rollback = None
        r = s.result
        vr = self.view_result()
        vsum = vr.summary()
        summ = r.summary()
        feats = []
        for f, fr in zip(s.doc.features, r.features):
            d = {"id": f.id, "type": f.type, "name": f.name, "intent": f.intent, "status": fr.status,
                 "message": fr.message, "warnings": fr.warnings, "cached": fr.cached}
            if f.type == "sketch":
                d["dof"] = fr.info.get("dof")
                d["plane"] = f.plane.model_dump(exclude_none=True)
                d["n_entities"] = len(f.entities)
            feats.append(d)
        return {
            "path": self.ws.active, "rel": _rel(self.ws.active, self.ws.root), "name": s.doc.name,
            "material": s.doc.material, "process": s.doc.process, "design_notes": s.doc.design_notes,
            "params": [{"name": k, "expr": v, "value": r.env.get(k)} for k, v in s.doc.params.items()],
            "features": feats, "ok": r.ok, "volume": vsum.get("volume_mm3"), "bbox": vsum.get("bbox_mm", {}).get("size"),
            "valid": summ.get("valid"), "rev": _rev(s) + f"@{self.rollback}", "can_undo": bool(s.undo_stack),
            "can_redo": bool(s.redo_stack), "rollback": self.rollback, "tree": s.tree(),
        }

    def mesh(self) -> dict:
        # OCCT meshes a shape in place and is not thread-safe: two requests tessellating the same shape at
        # once (the GUI asks twice per edit, the agent may render meanwhile) yield faces with no triangulation
        with self.ws.lock:
            return self._mesh()

    def _mesh(self) -> dict:
        s = self.ws.session()
        rev = _rev(s) + f"@{self.rollback}"
        key = f"{self.ws.active}:{rev}"
        if key in self.mesh_cache:
            return self.mesh_cache[key]
        import build123d as bd
        from OCP.TopoDS import TopoDS

        from .topo import list_edges, list_faces
        body = self.view_result().body
        out = {"faces": [], "edges": [], "rev": rev}
        if body.shape is None:
            return out
        diag = bd.Shape.cast(body.shape).bounding_box().diagonal or 1.0
        for f in list_faces(body.shape):
            face = bd.Face(TopoDS.Face(f))
            verts, tris = face.tessellate(diag / 500, 0.15)
            if not tris:
                continue
            labels = body.labels_of(f)
            out["faces"].append({
                "p": [round(c, 4) for v in verts for c in (v.X, v.Y, v.Z)],
                "i": [k for t in tris for k in t],
                "features": sorted({l.feature for l in labels}),
                "labels": sorted({str(l) for l in labels}),
            })
        for e in list_edges(body.shape):
            edge = bd.Edge(TopoDS.Edge(e))
            k = 2 if edge.geom_type == bd.GeomType.LINE else 40
            pts = edge.positions([i / (k - 1) for i in range(k)])
            out["edges"].append([round(c, 4) for q in pts for c in (q.X, q.Y, q.Z)])
        self.mesh_cache = {key: out}
        return out

    def sketch_geometry(self, sid: str) -> dict:
        """A solved sketch for the sketch editor, in sketch-local coordinates (the client maps them onto the
        plane with `frame`): entities with a fully-constrained flag, pickable points, and every constraint
        with where to draw its label or glyph."""
        from .expr import evaluate
        from .sketch import freedom

        r = self.view_result()
        s = self.ws.session()
        if sid not in r.sketches:
            raise ToolError(f"sketch {sid!r} is not built at this point in the tree")
        solved, frame = r.sketches[sid]
        feat = s.doc.feature(sid)
        fixed = freedom(solved.source or feat, r.env, solved)
        ents = sketch_entities(solved, fixed, {e.id for e in feat.entities if e.type == "external"})
        anchors = _anchors(solved)
        cons = []
        for i, c in enumerate(feat.constraints):
            refs = [anchors[x] for x in c.on if x in anchors] or [(0.0, 0.0)]
            d = {"index": i, "type": c.type, "on": c.on, "name": c.name, "id": c.id,
                 "at": [sum(p[0] for p in refs) / len(refs), sum(p[1] for p in refs) / len(refs)]}
            if c.value is not None:
                expr = str(c.value).strip()
                try:
                    d["value"] = evaluate(c.value, r.env)
                except Exception:
                    d["value"] = None
                d["expr"] = expr
                d["param"] = expr if isinstance(c.value, str) and expr in s.doc.params else None
            cons.append(d)
        is_point = lambda ref: ref == "origin" or "." in ref or solved.entities[ref].type == "point"
        points = [{"ref": ref, "at": _r(p)} for ref, p in anchors.items() if is_point(ref)]
        rep = solved.report
        return {"id": sid, "frame": frame.describe(), "entities": ents, "points": points, "constraints": cons,
                "dof": rep.dof, "status": rep.status, "conflicting": rep.conflicting, "redundant": rep.redundant,
                "conflicting_idx": rep.conflicting_idx, "redundant_idx": rep.redundant_idx,
                "params": sorted(s.doc.params), "on_face": feat.plane.__class__.__name__ == "FacePlane"}

    def face_outline(self, sid: str) -> dict:
        """External entities for every edge of the face a sketch sits on, each named as the edge between
        that face and its neighbour. Edges that can't be named uniquely are skipped and counted."""
        from . import schema as S
        from .topo import list_edges, list_faces, resolve_edges, resolve_faces

        s = self.ws.session()
        feat = s.doc.feature(sid)
        if not isinstance(feat.plane, S.FacePlane):
            raise ToolError("only a sketch on a face can project that face's outline")
        i = s.doc.features.index(feat)
        body = s.regen.run(s.doc.model_copy(update={"features": s.doc.features[:i]})).body  # the part as the sketch sees it
        plane_ref = feat.plane.face.model_dump(exclude_none=True, exclude={"note"})
        if plane_ref.get("pick") == "all":
            plane_ref.pop("pick")
        taken = {e.id for e in feat.entities}
        out, skipped, n = [], 0, 1
        for face in resolve_faces(body, feat.plane.face):
            for edge in list_edges(face):
                neighbours = [f for f in list_faces(body.shape) if not f.IsSame(face) and any(edge.IsSame(x) for x in list_edges(f))]
                labs = body.labels_of(neighbours[0]) if neighbours else []
                if not labs:
                    skipped += 1
                    continue
                lab = labs[0]
                other = {"feature": lab.feature, "role": lab.role, **({"entity": lab.entity} if lab.entity else {}),
                         **({"instance": lab.instance} if lab.instance else {})}
                ref = {"between": [plane_ref, other]}
                for flt in (None, {"type": "line"}, {"type": "circle"}):
                    trial = {**ref, **({"filter": flt} if flt else {})}
                    try:
                        hits = resolve_edges(body, S.EdgeRef.model_validate(trial))
                    except Exception:
                        continue
                    if len(hits) == 1 and hits[0].IsSame(edge):
                        ref = trial
                        break
                else:
                    skipped += 1
                    continue
                while f"proj{n}" in taken:
                    n += 1
                taken.add(f"proj{n}")
                ref["note"] = f"edge where {sid}'s face meets {lab}, projected in the GUI"
                out.append({"id": f"proj{n}", "type": "external", "edge": ref})
        return {"entities": out, "skipped": skipped}

    def sketch_drag(self, sid: str, ref: str, to, grab=None, guess=None) -> dict:
        """Preview a drag without saving it: the client shows it live and commits `ir` as update_entity ops."""
        from .sketch import drag

        r = self.view_result()
        if sid not in r.sketches:
            raise ToolError(f"sketch {sid!r} is not built at this point in the tree")
        feat = r.sketches[sid][0].source or self.ws.session().doc.feature(sid)  # externals already projected
        out, moved = drag(feat, r.env, ref, to, grab, guess)
        return {"moved": moved, "ok": out.report.ok, "entities": sketch_entities(out, {}), "ir": out.to_ir_entities()}

    def marks_context(self, sid: str, marks: list) -> str:
        """Freehand strokes the user drew on a sketch, as coordinates plus the geometry each passes near."""
        r = self.view_result()
        solved = r.sketches.get(sid, (None,))[0]
        lines = []
        for i, stroke in enumerate(marks, start=1):
            pts = [(float(u), float(v)) for u, v in stroke][:200]
            if not pts:
                continue
            xs, ys = [p[0] for p in pts], [p[1] for p in pts]
            size = max(max(xs) - min(xs), max(ys) - min(ys))
            step = max(1, len(pts) // 24)  # ~24 points is plenty to convey a shape
            path = " -> ".join(f"({u:.1f}, {v:.1f})" for u, v in pts[::step] + ([pts[-1]] if (len(pts) - 1) % step else []))
            near = []
            if solved is not None:
                tol = max(0.5, 0.15 * size)
                for e in solved.entities.values():
                    d = min(e.distance_to(p) for p in pts)
                    if d <= tol:
                        near.append((d, e.id))
            closed = len(pts) > 2 and ((pts[0][0] - pts[-1][0]) ** 2 + (pts[0][1] - pts[-1][1]) ** 2) ** 0.5 < 0.2 * max(size, 1e-9)
            desc = f"mark {i} ({'closed loop' if closed else 'stroke'}, spans x {min(xs):.1f}..{max(xs):.1f}, y {min(ys):.1f}..{max(ys):.1f}): {path}"
            if near:
                desc += "; passes near " + ", ".join(eid for _, eid in sorted(near)[:6])
            lines.append(desc)
        if not lines:
            return ""
        return (f"[The user drew {len(lines)} freehand mark(s) on sketch `{sid}` to show what they mean (sketch coordinates, mm; "
                f"not part of the model): " + " | ".join(lines) + "]\n")

    # ── agent ──────────────────────────────────────────────────────
    async def ensure_runner(self):
        if self.runner is None:
            from .agent import AgentRunner
            self.runner = AgentRunner(self.ws, model=self.model, on_event=self.publish,
                                      effort=None if self.effort == "default" else self.effort, resume=self.session_id)
            await self.runner.__aenter__()
        return self.runner

    async def close_runner(self):
        if self.runner is not None:
            try:
                await self.runner.__aexit__(None, None, None)
            except Exception:
                pass
        self.runner = None

    async def reset_runner(self):
        await self.close_runner()
        self.session_id = None
        self.transcript = []
        self.publish({"type": "conversation_reset"})

    async def prompt(self, text: str, selection: str | None, scope: bool, entities: list[str] | None = None,
                     face: dict | None = None, marks: list | None = None) -> None:
        if self.run_task and not self.run_task.done():
            self.publish({"type": "error", "message": "the agent is still working; stop it or wait"})
            return
        prefix = ""
        if self.ws.active:
            prefix = f"[Active part: {_rel(self.ws.active, self.ws.root)}]\n"
        if self.rollback is not None and self.ws.active:
            feats = self.ws.session().doc.features
            if 0 < self.rollback < len(feats):
                prefix += (f"[The user's rollback bar is after feature `{feats[self.rollback - 1].id}`: the model is shown as of "
                           f"that point. Insert new features there (add_feature with \"after\": \"{feats[self.rollback - 1].id}\").]\n")
        if selection and self.ws.active:
            deps = dependents(self.ws.session().doc, selection)
            f = self.ws.session().doc.feature(selection)
            prefix += f"[The user selected feature `{selection}` ({f.type}: {f.intent or 'no intent'}).]\n"
            if entities and f.type == "sketch":
                prefix += sketch_selection_context(f, entities)
            if marks and f.type == "sketch":
                prefix += self.marks_context(selection, marks)
            if scope:
                self.ws.scope = {selection, *deps}
                prefix += f"[Edits are limited to `{selection}` and features that depend on it: {sorted(deps) or 'none'}.]\n"
        if face and face.get("labels") and self.ws.active:
            prefix += face_context(face)
        self.publish({"type": "user_prompt", "text": text, "selection": selection, "scope": scope,
                      "entities": entities or None, "face": (face or {}).get("labels", [None])[0],
                      "marks": len(marks) if marks else None})

        async def go():
            try:
                r = await self.ensure_runner()
                m = await r.run(prefix + text if prefix else text)
                self.session_id = m.session_id or self.session_id
            except Exception as e:
                self.publish({"type": "error", "message": f"{type(e).__name__}: {e}"})
                await self.reset_runner()
            finally:
                self.ws.scope = None

        self.run_task = asyncio.create_task(go())


def _r(p):
    return [round(p[0], 6) + 0.0, round(p[1], 6) + 0.0]


def sketch_entities(solved, fixed: dict[str, bool], external: set[str] = frozenset()) -> list[dict]:
    out = []
    for e in solved.entities.values():
        d = {"id": e.id, "type": e.type, "construction": e.construction, "fixed": fixed.get(e.id, False)}
        if e.id in external:
            d["external"] = True
        if e.type in ("line", "point"):
            d["p1"] = _r(e.p1)
            if e.type == "line":
                d["p2"] = _r(e.p2)
        else:
            d.update(center=_r(e.center), r=round(e.r, 6))
            if e.type == "arc":
                d.update(start_angle=e.start_angle, end_angle=e.end_angle, p1=_r(e.p1), p2=_r(e.p2))
        out.append(d)
    return out


def _anchors(solved) -> dict[str, tuple[float, float]]:
    """Where each reference sits in the sketch: points at themselves, curves at their midpoint."""
    import math

    a: dict[str, tuple[float, float]] = {"origin": (0.0, 0.0)}
    for e in solved.entities.values():
        if e.type == "line":
            a.update({f"{e.id}.p1": e.p1, f"{e.id}.p2": e.p2, e.id: ((e.p1[0] + e.p2[0]) / 2, (e.p1[1] + e.p2[1]) / 2)})
        elif e.type in ("circle", "arc"):
            mid = math.radians(90.0 if e.type == "circle" else (e.start_angle + e.end_angle) / 2)
            a.update({f"{e.id}.center": e.center, e.id: (e.center[0] + e.r * math.cos(mid), e.center[1] + e.r * math.sin(mid))})
            if e.type == "arc":
                a.update({f"{e.id}.start": e.p1, f"{e.id}.end": e.p2})
        else:
            a[e.id] = e.p1
    return a


def sketch_selection_context(sketch, entities: list[str]) -> str:
    """What to tell the agent about entities the user picked in a sketch: their ids and every constraint
    that touches them (by index, so the agent can update or remove exactly those)."""
    picked = set(entities)
    base = {r.split(".")[0] for r in picked}
    touching = []
    for i, c in enumerate(sketch.constraints):
        if any(r in picked or r.split(".")[0] in base for r in c.on):
            val = f" = {c.value}" if c.value is not None else ""
            touching.append(f"#{i}{' ' + c.name if c.name else ''} {c.type}({', '.join(c.on)}){val}")
    s = f"[In sketch `{sketch.id}` the user selected: {', '.join(entities)}."
    s += f" Constraints on them: {'; '.join(touching)}.]\n" if touching else " No constraints touch them.]\n"
    return s


def face_context(face: dict) -> str:
    """The face the user clicked in the 3D view, with a ready-made FaceRef for each of its labels."""
    import re

    refs = []
    for lab in face["labels"][:4]:
        m = re.match(r"^([^.]+)\.([a-z_]+)(?:\[([^\]]+)\])?(?:@(.+))?$", lab)
        if m:
            ref = {"feature": m.group(1), "role": m.group(2)}
            if m.group(3):
                ref["entity"] = m.group(3)
            if m.group(4):
                ref["instance"] = m.group(4)
            refs.append(f"{lab} = {json.dumps(ref)}")
    at = face.get("point")
    where = f" at ({at[0]:.2f}, {at[1]:.2f}, {at[2]:.2f})" if at and len(at) == 3 else ""
    return f"[The user clicked a face in the 3D view{where}: {'; '.join(refs) or ', '.join(face['labels'])}.]\n"


def dependents(doc, fid: str) -> set[str]:
    from .ops import _mentions
    raw = doc.model_dump(mode="json", exclude_none=True)
    found, frontier = set(), {fid}
    while frontier:
        nxt = set()
        for f in raw["features"]:
            if f["id"] not in found and f["id"] != fid and any(_mentions(f, x) for x in frontier):
                nxt.add(f["id"])
        found |= nxt
        frontier = nxt
    return found


def _rev(s) -> str:
    return f"{len(s.undo_stack)}.{len(s.redo_stack)}.{id(s.doc)}"


def _rel(p: str, root: Path) -> str:
    try:
        return str(Path(p).relative_to(root))
    except ValueError:
        return p


def create_app(root: Path, model: str = "sonnet", effort: str = "low") -> FastAPI:
    from contextlib import asynccontextmanager

    A = App(root, model, effort)

    @asynccontextmanager
    async def lifespan(_):
        A.loop = asyncio.get_running_loop()
        A.queue = asyncio.Queue()
        task = asyncio.create_task(A.broadcaster())
        A.loop.run_in_executor(None, lambda: __import__("vibecad.session"))  # warm the CAD imports
        yield
        task.cancel()
        await A.reset_runner()

    api = FastAPI(title="VibeCAD", lifespan=lifespan)
    api.state.A = A

    @api.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    api.mount("/static", StaticFiles(directory=STATIC), name="static")

    def guard(fn, *a, **k):
        try:
            return fn(*a, **k)
        except (ToolError, ValueError, KeyError, FileNotFoundError, FileExistsError) as e:
            raise HTTPException(400, str(e))

    @api.get("/api/parts")
    def parts():
        out = []
        for p in sorted(A.ws.root.rglob("*.vcad.json")):
            if not SKIP_DIRS & set(p.relative_to(A.ws.root).parts):
                out.append(str(p.relative_to(A.ws.root)))
        return {"parts": out, "active": _rel(A.ws.active, A.ws.root) if A.ws.active else None}

    @api.get("/api/state")
    def state():
        return {"state": A.state(), "busy": bool(A.run_task and not A.run_task.done()), "model": A.model}

    @api.post("/api/open")
    async def open_(body: dict = Body(...)):
        await asyncio.to_thread(guard, A.ws.open_part, body["path"])
        return {"state": A.state()}

    @api.post("/api/new")
    async def new(body: dict = Body(...)):
        await asyncio.to_thread(guard, A.ws.new_part, body["path"], body.get("name") or Path(body["path"]).stem)
        return {"state": A.state()}

    @api.post("/api/ops")
    async def ops(body: dict = Body(...)):
        rep = json.loads(await asyncio.to_thread(guard, A.ws.apply_ops, body["ops"], body.get("message", "edit in GUI"), "user"))
        return {"report": {k: v for k, v in rep.items() if k != "tree"}, "state": A.state()}

    @api.post("/api/undo")
    async def undo():
        await asyncio.to_thread(guard, A.ws.undo)
        return {"state": A.state()}

    @api.post("/api/redo")
    async def redo():
        await asyncio.to_thread(guard, A.ws.redo)
        return {"state": A.state()}

    @api.get("/api/feature/{fid}")
    def feature(fid: str):
        return JSONResponse(json.loads(guard(A.ws.get_feature, fid)))

    @api.get("/api/history")
    def history():
        s = guard(A.ws.session)
        p = s.history_path
        lines = [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []
        return {"history": [{k: v for k, v in h.items() if k != "ops"} | {"n_ops": len(h.get("ops", []))} for h in lines][-200:]}

    @api.get("/api/mesh")
    async def mesh():
        return await asyncio.to_thread(guard, A.mesh)

    @api.get("/api/render.png")
    async def render(views: str = "iso,iso_back,iso_below,top", highlight: str = "", v: str = ""):
        png = await asyncio.to_thread(guard, A.ws.render, views.split(","), [h for h in highlight.split(",") if h], 480)
        return Response(png, media_type="image/png", headers={"Cache-Control": "max-age=3600"})

    @api.post("/api/rollback")
    async def rollback(body: dict = Body(...)):
        A.rollback = body.get("index")
        st = await asyncio.to_thread(A.state)
        A.publish({"type": "part_changed", "author": "rollback", "path": A.ws.active})
        return {"state": st}

    @api.get("/api/sketch/{sid}.json")
    async def sketch_json(sid: str):
        return await asyncio.to_thread(guard, A.sketch_geometry, sid)

    @api.get("/api/sketch/{sid}/outline")
    async def sketch_outline(sid: str):
        return await asyncio.to_thread(guard, A.face_outline, sid)

    @api.post("/api/sketch/{sid}/drag")
    async def sketch_drag(sid: str, body: dict = Body(...)):
        return await asyncio.to_thread(guard, A.sketch_drag, sid, body["ref"], body["to"], body.get("grab"), body.get("guess"))

    @api.get("/api/sketch/{sid}.png")
    async def sketch_png(sid: str, v: str = ""):
        png = await asyncio.to_thread(guard, A.ws.render_sketch_image, sid)
        return Response(png, media_type="image/png")

    @api.websocket("/ws")
    async def socket(sock: WebSocket):
        await sock.accept()
        A.clients.add(sock)
        await sock.send_text(json.dumps({"type": "hello", "state": A.state(), "transcript": A.transcript,
                                         "busy": bool(A.run_task and not A.run_task.done()), "model": A.model,
                                         "effort": A.effort}, default=str))
        try:
            while True:
                msg = json.loads(await sock.receive_text())
                if msg["type"] == "prompt":
                    await A.prompt(msg["text"], msg.get("selection"), bool(msg.get("scope")), msg.get("entities"),
                                   msg.get("face"), msg.get("marks"))
                elif msg["type"] == "stop" and A.runner:
                    await A.runner.interrupt()
                elif msg["type"] == "reset":
                    await A.reset_runner()
                elif msg["type"] == "config":
                    if msg.get("model") and msg["model"] != A.model:
                        A.model = msg["model"]
                        if A.runner:
                            await A.runner.set_model(A.model)  # same conversation, next turn uses the new model
                        A.publish({"type": "note", "text": f"model: {A.model}"})
                    if msg.get("effort") and msg["effort"] != A.effort:
                        A.effort = msg["effort"]
                        await A.close_runner()  # effort is fixed per connection: reconnect, resuming the conversation
                        A.publish({"type": "note", "text": f"thinking effort: {A.effort}"})
        except WebSocketDisconnect:
            A.clients.discard(sock)

    return api


def main(argv=None) -> None:
    import uvicorn
    ap = argparse.ArgumentParser(prog="vibecad-app")
    ap.add_argument("--root", default=".", help="folder with your parts (default: current directory)")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--effort", default="low", choices=["low", "medium", "high", "default"],
                    help="thinking effort (low measured 3x faster than default on a bracket, same pass rate)")
    a = ap.parse_args(argv)
    print(f"VibeCAD: http://127.0.0.1:{a.port}")
    uvicorn.run(create_app(Path(a.root).resolve(), a.model, a.effort), host="127.0.0.1", port=a.port, log_level="warning")


if __name__ == "__main__":
    main()
