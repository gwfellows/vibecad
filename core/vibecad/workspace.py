"""Agent-facing tools, independent of transport.

A Workspace holds the open parts and implements every tool as a plain method. The stdio MCP server
(`mcp_server.py`), the in-process agent runner (`agent.py`) and the GUI all use the same Workspace, so
a change made by the agent shows up in the GUI immediately (listeners get `part_changed` events).
"""
from __future__ import annotations

import io
import json
import tempfile
import threading
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[2]
GUIDE_PATH = ROOT / "agent" / "GUIDE.md"
IR_PATH = ROOT / "docs" / "IR.md"
DEFAULT_VIEWS = ["iso", "iso_back", "iso_below", "top"]


class ToolError(ValueError):
    pass


class Workspace:
    def __init__(self, root: str | Path = ".") -> None:
        self.root = Path(root).resolve()
        self.sessions: dict[str, Any] = {}  # path -> Session
        self.active: str | None = None
        self.listeners: list[Callable[[dict], None]] = []
        self.lock = threading.RLock()
        self.scope: set[str] | None = None  # feature ids the agent may edit (None = all)

    # ── plumbing ───────────────────────────────────────────────────
    def _path(self, path: str) -> Path:
        p = Path(path)
        return p if p.is_absolute() else self.root / p

    def session(self):
        if self.active is None:
            raise ToolError("no part open: call open_part(path) or new_part(path, name) first")
        return self.sessions[self.active]

    def emit(self, event: dict) -> None:
        for fn in list(self.listeners):
            try:
                fn(event)
            except Exception:  # a broken listener must never break a tool call
                pass

    def _changed(self, author: str, report: dict | None = None) -> None:
        self.emit({"type": "part_changed", "path": self.active, "author": author,
                   "report": {k: v for k, v in (report or {}).items() if k != "tree"}})

    # ── tools ──────────────────────────────────────────────────────
    def ir_reference(self) -> str:
        return IR_PATH.read_text()

    def open_part(self, path: str) -> str:
        from .session import Session

        with self.lock:
            key = str(self._path(path))
            if key not in self.sessions:
                self.sessions[key] = Session(key)
            elif self.sessions[key].stale():
                # the file changed on disk since we last loaded it (hand edit, another tool, or
                # another session on the same path): reload instead of silently serving stale data
                self.sessions[key].reload()
            self.active = key
            self.sessions[key].scope = self.scope
            self._changed("open")
            return self.session().tree()

    def new_part(self, path: str, name: str) -> str:
        from .session import Session

        if not path.endswith(".vcad.json"):
            raise ToolError("path must end in .vcad.json")
        with self.lock:
            key = str(self._path(path))
            self.sessions[key] = Session(key, create_name=name)
            self.active = key
            self._changed("new")
            return f"created {path}"

    def get_tree(self) -> str:
        return self.session().tree()

    def get_feature(self, feature_id: str) -> str:
        from .session import dump_doc

        for f in json.loads(dump_doc(self.session().doc))["features"]:
            if f["id"] == feature_id:
                return json.dumps(f, indent=1)
        raise ToolError(f"no feature {feature_id!r}")

    def get_part_json(self) -> str:
        from .session import dump_doc

        return dump_doc(self.session().doc)

    def get_sketch(self, sketch_id: str) -> str:
        s = self.session()
        if sketch_id not in s.result.sketches:
            raise ToolError(f"sketch {sketch_id!r} has not been built (check get_tree for errors)")
        solved, frame = s.result.sketches[sketch_id]
        feat = s.doc.feature(sketch_id)
        return json.dumps({
            "frame": frame.describe(), "dof": solved.report.dof, "solve": solved.report.status,
            "conflicting": solved.report.conflicting, "redundant": solved.report.redundant,
            "entities": solved.to_ir_entities(),
            "constraints": [{"index": i, **c.model_dump(exclude_none=True)} for i, c in enumerate(feat.constraints)],
        }, indent=1)

    def apply_ops(self, ops: list[dict[str, Any]], message: str, author: str = "agent") -> str:
        with self.lock:
            s = self.session()
            s.scope = self.scope if author == "agent" else None
            rep = s.apply(ops, message=message, author=author)
            if rep.get("applied"):
                self._changed(author, rep)
            else:
                self.emit({"type": "op_rejected", "path": self.active, "author": author, "error": rep.get("error")})
            return json.dumps(rep, indent=1)

    def undo(self) -> str:
        with self.lock:
            rep = self.session().undo()
            self._changed("undo", rep)
            return json.dumps(rep, indent=1)

    def redo(self) -> str:
        with self.lock:
            rep = self.session().redo()
            self._changed("redo", rep)
            return json.dumps(rep, indent=1)

    def reload(self) -> str:
        with self.lock:
            rep = self.session().reload()
            self._changed("reload", rep)
            return json.dumps(rep, indent=1)

    def face_labels(self, feature_id: str | None = None) -> str:
        from .topo import available

        res = self.session().result
        if feature_id in res.refs:  # a reference import: its faces with what they are, to pick for sketches and placing
            return "\n".join(_ref_faces(feature_id, res.refs[feature_id]))
        out = available(res.body, feature_id)
        if feature_id is not None and any(l.feature == feature_id for _, l in res.body.labels):
            # with where each face is and which way it faces: a misnamed polygon edge shows up here, not three
            # place_import calls later
            return "\n".join([f"faces of {feature_id} (plane normals point out of the solid):"]
                             + _ref_faces(feature_id, res.body, feature=feature_id))
        if feature_id is None and res.refs:
            out.append(f"reference imports (face_labels with their id for their faces): {', '.join(res.refs)}")
        return "\n".join(out)

    def _frame(self, sketch_id: str):
        sketches = self.session().result.sketches
        if sketch_id not in sketches:
            raise ToolError(f"sketch {sketch_id!r} has not been built; built sketches: {sorted(sketches)}")
        return sketches[sketch_id][1]

    def to_world(self, sketch_id: str, u: float, v: float) -> list[float]:
        p = self._frame(sketch_id).to_world(u, v)
        return [round(p.X, 6), round(p.Y, 6), round(p.Z, 6)]

    def to_sketch(self, sketch_id: str, x: float, y: float, z: float) -> list[float]:
        return [round(c, 6) for c in self._frame(sketch_id).to_local((x, y, z))]

    def measure(self) -> str:
        from .measure import mass_properties
        res = self.session().result
        s = res.summary()
        s.pop("features", None)
        s.pop("face_labels", None)
        if res.body.shape is not None:
            m = mass_properties(res.body.shape, res.doc.material)
            s.update({k: m[k] for k in ("area", "center_of_mass", "density", "mass_g") if k in m})
            s["area_mm2"] = s.pop("area")
        return json.dumps(s, indent=1)

    def place_import(self, import_id: str, face: dict, target: dict, gap: float = 0.0, align: str = "center",
                     then_face: dict | None = None, then_target: dict | None = None, then_gap: float = 0.0) -> str:
        """Move an import so one of its flat faces lies against a flat face of the part or another import."""
        from . import schema as S
        from .features import FeatureError

        s = self.session()
        res = s.result

        def one(ref: dict, what: str):
            fr = S.FaceRef.model_validate(ref)
            body = res.refs.get(fr.feature, res.body)
            from .topo import resolve_faces
            try:
                fs = resolve_faces(body, fr)
            except (FeatureError, ValueError) as e:
                raise ToolError(f"{what}: {e}") from None
            if len(fs) != 1:
                raise ToolError(f"{what} matched {len(fs)} faces; narrow it to one (entity, or pick + near)")
            return fs[0]
        if ((face.get("feature") != import_id)):
            raise ToolError(f"face must be on {import_id} (its feature is the import id)")
        second = None
        if then_face or then_target:
            if not (then_face and then_target):
                raise ToolError("then_face and then_target go together: a second face of the import and the face it slides onto")
            if then_face.get("feature") != import_id:
                raise ToolError(f"then_face must be on {import_id}")
            second = (one(then_face, "then_face"), one(then_target, "then_target"), then_gap)
        return json.dumps(self.place_faces(import_id, one(face, "face"), one(target, "target"), gap, align,
                                           f"place {import_id} against {target.get('feature')}.{target.get('role')}", "agent",
                                           second=second))

    def place_faces(self, import_id: str, fa, fb, gap: float, align: str, message: str, author: str, second=None) -> dict:
        """The shared core of placing an import: two faces (TopoDS) to a new rotate / translate, applied as one edit."""
        import build123d as bd
        from OCP.TopoDS import TopoDS

        from .expr import evaluate
        from .mate import mate
        s = self.session()
        f = next((x for x in s.doc.features if x.id == import_id), None)
        if f is None or f.type != "import":
            raise ToolError(f"{import_id!r} is not an import")
        planes = []
        for sh in (fa, fb):
            F = bd.Face(TopoDS.Face(sh))
            if F.geom_type != bd.GeomType.PLANE:
                raise ToolError("that face is curved; place imports by flat faces")
            planes.append((tuple(F.normal_at()), tuple(F.center())))
        (n_a, c_a), (n_b, c_b) = planes
        env = s.result.env
        rot0, tr0 = [evaluate(v, env) for v in f.rotate], [evaluate(v, env) for v in f.translate]
        rot, tr = mate(rot0, tr0, n_a, c_a, n_b, c_b, gap=float(gap), align=align)
        import numpy as np
        if second is None:
            # one face pair leaves the turn about the contact open: line up the two faces' long sides (a phone
            # against a wide backrest lands landscape), keeping the face centred where the mate put it
            from .mate import long_axis, rot_xyz, spin_long_sides
            pts = lambda F: [tuple(v) for v in F.tessellate(max(F.bounding_box().diagonal / 30, 1e-3), 0.5)[0]]
            FA, FB = bd.Face(TopoDS.Face(fa)), bd.Face(TopoDS.Face(fb))
            turn0 = rot_xyz(rot) @ rot_xyz(rot0).T
            ax_a = long_axis(pts(FA), n_a)
            rot_s = spin_long_sides(rot, n_b, None if ax_a is None else turn0 @ ax_a, long_axis(pts(FB), n_b))
            if rot_s != list(rot):  # turn about where the face now sits (centred, or slid straight in for "touch")
                c_now = rot_xyz(rot) @ rot_xyz(rot0).T @ (np.array(c_a) - np.array(tr0)) + np.array(tr)
                spin = rot_xyz(rot_s) @ rot_xyz(rot).T
                tr, rot = list(spin @ (np.array(tr) - c_now) + c_now), rot_s
        if second is not None:  # then slide within the first contact plane until a second pair of faces meets

            from .mate import rot_xyz, slide, spin_to
            fa2, fb2, gap2 = second
            A2, B2 = bd.Face(TopoDS.Face(fa2)), bd.Face(TopoDS.Face(fb2))
            if A2.geom_type != bd.GeomType.PLANE or B2.geom_type != bd.GeomType.PLANE:
                raise ToolError("then_face and then_target must be flat")
            # turn about the first contact's normal so the second pair of faces face each other too, keeping the
            # first face's centre where the mate put it
            c_first = rot_xyz(rot) @ rot_xyz(rot0).T @ (np.array(c_a) - np.array(tr0)) + np.array(tr)
            n_a2_now = rot_xyz(rot) @ rot_xyz(rot0).T @ np.array(tuple(A2.normal_at()))
            rot_s = spin_to(rot, n_b, n_a2_now, tuple(B2.normal_at()))
            spin = rot_xyz(rot_s) @ rot_xyz(rot).T
            tr = list(spin @ (np.array(tr) - c_first) + c_first)
            rot = rot_s
            move = rot_xyz(rot) @ rot_xyz(rot0).T  # the whole turn: where the second face points and sits now
            c_a2 = move @ (np.array(tuple(A2.center())) - np.array(tr0)) + np.array(tr)
            n_a2 = move @ np.array(tuple(A2.normal_at()))
            try:
                tr = slide(rot, tr, n_b, n_a2, c_a2, tuple(B2.normal_at()), tuple(B2.center()), float(gap2))
            except ValueError as e:
                raise ToolError(str(e)) from None
        from .mate import rot_xyz
        n_now = rot_xyz(rot) @ rot_xyz(rot0).T @ np.array(n_a)
        contact = (f"the import's face now points {_heading(n_now)}, against a face pointing {_heading(n_b)}. If that "
                   "isn't the face you meant (a misnamed sketch edge), face_labels with the feature id lists each face's normal")
        rep = self.apply_ops([{"op": "update_feature", "id": import_id, "set": {"rotate": rot, "translate": tr}}], message, author)
        out = json.loads(rep) if isinstance(rep, str) else rep
        return {"ok": out.get("ok", False), "rotate": rot, "translate": tr, "contact": contact, "report": out}

    def check_fit(self, other_paths: list[str]) -> str:
        """Overlap volume between the active part and other parts (all modeled in shared world coordinates), and
        its reference imports."""
        import build123d as bd

        from .regen import Regenerator, load

        me = self.session().result.part
        if me is None:
            raise ToolError("active part has no solid")
        out = {}
        res = self.session().result
        for rid, rb in res.refs.items():  # reference imports (the phone, the motor): always checked
            other = bd.Shape.cast(rb.shape)
            if not other.solids():
                out[f"reference {rid}"] = {"min_gap_mm": _gap(me, other), "note": "surfaces or mesh only: no overlap volume"}
                continue
            ov = (me & other).volume
            gap = _gap(me, other)
            out[f"reference {rid}"] = {"overlap_mm3": round(ov, 4), "min_gap_mm": gap, "touching": ov < 1e-6 and gap is not None and gap < 1e-4}
        for p in other_paths:
            key = str(self._path(p))
            if key not in self.sessions and not Path(key).exists():
                raise ToolError(f"no part file {p!r} (paths are relative to {self.root})")
            other = self.sessions[key].result.part if key in self.sessions else Regenerator(Path(key).parent).run(load(key)).part
            if other is None:
                out[p] = "no solid"
                continue
            ov = (me & other).volume
            gap = _gap(me, other)
            out[p] = {"overlap_mm3": round(ov, 4), "min_gap_mm": gap,
                      "touching": ov < 1e-6 and gap is not None and gap < 1e-4}
        bad = [k for k, v in out.items() if isinstance(v, dict) and v.get("overlap_mm3", 0) > 1e-3]
        apart = [f"{k} ({v['min_gap_mm']:g} mm away)" for k, v in out.items()
                 if isinstance(v, dict) and v.get("min_gap_mm") is not None and v["min_gap_mm"] > 1e-4]
        out["verdict"] = (f"OVERLAP with {', '.join(bad)}: the part runs into it. Fix this before reporting: move the "
                          "geometry or the import (place_import), or cut a nest for it (boolean, mode cut, clearance)"
                          if bad else "no overlaps") + (
            f". Not touching: {', '.join(apart)}; if it should rest on or mount to the part, that gap is a mistake"
            if apart else "")
        return json.dumps(out, indent=1)

    def render(self, views: list[str] | None = None, highlight: list[str] | None = None, size: int = 520) -> bytes:
        from PIL import Image as PILImage

        from .render import VIEWS, render_view

        s = self.session()
        if s.result.body.shape is None and not s.result.refs:
            raise ToolError("no solid yet")
        views = views or DEFAULT_VIEWS
        bad = [v for v in views if v not in VIEWS]
        if bad:
            raise ToolError(f"unknown views {bad}; valid: {list(VIEWS)}")
        tiles = []
        with self.lock, tempfile.TemporaryDirectory() as td:  # meshing is not thread-safe (see App.mesh)
            for v in views:
                p = Path(td) / f"{v}.png"
                render_view(s.result.body, p, v, f"{s.doc.name}  {v}", highlight=set(highlight or []), size_px=size,
                            refs=s.result.refs)
                tiles.append(PILImage.open(p).convert("RGB"))
        return tile(tiles)

    def render_sketch_image(self, sketch_id: str) -> bytes:
        from .render import render_sketch

        s = self.session()
        if sketch_id not in s.result.sketches:
            raise ToolError(f"sketch {sketch_id!r} has not been built")
        solved, frame = s.result.sketches[sketch_id]
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "sk.png"
            render_sketch(solved, frame, p, s.doc.feature(sketch_id).constraints)
            return p.read_bytes()

    def export(self, fmt: str = "step", path: str | None = None) -> str:
        import build123d as bd

        s = self.session()
        part = s.result.part
        if part is None:
            raise ToolError("no solid to export")
        out = self._path(path) if path else self.root / "out" / s.doc.name / f"{s.doc.name}.{fmt}"
        out.parent.mkdir(parents=True, exist_ok=True)
        with self.lock:
            write_part(s.result, fmt, out)
        from .session import _ref_apart, _ref_overlaps
        fit = _ref_overlaps(s.result) + _ref_apart(s.result)
        return f"wrote {out}" + "".join(f"\nNOT READY: {w}" for w in fit)


