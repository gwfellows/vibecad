"""Compile an IR sketch to PlaneGCS, solve it, and turn its closed loops into faces."""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import build123d as bd
from planegcs import Sketch as GcsSketch

from . import schema as S
from .expr import evaluate


class SketchError(ValueError):
    pass


@dataclass
class SolvedEntity:
    id: str
    type: str
    construction: bool
    # line: p1, p2 ; circle: center, r ; arc: center, r, start_angle, end_angle (deg), p1=start, p2=end ; point: p1
    p1: tuple[float, float] | None = None
    p2: tuple[float, float] | None = None
    center: tuple[float, float] | None = None
    r: float | None = None
    start_angle: float | None = None
    end_angle: float | None = None

    def distance_to(self, p: tuple[float, float]) -> float:
        x, y = p
        if self.type == "line":
            (ax, ay), (bx, by) = self.p1, self.p2
            dx, dy = bx - ax, by - ay
            L2 = dx * dx + dy * dy or 1e-30
            t = max(0.0, min(1.0, ((x - ax) * dx + (y - ay) * dy) / L2))
            return math.hypot(x - ax - t * dx, y - ay - t * dy)
        if self.type in ("circle", "arc"):
            cx, cy = self.center
            d = abs(math.hypot(x - cx, y - cy) - self.r)
            if self.type == "arc":
                ang = math.degrees(math.atan2(y - cy, x - cx))
                sweep = (self.end_angle - self.start_angle) % 360 or 360
                if (ang - self.start_angle) % 360 > sweep + 1e-6:
                    d = min(math.dist(p, self.p1), math.dist(p, self.p2))
            return d
        return math.dist(p, self.p1)


@dataclass
class SolveReport:
    status: str
    dof: int
    conflicting: list[str] = field(default_factory=list)
    redundant: list[str] = field(default_factory=list)
    conflicting_idx: list[int] = field(default_factory=list)  # constraint indices, for the editor
    redundant_idx: list[int] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status in ("Success", "Converged") and not self.conflicting


@dataclass
class SolvedSketch:
    id: str
    entities: dict[str, SolvedEntity]
    report: SolveReport

    def entity_at(self, p: tuple[float, float], tol: float = 1e-3) -> str | None:
        best, best_d = None, tol
        for e in self.entities.values():
            if e.construction or e.type == "point":
                continue
            d = e.distance_to(p)
            if d < best_d:
                best, best_d = e.id, d
        return best

    def to_ir_entities(self) -> list[dict]:
        """Solved coordinates in IR form, for writing back as better initial guesses."""
        out = []
        for e in self.entities.values():
            r6 = lambda v: round(v, 6)
            if e.type == "point":
                out.append({"id": e.id, "at": [r6(e.p1[0]), r6(e.p1[1])]})
            elif e.type == "line":
                out.append({"id": e.id, "p1": [r6(v) for v in e.p1], "p2": [r6(v) for v in e.p2]})
            elif e.type == "circle":
                out.append({"id": e.id, "center": [r6(v) for v in e.center], "r": r6(e.r)})
            else:
                out.append({"id": e.id, "center": [r6(v) for v in e.center], "r": r6(e.r),
                            "start_angle": r6(e.start_angle), "end_angle": r6(e.end_angle)})
        return out


def _describe(i: int, c: S.Constraint) -> str:
    label = c.name or f"#{i}"
    return f"{label} {c.type}({', '.join(c.on)})" + (f"={c.value}" if c.value is not None else "")


