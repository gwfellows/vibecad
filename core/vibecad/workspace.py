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

        return "\n".join(available(self.session().result.body, feature_id))

    def to_world(self, sketch_id: str, u: float, v: float) -> list[float]:
        _, frame = self.session().result.sketches[sketch_id]
        p = frame.to_world(u, v)
        return [round(p.X, 6), round(p.Y, 6), round(p.Z, 6)]

    def to_sketch(self, sketch_id: str, x: float, y: float, z: float) -> list[float]:
        _, frame = self.session().result.sketches[sketch_id]
        return [round(c, 6) for c in frame.to_local((x, y, z))]

    def measure(self) -> str:
        s = self.session().result.summary()
        s.pop("features", None)
        s.pop("face_labels", None)
        return json.dumps(s, indent=1)

    def check_fit(self, other_paths: list[str]) -> str:
        """Overlap volume between the active part and other parts (all modeled in shared world coordinates)."""
        from .regen import Regenerator, load

        me = self.session().result.part
        if me is None:
            raise ToolError("active part has no solid")
        out = {}
        for p in other_paths:
            key = str(self._path(p))
            other = self.sessions[key].result.part if key in self.sessions else Regenerator().run(load(key)).part
            if other is None:
                out[p] = "no solid"
                continue
            ov = (me & other).volume
            gap = me.distance_to(other)
            out[p] = {"overlap_mm3": round(ov, 4), "min_gap_mm": round(gap, 4),
                      "touching": ov < 1e-6 and gap < 1e-4}
        return json.dumps(out, indent=1)

    def render(self, views: list[str] | None = None, highlight: list[str] | None = None, size: int = 520) -> bytes:
        from PIL import Image as PILImage

        from .render import VIEWS, render_view

        s = self.session()
        if s.result.body.shape is None:
            raise ToolError("no solid yet")
        views = views or DEFAULT_VIEWS
        bad = [v for v in views if v not in VIEWS]
        if bad:
            raise ToolError(f"unknown views {bad}; valid: {list(VIEWS)}")
        tiles = []
        with tempfile.TemporaryDirectory() as td:
            for v in views:
                p = Path(td) / f"{v}.png"
                render_view(s.result.body, p, v, f"{s.doc.name}  {v}", highlight=set(highlight or []), size_px=size)
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
        {"step": bd.export_step, "stl": bd.export_stl}[fmt](part, str(out))
        return f"wrote {out}"


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
_OPS_DOC = ("Apply a batch of edit ops as one undoable transaction, regenerate, and report. Op kinds: set_param, "
            "remove_param, set_meta, add_feature, update_feature, remove_feature, move_feature, add_entity, "
            "update_entity, remove_entity, add_constraint, update_constraint, remove_constraint, set_dimension "
            "(shapes in the IR reference). The report has ok, errors, warnings, underconstrained_sketches, notes, "
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
    {"name": "face_labels", "desc": "Face labels on the current body, optionally for one feature. Use to write or repair FaceRefs.",
     "props": {"feature_id": S_STR}, "req": []},
    {"name": "to_world", "desc": "Sketch (u, v) -> world [x, y, z] in mm.",
     "props": {"sketch_id": S_STR, "u": {"type": "number"}, "v": {"type": "number"}}, "req": ["sketch_id", "u", "v"]},
    {"name": "to_sketch", "desc": "World point -> sketch [u, v, w]; w is distance along the sketch normal.",
     "props": {"sketch_id": S_STR, "x": {"type": "number"}, "y": {"type": "number"}, "z": {"type": "number"}},
     "req": ["sketch_id", "x", "y", "z"]},
    {"name": "measure", "desc": "Volume, bounding box, face count, validity and params of the active part.", "props": {}, "req": []},
    {"name": "check_fit", "desc": "Overlap volume and minimum gap between the active part and other part files that share its world coordinates. Use for multi-part designs.",
     "props": {"other_paths": {"type": "array", "items": S_STR}}, "req": ["other_paths"]},
    {"name": "render", "desc": "Render the active part as one tiled PNG. views: iso, iso_back, iso_below, front, top, right (default iso, iso_back, iso_below, top). highlight: feature ids drawn orange.",
     "props": {"views": {"type": "array", "items": S_STR}, "highlight": {"type": "array", "items": S_STR}}, "req": [], "image": True},
    {"name": "render_sketch_image", "desc": "Plot a sketch in its 2D coordinates with entity ids, dimensions, DOF and frame.",
     "props": {"sketch_id": S_STR}, "req": ["sketch_id"], "image": True},
    {"name": "export", "desc": "Export the active part as step or stl (default out/<name>/<name>.<fmt>).",
     "props": {"fmt": {"type": "string", "enum": ["step", "stl"]}, "path": S_STR}, "req": []},
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
