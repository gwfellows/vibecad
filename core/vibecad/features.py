"""Feature builders. Each takes the regeneration context and the feature, and updates ctx.body."""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path

import build123d as bd
from OCP.BRep import BRep_Builder
from OCP.BRepAdaptor import BRepAdaptor_Curve
from OCP.BRepAlgoAPI import BRepAlgoAPI_Common, BRepAlgoAPI_Cut, BRepAlgoAPI_Fuse
from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
from OCP.BRepFilletAPI import BRepFilletAPI_MakeChamfer, BRepFilletAPI_MakeFillet
from OCP.BRepOffsetAPI import BRepOffsetAPI_MakeThickSolid
from OCP.BRepPrimAPI import BRepPrimAPI_MakePrism, BRepPrimAPI_MakeRevol
from OCP.gp import gp_Ax1, gp_Ax2, gp_Dir, gp_Pnt, gp_Trsf, gp_Vec
from OCP.OCP.collections import List_TopoDS_Shape
from OCP.ShapeUpgrade import ShapeUpgrade_UnifySameDomain
from OCP.TopoDS import TopoDS, TopoDS_Compound, TopoDS_Shape

from . import schema as S
from .expr import evaluate
from .frames import Frame, axis_vec, datum_frame, face_frame
from .sketch import SolvedSketch, regions, solve_sketch
from .topo import Body, Label, list_edges, list_faces, propagate, resolve_edges, resolve_faces


class FeatureError(ValueError):
    pass


@dataclass
class Tool:
    """The solid a feature combined with the body, kept so patterns and mirrors can re-apply it."""
    shape: TopoDS_Shape
    labels: list[tuple[TopoDS_Shape, Label]]
    mode: str


@dataclass
class Ctx:
    env: dict[str, float]
    body: Body = field(default_factory=Body)
    sketches: dict[str, tuple[SolvedSketch, Frame]] = field(default_factory=dict)
    tools: dict[str, Tool] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    refs: dict[str, Body] = field(default_factory=dict)  # reference imports: shown and referenced, not part of the solid
    base_dir: Path = field(default_factory=Path.cwd)  # the part file's folder: import paths are relative to it

    def num(self, v) -> float:
        return evaluate(v, self.env)


def faces_of(ctx: Ctx, ref: S.FaceRef) -> list:
    """resolve_faces on the solid, or on a reference import when the ref names one."""
    if ref.feature in ctx.refs:
        return resolve_faces(ctx.refs[ref.feature], ref)
    if ctx.body.shape is None:
        raise FeatureError("a face reference needs an existing body")
    return resolve_faces(ctx.body, ref)


def edges_of(ctx: Ctx, ref: S.EdgeRef) -> list:
    feats = {r.feature for r in (ref.between or [ref.of]) if r is not None}
    ref_bodies = [ctx.refs[f] for f in feats if f in ctx.refs]
    if ref_bodies:
        if len(feats) > 1:
            raise FeatureError("an edge ref can't mix a reference import's faces with the part's faces")
        return resolve_edges(ref_bodies[0], ref)
    return resolve_edges(ctx.body, ref)


def _compound(shapes) -> TopoDS_Compound:
    comp, b = TopoDS_Compound(), BRep_Builder()
    b.MakeCompound(comp)
    for s in shapes:
        b.Add(comp, s)
    return comp


def _vec(v: bd.Vector) -> gp_Vec:
    return gp_Vec(v.X, v.Y, v.Z)


def _is_valid(shape) -> bool:
    return bd.Shape.cast(shape).is_valid


# ── Sketch ──────────────────────────────────────────────────────────
def do_sketch(ctx: Ctx, f: S.Sketch) -> dict:
    if isinstance(f.plane, S.DatumPlane):
        frame = datum_frame(f.plane.datum, ctx.num(f.plane.offset))
    else:
        faces = faces_of(ctx, f.plane.face)
        frames = [face_frame(bd.Face(TopoDS.Face(x)), ctx.num(f.plane.offset)) for x in faces]
        frame = frames[0]
        for fr in frames[1:]:  # several faces are fine if they lie in one plane (e.g. a top face split by a boss)
            if fr.n.dot(frame.n) < 0.9999 or abs((fr.origin - frame.origin).dot(frame.n)) > 1e-6:
                raise FeatureError(f"sketch plane face ref matched {len(faces)} faces that are not coplanar; "
                                   "add `entity`, or `pick: largest|smallest|nearest`")
    src = _resolve_externals(ctx, f, frame)
    if any(isinstance(e, S.Offset) for e in src.entities):
        from .offset import OffsetError, resolve_offsets

        def solve_base(sk):
            base = solve_sketch(sk, ctx.env)
            if not base.report.ok:
                raise FeatureError(f"the geometry an offset copies did not solve ({base.report.status}); "
                                   f"conflicting: {base.report.conflicting or 'none reported'}")
            return base
        try:
            src = resolve_offsets(src, ctx.env, solve_base)
        except OffsetError as e:
            raise FeatureError(str(e)) from None
    solved = solve_sketch(src, ctx.env)
    solved.source = src
    ctx.sketches[f.id] = (solved, frame)
    rep = solved.report
    info = {"dof": rep.dof, "solve": rep.status, "frame": frame.describe()}
    if rep.conflicting:
        info["conflicting"] = rep.conflicting
    if rep.redundant:
        info["redundant"] = rep.redundant
    if not rep.ok:
        msg = f"sketch did not solve ({rep.status}); conflicting: {rep.conflicting or 'none reported'}"
        bad = _nonpositive_dims(f, ctx.env)
        if bad:
            msg += f". These dimensions are <= 0, which no geometry can satisfy: {', '.join(bad)}"
        raise FeatureError(msg)
    return info


