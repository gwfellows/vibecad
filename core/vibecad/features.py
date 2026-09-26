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
    refs: dict[str, Body] = field(default_factory=dict)  # imported reference geometry (import mode "reference")
    base_dir: Path | None = None                          # the part file's folder: relative import paths

    def num(self, v) -> float:
        return evaluate(v, self.env)


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
        src_body = ctx.refs.get(f.plane.face.feature, ctx.body)  # a face of imported reference geometry, or the part
        if src_body.shape is None:
            raise FeatureError("sketch on a face needs an existing body")
        faces = resolve_faces(src_body, f.plane.face)
        frames = [face_frame(bd.Face(TopoDS.Face(x)), ctx.num(f.plane.offset)) for x in faces]
        frame = frames[0]
        for fr in frames[1:]:  # several faces are fine if they lie in one plane (e.g. a top face split by a boss)
            if fr.n.dot(frame.n) < 0.9999 or abs((fr.origin - frame.origin).dot(frame.n)) > 1e-6:
                raise FeatureError(f"sketch plane face ref matched {len(faces)} faces that are not coplanar; "
                                   "add `entity`, or `pick: largest|smallest|nearest`")
    src = _resolve_externals(ctx, f, frame)
    solved = solve_sketch(src, ctx.env)
    solved.source = src
    solved.externals = frozenset(e.id for e in f.entities if isinstance(e, S.External))
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
    ents, cons = [], []
    fix = lambda ref, uv: cons.append(S.Constraint(type="fix", on=[ref], at=(uv[0], uv[1])))
    for e in f.entities:
        if not isinstance(e, S.External):
            ents.append(e)
            continue
        owners = {r.feature for r in ([*e.edge.between] if e.edge.between else [e.edge.of])}
        src_body = next((ctx.refs[o] for o in owners if o in ctx.refs), ctx.body)  # project reference edges too
        if src_body.shape is None:
            raise FeatureError("external geometry needs an existing body to project")
        edges = resolve_edges(src_body, e.edge)
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


# ── Import ────────────────────────────────────────────────────────
_CAD_CACHE: dict[tuple, TopoDS_Shape] = {}


def import_path(ctx: Ctx, file: str) -> Path:
    p = Path(file).expanduser()
    if not p.is_absolute():
        p = (ctx.base_dir or Path.cwd()) / p
    return p


def _read_cad(p: Path) -> TopoDS_Shape:
    st = p.stat()
    key = (str(p.resolve()), st.st_mtime_ns, st.st_size)
    if key not in _CAD_CACHE:
        ext = p.suffix.lower()
        if ext in (".step", ".stp"):
            shape = bd.import_step(str(p))
        elif ext == ".brep":
            shape = bd.import_brep(str(p))
        else:
            raise FeatureError(f"can't import {p.name}: STEP (.step/.stp) or .brep files carry exact geometry; "
                               "mesh files (STL, OBJ, 3MF) don't, so they can't be referenced or cut")
        wrapped = shape.wrapped if hasattr(shape, "wrapped") else shape
        if wrapped is None or not list_faces(wrapped):
            raise FeatureError(f"{p.name} has no faces to import")
        if len(_CAD_CACHE) > 16:
            _CAD_CACHE.clear()
        _CAD_CACHE[key] = wrapped
    return _CAD_CACHE[key]


def do_import(ctx: Ctx, f: S.Import) -> dict:
    p = import_path(ctx, f.file)
    if not p.is_file():
        raise FeatureError(f"import file {f.file!r} not found (looked at {p})")
    try:
        src = _read_cad(p)
    except FeatureError:
        raise
    except Exception as e:
        raise FeatureError(f"could not read {p.name}: {type(e).__name__}: {e}")
    t = gp_Trsf()
    for ax, ang in zip(((1, 0, 0), (0, 1, 0), (0, 0, 1)), f.rotate):
        a = ctx.num(ang)
        if a:
            r = gp_Trsf()
            r.SetRotation(gp_Ax1(gp_Pnt(0, 0, 0), gp_Dir(*ax)), math.radians(a))
            t = r.Multiplied(t)
    at = [ctx.num(v) for v in f.at]
    if any(at):
        m = gp_Trsf()
        m.SetTranslation(gp_Vec(*at))
        t = m.Multiplied(t)
    shape = BRepBuilderAPI_Transform(src, t, True).Shape()
    # faces are numbered in file order: the file doesn't change under the feature, so the numbers are stable
    labels = [(fc, Label(f.id, "face", f"f{i}")) for i, fc in enumerate(list_faces(shape))]
    bb = bd.Shape.cast(shape).bounding_box()
    info = {"file": p.name, "faces": len(labels), "solids": len(bd.Shape.cast(shape).solids()),
            "bbox": {"min": [round(v, 3) for v in (bb.min.X, bb.min.Y, bb.min.Z)], "max": [round(v, 3) for v in (bb.max.X, bb.max.Y, bb.max.Z)]}}
    if f.mode == "reference":
        ctx.refs[f.id] = Body(shape, labels)
        return info
    ctx.tools[f.id] = Tool(shape, labels, f.mode)
    _combine(ctx, f.id, shape, labels, f.mode)
    return info