def _ref_faces(rid: str, body, limit: int = 80, feature: str | None = None) -> list[str]:
    """A reference import's (or one feature's) faces: label, surface, and where it is (plane normal and centre,
    cylinder axis and diameter), largest first."""
    import build123d as bd
    from OCP.TopoDS import TopoDS

    from .measure import describe
    from .topo import list_faces
    rows = []
    # the solid's own faces, not the stored label faces: those can carry the opposite orientation, and the
    # normal must point out of the solid
    pairs = [(f, l) for f in list_faces(body.shape) for l in body.labels_of(f)] if body.shape is not None else []
    for face, lab in pairs:
        if feature is not None and lab.feature != feature:
            continue
        F = bd.Face(TopoDS.Face(face))
        d = describe(face)
        c = F.center()
        at = f"centre ({c.X + 0:.2f}, {c.Y + 0:.2f}, {c.Z + 0:.2f})"
        if d.get("surface") == "plane":
            what = f"plane, normal ({', '.join(f'{v + 0:g}' for v in d['normal'])}), {at}"
        elif d.get("surface") == "cylinder":
            what = (f"cylinder d {d['diameter']:g}, axis ({', '.join(f'{v + 0:g}' for v in d['axis'])}) through "
                    f"({', '.join(f'{v + 0:g}' for v in d['axis_point'])})")
        else:
            what = f"{d.get('surface', 'face')}, {at}"
        rows.append((d.get("area", 0), f"{lab}  {what}, area {d.get('area', 0):g} mm²"))
    rows.sort(key=lambda r: -r[0])
    out = [r for _, r in rows[:limit]]
    if len(rows) > limit:
        out.append(f"... {len(rows) - limit} smaller faces not listed")
    return out