def _resolve_externals(ctx: Ctx, f: S.Sketch, frame: Frame) -> S.Sketch:
    """Replace `external` entities by the projection of their edge: fixed construction geometry. The fixing
    constraints go after the user's, so constraint indices don't change."""
    if not any(isinstance(e, S.External) for e in f.entities):
        return f
    if ctx.body.shape is None and not ctx.refs:
        raise FeatureError("external geometry needs an existing body to project")
    ents, cons = [], []
    fix = lambda ref, uv: cons.append(S.Constraint(type="fix", on=[ref], at=(uv[0], uv[1])))
    for e in f.entities:
        if not isinstance(e, S.External):
            ents.append(e)
            continue
        edges = edges_of(ctx, e.edge)
        if len(edges) != 1:
            raise FeatureError(f"external {e.id!r}: its edge ref matched {len(edges)} edges; it must name one "
                               "(use `between` two faces, or a `filter`)")
        edge = bd.Edge(TopoDS.Edge(edges[0]))
        uv = lambda p: tuple(round(c, 9) for c in frame.to_local(p)[:2])
        a, b, mid = uv(edge.position_at(0)), uv(edge.position_at(1)), uv(edge.position_at(0.5))
        if edge.geom_type == bd.GeomType.LINE:
            if math.dist(a, b) < 1e-7:  # perpendicular to the plane: projects to a point
                ents.append(S.Point(id=e.id, at=a))
                fix(e.id, a)
            else:
                ents.append(S.Line(id=e.id, p1=a, p2=b, construction=True))
                fix(f"{e.id}.p1", a)
                fix(f"{e.id}.p2", b)
            continue
        if edge.geom_type != bd.GeomType.CIRCLE:
            raise FeatureError(f"external {e.id!r}: can only project straight or circular edges, not {edge.geom_type.name.lower()}")
        ax = BRepAdaptor_Curve(edge.wrapped).Circle().Axis().Direction()
        if abs(ax.X() * frame.n.X + ax.Y() * frame.n.Y + ax.Z() * frame.n.Z) < 1 - 1e-6:
            raise FeatureError(f"external {e.id!r}: that circular edge is not parallel to the sketch plane")
        c, r = uv(edge.arc_center), edge.radius
        if math.dist(a, b) < 1e-7:  # full circle
            ents.append(S.Circle(id=e.id, center=c, r=r, construction=True))
            fix(f"{e.id}.center", c)
            cons.append(S.Constraint(type="diameter", on=[e.id], value=2 * r))
            continue
        ang = lambda p: math.degrees(math.atan2(p[1] - c[1], p[0] - c[0])) % 360
        s0, s1, sm = ang(a), ang(b), ang(mid)
        if (sm - s0) % 360 > (s1 - s0) % 360:  # the arc runs clockwise here: swap ends so it's counterclockwise
            a, b, s0, s1 = b, a, s1, s0
        ents.append(S.Arc(id=e.id, center=c, r=r, start_angle=s0, end_angle=s1, construction=True))
        fix(f"{e.id}.center", c)
        fix(f"{e.id}.start", a)
        # the end lies on the circle already: pin only the coordinate that moves fastest along it (no redundancy)
        k = 0 if abs(b[1] - c[1]) >= abs(b[0] - c[0]) else 1
        cons.append(S.Constraint(type=("distance_x", "distance_y")[k], on=["origin", f"{e.id}.end"], value=b[k]))
    return f.model_copy(update={"entities": ents, "constraints": list(f.constraints) + cons})


def _nonpositive_dims(f: S.Sketch, env) -> list[str]:
    out = []
    for i, c in enumerate(f.constraints):
        if c.type in ("distance", "radius", "diameter") and c.value is not None:
            try:
                v = evaluate(c.value, env)
            except Exception:
                continue
            if v <= 0:
                out.append(f"{c.name or '#' + str(i)} {c.type} = {c.value!s} = {v:g}")
    return out


