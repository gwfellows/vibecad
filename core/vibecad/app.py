"""VibeCAD desktop GUI (local web app).

  vibecad-app [--root DIR] [--port 8765] [--model sonnet]   then open http://127.0.0.1:8765

One process holds the Workspace (open parts) and one agent conversation per part (saved next to it as
`<part>.chat.json`, resumed when the part is opened again). Every agent step and every part change is pushed
to the browser over a WebSocket, so you can watch the agent work: its messages and tool calls, the feature
tree changing, the 3D view and renders updating.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
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
        # the agent conversation for the part being worked on: its transcript (replayed to browsers that connect,
        # and when the part is opened again) and the SDK session id (kept across model/effort changes).
        # One per part: opening another part switches to that part's conversation.
        self.conv: dict = {"transcript": [], "session_id": None}
        self.conv_part: str | None = None
        self.convs: dict[str, dict] = {}  # part path -> conversation (a part the agent created shares its creator's)
        self.rollback: int | None = None  # show the part as of features[:rollback]; None = end
        self.mesh_cache: dict[str, Any] = {}
        self._view_cache: tuple | None = None
        self.ws.listeners.append(self.publish)

    @property
    def transcript(self) -> list[dict]:
        return self.conv["transcript"]

    @transcript.setter
    def transcript(self, v: list[dict]) -> None:
        self.conv["transcript"] = v

    @property
    def session_id(self) -> str | None:
        return self.conv["session_id"]

    @session_id.setter
    def session_id(self, v: str | None) -> None:
        self.conv["session_id"] = v

    @property
    def busy(self) -> bool:
        return bool(self.run_task and not self.run_task.done())

    # ── conversations, one per part ────────────────────────────────
    @staticmethod
    def chat_path(part: str) -> Path | None:
        return Path(part[: -len(".vcad.json")] + ".chat.json") if part.endswith(".vcad.json") else None

    def load_conv(self, part: str) -> dict:
        if part not in self.convs:
            c = {"transcript": [], "session_id": None}
            p = self.chat_path(part)
            try:
                if p and p.exists():
                    d = json.loads(p.read_text())
                    c.update(transcript=d.get("transcript") or [], session_id=d.get("session_id"))
            except Exception:
                pass  # a damaged chat file starts a fresh conversation
            self.convs[part] = c
        return self.convs[part]

    def save_conv(self) -> None:
        """Write the current conversation next to every part it belongs to (renders are left out: large)."""
        data = {"session_id": self.session_id, "transcript": [e for e in self.transcript if e.get("type") != "tool_image"]}
        for part, c in list(self.convs.items()):
            if c is self.conv and (p := self.chat_path(part)) is not None and p.parent.exists():
                try:
                    if data["transcript"] or data["session_id"]:
                        p.write_text(json.dumps(data, default=str) + "\n")
                    elif p.exists():
                        p.unlink()
                except OSError:
                    pass

    async def switch_conversation(self, part: str | None) -> None:
        """Show (and continue) `part`'s own conversation instead of the current one."""
        if part is None or part == self.conv_part:
            return
        if self.conv_part is not None:
            self.convs[self.conv_part] = self.conv
            self.save_conv()
        await self.close_runner()
        self.conv, self.conv_part = self.load_conv(part), part
        self.publish({"type": "conversation", "part": _rel(part, self.ws.root), "transcript": self.transcript})

    def adopt_parts(self) -> None:
        """After an agent turn: parts it created or opened continue this conversation."""
        a = self.ws.active
        if a is None:
            return
        if self.conv_part is None or a != self.conv_part:
            self.convs[a] = self.conv
            self.conv_part = a
        self.convs.setdefault(a, self.conv)
        self.save_conv()

    def on_regen(self, name: str, i: int, n: int, fid: str | None) -> None:
        self.publish({"type": "regen", "part": name, "i": i, "n": n, "feature": fid})

    # events can come from worker threads (tools) or the loop (agent stream)
    def publish(self, event: dict) -> None:
        if self.loop is None:
            return
        event = {**event, "t": event.get("t", time.time())}
        if event["type"] not in ("part_changed", "conversation", "regen"):
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
        key = (self.ws.active, _rev(s), self.rollback)
        if self._view_cache and self._view_cache[0] == key:
            return self._view_cache[1]
        doc = s.doc.model_copy(update={"features": s.doc.features[: self.rollback]})
        r = s.regen.run(doc)
        self._view_cache = (key, r)
        return r

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
        users, per = param_usage(s.doc, r.env)
        for f, fr in zip(s.doc.features, r.features):
            d = {"id": f.id, "type": f.type, "name": f.name, "intent": f.intent, "status": fr.status,
                 "message": fr.message, "warnings": fr.warnings, "cached": fr.cached, **per.get(f.id, {})}
            if f.type == "sketch":
                d["dof"] = fr.info.get("dof")
                d["plane"] = f.plane.model_dump(exclude_none=True)
                d["n_entities"] = len(f.entities)
            feats.append(d)
        return {
            "path": self.ws.active, "rel": _rel(self.ws.active, self.ws.root), "name": s.doc.name,
            "material": s.doc.material, "process": s.doc.process, "design_notes": s.doc.design_notes,
            "params": [{"name": k, "expr": v, "value": r.env.get(k), "users": users.get(k, [])} for k, v in s.doc.params.items()],
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
        face_edges = [list_edges(f) for f in list_faces(body.shape)]
        out["edge_seam"] = []  # a cylinder's seam lies inside one face: drawn by nobody, pickable by nobody
        for e in list_edges(body.shape):
            out["edge_seam"].append(sum(any(x.IsSame(e) for x in fe) for fe in face_edges) < 2)
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

    def edge_ref(self, i: int) -> dict:
        """A semantic EdgeRef for edge `i` of the shown body (the index the mesh uses), checked to resolve
        to exactly that edge: the edge between its two faces, narrowed by pick/filter when needed."""
        from itertools import product

        import build123d as bd
        from OCP.TopoDS import TopoDS

        from . import schema as S
        from .topo import face_center, list_edges, list_faces, resolve_edges

        body = self.view_result().body
        edges = list_edges(body.shape) if body.shape is not None else []
        if not 0 <= i < len(edges):
            raise ToolError(f"no edge {i}")
        edge = edges[i]
        faces = [f for f in list_faces(body.shape) if any(edge.IsSame(x) for x in list_edges(f))]

        def as_ref(lab, face, near):
            r = {"feature": lab.feature, "role": lab.role}
            if lab.entity:
                r["entity"] = lab.entity
            if lab.instance:
                r["instance"] = lab.instance
            if near:
                c = face_center(face)
                r.update(pick="nearest", near=[round(c.X, 4), round(c.Y, 4), round(c.Z, 4)])
            return r

        mid = bd.Edge(TopoDS.Edge(edge)).position_at(0.5)
        at = [round(mid.X, 4), round(mid.Y, 4), round(mid.Z, 4)]
        if len(faces) == 2:
            for near, (la, lb), flt in product((False, True), product(body.labels_of(faces[0]), body.labels_of(faces[1])),
                                               (None, {"type": "line"}, {"type": "circle"})):
                ref = {"between": [as_ref(la, faces[0], near), as_ref(lb, faces[1], near)], **({"filter": flt} if flt else {})}
                try:
                    hits = resolve_edges(body, S.EdgeRef.model_validate(ref))
                except Exception:
                    continue
                if len(hits) == 1 and hits[0].IsSame(edge):
                    ref["note"] = f"edge between {la} and {lb}, picked in the GUI"
                    return {"ref": ref, "label": f"{la} | {lb}", "point": at}
            # the two faces meet along more than one edge: keep the one nearest where it was picked
            for la, lb in product(body.labels_of(faces[0]), body.labels_of(faces[1])):
                ref = {"between": [as_ref(la, faces[0], True), as_ref(lb, faces[1], True)], "pick": "nearest", "near": at}
                try:
                    hits = resolve_edges(body, S.EdgeRef.model_validate(ref))
                except Exception:
                    continue
                if len(hits) == 1 and hits[0].IsSame(edge):
                    ref["note"] = f"edge between {la} and {lb} nearest {at}, picked in the GUI"
                    return {"ref": ref, "label": f"{la} | {lb}", "point": at}
        if len(faces) < 2:
            raise ToolError(f"edge {i} is a seam inside one face, not an edge between faces")
        raise ToolError(f"edge {i} can't be named uniquely from its faces' labels")

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

    def feature_edges(self, fid: str) -> dict:
        """The edges a fillet/chamfer's refs resolve to, as edge indices of the body just before it (the body
        the GUI shows when the rollback bar sits above the feature), so the GUI can show and change them."""
        from .topo import list_edges, resolve_edges

        s = self.ws.session()
        ids = [f.id for f in s.doc.features]
        if fid not in ids:
            raise ToolError(f"no feature {fid!r}")
        k = ids.index(fid)
        f = s.doc.features[k]
        if f.type not in ("fillet", "chamfer"):
            raise ToolError(f"{fid} is a {f.type}; only fillet and chamfer edges can be picked")
        if self.rollback == k:
            body = self.view_result().body
        else:
            body = s.regen.run(s.doc.model_copy(update={"features": s.doc.features[:k]})).body
        edges = list_edges(body.shape) if body.shape is not None else []
        items = []
        for ref in f.edges:
            try:
                hits = resolve_edges(body, ref)
                idx, err = [i for i, e in enumerate(edges) if any(e.IsSame(h) for h in hits)], None
            except Exception as e:
                idx, err = [], str(e)
            items.append({"ref": ref.model_dump(mode="json", exclude_none=True, exclude_defaults=True), "idx": idx, "error": err})
        return {"feature": fid, "index": k, "type": f.type, "items": items, "rollback": self.rollback}

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
        self.save_conv()
        self.publish({"type": "conversation_reset"})

    async def prompt(self, text: str, selection: str | None, scope: bool, entities: list[str] | None = None,
                     face: dict | None = None, marks: list | None = None, refs: list | None = None,
                     attachments: list[str] | None = None) -> None:
        if self.busy:
            self.publish({"type": "error", "message": "the agent is still working; stop it or wait"})
            return
        from . import uploads
        try:
            att_ctx, att_blocks = uploads.blocks(self.ws.root, attachments or [])
        except (ValueError, OSError) as e:
            self.publish({"type": "error", "message": f"attachment: {e}"})
            return
        if self.conv_part is None and self.ws.active:
            self.conv_part = self.ws.active
            self.convs.setdefault(self.ws.active, self.conv)
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
        if refs and self.ws.active:
            prefix += refs_context(refs, self.ws.session().doc)
        prefix += att_ctx
        self.publish({"type": "user_prompt", "text": text, "selection": selection, "scope": scope,
                      "entities": entities or None, "face": (face or {}).get("labels", [None])[0],
                      "marks": len(marks) if marks else None,
                      "attachments": [{"id": a, "name": a.split("/")[-1].split("-", 2)[-1]} for a in attachments] if attachments else None})

        async def go():
            try:
                r = await self.ensure_runner()
                p = prefix + text if prefix else text
                m = await (r.run(p, att_blocks) if att_blocks else r.run(p))
                self.session_id = m.session_id or self.session_id
            except Exception as e:
                self.publish({"type": "error", "message": f"{type(e).__name__}: {e}"})
                await self.reset_runner()
            finally:
                self.ws.scope = None
                self.adopt_parts()

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


_TEXT_KEYS = {"intent", "note", "name", "id", "type", "feature", "role", "entity", "sketch", "instance", "on", "features",
              "regions", "axis", "mode", "direction", "extent", "pick", "datum", "construction", "filter"}
_FIELDS = {"extrude": ["distance"], "revolve": ["angle"], "fillet": ["radius"], "chamfer": ["distance"], "shell": ["thickness"],
           "linear_pattern": ["spacing", "count"], "circular_pattern": ["count", "angle"]}


def _param_names(obj, params: set[str]) -> set[str]:
    """Params an IR fragment's numeric fields mention (ids, notes and other text are skipped)."""
    from .expr import names_in

    out = set()
    if isinstance(obj, str):
        return names_in(obj) & params
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k not in _TEXT_KEYS or (k == "direction" and isinstance(v, list)):
                out |= _param_names(v, params)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            out |= _param_names(v, params)
    return out


def param_usage(doc, env: dict[str, float]) -> tuple[dict[str, list[str]], dict[str, dict]]:
    """Which features use each param (directly or through other params' expressions), and for each feature the
    params only it uses plus its own editable numbers: the per-feature parameters the tree shows."""
    from .expr import evaluate, names_in

    names = set(doc.params)
    deps = {p: names_in(e) & names for p, e in doc.params.items()}

    def closure(ps):
        seen, todo = set(), list(ps)
        while todo:
            p = todo.pop()
            if p not in seen:
                seen.add(p)
                todo.extend(deps.get(p, ()))
        return seen

    def val(e):
        try:
            return evaluate(e, env)
        except Exception:
            return None

    users: dict[str, list[str]] = {p: [] for p in doc.params}
    per: dict[str, dict] = {}
    for f in doc.features:
        raw = f.model_dump(mode="json", exclude_none=True)
        for p in closure(_param_names(raw, names)):
            users[p].append(f.id)
        fields = [{"key": k, "expr": raw[k], "value": val(raw[k])} for k in _FIELDS.get(f.type, [])
                  if k in raw and not (k == "distance" and raw.get("extent") == "through_all")]
        dims = []
        if f.type == "sketch":
            dims = [{"name": c.name, "expr": c.value, "value": val(c.value)} for c in f.constraints
                    if c.name and c.value is not None]
        per[f.id] = {"fields": fields, "dims": dims}
    for fid, d in per.items():
        d["params"] = [p for p in doc.params if users[p] == [fid]]
    return users, per


def refs_context(refs: list[dict], doc=None) -> str:
    """References the user embedded in the message (chips like @face:base.end): what each token names,
    with the FaceRef / EdgeRef to use for it. Sketch entities get their touching constraints."""
    lines, sketches = [], {}
    for r in refs[:20]:
        tok, kind, at = r.get("token", "?"), r.get("kind"), r.get("point")
        where = f" at ({at[0]:.2f}, {at[1]:.2f}, {at[2]:.2f})" if at and len(at) == 3 else ""
        if kind == "face" and r.get("ref"):
            lines.append(f"{tok} = the face {r.get('label')}{where}; FaceRef {json.dumps(r['ref'])}")
        elif kind == "edge" and r.get("ref"):
            lines.append(f"{tok} = the edge between {' and '.join(str(r.get('label', '')).split(' | '))}{where}; "
                         f"EdgeRef {json.dumps(r['ref'])}")
        elif kind == "sketch" and r.get("sketch") and r.get("key"):
            sketches.setdefault(r["sketch"], []).append(r["key"])
            lines.append(f"{tok} = {'constraint ' if r['key'].startswith('#') else ''}{r['key']} in sketch `{r['sketch']}`")
    out = "[The user's message references geometry they clicked (each @token in the text): " + "; ".join(lines) + ".]\n" if lines else ""
    for sid, keys in sketches.items():
        try:
            f = doc.feature(sid) if doc is not None else None
        except Exception:
            f = None
        ents = [k for k in keys if not k.startswith("#")]
        if f is not None and f.type == "sketch" and ents:
            out += sketch_selection_context(f, ents)
    return out


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
        from . import regen
        regen.PROGRESS.append(A.on_regen)
        yield
        regen.PROGRESS.remove(A.on_regen)
        A.save_conv()
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
        for d, dirs, files in os.walk(A.ws.root):  # prune .venv etc. instead of walking them (rglob took ~0.5 s)
            dirs[:] = sorted(x for x in dirs if x not in SKIP_DIRS and not x.startswith("."))
            out += [str((Path(d) / f).relative_to(A.ws.root)) for f in files if f.endswith(".vcad.json")]
        out.sort()
        return {"parts": out, "active": _rel(A.ws.active, A.ws.root) if A.ws.active else None}

    @api.get("/api/state")
    def state():
        return {"state": A.state(), "busy": bool(A.run_task and not A.run_task.done()), "model": A.model}

    def not_busy():
        if A.busy:
            raise HTTPException(409, "the agent is working on this part: stop it (or wait) before switching parts")

    @api.post("/api/open")
    async def open_(body: dict = Body(...)):
        not_busy()
        await asyncio.to_thread(guard, A.ws.open_part, body["path"])
        await A.switch_conversation(A.ws.active)
        return {"state": A.state()}

    @api.post("/api/new")
    async def new(body: dict = Body(...)):
        not_busy()
        await asyncio.to_thread(guard, A.ws.new_part, body["path"], body.get("name") or Path(body["path"]).stem)
        await A.switch_conversation(A.ws.active)
        return {"state": A.state()}

    @api.post("/api/upload")
    async def upload(request: Request, name: str):
        from . import uploads
        data = await request.body()
        return await asyncio.to_thread(guard, uploads.save, A.ws.root, name, data)

    @api.get("/api/upload/{fid:path}")
    def upload_file(fid: str):
        from . import uploads
        return FileResponse(guard(uploads.resolve, A.ws.root, fid))

    @api.get("/api/feature/{fid}/edges")
    async def feature_edges(fid: str):
        return await asyncio.to_thread(guard, A.feature_edges, fid)

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

    @api.get("/api/edge/{i}")
    async def edge(i: int):
        return await asyncio.to_thread(guard, A.edge_ref, i)

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
                                         "busy": A.busy, "model": A.model,
                                         "effort": A.effort}, default=str))
        try:
            while True:
                msg = json.loads(await sock.receive_text())
                if msg["type"] == "prompt":
                    await A.prompt(msg["text"], msg.get("selection"), bool(msg.get("scope")), msg.get("entities"),
                                   msg.get("face"), msg.get("marks"), msg.get("refs"), msg.get("attachments"))
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