def solve_sketch(sk: S.Sketch, env: dict[str, float]) -> SolvedSketch:
    g = GcsSketch()
    refs: dict[str, tuple[str, int]] = {}
    origin = g.add_fixed_point(0.0, 0.0)
    refs["origin"] = ("point", origin)
    refs["x_axis"] = ("line", g.add_line(origin, g.add_fixed_point(1.0, 0.0)))
    refs["y_axis"] = ("line", g.add_line(origin, g.add_fixed_point(0.0, 1.0)))
    ent_ids: dict[str, dict[str, int]] = {}

    def ev(v, default=0.0):
        try:
            return float(evaluate(v, env))
        except Exception:
            return default

    def xy(v):
        return ev(v[0]), ev(v[1])

    for e in sk.entities:
        if isinstance(e, S.Point):
            p = g.add_point(*xy(e.at))
            refs[e.id] = ("point", p)
            ent_ids[e.id] = {"p": p}
        elif isinstance(e, S.Line):
            p1, p2 = g.add_point(*xy(e.p1)), g.add_point(*xy(e.p2))
            refs[e.id] = ("line", g.add_line(p1, p2))
            refs[f"{e.id}.p1"], refs[f"{e.id}.p2"] = ("point", p1), ("point", p2)
            ent_ids[e.id] = {"p1": p1, "p2": p2}
        elif isinstance(e, S.Circle):
            c = g.add_point(*xy(e.center))
            cid = g.add_circle(c, g.add_param(ev(e.r, 1.0)))
            refs[e.id], refs[f"{e.id}.center"] = ("circle", cid), ("point", c)
            ent_ids[e.id] = {"c": c, "id": cid}
        elif isinstance(e, S.Arc):
            sa, ea = math.radians(ev(e.start_angle)), math.radians(ev(e.end_angle, 90.0))
            if ea <= sa:
                ea += 2 * math.pi
            cx, cy = xy(e.center)
            r = ev(e.r, 1.0)
            c = g.add_point(cx, cy)
            ps = g.add_point(cx + r * math.cos(sa), cy + r * math.sin(sa))
            pe = g.add_point(cx + r * math.cos(ea), cy + r * math.sin(ea))
            aid = g.add_arc_cse(c, ps, pe, r, sa, ea)
            refs[e.id] = ("arc", aid)
            refs[f"{e.id}.center"], refs[f"{e.id}.start"], refs[f"{e.id}.end"] = ("point", c), ("point", ps), ("point", pe)
            ent_ids[e.id] = {"c": c, "id": aid}

    tag_owner: dict[int, int] = {}
    for i, c in enumerate(sk.constraints):
        try:
            tags = _add_constraint(g, c, refs, env)
        except SketchError as ex:
            raise SketchError(f"sketch {sk.id!r}, constraint {_describe(i, c)}: {ex}") from None
        for t in tags if isinstance(tags, (list, tuple)) else [tags]:
            tag_owner[int(t)] = i

    status = g.solve()
    diag = g.diagnose()

    def names(tags):
        return sorted({_describe(tag_owner[int(t)], sk.constraints[tag_owner[int(t)]]) for t in tags if int(t) in tag_owner})

    def idx(tags):
        return sorted({tag_owner[int(t)] for t in tags if int(t) in tag_owner})

    report = SolveReport(status=status.name, dof=int(diag.dof), conflicting=names(diag.conflicting),
                         redundant=names(diag.redundant), conflicting_idx=idx(diag.conflicting),
                         redundant_idx=idx(diag.redundant))

    solved: dict[str, SolvedEntity] = {}
    pt = lambda pid: tuple(float(v) for v in g.get_point(pid))
    for e in sk.entities:
        ids = ent_ids[e.id]
        if isinstance(e, S.Point):
            solved[e.id] = SolvedEntity(e.id, "point", e.construction, p1=pt(ids["p"]))
        elif isinstance(e, S.Line):
            solved[e.id] = SolvedEntity(e.id, "line", e.construction, p1=pt(ids["p1"]), p2=pt(ids["p2"]))
        elif isinstance(e, S.Circle):
            ci = g.get_circle(ids["id"])
            solved[e.id] = SolvedEntity(e.id, "circle", e.construction, center=tuple(ci.center), r=float(ci.radius))
        else:
            a = g.get_arc(ids["id"])
            sa, ea = math.degrees(a.start_angle), math.degrees(a.end_angle)
            if ea < sa:  # normalise to a CCW sweep
                sa, ea = ea, sa
                p1, p2 = tuple(a.end_point), tuple(a.start_point)
            else:
                p1, p2 = tuple(a.start_point), tuple(a.end_point)
            solved[e.id] = SolvedEntity(e.id, "arc", e.construction, center=tuple(a.center), r=float(a.radius),
                                        start_angle=sa, end_angle=ea, p1=p1, p2=p2)
    return SolvedSketch(sk.id, solved, report)