def _profile(ctx: Ctx, p: S.Profile):
    if p.sketch not in ctx.sketches:
        raise FeatureError(f"profile sketch {p.sketch!r} is missing, later in the tree, or failed to build")
    solved, frame = ctx.sketches[p.sketch]
    regs = regions(solved)
    if p.regions != "all":
        want = set(p.regions)
        regs = [r for r in regs if r.outer_entities & want]
    if not regs:
        raise FeatureError(f"no closed regions selected in sketch {p.sketch!r} (regions={p.regions})")
    faces = [(frame.location * r.face).wrapped for r in regs]
    return solved, frame, faces


def _side_labels(gen, tool, faces, solved: SolvedSketch, frame: Frame, fid: str, caps: list) -> list:
    """Label each swept face with the sketch entity that generated it.

    Uses OCCT's Generated() history, and falls back to geometry for faces it misses (OCCT's revolve
    reports no history for profile edges perpendicular to the axis): a swept face always contains its
    generating profile edge, so an unlabeled face gets the entity whose edge midpoint lies on it.
    """
    pairs = []
    mids = []
    for face in faces:
        for e in list_edges(face):
            mid = bd.Edge(TopoDS.Edge(e)).position_at(0.5)
            u, v, _ = frame.to_local(mid)
            ent = solved.entity_at((u, v), tol=1e-3 * max(1.0, abs(u) + abs(v)))
            mids.append((mid, ent))
            for g in gen.Generated(e):
                pairs.append((g, Label(fid, "side", ent)))
    labeled = [f for f, _ in pairs] + [f for f, _ in caps]
    for tf in list_faces(tool):
        if any(tf.IsSame(x) for x in labeled):
            continue
        F = bd.Face(TopoDS.Face(tf))
        for mid, ent in mids:
            if F.distance_to(mid) < 1e-6:
                pairs.append((tf, Label(fid, "side", ent)))
                break
    return pairs


def _n_solids(shape) -> int:
    return len(bd.Shape.cast(shape).solids())


def _overlaps(body, tool) -> bool:
    op = BRepAlgoAPI_Common(body, tool)
    op.Build()
    return op.IsDone() and bd.Shape.cast(op.Shape()).volume > 1e-9


def _combine(ctx: Ctx, fid: str, tool: TopoDS_Shape, tool_labels, mode: str, reversed_tool=None) -> None:
    """`reversed_tool`: builds the same tool extruded the other way, to tell a wrong direction from a wrong
    position when a cut removes nothing."""
    body = ctx.body
    if body.shape is None or mode == "new":
        if mode in ("cut", "intersect"):
            raise FeatureError(f"mode {mode!r} needs an existing body")
        if body.shape is None:
            ctx.body = Body(tool, propagate(_Identity(), list(tool_labels), tool, Label(fid, "new")))
        else:
            ctx.body = Body(_compound([body.shape, tool]), body.labels + list(tool_labels))
        return
    op_cls = {"add": BRepAlgoAPI_Fuse, "cut": BRepAlgoAPI_Cut, "intersect": BRepAlgoAPI_Common}[mode]
    op = op_cls(body.shape, tool)
    op.Build()
    if not op.IsDone():
        raise FeatureError(f"boolean {mode} failed")
    result = op.Shape()
    pairs = propagate(op, body.labels + list(tool_labels), result, Label(fid, "new"))
    # merge coplanar / co-cylindrical faces split by the boolean, keeping labels
    u = ShapeUpgrade_UnifySameDomain(result, True, True, False)
    u.Build()
    merged = u.Shape()
    pairs = propagate(u.History(), pairs, merged, Label(fid, "new"))
    if not list_faces(merged):
        raise FeatureError(f"{mode} left an empty body")
    v0, v1 = bd.Shape.cast(body.shape).volume, bd.Shape.cast(merged).volume
    if abs(v1 - v0) <= 1e-7 * max(v0, 1.0):
        if mode == "cut" and reversed_tool is not None:
            if _overlaps(body.shape, reversed_tool()):
                hint = (" It would cut if extruded the other way: a face sketch's normal points out of the solid, "
                        "so cuts into it need 'reverse'.")
            else:
                hint = (" It would not cut in the other direction either: the profile lies outside the body. Check "
                        "the sketch position and sizes (e.g. holes placed beyond the edge of the part).")
        elif mode == "add":
            hint = " The tool lies entirely inside the body."
        else:
            hint = ""
        ctx.warnings.append(f"{mode} changed no volume (the tool does not overlap the body as intended).{hint}")
    n0, n1 = _n_solids(body.shape), _n_solids(merged)
    if mode == "cut" and n1 > n0:
        ctx.warnings.append(f"cut split the body into {n1} separate solids (it had {n0}); a cut wider than the "
                            "material around it leaves disconnected pieces. Check the cut's size against the part.")
    ctx.body = Body(merged, pairs)