def describe_reference(body: Body, part: TopoDS_Shape | None = None, limit: int = 60) -> dict:
    """What an agent needs to design around imported geometry: its big flat faces (mounting faces) and its
    round faces (shafts, bores, bolt holes) with axes and positions, plus how it sits against the part."""
    r3 = lambda v: [round(v.X, 3) + 0.0, round(v.Y, 3) + 0.0, round(v.Z, 3) + 0.0]
    planes, cyls = [], []
    for fc, lab in body.labels:
        F = bd.Face(TopoDS.Face(fc))
        gt = F.geom_type
        if gt == bd.GeomType.PLANE:
            planes.append({"face": lab.entity, "normal": r3(F.normal_at()), "center": r3(F.center()), "area": round(F.area, 2)})
        elif gt == bd.GeomType.CYLINDER:
            from OCP.BRepAdaptor import BRepAdaptor_Surface
            cy = BRepAdaptor_Surface(TopoDS.Face(fc)).Cylinder()
            ax, loc = cy.Axis().Direction(), cy.Axis().Location()
            d = bd.Vector(ax.X(), ax.Y(), ax.Z())
            c = F.center()
            p0 = bd.Vector(loc.X(), loc.Y(), loc.Z())
            foot = p0 + d * (c - p0).dot(d)  # axis point level with the face's middle
            cyls.append({"face": lab.entity, "d": round(2 * cy.Radius(), 3), "axis": r3(d), "at": r3(foot), "area": round(F.area, 2)})
    planes.sort(key=lambda x: -x["area"])
    cyls.sort(key=lambda x: (x["d"], x["at"]))
    bb = bd.Shape.cast(body.shape).bounding_box()
    out = {"faces": len(body.labels), "bbox": {"min": r3(bb.min), "max": r3(bb.max), "size": r3(bb.size)},
           "largest_flat_faces": planes[: limit // 3], "round_faces": cyls[:limit],
           "note": "Reference faces as {\"feature\": <import id>, \"role\": \"face\", \"entity\": \"f<n>\"}: sketch on them, "
                   "or project their edges into a sketch with an `external` entity."}
    if part is not None:
        P, R = bd.Shape.cast(part), bd.Shape.cast(body.shape)
        try:
            out["against_part"] = {"overlap_mm3": round((P & R).volume, 3), "min_gap_mm": round(P.distance_to(R), 4)}
        except Exception:
            pass
    return out


# ── Holes ─────────────────────────────────────────────────────────
def _hole_locations(ctx: Ctx, f: S.Hole):
    if f.sketch not in ctx.sketches:
        raise FeatureError(f"hole sketch {f.sketch!r} is missing, later in the tree, or failed to build")
    solved, frame = ctx.sketches[f.sketch]
    ents = {k: e for k, e in solved.entities.items() if k not in solved.externals}
    if f.points:
        missing = [p for p in f.points if p not in ents]
        if missing:
            raise FeatureError(f"hole points {missing} are not entities of sketch {f.sketch!r}")
        pick = [ents[p] for p in f.points]
    else:
        pick = [e for e in ents.values() if e.type == "point"] or [e for e in ents.values() if e.type == "circle"]
    locs = []
    for e in pick:
        if e.type == "point":
            locs.append((e.id, e.p1))
        elif e.type in ("circle", "arc"):
            locs.append((e.id, e.center))
        else:
            raise FeatureError(f"hole point {e.id!r} is a {e.type}; use point entities (or circles, for their centres)")
    if not locs:
        raise FeatureError(f"sketch {f.sketch!r} has no points for holes: add point entities (op add_point) at the hole centres")
    return solved, frame, locs


def _hole_dims(ctx: Ctx, f: S.Hole) -> dict:
    from .fasteners import FITS, lookup

    size = None
    if f.size is not None:
        try:
            size = lookup(f.size)
        except KeyError as e:
            raise FeatureError(str(e.args[0]))
    if f.diameter is not None:
        d = ctx.num(f.diameter)
    else:
        d = size.tap if f.kind == "tapped" else size.clearance[FITS.index(f.fit)]
    if d <= 0:
        raise FeatureError(f"hole diameter must be > 0, got {d:g}")
    depth = None if f.depth is None else ctx.num(f.depth)
    if depth is not None and depth <= 0:
        raise FeatureError(f"hole depth must be > 0, got {depth:g}")
    out = {"d": d, "depth": depth}
    if f.kind == "counterbore":
        D = ctx.num(f.cbore_diameter) if f.cbore_diameter is not None else size.cbore[0]
        h = ctx.num(f.cbore_depth) if f.cbore_depth is not None else size.cbore[1]
        if D <= d or h <= 0:
            raise FeatureError(f"counterbore must be wider than the hole ({D:g} vs {d:g}) and deeper than 0 ({h:g})")
        if depth is not None and h >= depth:
            raise FeatureError(f"counterbore depth {h:g} must be less than the hole depth {depth:g}")
        out["cbore"] = (D, h)
    if f.kind == "countersink":
        D = ctx.num(f.csink_diameter) if f.csink_diameter is not None else size.csink
        a = ctx.num(f.csink_angle) if f.csink_angle is not None else (size.csink_angle if size else 90.0)
        if D <= d or not 0 < a < 180:
            raise FeatureError(f"countersink must be wider than the hole ({D:g} vs {d:g}) with an angle in (0, 180), got {a:g}")
        zc = (D - d) / 2 / math.tan(math.radians(a / 2))
        if depth is not None and zc >= depth:
            raise FeatureError(f"countersink ({D:g} at {a:g}°) is deeper ({zc:.3g}) than the hole ({depth:g})")
        out["csink"] = (D, a)
    if f.kind == "tapped":
        out["thread"] = (size.name if size else f"⌀{d:g} tap drill") + (f", {ctx.num(f.thread_depth):g} deep" if f.thread_depth is not None else "")
    return out


def _hole_tool(p: bd.Vector, drill: bd.Vector, dims: dict, through: float, fid: str, inst: str):
    """One hole as a solid of revolution about the drill axis, and a label for each of its faces."""
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace, BRepBuilderAPI_MakePolygon

    r, depth = dims["d"] / 2, dims["depth"]
    R = max(r, dims.get("cbore", (0, 0))[0] / 2, dims.get("csink", (0, 0))[0] / 2)
    e = max(0.5, 0.2 * R)  # start above the surface so the tool never shares a face with the part
    prof: list[tuple[float, float]] = [(0.0, -e)]
    names: list[str | None] = ["entry"]
    if "cbore" in dims:
        D, h = dims["cbore"]
        prof += [(D / 2, -e), (D / 2, h), (r, h)]
        names += ["cbore_wall", "cbore_floor", "wall"]
    elif "csink" in dims:
        D, a = dims["csink"]
        t = math.tan(math.radians(a / 2))
        prof += [(D / 2 + e * t, -e), (r, (D / 2 - r) / t)]
        names += ["csink", "wall"]
    else:
        prof += [(r, -e)]
        names += ["wall"]
    if depth is None:
        prof += [(r, through), (0.0, through)]
        names += ["exit", None]
    else:
        prof += [(r, depth), (0.0, depth + r / math.tan(math.radians(59)))]  # 118° drill point
        names += ["tip", None]
    u = drill.cross(bd.Vector(1, 0, 0) if abs(drill.X) < 0.9 else bd.Vector(0, 1, 0)).normalized()
    poly = BRepBuilderAPI_MakePolygon()
    for rr, zz in prof:
        q = p + u * rr + drill * zz
        poly.Add(gp_Pnt(q.X, q.Y, q.Z))
    poly.Close()
    face = BRepBuilderAPI_MakeFace(poly.Wire(), True).Face()
    rev = BRepPrimAPI_MakeRevol(face, gp_Ax1(gp_Pnt(p.X, p.Y, p.Z), gp_Dir(drill.X, drill.Y, drill.Z)), 2 * math.pi)
    rev.Build()
    if not rev.IsDone():
        raise FeatureError("building the hole failed")
    labels, mids = [], []
    for edge, name in zip(list_edges(face), names):
        if name is None:
            continue
        mids.append((bd.Edge(TopoDS.Edge(edge)).position_at(0.5), name))
        for g in rev.Generated(edge):
            labels.append((g, Label(fid, "side", name, inst)))
    tool = rev.Shape()
    # OCCT's revolve reports no history for profile edges perpendicular to the axis (the counterbore floor):
    # a swept face contains its generating edge, so label those by the edge midpoint lying on them
    for tf in list_faces(tool):
        if any(tf.IsSame(x) for x, _ in labels):
            continue
        F = bd.Face(TopoDS.Face(tf))
        for mid, name in mids:
            if F.distance_to(mid) < 1e-6:
                labels.append((tf, Label(fid, "side", name, inst)))
                break
    return tool, labels


def do_hole(ctx: Ctx, f: S.Hole) -> dict:
    if ctx.body.shape is None:
        raise FeatureError("a hole needs an existing body")
    solved, frame, locs = _hole_locations(ctx, f)
    dims = _hole_dims(ctx, f)
    drill = frame.n * (-1.0 if f.direction == "reverse" else 1.0)
    through = _through_all_length(ctx, [])

    def build(dr):
        tools, labels = [], []
        for pid, (u, v) in locs:
            t, lab = _hole_tool(frame.to_world(u, v), dr, dims, through, f.id, pid)
            tools.append(t)
            labels += lab
        return _compound(tools), labels

    tool, labels = build(drill)
    # a hole should start on the part's surface: say which ones don't (a point off the part, or a sketch
    # plane that isn't on the face)
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeVertex
    from OCP.BRepExtrema import BRepExtrema_DistShapeShape
    off = []
    for pid, (u, v) in locs:
        q = frame.to_world(u, v)
        dist = BRepExtrema_DistShapeShape(BRepBuilderAPI_MakeVertex(gp_Pnt(q.X, q.Y, q.Z)).Vertex(), ctx.body.shape)
        if dist.IsDone() and dist.Value() > 1e-4 * max(1.0, through):
            off.append(f"{pid} ({dist.Value():.3g} mm away)")
    if off:
        ctx.warnings.append(f"hole centre(s) not on the part's surface: {', '.join(off)}. Holes are drilled from the "
                            "sketch plane, so sketch hole points on the face they go into.")
    ctx.tools[f.id] = Tool(tool, labels, "cut")
    _combine(ctx, f.id, tool, labels, "cut", lambda: build(drill * -1.0)[0])
    info = {"holes": len(locs), "diameter": round(dims["d"], 4), "depth": "through" if dims["depth"] is None else round(dims["depth"], 4)}
    if f.size:
        info["size"] = f.size
    for k in ("cbore", "csink"):
        if k in dims:
            info[k] = [round(x, 4) for x in dims[k]]
    if "thread" in dims:
        info["thread"] = dims["thread"]
    return info


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


def _dressup(ctx: Ctx, fid: str, maker, edges, role: str) -> dict:
    maker.Build()
    if not maker.IsDone():
        raise FeatureError(f"{role} failed on {len(edges)} edge(s); try a smaller size or fewer edges")
    result = maker.Shape()
    pairs = [(g, Label(fid, role)) for e in edges for g in maker.Generated(e)]
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
    mk = BRepOffsetAPI_MakeThickSolid()
    mk.MakeThickSolidByJoin(ctx.body.shape, lst, -t, 1e-4)
    mk.Build()
    if not mk.IsDone():
        raise FeatureError("shell failed; thickness may exceed a local feature size")
    result = mk.Shape()
    pairs = propagate(mk, ctx.body.labels, result, Label(f.id, "inner"))
    ctx.body = Body(result, pairs)
    return {"removed_faces": len(faces), "thickness": t}


# ── Patterns / mirror ──────────────────────────────────────────────
def _replay(ctx: Ctx, pid: str, features: list[str], transforms: list[gp_Trsf]) -> dict:
    for src in features:
        if src not in ctx.tools:
            raise FeatureError(f"can only pattern extrude/revolve features that built before this one; {src!r} is not one")
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


BUILDERS = {
    "sketch": do_sketch, "import": do_import, "extrude": do_extrude, "revolve": do_revolve, "hole": do_hole, "fillet": do_fillet,
    "chamfer": do_chamfer, "shell": do_shell, "linear_pattern": do_linear_pattern,
    "circular_pattern": do_circular_pattern, "mirror": do_mirror,
}