def _add_constraint(g: GcsSketch, c: S.Constraint, refs, env):
    def get(r):
        if r not in refs:
            raise SketchError(f"unknown reference {r!r}")
        return refs[r]

    items = [get(r) for r in c.on]
    kinds = [k for k, _ in items]
    ids = [i for _, i in items]
    val = evaluate(c.value, env) if c.value is not None else None
    n = len(items)

    def need(*patterns):
        if tuple(kinds) not in patterns:
            want = " or ".join("(" + ", ".join(p) + ")" for p in patterns)
            raise SketchError(f"expects {want}, got ({', '.join(kinds)})")

    def endpoints(line_ref):
        return get(f"{line_ref}.p1")[1], get(f"{line_ref}.p2")[1]

    t = c.type
    if t == "coincident":
        if kinds == ["point", "point"]:
            return g.coincident(*ids)
        if sorted(kinds) == ["line", "point"]:
            p, l = (ids[0], ids[1]) if kinds[0] == "point" else (ids[1], ids[0])
            return g.point_on_line(p, l)
        need(("point", "point"))
    if t in ("horizontal", "vertical"):
        if kinds == ["line"]:
            return g.horizontal(ids[0]) if t == "horizontal" else g.vertical(ids[0])
        need(("line",), ("point", "point"))
        return g.horizontal_points(*ids) if t == "horizontal" else g.vertical_points(*ids)
    if t in ("parallel", "perpendicular"):
        need(("line", "line"))
        return g.parallel(*ids) if t == "parallel" else g.perpendicular(*ids)
    if t == "equal":
        if kinds == ["line", "line"]:
            return g.equal_length(*ids)
        if kinds == ["circle", "circle"]:
            return g.equal_radius_cc(*ids)
        if kinds == ["arc", "arc"]:
            return g.equal_radius_aa(*ids)
        if sorted(kinds) == ["arc", "circle"]:
            ci, ai = (ids[0], ids[1]) if kinds[0] == "circle" else (ids[1], ids[0])
            return g.equal_radius_ca(ci, ai)
        need(("line", "line"), ("circle", "circle"), ("arc", "arc"), ("circle", "arc"))
    if t == "tangent" and n == 3:
        # endpoint tangency (FreeCAD style): curves meet at the given point with parallel tangents
        if kinds[2] != "point" or kinds[0] == "point" or kinds[1] == "point":
            raise SketchError("3-reference tangent expects (curve, curve, shared_point)")
        ang = 0.0 if _tangent_dot(g, items[0], items[1], ids[2], refs, c.on) >= 0 else math.pi
        return g.angle_via_point(ids[0], ids[1], ids[2], g.add_fixed_param(ang))
    if t == "tangent":
        pair = tuple(kinds)
        if pair in (("line", "circle"), ("circle", "line")):
            l, ci = (ids[0], ids[1]) if kinds[0] == "line" else (ids[1], ids[0])
            return g.tangent_line_circle(l, ci)
        if pair in (("line", "arc"), ("arc", "line")):
            l, a = (ids[0], ids[1]) if kinds[0] == "line" else (ids[1], ids[0])
            return g.tangent_line_arc(l, a)
        if pair in (("circle", "arc"), ("arc", "circle")):
            ci, a = (ids[0], ids[1]) if kinds[0] == "circle" else (ids[1], ids[0])
            return g.tangent_circle_arc(ci, a)
        if pair == ("arc", "arc"):
            return g.tangent_arc_arc(*ids)
        if pair == ("circle", "circle"):
            return g.tangent_circle_circle(*ids)
        need(("line", "circle"), ("line", "arc"), ("circle", "arc"), ("arc", "arc"), ("circle", "circle"))
    if t == "point_on":
        need(("point", "line"), ("point", "circle"), ("point", "arc"))
        return {"line": g.point_on_line, "circle": g.point_on_circle, "arc": g.point_on_arc}[kinds[1]](*ids)
    if t == "midpoint":
        need(("point", "line"))
        a, b = endpoints(c.on[1])
        return g.symmetric_point(a, b, ids[0])
    if t == "symmetric":
        need(("point", "point", "line"), ("point", "point", "point"))
        return g.symmetric_line(*ids) if kinds[2] == "line" else g.symmetric_point(*ids)
    if t == "concentric":
        need(("circle", "circle"), ("circle", "arc"), ("arc", "circle"), ("arc", "arc"))
        return g.coincident(get(f"{c.on[0]}.center")[1], get(f"{c.on[1]}.center")[1])
    if t == "fix":
        need(("point",))
        return list(g.fix_point(ids[0], evaluate(c.at[0], env), evaluate(c.at[1], env)))
    if t == "distance":
        if kinds == ["line"]:
            return g.set_p2p_distance(*endpoints(c.on[0]), val)
        if kinds == ["point", "point"]:
            return g.set_p2p_distance(*ids, val)
        if sorted(kinds) == ["line", "point"]:
            p, l = (ids[0], ids[1]) if kinds[0] == "point" else (ids[1], ids[0])
            return g.set_p2l_distance(p, l, val)
        need(("line",), ("point", "point"), ("point", "line"))
    if t in ("distance_x", "distance_y"):
        if kinds == ["line"]:
            a, b = endpoints(c.on[0])
        else:
            need(("line",), ("point", "point"))
            a, b = ids
        k = 0 if t == "distance_x" else 1
        pa, pb = g.get_point_param_ids(a)[k], g.get_point_param_ids(b)[k]
        return g.difference(pa, pb, g.add_fixed_param(val))
    if t in ("radius", "diameter"):
        need(("circle",), ("arc",))
        if kinds[0] == "circle":
            return g.set_circle_radius(ids[0], val) if t == "radius" else g.set_circle_diameter(ids[0], val)
        return g.set_arc_radius(ids[0], val) if t == "radius" else g.set_arc_diameter(ids[0], val)
    if t == "angle":
        if kinds == ["line"]:
            return g.set_l2l_angle(refs["x_axis"][1], ids[0], math.radians(val))
        need(("line",), ("line", "line"))
        return g.set_l2l_angle(ids[0], ids[1], math.radians(val))
    raise SketchError(f"unsupported constraint {t!r}")