# ── Extrude / revolve ──────────────────────────────────────────────
def do_extrude(ctx: Ctx, f: S.Extrude) -> dict:
    solved, frame, faces = _profile(ctx, f.profile)
    n = frame.n
    if f.extent == "through_all":
        d = _through_all_length(ctx, faces)
    else:
        d = ctx.num(f.distance)
        if d <= 0:
            raise FeatureError(f"extrude distance must be > 0, got {d:g}")
    if f.direction == "symmetric":
        t = gp_Trsf()
        t.SetTranslation(_vec(n * (-d / 2)))
        faces = [BRepBuilderAPI_Transform(fc, t, True).Shape() for fc in faces]
        vec = n * d
    else:
        vec = n * (d if f.direction == "normal" else -d)
    src = _compound(faces)
    prism = BRepPrimAPI_MakePrism(src, _vec(vec))
    prism.Build()
    tool = prism.Shape()
    labels = [(x, Label(f.id, "start")) for x in list_faces(prism.FirstShape())]
    labels += [(x, Label(f.id, "end")) for x in list_faces(prism.LastShape())]
    labels += _side_labels(prism, tool, faces, solved, frame, f.id, labels)
    ctx.tools[f.id] = Tool(tool, labels, f.mode)

    def reversed_tool():
        return BRepPrimAPI_MakePrism(src, _vec(vec * -1)).Shape()

    _combine(ctx, f.id, tool, labels, f.mode, reversed_tool if f.direction != "symmetric" else None)
    return {"length": round(d, 6)}


def _through_all_length(ctx: Ctx, faces) -> float:
    shapes = list(faces) + ([ctx.body.shape] if ctx.body.shape is not None else [])
    bb = bd.Shape.cast(_compound(shapes)).bounding_box()
    return 2.0 * bb.diagonal + 10.0


def do_revolve(ctx: Ctx, f: S.Revolve) -> dict:
    solved, frame, faces = _profile(ctx, f.profile)
    if f.axis in ("x_axis", "y_axis"):
        p, d = frame.origin, (frame.x if f.axis == "x_axis" else frame.y)
    else:
        e = solved.entities.get(f.axis)
        if e is None or e.type != "line":
            raise FeatureError(f"revolve axis {f.axis!r} must be a line in sketch {f.profile.sketch!r}, or x_axis / y_axis")
        a, b = frame.to_world(*e.p1), frame.to_world(*e.p2)
        p, d = a, (b - a).normalized()
    ang = ctx.num(f.angle)
    if not 0 < ang <= 360:
        raise FeatureError(f"revolve angle must be in (0, 360], got {ang:g}")
    rev = BRepPrimAPI_MakeRevol(_compound(faces), gp_Ax1(gp_Pnt(p.X, p.Y, p.Z), gp_Dir(d.X, d.Y, d.Z)), math.radians(ang))
    rev.Build()
    if not rev.IsDone():
        raise FeatureError("revolve failed (does the profile cross the axis?)")
    tool = rev.Shape()
    labels = []
    if ang < 360:
        labels += [(x, Label(f.id, "start")) for x in list_faces(rev.FirstShape())]
        labels += [(x, Label(f.id, "end")) for x in list_faces(rev.LastShape())]
    labels += _side_labels(rev, tool, faces, solved, frame, f.id, labels)
    ctx.tools[f.id] = Tool(tool, labels, f.mode)
    _combine(ctx, f.id, tool, labels, f.mode)
    return {"angle": ang}


# ── Dress-up features ──────────────────────────────────────────────
def _edges(ctx: Ctx, refs: list[S.EdgeRef]):
    if ctx.body.shape is None:
        raise FeatureError("needs an existing body")
    out = []
    for r in refs:
        for e in resolve_edges(ctx.body, r):
            if not any(e.IsSame(o) for o in out):
                out.append(e)
    return out


def _short(lab: Label) -> str:
    """A label as an entity name inside another label: `box.side:box_front` (no brackets or @)."""
    return f"{lab.feature}.{lab.role}" + (f":{lab.entity}" if lab.entity else "") + (f"~{lab.instance}" if lab.instance else "")


def _edge_name(body: Body, edge) -> str | None:
    """An edge named by the faces it joins, `A|B`: tells apart the faces a fillet or chamfer makes on it."""
    names = []
    for f in list_faces(body.shape):
        if any(edge.IsSame(x) for x in list_edges(f)):
            labs = body.labels_of(f)
            if labs:
                names.append(_short(labs[0]))
    return "|".join(sorted(names)) if len(names) == 2 else None