def _heading(n) -> str:
    """A direction in words: '+Z (up)', or the vector with its tilt from the nearest axis."""
    import math
    names = {(2, 1): "+Z (up)", (2, -1): "-Z (down)", (0, 1): "+X", (0, -1): "-X", (1, 1): "+Y", (1, -1): "-Y"}
    n = [float(v) for v in n]
    i = max(range(3), key=lambda k: abs(n[k]))
    tilt = math.degrees(math.acos(min(1.0, abs(n[i]) / (math.hypot(*n) or 1))))
    axis = names[(i, 1 if n[i] > 0 else -1)]
    if tilt < 0.5:
        return axis
    return f"({', '.join(f'{v + 0:.3g}' for v in n)}), {tilt:.0f}° off {axis}"


def _gap(a, b) -> float | None:
    from .measure import min_distance
    d = min_distance(a.wrapped, b.wrapped)
    return round(d[0], 4) if d else None


EXPORT_FORMATS = ("step", "stl", "3mf", "brep", "glb", "svg")


def write_part(result, fmt: str, out: Path) -> None:
    """A regenerated part to a file: STEP (exact), STL / 3MF (meshes for printing), BREP (OpenCascade), GLB
    (viewers), SVG (a dimensioned 2D drawing with front/top/right/iso views and hole callouts)."""
    import build123d as bd
    part = result.part
    if fmt == "svg":
        from .drawing import drawing_svg
        out.write_text(drawing_svg(result), encoding="utf-8")
    elif fmt == "step":
        bd.export_step(part, str(out))
    elif fmt == "stl":
        bd.export_stl(part, str(out), tolerance=0.01, angular_tolerance=0.1)
    elif fmt == "brep":
        bd.export_brep(part, str(out))
    elif fmt == "glb":
        bd.export_gltf(part, str(out), binary=True)
    elif fmt == "3mf":
        m = bd.Mesher()
        m.add_shape(part, linear_deflection=0.01, angular_deflection=0.1)
        m.write(str(out))
    else:
        raise ToolError(f"can't export {fmt!r}; use {', '.join(EXPORT_FORMATS)}")


