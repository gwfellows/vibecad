"""An editing session on one part file: apply op batches, regenerate, autosave, undo/redo, audit log.

Used by the MCP server (AI agents), and later by the web app. Every successful batch is written to the
part file and appended to `<part>.history.jsonl` with its author and message.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from . import schema as S
from .cli import tree_text
from .expr import names_in
from .ops import OpError, apply_ops, touched_features
from .regen import Regenerator, RegenResult, _strings


def _ref_overlaps(r: RegenResult, max_faces: int = 3000) -> list[str]:
    """Reference bodies the part runs into, shown in every edit report: designs around an import went wrong
    silently when this waited for a check_fit call. Cheap: bounding boxes first, big meshes skipped."""
    import build123d as bd

    part = r.part
    if part is None or not r.refs:
        return []
    out = []
    pb = part.bounding_box()
    for rid, rb in r.refs.items():
        other = bd.Shape.cast(rb.shape)
        if not other.solids() or len(other.faces()) > max_faces:
            continue
        ob = other.bounding_box()
        apart = max(ob.min.X - pb.max.X, pb.min.X - ob.max.X, ob.min.Y - pb.max.Y, pb.min.Y - ob.max.Y,
                    ob.min.Z - pb.max.Z, pb.min.Z - ob.max.Z)
        if apart > 5.0:  # boxes clearly apart: the part isn't where the reference is
            out.append(f"reference {rid} is at least {apart:.0f} mm from the part: the part doesn't hold or meet it yet. "
                       f"Fine mid-build; before you report, put {rid} where the part holds it (place_import onto the face it "
                       "rests on, then_face/then_target for a second contact) or build the part around it")
            continue
        if apart > 0:
            continue  # boxes apart: no overlap
        try:
            common = part & other
            ov = common.volume if common is not None else 0.0
        except Exception:
            continue
        if ov > 1e-3:
            out.append(f"the part overlaps reference {rid} by {ov:.1f} mm³{overlap_where(common)}: in the assembly (and in the user's view) {rid} runs "
                       "through the part. Fine mid-build if the cut for it is still to come; before you report, make it 0: put "
                       f"the geometry where {rid} is, or move {rid} to where you designed for it (translate, place_import). Keep the "
                       "import: it ties the part to the real body; don't remove it to clear this")
    return out


def overlap_where(common) -> str:
    """Where an overlap is, in world mm: 'at x 65..69 y -4..4 z 6..12 (4 x 8 x 6 mm)'. An overlap is a real
    collision (a cable block through a wall), never a numerical artifact; saying where makes that plain."""
    try:
        bb = common.bounding_box()
    except Exception:
        return ""
    return (f" at x {bb.min.X:.1f}..{bb.max.X:.1f} y {bb.min.Y:.1f}..{bb.max.Y:.1f} z {bb.min.Z:.1f}..{bb.max.Z:.1f} "
            f"({bb.size.X:.1f} x {bb.size.Y:.1f} x {bb.size.Z:.1f} mm; a real collision, not a rounding artifact)")


def _ref_apart(r: RegenResult, max_gap: float = 2.0, max_faces: int = 3000) -> list[str]:
    """Reference bodies the finished part doesn't reach: a holder whose phone floats 80 mm away was built
    somewhere else than the phone. Checked at export (min_distance is too slow for every edit on big bodies)."""
    import build123d as bd

    from .measure import min_distance
    part = r.part
    if part is None:
        return []
    out = []
    for rid, rb in r.refs.items():
        if len(bd.Shape.cast(rb.shape).faces()) > max_faces:
            continue
        d = min_distance(part.wrapped, rb.shape)
        if d and d[0] > max_gap:
            out.append(f"reference {rid} is {d[0]:.1f} mm from the part, so the part doesn't hold or meet it where it "
                       f"sits. Move {rid} to where it goes in the assembly (place_import onto the face it rests on, or "
                       "translate/rotate), or build the part around it")
    return out


def dump_doc(doc: S.Document) -> str:
    """Compact, human-readable JSON: defaults omitted, discriminator `type` fields kept."""
    raw = doc.model_dump(mode="json", exclude_none=True, exclude_defaults=True)
    full = doc.model_dump(mode="json", exclude_none=True)
    raw["vibecad"] = full["vibecad"]
    raw["name"] = full["name"]
    raw.setdefault("params", {})
    raw["features"] = raw.get("features", [])
    for f, ff in zip(raw["features"], full["features"]):
        f["type"] = ff["type"]
        f["id"] = ff["id"]
        if ff["type"] == "sketch":
            f.setdefault("entities", [])
            f.setdefault("constraints", [])
            for e, ef in zip(f["entities"], ff["entities"]):
                e["type"] = ef["type"]
    order = ["vibecad", "name", "units", "material", "process", "design_notes", "params", "features"]
    raw = {k: raw[k] for k in order if k in raw}
    feats = []
    for f in raw["features"]:
        head = {k: f[k] for k in ("id", "type", "name", "intent") if k in f}
        feats.append({**head, **{k: v for k, v in f.items() if k not in head}})
    raw["features"] = feats
    return json.dumps(raw, indent=2) + "\n"


class Session:
    def __init__(self, path: str | Path, *, create_name: str | None = None):
        self.path = Path(path)
        if create_name is not None:
            if self.path.exists():
                raise FileExistsError(f"{self.path} already exists")
            self.doc = S.Document(name=create_name)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(dump_doc(self.doc))
        else:
            self.doc = S.Document.model_validate_json(self.path.read_text())
        self.regen = Regenerator(self.path.parent)  # imports resolve relative to the part file
        self.undo_stack: list[tuple[S.Document, str]] = []
        self.redo_stack: list[tuple[S.Document, str]] = []
        self.scope: set[str] | None = None
        self.result: RegenResult = self.regen.run(self.doc)
        self._stamp = self._file_stamp()

    def _file_stamp(self) -> tuple[int, int] | None:
        if not self.path.exists():
            return None
        st = self.path.stat()
        return st.st_mtime_ns, st.st_size

    def stale(self) -> bool:
        """True if the file on disk has changed since this session last loaded or saved it
        (a hand edit, another tool, or another session sharing the same path)."""
        return self.path.exists() and self._file_stamp() != self._stamp

    @property
    def history_path(self) -> Path:
        return self.path.with_name(self.path.name.replace(".vcad.json", "") + ".history.jsonl")

    # ── editing ────────────────────────────────────────────────────
    def apply(self, ops: list[dict[str, Any]], message: str = "", author: str = "agent") -> dict:
        if self.scope is not None:
            outside = sorted(set().union(*[touched_features(o) for o in ops]) - self.scope - {"*params*"})
            if outside:
                return {"ok": False, "applied": 0,
                        "error": f"ops touch {outside}, outside the current scope {sorted(self.scope)}. "
                                 "Ask the user to widen the scope."}
        # changes stay in memory until the file write below, so a failure leaves nothing half-applied
        try:
            new_doc, notes = apply_ops(self.doc, ops)
        except OpError as e:
            self._log({"author": author, "message": message, "rejected": str(e)[:500], "n_ops": len(ops)})
            return {"ok": False, "applied": 0, "error": str(e) + ("\nNothing in this batch was applied (a batch is all or "
                    "nothing): send the whole batch again with this fixed." if len(ops) > 1 else "")}
        before = self.result
        result = self.regen.run(new_doc)  # before swapping: readers never see a new doc with an old result
        self.undo_stack.append((self.doc, message))
        self.redo_stack.clear()
        self.doc, self.result = new_doc, result
        self._absorb_solved()
        self._save()
        self._log({"author": author, "message": message, "ops": ops})
        notes += self._stale_text_notes(before.env, self.result.env, ops)
        return self._report(before, notes, applied=len(ops))

    def _stale_text_notes(self, old: dict, new: dict, ops: list[dict]) -> list[str]:
        """After param values change, point at intents and notes that quote sizes and may now be wrong."""
        changed = {k for k in old.keys() & new.keys() if abs(old[k] - new[k]) > 1e-12}
        if not changed:
            return []
        rewritten = {o.get("id") for o in ops if o.get("op") == "update_feature" and "intent" in o.get("set", {})}
        suspects = []
        for f in self.doc.features:
            if not f.intent or not re.search(r"\d", f.intent) or f.id in rewritten:
                continue
            used = set().union(*[names_in(s) for s in _strings(f.model_dump(mode="json"))])
            if used & changed:
                suspects.append(f"{f.id} ({f.intent!r})")
        if self.doc.design_notes and re.search(r"\d", self.doc.design_notes) and not any(
                o.get("op") == "set_meta" and "design_notes" in o.get("set", {}) for o in ops):
            suspects.append("design_notes")
        if not suspects:
            return []
        what = ", ".join(f"{k} {old[k]:g} -> {new[k]:g}" for k in sorted(changed))
        return [f"sizes changed ({what}); these texts quote numbers and may be out of date: {'; '.join(suspects)}"]

    def undo(self) -> dict:
        if not self.undo_stack:
            return {"ok": False, "error": "nothing to undo"}
        before = self.result
        doc, msg = self.undo_stack[-1]
        result = self.regen.run(doc)
        self.undo_stack.pop()
        self.redo_stack.append((self.doc, msg))
        self.doc, self.result = doc, result
        self._save()
        self._log({"author": "undo", "message": f"undo: {msg}"})
        return self._report(before, [f"undid: {msg or '(no message)'}"])

    def redo(self) -> dict:
        if not self.redo_stack:
            return {"ok": False, "error": "nothing to redo"}
        before = self.result
        doc, msg = self.redo_stack[-1]
        result = self.regen.run(doc)
        self.redo_stack.pop()
        self.undo_stack.append((self.doc, msg))
        self.doc, self.result = doc, result
        self._save()
        self._log({"author": "redo", "message": f"redo: {msg}"})
        return self._report(before, [f"redid: {msg or '(no message)'}"])

    def reload(self) -> dict:
        """Re-read the file (after a user edited it by hand or in another tool)."""
        before = self.result
        doc = S.Document.model_validate_json(self.path.read_text())
        self.doc, self.result = doc, self.regen.run(doc)
        self._stamp = self._file_stamp()
        return self._report(before, ["reloaded from disk"])

    # ── reporting ──────────────────────────────────────────────────
    def tree(self) -> str:
        return tree_text(self.result)

    def _report(self, before: RegenResult, notes: list[str], applied: int = 0) -> dict:
        r = self.result
        errors = [f"{f.id}: {f.message}" for f in r.features if f.status == "error"]
        underconstrained = [f"{sid}: {s.report.dof} DOF" for sid, (s, _) in r.sketches.items() if s.report.dof]
        out: dict[str, Any] = {"ok": not errors, "applied": applied, "tree": self.tree()}
        if notes:
            out["notes"] = notes
        if errors:
            out["errors"] = errors
        warnings = [f"{f.id}: {w}" for f in r.features for w in f.warnings]
        if warnings:
            out["warnings"] = warnings
            out["ok"] = False
        if underconstrained:
            out["underconstrained_sketches"] = underconstrained
        fit = _ref_overlaps(r)
        if fit:
            out["fit"] = fit
        a, b = before.summary(), r.summary()
        if "volume_mm3" in a or "volume_mm3" in b:
            out["change"] = {
                "volume_mm3": [a.get("volume_mm3"), b.get("volume_mm3")],
                "bbox_size_mm": [a.get("bbox_mm", {}).get("size"), b.get("bbox_mm", {}).get("size")],
            }
        return out

    # ── persistence ────────────────────────────────────────────────
    def _save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(dump_doc(self.doc))
        tmp.replace(self.path)
        self._stamp = self._file_stamp()

    def _log(self, entry: dict) -> None:
        entry = {"time": time.strftime("%Y-%m-%dT%H:%M:%S"), **entry}
        with self.history_path.open("a") as fh:
            fh.write(json.dumps(entry) + "\n")

    def _absorb_solved(self) -> None:
        """Store solved sketch coordinates as the new initial guesses, so later dimension changes
        start the solver near the current shape."""
        raw = self.doc.model_dump(mode="json", exclude_none=True)
        changed = False
        for f in raw["features"]:
            if f["type"] != "sketch" or f["id"] not in self.result.sketches:
                continue
            solved, _ = self.result.sketches[f["id"]]
            if not solved.report.ok:
                continue
            upd = {e["id"]: e for e in solved.to_ir_entities()}
            for e in f["entities"]:
                if e["id"] in upd and e.get("type") != "external":  # projected geometry has no stored coordinates
                    new = {k: v for k, v in upd[e["id"]].items() if k != "id"}
                    if any(e.get(k) != v for k, v in new.items()):
                        e.update(new)
                        changed = True
        if changed:
            self.doc = S.Document.model_validate(raw)