# ── Interactive editing: drag preview and freedom ───────────────────
# PlaneGCS leaves a conflicting system untouched, so a drag can't be one "put this point at the cursor"
# solve: that only works for a point with 2 free directions. For less free points we try weaker pulls
# (x only, y only, onto the line through the cursor across the point's direction of motion) and keep the
# result nearest the cursor. Nothing here is saved; the caller commits the result as new initial guesses.
_TMP = "__drag"


def point_of(solved: SolvedSketch, ref: str) -> tuple[float, float] | None:
    """Position of a point reference (`l.p1`, `c.center`, `a.start`, a point entity id, `origin`)."""
    if ref == "origin":
        return (0.0, 0.0)
    eid, _, sub = ref.partition(".")
    e = solved.entities.get(eid)
    if e is None:
        return None
    if not sub:
        return e.p1 if e.type == "point" else None
    return {"p1": e.p1, "p2": e.p2, "center": e.center, "start": e.p1, "end": e.p2}.get(sub)


def _with_guess(sk: S.Sketch, solved: SolvedSketch) -> S.Sketch:
    upd = {e["id"]: {k: v for k, v in e.items() if k != "id"} for e in solved.to_ir_entities()}
    return sk.model_copy(update={"entities": [e.model_copy(update=upd.get(e.id, {})) for e in sk.entities]})


def _attempt(sk: S.Sketch, env, entities=(), constraints=()) -> SolvedSketch | None:
    """Solve sk plus temporary entities/constraints; None if it doesn't solve cleanly."""
    trial = sk.model_copy(update={"entities": list(sk.entities) + list(entities),
                                  "constraints": list(sk.constraints) + list(constraints)})
    try:
        s = solve_sketch(trial, env)
    except SketchError:
        return None
    if not s.report.ok:
        return None
    s.entities = {k: v for k, v in s.entities.items() if not k.startswith(_TMP)}
    return s


def _fix(ref, at) -> S.Constraint:
    return S.Constraint(type="fix", on=[ref], at=(float(at[0]), float(at[1])))