def _dressup(ctx: Ctx, fid: str, maker, edges, role: str) -> dict:
    maker.Build()
    if not maker.IsDone():
        raise FeatureError(f"{role} failed on {len(edges)} edge(s); try a smaller size or fewer edges")
    result = maker.Shape()
    # each face named after the edge it rounds (A|B), and plainly too, so refs without an entity still match
    pairs = []
    for e in edges:
        n = _edge_name(ctx.body, e)
        labs = ([Label(fid, role, n)] if n else []) + [Label(fid, role)]
        pairs += [(g, lab) for g in maker.Generated(e) for lab in labs]
    pairs = propagate(maker, ctx.body.labels, result, None) + pairs
    pairs = propagate(_Identity(), pairs, result, Label(fid, role))
    if not _is_valid(result):
        raise FeatureError(f"{role} produced invalid geometry")
    ctx.body = Body(result, pairs)
    return {"edges": len(edges)}


class _Identity:
    def Modified(self, s):
        return []

    def IsDeleted(self, s):
        return False


def _positive(v: float, what: str) -> float:
    if v <= 0:
        raise FeatureError(f"{what} must be > 0, got {v:g}")
    return v


def do_fillet(ctx: Ctx, f: S.Fillet) -> dict:
    edges = _edges(ctx, f.edges)
    r = _positive(ctx.num(f.radius), "fillet radius")
    mk = BRepFilletAPI_MakeFillet(ctx.body.shape)
    for e in edges:
        mk.Add(r, TopoDS.Edge(e))
    return _dressup(ctx, f.id, mk, edges, "fillet")


def do_chamfer(ctx: Ctx, f: S.Chamfer) -> dict:
    edges = _edges(ctx, f.edges)
    d = _positive(ctx.num(f.distance), "chamfer distance")
    mk = BRepFilletAPI_MakeChamfer(ctx.body.shape)
    for e in edges:
        mk.Add(d, TopoDS.Edge(e))
    return _dressup(ctx, f.id, mk, edges, "chamfer")


def do_shell(ctx: Ctx, f: S.Shell) -> dict:
    if ctx.body.shape is None:
        raise FeatureError("needs an existing body")
    faces = [x for r in f.remove_faces for x in resolve_faces(ctx.body, r)]
    lst = List_TopoDS_Shape()
    for x in faces:
        lst.Append(x)
    t = _positive(ctx.num(f.thickness), "shell thickness")
    from OCP.BRepOffset import BRepOffset_Skin
    from OCP.GeomAbs import GeomAbs_Arc, GeomAbs_Intersection
    # rounded joins first (offset corners follow the part's edges); OCCT often can't build them around a bump or
    # a boss next to the opening, where sharp (intersection) joins still work
    for join, name in ((GeomAbs_Arc, "rounded"), (GeomAbs_Intersection, "sharp")):
        mk = BRepOffsetAPI_MakeThickSolid()
        try:
            mk.MakeThickSolidByJoin(ctx.body.shape, lst, t if f.outward else -t, 1e-4, BRepOffset_Skin, False, False, join)
            mk.Build()
        except Exception:
            continue
        if mk.IsDone() and _is_valid(mk.Shape()):
            break
    else:
        raise FeatureError("shell failed; the wall may be thicker than a local feature (a thin rib, a small boss, a "
                           "hole near the wall), or a removed face carries other features (remove the face they sit on too)")
    result = mk.Shape()
    role = "outer" if f.outward else "inner"
    # each new wall face is named after the face it was offset from (hollow.inner[box.side:box_front]), so the
    # inside of a shelled box has as many names as the outside
    made = [(g, lb) for x, lab in ctx.body.labels for g in mk.Generated(x) for lb in (Label(f.id, role, _short(lab)), Label(f.id, role))]
    pairs = propagate(mk, ctx.body.labels, result, None) + made
    pairs = propagate(_Identity(), pairs, result, Label(f.id, role))
    ctx.body = Body(result, pairs)
    return {"removed_faces": len(faces), "thickness": t, "corners": name}


# ── Patterns / mirror ──────────────────────────────────────────────
def _replay(ctx: Ctx, pid: str, features: list[str], transforms: list[gp_Trsf]) -> dict:
    for src in features:
        if src not in ctx.tools:
            raise FeatureError(f"can only pattern extrude, revolve, hole or import features that built before this one; {src!r} is not one")
    for i, trsf in enumerate(transforms, start=1):
        for src in features:
            tool = ctx.tools[src]
            tr = BRepBuilderAPI_Transform(tool.shape, trsf, True)
            tr.Build()
            inst = f"{pid}#{i}"
            labels = [(m, Label(l.feature, l.role, l.entity, inst)) for s, l in tool.labels for m in tr.Modified(s)]
            _combine(ctx, pid, tr.Shape(), labels, "add" if tool.mode == "new" else tool.mode)
    return {"copies": len(transforms)}


