"""Face labels that survive regeneration, and resolution of FaceRef / EdgeRef against them.

Every face of the current body carries one or more Labels saying which feature created it and how
(`start`/`end`/`side` of an extrude, `fillet`, ...). Labels are carried through each modelling
operation using OpenCascade's own history (Modified / Generated / IsDeleted), so a reference such as
"the side face swept by sketch line `wall_inner`" keeps pointing at the right face after upstream
dimensions change.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import build123d as bd
from OCP.BRepAdaptor import BRepAdaptor_Curve
from OCP.GeomAbs import GeomAbs_Circle, GeomAbs_Line
from OCP.GProp import GProp_GProps
from OCP.BRepGProp import BRepGProp
from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE
from OCP.TopExp import TopExp_Explorer
from OCP.TopoDS import TopoDS, TopoDS_Shape

from . import schema as S
from .frames import axis_vec


class RefError(ValueError):
    pass


@dataclass(frozen=True)
class Label:
    feature: str
    role: str
    entity: str | None = None
    instance: str | None = None  # None = original; "<pattern_id>#<n>" for copies

    def __str__(self) -> str:
        s = f"{self.feature}.{self.role}"
        if self.entity:
            s += f"[{self.entity}]"
        if self.instance:
            s += f"@{self.instance}"
        return s


@dataclass
class Body:
    shape: TopoDS_Shape | None = None
    labels: list[tuple[TopoDS_Shape, Label]] = field(default_factory=list)

    def faces(self) -> list[TopoDS_Shape]:
        return list_faces(self.shape) if self.shape is not None else []

    def labels_of(self, face) -> list[Label]:
        return [l for f, l in self.labels if f.IsSame(face)]


def explore(shape, kind) -> list[TopoDS_Shape]:
    out, ex = [], TopExp_Explorer(shape, kind)
    while ex.More():
        s = ex.Current()
        if not any(s.IsSame(o) for o in out):
            out.append(s)
        ex.Next()
    return out


def list_faces(shape) -> list[TopoDS_Shape]:
    return explore(shape, TopAbs_FACE)


def list_edges(shape) -> list[TopoDS_Shape]:
    return explore(shape, TopAbs_EDGE)


def propagate(history, pairs, result, fallback: Label | None) -> list[tuple[TopoDS_Shape, Label]]:
    """Carry (face, label) pairs through an operation with OCCT history.

    `history` needs Modified(shape) and IsDeleted(shape) (BRepBuilderAPI_MakeShape subclasses) or
    Modified / IsRemoved (BRepTools_History). Faces of `result` left without any label get `fallback`.
    """
    is_del = getattr(history, "IsDeleted", None) or getattr(history, "IsRemoved")
    out: list[tuple[TopoDS_Shape, Label]] = []
    for f, lab in pairs:
        if is_del(f):
            continue
        mods = list(history.Modified(f))
        for m in mods or [f]:
            out.append((m, lab))
    res_faces = list_faces(result)
    out = [(f, l) for f, l in out if any(f.IsSame(r) for r in res_faces)]
    if fallback is not None:
        for r in res_faces:
            if not any(r.IsSame(f) for f, _ in out):
                out.append((r, fallback))
    return _dedupe(out)


def _dedupe(pairs):
    out = []
    for f, l in pairs:
        if not any(f.IsSame(g) and l == m for g, m in out):
            out.append((f, l))
    return out


# ── Resolution ──────────────────────────────────────────────────────
def _match(lab: Label, ref: S.FaceRef) -> bool:
    if lab.feature != ref.feature or lab.role != ref.role:
        return False
    if ref.entity is not None and lab.entity != ref.entity:
        return False
    if ref.instance == "*":
        return True
    return lab.instance == ref.instance


def face_area(f) -> float:
    p = GProp_GProps()
    BRepGProp.SurfaceProperties_s(f, p)
    return p.Mass()


def face_center(f) -> bd.Vector:
    return bd.Face(TopoDS.Face(f)).center()


def resolve_faces(body: Body, ref: S.FaceRef, require_one: bool = False) -> list[TopoDS_Shape]:
    found: list[TopoDS_Shape] = []
    actual = body.faces()  # return faces as oriented in the solid, not as stored in history
    for f, lab in body.labels:
        if _match(lab, ref) and not any(f.IsSame(g) for g in found):
            found.append(next((a for a in actual if a.IsSame(f)), f))
    if not found:
        raise RefError(f"face ref {_fmt(ref)} matched no faces. Available labels: {available(body, ref.feature)}")
    if ref.pick == "largest":
        found = [max(found, key=face_area)]
    elif ref.pick == "smallest":
        found = [min(found, key=face_area)]
    elif ref.pick == "nearest":
        if ref.near is None:
            raise RefError(f"face ref {_fmt(ref)}: pick 'nearest' needs `near: [x, y, z]`")
        p = bd.Vector(*ref.near)
        found = [min(found, key=lambda f: (face_center(f) - p).length)]
    if require_one and len(found) != 1:
        raise RefError(f"face ref {_fmt(ref)} matched {len(found)} faces where one is required; "
                       "add `entity`, or `pick: largest|smallest|nearest`")
    return found


def resolve_edges(body: Body, ref: S.EdgeRef) -> list[TopoDS_Shape]:
    if ref.between is not None:
        a = resolve_faces(body, ref.between[0])
        b = resolve_faces(body, ref.between[1])
        ea = [e for f in a for e in list_edges(f)]
        eb = [e for f in b for e in list_edges(f)]
        edges = [e for e in ea if any(e.IsSame(x) for x in eb)]
        what = f"between {_fmt(ref.between[0])} and {_fmt(ref.between[1])}"
    else:
        edges = [e for f in resolve_faces(body, ref.of) for e in list_edges(f)]
        what = f"of {_fmt(ref.of)}"
    uniq: list[TopoDS_Shape] = []
    for e in edges:
        if not any(e.IsSame(u) for u in uniq):
            uniq.append(e)
    if ref.filter is not None:
        uniq = [e for e in uniq if _edge_passes(e, ref.filter)]
    if not uniq:
        raise RefError(f"edge ref {what}" + (f" with filter {ref.filter.model_dump(exclude_none=True)}" if ref.filter else "")
                       + " matched no edges")
    if ref.pick == "nearest":
        p = bd.Vertex(*ref.near)
        uniq = [min(uniq, key=lambda e: bd.Edge(TopoDS.Edge(e)).distance_to(p))]
    return uniq


def _edge_passes(edge, flt: S.EdgeFilter) -> bool:
    c = BRepAdaptor_Curve(TopoDS.Edge(edge))
    t = c.GetType()
    if flt.type == "line" and t != GeomAbs_Line:
        return False
    if flt.type == "circle" and t != GeomAbs_Circle:
        return False
    if flt.parallel_to is not None or flt.perpendicular_to is not None:
        if t != GeomAbs_Line:
            return False
        d = c.Line().Direction()
        dv = bd.Vector(d.X(), d.Y(), d.Z())
        if flt.parallel_to is not None and abs(dv.dot(axis_vec(flt.parallel_to))) < 0.999:
            return False
        if flt.perpendicular_to is not None and abs(dv.dot(axis_vec(flt.perpendicular_to))) > 1e-3:
            return False
    return True


def _fmt(ref: S.FaceRef) -> str:
    d = ref.model_dump(exclude_none=True, exclude={"note"})
    if d.get("pick") == "all":
        d.pop("pick")
    return str(d)


def available(body: Body, feature: str | None = None) -> list[str]:
    labs = sorted({str(l) for _, l in body.labels if feature is None or l.feature == feature})
    if labs:
        return labs
    feats = sorted({l.feature for _, l in body.labels})
    return [f"(no faces from {feature!r}; it may have failed or been consumed) features with faces: {feats}"]