def _pull_point(sk: S.Sketch, env, cur: SolvedSketch, ref: str, to) -> SolvedSketch | None:
    """Move point `ref` as close to `to` as its constraints allow. Long moves go in sub-steps so a point
    that slides along a curve follows it instead of jumping to a far branch."""
    s = _attempt(sk, env, constraints=[_fix(ref, to)])
    if s is not None:
        return s
    p = point_of(cur, ref)
    size = max((math.hypot(*q) for e in cur.entities.values() for q in (e.p1, e.p2, e.center) if q), default=1.0)
    n = min(8, max(1, math.ceil(math.dist(p, to) / (0.1 * max(size, 1.0)))))
    at, at_sk, best = cur, sk, None
    for k in range(1, n + 1):
        t = k / n
        sub = (p[0] + (to[0] - p[0]) * t, p[1] + (to[1] - p[1]) * t)
        nxt = _pull_once(at_sk, env, at, ref, sub)
        if nxt is None:
            break
        best = at = nxt
        at_sk = _with_guess(sk, nxt)
    return best


def _pull_once(sk: S.Sketch, env, cur: SolvedSketch, ref: str, to) -> SolvedSketch | None:
    s = _attempt(sk, env, constraints=[_fix(ref, to)])
    if s is not None:
        return s
    cands = [_attempt(sk, env, constraints=[S.Constraint(type=t, on=["origin", ref], value=float(v))])
             for t, v in (("distance_x", to[0]), ("distance_y", to[1]))]
    best = min((c for c in cands if c is not None), key=lambda c: math.dist(point_of(c, ref), to), default=None)
    # refine: from the current best position, find the direction the point can move in and pull it onto the
    # line through the cursor across that direction (exact for straight paths; a few rounds for curved ones)
    at, at_sk = cur, sk
    for _ in range(4):
        nxt = _pull_across(at_sk, env, at, ref, to)
        if nxt is None or (best is not None and math.dist(point_of(nxt, ref), to) >= math.dist(point_of(best, ref), to) - 1e-9):
            break
        best = at = nxt
        at_sk = _with_guess(sk, nxt)
    return best


def _pull_across(sk: S.Sketch, env, cur: SolvedSketch, ref: str, to) -> SolvedSketch | None:
    p = point_of(cur, ref)
    step = max(1e-3, 1e-3 * math.hypot(*p))
    for t, v in (("distance_x", p[0] + step), ("distance_y", p[1] + step), ("distance_x", p[0] - step)):
        nb = _attempt(sk, env, constraints=[S.Constraint(type=t, on=["origin", ref], value=float(v))])
        if nb is None:
            continue
        q = point_of(nb, ref)
        tx, ty = q[0] - p[0], q[1] - p[1]
        n = math.hypot(tx, ty)
        if n < 1e-12:
            continue
        L = 10.0 * (math.dist(p, to) + 1.0)
        nx, ny = -ty / n * L, tx / n * L
        line = S.Line(id=f"{_TMP}_l", p1=(to[0] - nx, to[1] - ny), p2=(to[0] + nx, to[1] + ny), construction=True)
        return _attempt(sk, env, entities=[line], constraints=[
            _fix(f"{_TMP}_l.p1", line.p1), _fix(f"{_TMP}_l.p2", line.p2), S.Constraint(type="point_on", on=[ref, line.id])])
    return None