def do_linear_pattern(ctx: Ctx, f: S.LinearPattern) -> dict:
    n, sp = int(round(ctx.num(f.count))), ctx.num(f.spacing)
    if n < 2:
        raise FeatureError("count must be >= 2")
    d = axis_vec(f.direction)
    trs = []
    for i in range(1, n):
        t = gp_Trsf()
        t.SetTranslation(_vec(d * (sp * i)))
        trs.append(t)
    return _replay(ctx, f.id, f.features, trs)


def do_circular_pattern(ctx: Ctx, f: S.CircularPattern) -> dict:
    n, span = int(round(ctx.num(f.count))), ctx.num(f.angle)
    if n < 2:
        raise FeatureError("count must be >= 2")
    step = span / n if abs(span - 360) < 1e-9 else span / (n - 1)
    a = axis_vec(f.axis)
    o = [ctx.num(v) for v in f.origin]
    ax = gp_Ax1(gp_Pnt(*o), gp_Dir(a.X, a.Y, a.Z))
    trs = []
    for i in range(1, n):
        t = gp_Trsf()
        t.SetRotation(ax, math.radians(step * i))
        trs.append(t)
    return _replay(ctx, f.id, f.features, trs)


def do_mirror(ctx: Ctx, f: S.Mirror) -> dict:
    fr = datum_frame(f.plane.datum, ctx.num(f.plane.offset))
    t = gp_Trsf()
    t.SetMirror(gp_Ax2(gp_Pnt(fr.origin.X, fr.origin.Y, fr.origin.Z), gp_Dir(fr.n.X, fr.n.Y, fr.n.Z)))
    return _replay(ctx, f.id, f.features, [t])


# ── Hole ───────────────────────────────────────────────────────────
def hole_points(ctx: Ctx, f: S.Hole) -> list[tuple[str, tuple[float, float]]]:
    if f.sketch not in ctx.sketches:
        raise FeatureError(f"hole sketch {f.sketch!r} is missing, later in the tree, or failed to build")
    solved, _ = ctx.sketches[f.sketch]
    out = []
    for e in solved.entities.values():
        if f.points != "all" and e.id not in f.points:
            continue
        if e.type == "point":
            out.append((e.id, tuple(e.p1)))
        elif e.type in ("circle", "arc") and not (f.points == "all" and e.construction):  # construction circles
            out.append((e.id, tuple(e.center)))  # (and projected edges) only when named in `points`
    if f.points != "all":
        missing = set(f.points) - {i for i, _ in out}
        if missing:
            raise FeatureError(f"hole points {sorted(missing)} are not points, circles or arcs in sketch {f.sketch!r}")
    if not out:
        raise FeatureError(f"sketch {f.sketch!r} has no points or circles to put holes at")
    return out


def hole_profile(ctx: Ctx, f: S.Hole, depth: float) -> list[tuple[tuple[float, float], str]]:
    """The hole's half-section as (r, z) vertices, z = 0 at the sketch plane and negative into the part, each
    with the role of the face the edge from it to the next vertex sweeps. Closed back along the axis."""
    r = _positive(ctx.num(f.diameter), "hole diameter") / 2
    # on a face, start just outside the surface so the cut never leaves a sliver; from a datum plane (which may
    # lie inside the part) start exactly on it
    src = ctx.sketches[f.sketch][0].source
    lead = max(0.05, r * 0.05) if src is not None and isinstance(src.plane, S.FacePlane) else 0.0
    pts: list[tuple[tuple[float, float], str]] = []
    if f.kind == "counterbore":
        R, h = ctx.num(f.cbore_diameter) / 2, ctx.num(f.cbore_depth)
        if R <= r:
            raise FeatureError(f"counterbore diameter ({2 * R:g}) must be larger than the hole ({2 * r:g})")
        if not 0 < h < depth:
            raise FeatureError(f"counterbore depth must be > 0 and less than the hole depth ({depth:g}), got {h:g}")
        pts += [((0, lead), "top"), ((R, lead), "cbore_wall"), ((R, -h), "cbore_floor"), ((r, -h), "wall")]
    elif f.kind == "countersink":
        R, a = ctx.num(f.csk_diameter) / 2, ctx.num(f.csk_angle)
        if R <= r:
            raise FeatureError(f"countersink diameter ({2 * R:g}) must be larger than the hole ({2 * r:g})")
        if not 0 < a < 180:
            raise FeatureError(f"countersink angle must be between 0 and 180, got {a:g}")
        t = math.tan(math.radians(a / 2))
        h = (R - r) / t
        if h >= depth:
            raise FeatureError(f"the countersink ({h:.3g} deep) is deeper than the hole ({depth:g})")
        pts += [((0, lead), "top"), ((R + lead * t, lead), "csk"), ((r, -h), "wall")]
    else:
        pts += [((0, lead), "top"), ((r, lead), "wall")]
    tip = ctx.num(f.tip_angle) if f.extent == "blind" else 0
    if tip and not 0 < tip < 180:
        raise FeatureError(f"tip angle must be between 0 and 180 (0 = flat bottom), got {tip:g}")
    if tip:
        pts += [((r, -depth), "tip"), ((0, -depth - r / math.tan(math.radians(tip / 2))), "axis")]
    else:
        pts += [((r, -depth), "bottom"), ((0, -depth), "axis")]
    return pts