def tile(imgs: list) -> bytes:
    from PIL import Image as PILImage

    cols = 2 if len(imgs) > 1 else 1
    rows = (len(imgs) + cols - 1) // cols
    w, h = max(i.width for i in imgs), max(i.height for i in imgs)
    sheet = PILImage.new("RGB", (cols * w, rows * h), "white")
    for k, im in enumerate(imgs):
        sheet.paste(im, ((k % cols) * w, (k // cols) * h))
    buf = io.BytesIO()
    sheet.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


# ── tool specs, shared by every transport ─────────────────────────────
_OPS_DOC = ("Apply a batch of edit ops as one undoable transaction (all or nothing), regenerate, and report. Ops are not separate tools: every op goes in `ops` here. Op kinds: set_param, "
            "remove_param, set_meta, add_feature, update_feature, remove_feature, move_feature, add_entity, "
            "update_entity, remove_entity, add_constraint, update_constraint, remove_constraint, set_dimension, "
            "rename_feature, rename_entity, and the sketch shortcuts add_rectangle, add_circle, add_slot, add_polygon, "
            "add_regular_polygon (shapes in the IR reference). The report has ok, errors, warnings, underconstrained_sketches, notes, "
            "change (volume/bbox before->after) and the tree. `message` says what the batch does.")

S_STR = {"type": "string"}
TOOLS: list[dict] = [
    {"name": "ir_reference", "desc": "The VibeCAD IR reference: document format, sketches, constraints, features, references, edit ops.",
     "props": {}, "req": []},
    {"name": "open_part", "desc": "Open an existing part file (*.vcad.json) and make it active. Returns the feature tree.",
     "props": {"path": S_STR}, "req": ["path"]},
    {"name": "new_part", "desc": "Create an empty part file (path must end in .vcad.json) and make it active.",
     "props": {"path": S_STR, "name": S_STR}, "req": ["path", "name"]},
    {"name": "get_tree", "desc": "Feature tree of the active part: params, status, sketch DOF, intents, errors, warnings, volume, bbox.",
     "props": {}, "req": []},
    {"name": "get_feature", "desc": "Full JSON of one feature.", "props": {"feature_id": S_STR}, "req": ["feature_id"]},
    {"name": "get_part_json", "desc": "The whole active part file as JSON (prefer get_tree + get_feature).", "props": {}, "req": []},
    {"name": "get_sketch", "desc": "Solved sketch: world frame, DOF, conflicts, solved entity coordinates, constraints with indices.",
     "props": {"sketch_id": S_STR}, "req": ["sketch_id"]},
    {"name": "apply_ops", "desc": _OPS_DOC,
     "props": {"ops": {"type": "array", "items": {"type": "object"}}, "message": S_STR}, "req": ["ops", "message"]},
    {"name": "undo", "desc": "Undo the last applied batch.", "props": {}, "req": []},
    {"name": "redo", "desc": "Redo the last undone batch.", "props": {}, "req": []},
    {"name": "face_labels", "desc": "Face labels on the current body. With a feature id (a part feature or a reference import): each of its faces with its geometry (outward plane normal and centre, cylinder axis and diameter). Use to write or repair FaceRefs, and to check a face is the one you mean before placing against it or sketching on it.",
     "props": {"feature_id": S_STR}, "req": []},
    {"name": "to_world", "desc": "Sketch (u, v) -> world [x, y, z] in mm.",
     "props": {"sketch_id": S_STR, "u": {"type": "number"}, "v": {"type": "number"}}, "req": ["sketch_id", "u", "v"]},
    {"name": "to_sketch", "desc": "World point -> sketch [u, v, w]; w is distance along the sketch normal.",
     "props": {"sketch_id": S_STR, "x": {"type": "number"}, "y": {"type": "number"}, "z": {"type": "number"}},
     "req": ["sketch_id", "x", "y", "z"]},
    {"name": "measure", "desc": "Volume, surface area, bounding box, centre of mass, mass (from the part's material), face count, validity and params of the active part.", "props": {}, "req": []},
    {"name": "place_import", "desc": "Move an import (by rewriting its rotate/translate) so a flat face of it lies against a flat face of the part or another import: normals opposed, `gap` mm apart, centred on the target (align center) or only moved along its normal (align touch). With one face pair it also lines up the two faces' long sides. Optionally then slide it along that contact until a second face of it (then_face) meets a second target (then_target): any flat face it rests on, e.g. a phone leaning on a backrest slid down onto the base or the lip's inner face. Use this rather than nudging translate by hand. Faces are FaceRefs; the import's faces have the import id as feature.",
     "props": {"import_id": S_STR, "face": {"type": "object"}, "target": {"type": "object"}, "gap": {"type": "number"},
               "align": {"type": "string", "enum": ["center", "touch"]},
               "then_face": {"type": "object"}, "then_target": {"type": "object"}, "then_gap": {"type": "number"}},
     "req": ["import_id", "face", "target"]},
    {"name": "check_fit", "desc": "Overlap volume and minimum gap between the active part and other part files that share its world coordinates, and its reference imports (always included). Use for multi-part designs and for parts designed around an imported one.",
     "props": {"other_paths": {"type": "array", "items": S_STR}}, "req": ["other_paths"]},
    {"name": "render", "desc": "Render the active part as one tiled PNG. views: iso, iso_back, iso_below, front, top, right (default iso, iso_back, iso_below, top). highlight: feature ids drawn orange.",
     "props": {"views": {"type": "array", "items": S_STR}, "highlight": {"type": "array", "items": S_STR}}, "req": [], "image": True},
    {"name": "render_sketch_image", "desc": "Plot a sketch in its 2D coordinates with entity ids, dimensions, DOF and frame.",
     "props": {"sketch_id": S_STR}, "req": ["sketch_id"], "image": True},
    {"name": "export", "desc": "Export the active part as step, stl, 3mf, brep, glb, or svg (a dimensioned 2D drawing). Default path out/<name>/<name>.<fmt>.",
     "props": {"fmt": {"type": "string", "enum": list(EXPORT_FORMATS)}, "path": S_STR}, "req": []},
]


def call(ws: Workspace, name: str, args: dict) -> tuple[str | None, bytes | None]:
    """Run a tool by name. Returns (text, png_bytes)."""
    spec = next((t for t in TOOLS if t["name"] == name), None)
    if spec is None:
        raise ToolError(f"unknown tool {name!r}")
    out = getattr(ws, name)(**{k: v for k, v in args.items() if v is not None})
    if spec.get("image"):
        return None, out
    return (out if isinstance(out, str) else json.dumps(out)), None


def schema(spec: dict) -> dict:
    return {"type": "object", "properties": spec["props"], "required": spec["req"]}