def drag(sk: S.Sketch, env: dict[str, float], ref: str, to, grab=None,
         guess: list[dict] | None = None) -> tuple[SolvedSketch, bool]:
    """Preview dragging `ref` (a point reference, or a curve entity grabbed at `grab`) to `to`.

    `guess`: entity coordinates to start from (IR form, e.g. the previous preview of the same drag), so a
    drag follows the mouse step by step. Returns (solved sketch, moved); a fully constrained target comes
    back unchanged with moved=False."""
    if guess:
        upd = {g["id"]: {k: v for k, v in g.items() if k != "id"} for g in guess}
        sk = sk.model_copy(update={"entities": [e.model_copy(update=upd.get(e.id, {})) for e in sk.entities]})
    cur = solve_sketch(sk, env)
    if not cur.report.ok:
        return cur, False
    base = _with_guess(sk, cur)
    to = (float(to[0]), float(to[1]))
    out = None
    if point_of(cur, ref) is not None:
        out = _pull_point(base, env, cur, ref, to)
    elif ref in cur.entities:
        e = cur.entities[ref]
        grab = tuple(grab) if grab is not None else to
        if e.type == "line":  # translate the whole line if it's free to; else make it pass through the cursor
            d = (to[0] - grab[0], to[1] - grab[1])
            out = _attempt(base, env, constraints=[_fix(f"{ref}.p1", (e.p1[0] + d[0], e.p1[1] + d[1])),
                                                   _fix(f"{ref}.p2", (e.p2[0] + d[0], e.p2[1] + d[1]))])
            if out is None:
                t = S.Point(id=f"{_TMP}_t", at=grab)
                out = _attempt(base, env, entities=[t], constraints=[S.Constraint(type="point_on", on=[t.id, ref]),
                                                                    _fix(t.id, to)])
        elif e.type in ("circle", "arc"):  # dragging the rim resizes it, keeping the centre if possible
            t = S.Point(id=f"{_TMP}_t", at=to)
            on = [S.Constraint(type="point_on", on=[t.id, ref]), _fix(t.id, to)]
            out = (_attempt(base, env, entities=[t], constraints=on + [_fix(f"{ref}.center", e.center)])
                   or _attempt(base, env, entities=[t], constraints=on))
    if out is None:
        return cur, False
    moved = any(_moved(cur.entities[k], v) for k, v in out.entities.items() if k in cur.entities)
    return out, moved


def _moved(a: SolvedEntity, b: SolvedEntity, tol: float = 1e-9) -> bool:
    for f in ("p1", "p2", "center"):
        u, v = getattr(a, f), getattr(b, f)
        if u is not None and v is not None and math.dist(u, v) > tol:
            return True
    return a.r is not None and b.r is not None and abs(a.r - b.r) > tol


def freedom(sk: S.Sketch, env: dict[str, float], solved: SolvedSketch | None = None) -> dict[str, bool]:
    """Which entities are fully constrained (True) vs can still move (False).

    A point is fixed if nudging it in x and in y each conflicts with the sketch's constraints; a circle
    or arc also needs a fixed radius. Costs ~2 solves per point, so skipped (all fixed) at 0 DOF."""
    cur = solved or solve_sketch(sk, env)
    if not cur.report.ok:
        return {}
    if cur.report.dof == 0:
        return {eid: True for eid in cur.entities}
    base = _with_guess(sk, cur)
    memo: dict[str, bool] = {}

    def fixed_point(ref: str) -> bool:
        if ref not in memo:
            p = point_of(cur, ref)
            step = max(1e-3, 1e-3 * math.hypot(*p))
            memo[ref] = all(_attempt(base, env, constraints=[S.Constraint(type=t, on=["origin", ref], value=float(v))])
                            is None for t, v in (("distance_x", p[0] + step), ("distance_y", p[1] + step)))
        return memo[ref]

    out = {}
    for eid, e in cur.entities.items():
        if e.type == "point":
            out[eid] = fixed_point(eid)
        elif e.type == "line":
            out[eid] = fixed_point(f"{eid}.p1") and fixed_point(f"{eid}.p2")
        else:
            pts = [f"{eid}.center"] + ([f"{eid}.start", f"{eid}.end"] if e.type == "arc" else [])
            ok = all(fixed_point(r) for r in pts)
            if ok and e.type == "circle":
                ok = _attempt(base, env, constraints=[S.Constraint(type="diameter", on=[eid], value=2 * e.r * 1.001 + 1e-3)]) is None
            out[eid] = ok
    return out


# ── Regions ─────────────────────────────────────────────────────────
@dataclass
class Region:
    face: bd.Face          # in sketch-local coordinates (z = 0)
    outer_entities: set[str]


def _edge(e: SolvedEntity) -> bd.Edge:
    if e.type == "line":
        return bd.Edge.make_line((*e.p1, 0), (*e.p2, 0))
    if e.type == "circle":
        return bd.Pos(*e.center) * bd.Edge.make_circle(e.r)
    return bd.Pos(*e.center) * bd.Edge.make_circle(e.r, start_angle=e.start_angle, end_angle=e.end_angle)