def do_hole(ctx: Ctx, f: S.Hole) -> dict:
    if ctx.body.shape is None:
        raise FeatureError("a hole needs an existing body")
    solved, frame = ctx.sketches.get(f.sketch, (None, None))
    where = hole_points(ctx, f)
    if f.extent == "through_all":
        bb = bd.Shape.cast(ctx.body.shape).bounding_box()
        depth = 2 * bb.diagonal + 10
    else:
        depth = _positive(ctx.num(f.depth), "hole depth")
    prof = hole_profile(ctx, f, depth)
    down = frame.n * (-1 if f.direction == "reverse" else 1)
    tools, labels = [], []
    for pid, (u, v) in where:
        o = frame.to_world(u, v)
        # the profile lives in the plane through the axis spanned by the sketch x (radius) and the axis (depth)
        to3 = lambda rz: o + frame.x * rz[0] + down * (-rz[1])
        poly = bd.Wire.make_polygon([to3(p) for p, _ in prof], close=True)
        face = bd.Face(poly)
        rev = BRepPrimAPI_MakeRevol(face.wrapped, gp_Ax1(gp_Pnt(o.X, o.Y, o.Z), gp_Dir(down.X, down.Y, down.Z)), 2 * math.pi)
        rev.Build()
        if not rev.IsDone():
            raise FeatureError(f"hole at {pid} failed to build")
        edges = list_edges(poly.wrapped)
        roles = {}
        for e in edges:  # which profile segment each edge is: match by its midpoint
            mid = bd.Edge(TopoDS.Edge(e)).position_at(0.5)
            for k, (p, role) in enumerate(prof):
                a, b = to3(p), to3(prof[(k + 1) % len(prof)][0])
                if ((a + b) * 0.5 - mid).length < 1e-6 * max(1.0, depth):
                    roles[k] = (e, role)
        mine = []
        for e, role in roles.values():
            if role in ("axis", "top"):
                continue
            for g in rev.Generated(e):
                mine.append((g, Label(f.id, role, pid)))
        # OCCT's revolve reports no history for profile edges perpendicular to the axis (a counterbore floor, a
        # flat bottom): such a face contains its profile edge, so match the rest by geometry
        for tf in list_faces(rev.Shape()):
            if any(tf.IsSame(g) for g, _ in mine):
                continue
            F = bd.Face(TopoDS.Face(tf))
            for e, role in roles.values():
                if role not in ("axis", "top") and F.distance_to(bd.Edge(TopoDS.Edge(e)).position_at(0.5)) < 1e-6:
                    mine.append((tf, Label(f.id, role, pid)))
                    break
        labels += mine
        tools.append(rev.Shape())
    tool = _compound(tools) if len(tools) > 1 else tools[0]
    ctx.tools[f.id] = Tool(tool, labels, "cut")
    _combine(ctx, f.id, tool, labels, "cut")
    return {"holes": len(where), "depth": round(depth, 6) if f.extent == "blind" else "through"}