def regions(solved: SolvedSketch) -> list[Region]:
    """Closed loops of non-construction geometry, nested even-odd: depth-0 loops are faces,
    depth-1 loops are their holes, depth-2 loops are islands, and so on."""
    geo = [e for e in solved.entities.values() if not e.construction and e.type != "point"]
    if not geo:
        return []
    for e in geo:
        if (e.type == "line" and math.dist(e.p1, e.p2) < 1e-7) or (e.type != "line" and e.r < 1e-7):
            size = "length" if e.type == "line" else "radius"
            raise SketchError(f"sketch {solved.id!r}: {e.type} {e.id!r} has zero {size}. Usually two dimensions "
                              "have become equal (e.g. a flange diameter set to the body diameter); change a "
                              "dimension, or remove the entity")
    edges = [_edge(e) for e in geo]
    wires = list(bd.Wire.combine(edges, tol=1e-4))
    open_w = [w for w in wires if not w.is_closed]
    if open_w:
        ents = sorted({solved.entity_at(_mid(ed)) or "?" for w in open_w for ed in w.edges()})
        raise SketchError(f"sketch {solved.id!r} has an open profile through entities {ents}; "
                          "close the loop or mark stray entities construction")
    loops = []
    for w in wires:
        bad = _self_intersections(w, solved)
        if bad:
            names = " and ".join(dict.fromkeys(bad[:2]))
            raise SketchError(f"sketch {solved.id!r}: the profile through {names} crosses itself near "
                              f"({bad[2][0]:.2f}, {bad[2][1]:.2f}); a loop must not self-intersect (check vertex order "
                              "and coordinates)")
        f = bd.Face(w)
        ents = {solved.entity_at(_mid(ed)) for ed in w.edges()} - {None}
        loops.append({"wire": w, "face": f, "area": abs(f.area), "ents": ents, "parents": []})
    loops.sort(key=lambda l: -l["area"])
    for i, a in enumerate(loops):
        probe = a["wire"].edges()[0].position_at(0.5)
        for b in loops[:i]:
            if b["face"].is_inside(probe):
                a["parents"].append(b)
    out = []
    for a in loops:
        depth = len(a["parents"])
        if depth % 2:
            continue
        holes = [b["wire"] for b in loops if len(b["parents"]) == depth + 1 and a in b["parents"]]
        face = bd.Face(a["wire"], holes) if holes else a["face"]
        out.append(Region(face=face, outer_entities=a["ents"]))
    return out


def _mid(edge: bd.Edge) -> tuple[float, float]:
    p = edge.position_at(0.5)
    return (p.X, p.Y)


def _tangent_dot(g: GcsSketch, a, b, pt, refs, names) -> float:
    """Sign of the dot product of the two curves' tangent directions at pt, from current geometry."""
    px, py = g.get_point(pt)

    def tdir(item, name):
        kind, cid = item
        if kind == "line":
            (x1, y1), (x2, y2) = g.get_point(refs[f"{name}.p1"][1]), g.get_point(refs[f"{name}.p2"][1])
            return (x2 - x1, y2 - y1)
        cx, cy = g.get_point(refs[f"{name}.center"][1])
        return (-(py - cy), px - cx)  # CCW tangent

    (ax, ay), (bx, by) = tdir(a, names[0]), tdir(b, names[1])
    return ax * bx + ay * by


def _self_intersections(w: bd.Wire, solved: SolvedSketch):
    """First pair of non-adjacent edges in a loop that touch or cross, as entity ids, else None."""
    edges = w.edges()
    n = len(edges)
    if n < 4:
        return None
    for i in range(n):
        for j in range(i + 2, n):
            if i == 0 and j == n - 1:
                continue  # neighbours around the loop
            if edges[i].distance_to(edges[j]) < 1e-6:
                a = solved.entity_at(_mid(edges[i])) or f"edge {i}"
                b = solved.entity_at(_mid(edges[j])) or f"edge {j}"
                p = edges[i].intersect(edges[j]) if hasattr(edges[i], "intersect") else None
                try:
                    v = p.vertices()[0] if p is not None and p.vertices() else edges[i].position_at(0.5)
                    pt = (v.X, v.Y)
                except Exception:
                    pt = _mid(edges[i])
                return a, b, pt
    return None