# ── Text ───────────────────────────────────────────────────────────
def do_text(ctx: Ctx, f: S.Text) -> dict:
    if f.sketch not in ctx.sketches:
        raise FeatureError(f"text sketch {f.sketch!r} is missing, later in the tree, or failed to build")
    if not f.text.strip():
        raise FeatureError("text is empty")
    solved, frame = ctx.sketches[f.sketch]
    if f.at == "origin":
        u, v = 0.0, 0.0
    else:
        e = solved.entities.get(f.at)
        if e is None or e.type != "point":
            raise FeatureError(f"text position {f.at!r} must be a point of sketch {f.sketch!r} (or origin)")
        u, v = e.p1
    size = _positive(ctx.num(f.size), "text size")
    depth = _positive(ctx.num(f.depth), "text depth")
    kw = {}
    if f.font:
        kw["font_path" if f.font.lower().endswith((".ttf", ".otf")) else "font"] = f.font
    align = (bd.Align.CENTER, bd.Align.CENTER) if f.align == "center" else (bd.Align.MIN, bd.Align.CENTER)
    try:
        txt = bd.Text(f.text, font_size=size, align=align, **kw)
    except Exception as ex:
        raise FeatureError(f"could not lay out the text: {ex}") from None
    loc = frame.location * bd.Location((u, v, 0), (0, 0, ctx.num(f.angle)))
    faces = [(loc * x).wrapped for x in txt.faces()]
    if not faces:
        raise FeatureError("the text has no glyphs to cut (only spaces?)")
    n = frame.n
    vec = n * (-depth if f.mode == "cut" else depth)  # cut into the face, raise out of it
    src = _compound(faces)
    prism = BRepPrimAPI_MakePrism(src, _vec(vec))
    prism.Build()
    tool = prism.Shape()
    labels = [(x, Label(f.id, "start")) for x in list_faces(prism.FirstShape())]
    labels += [(x, Label(f.id, "end")) for x in list_faces(prism.LastShape())]
    labels += [(g, Label(f.id, "side")) for fc in faces for e in list_edges(fc) for g in prism.Generated(e)]
    labels = propagate(_Identity(), labels, tool, Label(f.id, "side"))
    ctx.tools[f.id] = Tool(tool, labels, f.mode)
    _combine(ctx, f.id, tool, labels, f.mode)
    bb = bd.Shape.cast(src).bounding_box()
    return {"glyph_faces": len(faces), "width": round(max(bb.size.X, bb.size.Y, bb.size.Z), 3)}


# ── Import ─────────────────────────────────────────────────────────
def import_transform(ctx: Ctx, f: S.Import) -> gp_Trsf:
    k = ctx.num(f.scale)
    if k <= 0:
        raise FeatureError(f"import scale must be > 0, got {k:g}")
    t = gp_Trsf()
    if abs(k - 1) > 1e-12:
        t.SetScale(gp_Pnt(0, 0, 0), k)
    for ang, axis in zip(f.rotate, ((1, 0, 0), (0, 1, 0), (0, 0, 1))):
        a = ctx.num(ang)
        if a:
            r = gp_Trsf()
            r.SetRotation(gp_Ax1(gp_Pnt(0, 0, 0), gp_Dir(*axis)), math.radians(a))
            t = r * t
    d = [ctx.num(v) for v in f.translate]
    if any(d):
        m = gp_Trsf()
        m.SetTranslation(gp_Vec(*d))
        t = m * t
    return t


def do_import(ctx: Ctx, f: S.Import) -> dict:
    from .importer import ImportError_, is_part, load, load_part, part_label

    path = Path(f.file) if Path(f.file).is_absolute() else ctx.base_dir / f.file
    part_labels = None
    try:
        if is_part(path):
            shape, part_labels, info = load_part(path)
        else:
            shape, info = load(path, as_solid=f.mode != "reference")
    except ImportError_ as e:
        raise FeatureError(str(e)) from None
    tr = BRepBuilderAPI_Transform(shape, import_transform(ctx, f), True)
    tr.Build()
    shape = tr.Shape()
    faces = list_faces(shape)
    if part_labels is not None:  # another part: its own labels, carried onto the moved copy
        labels = [(m, Label(f.id, "face", part_label(l))) for x, l in part_labels for m in tr.Modified(x)]
        labels += [(x, Label(f.id, "face", f"f{i}")) for i, x in enumerate(faces) if not any(x.IsSame(y) for y, _ in labels)]
    elif info.get("mesh"):
        labels = [(x, Label(f.id, "mesh")) for x in faces]
    else:
        labels = [(x, Label(f.id, "face", f"f{i}")) for i, x in enumerate(faces)]
    bb = bd.Shape.cast(shape).bounding_box()
    out = {"format": info["format"], "faces": len(faces),
           "bbox": [round(v, 3) for v in (bb.size.X, bb.size.Y, bb.size.Z)]}
    if "triangles" in info:
        out["triangles"] = info["triangles"]
    if f.mode == "reference":
        ctx.refs = {**ctx.refs, f.id: Body(shape, labels)}
        return out
    solids = bd.Shape.cast(shape).solids()
    if not solids:
        raise FeatureError(f"{path.name} has surfaces but no closed solid, so it can't be combined with the part; "
                           "import it with mode 'reference'")
    out["volume"] = round(sum(x.volume for x in solids), 3)
    ctx.tools[f.id] = Tool(shape, labels, f.mode)
    _combine(ctx, f.id, shape, labels, f.mode)
    return out


BUILDERS = {
    "sketch": do_sketch, "extrude": do_extrude, "revolve": do_revolve, "fillet": do_fillet,
    "chamfer": do_chamfer, "shell": do_shell, "linear_pattern": do_linear_pattern,
    "circular_pattern": do_circular_pattern, "mirror": do_mirror, "import": do_import, "hole": do_hole, "text": do_text,
}
