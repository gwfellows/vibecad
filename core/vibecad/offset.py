"""Sketch offsets: a chain of solved lines and arcs copied at a distance, as fixed geometry.

The source is solved first (without the offsets), then each offset is computed from the solved shape: lines move
along their normal, arcs keep their centre and change radius, sharp corners between lines are mitred, and
tangent joins stay joined. The result is fixed with `fix` constraints (arcs as projected edges are), so it adds
no degrees of freedom and follows the source whenever the sketch is rebuilt.
"""
from __future__ import annotations

import math

from . import schema as S

TOL = 1e-6


class OffsetError(ValueError):
    pass


def _pt_arc(c, r, deg):
    a = math.radians(deg)
    return (c[0] + r * math.cos(a), c[1] + r * math.sin(a))


def _segments(solved, ids: list[str]) -> list[dict]:
    segs = []
    for eid in ids:
        e = solved.entities.get(eid)
        if e is None:
            raise OffsetError(f"no entity {eid!r} to offset")
        if e.type == "line":
            segs.append({"id": eid, "kind": "line", "a": tuple(e.p1), "b": tuple(e.p2)})
        elif e.type == "arc":
            segs.append({"id": eid, "kind": "arc", "c": tuple(e.center), "r": e.r, "rev": False,
                         "a": _pt_arc(e.center, e.r, e.start_angle), "b": _pt_arc(e.center, e.r, e.end_angle)})
        elif e.type == "circle":
            if len(ids) > 1:
                raise OffsetError(f"a circle ({eid!r}) can only be offset on its own")
            segs.append({"id": eid, "kind": "circle", "c": tuple(e.center), "r": e.r})
        else:
            raise OffsetError(f"can only offset lines, arcs and circles, not {e.type} {eid!r}")
    return segs


def _flip(s: dict) -> dict:
    s = dict(s, a=s["b"], b=s["a"])
    if s["kind"] == "arc":
        s["rev"] = not s["rev"]
    return s


def _chain(segs: list[dict], tol: float) -> tuple[list[dict], bool]:
    """Order the segments end to start, flipping as needed. Raises if they don't form one chain."""
    rest = segs[1:]
    chain = [segs[0]]
    # grow at the end, then at the start
    for grow_end in (True, False):
        while rest:
            tip = chain[-1]["b"] if grow_end else chain[0]["a"]
            for i, s in enumerate(rest):
                if math.dist(s["a"], tip) < tol:
                    nxt = s if grow_end else _flip(s)
                    break
                if math.dist(s["b"], tip) < tol:
                    nxt = _flip(s) if grow_end else s
                    break
            else:
                break
            rest.pop(i)
            chain.append(nxt) if grow_end else chain.insert(0, nxt)
    if rest:
        raise OffsetError(f"{', '.join(s['id'] for s in rest)} don't join the chain {' → '.join(s['id'] for s in chain)}; "
                          "an offset needs connected lines and arcs")
    return chain, len(chain) > 1 and math.dist(chain[-1]["b"], chain[0]["a"]) < tol


def _sample(s: dict, n: int = 16) -> list[tuple[float, float]]:
    if s["kind"] == "line":
        return [s["a"]]
    a0 = math.degrees(math.atan2(s["a"][1] - s["c"][1], s["a"][0] - s["c"][0]))
    a1 = math.degrees(math.atan2(s["b"][1] - s["c"][1], s["b"][0] - s["c"][0]))
    sweep = (a1 - a0) % 360 if not s["rev"] else -((a0 - a1) % 360)
    return [_pt_arc(s["c"], s["r"], a0 + sweep * k / n) for k in range(n)]


def _signed_area(chain) -> float:
    pts = [p for s in chain for p in _sample(s)]
    return 0.5 * sum(pts[i][0] * pts[(i + 1) % len(pts)][1] - pts[(i + 1) % len(pts)][0] * pts[i][1] for i in range(len(pts)))


def _intersect_lines(p, d, q, e):
    den = d[0] * e[1] - d[1] * e[0]
    if abs(den) < 1e-12:
        return None
    t = ((q[0] - p[0]) * e[1] - (q[1] - p[1]) * e[0]) / den
    return (p[0] + d[0] * t, p[1] + d[1] * t)


def _intersect_line_circle(p, d, c, r):
    fx, fy = p[0] - c[0], p[1] - c[1]
    a = d[0] ** 2 + d[1] ** 2
    b = 2 * (fx * d[0] + fy * d[1])
    cc = fx * fx + fy * fy - r * r
    disc = b * b - 4 * a * cc
    if disc < 0:
        return []
    sq = math.sqrt(disc)
    return [(p[0] + d[0] * t, p[1] + d[1] * t) for t in ((-b - sq) / (2 * a), (-b + sq) / (2 * a))]


def _intersect_circles(c1, r1, c2, r2):
    d = math.dist(c1, c2)
    if d < 1e-12 or d > r1 + r2 or d < abs(r1 - r2):
        return []
    a = (r1 * r1 - r2 * r2 + d * d) / (2 * d)
    h = math.sqrt(max(r1 * r1 - a * a, 0))
    mx, my = c1[0] + a * (c2[0] - c1[0]) / d, c1[1] + a * (c2[1] - c1[1]) / d
    return [(mx + h * (c2[1] - c1[1]) / d, my - h * (c2[0] - c1[0]) / d), (mx - h * (c2[1] - c1[1]) / d, my + h * (c2[0] - c1[0]) / d)]


def offset_chain(solved, ids: list[str], dist: float, side: str) -> list[dict]:
    """The offset pieces as IR-like dicts: {"type": "line", "p1", "p2"} / {"type": "arc", "center", "r",
    "start_angle", "end_angle"} / {"type": "circle", ...}, in chain order."""
    if dist <= 0:
        raise OffsetError(f"offset distance must be > 0, got {dist:g}")
    segs = _segments(solved, ids)
    if segs[0]["kind"] == "circle":
        s = segs[0]
        r = s["r"] + (dist if side in ("outside", "left") else -dist)  # left of a CCW circle is outward
        if r <= TOL:
            raise OffsetError(f"offsetting circle {s['id']!r} (radius {s['r']:g}) inward by {dist:g} leaves nothing")
        return [{"type": "circle", "center": list(s["c"]), "r": r}]
    size = max(math.dist(s["a"], s["b"]) for s in segs) or 1.0
    chain, closed = _chain(segs, 1e-6 * max(size, 1.0) + 1e-7)
    if side in ("outside", "inside"):
        if not closed:
            raise OffsetError("outside / inside need a closed loop; for an open chain use side left or right")
        ccw = _signed_area(chain) > 0
        right = (side == "outside") == ccw  # outward is to the right of a counterclockwise loop
    else:
        right = side == "right"
    k = 1.0 if right else -1.0
    out = []
    for s in chain:
        if s["kind"] == "line":
            d = (s["b"][0] - s["a"][0], s["b"][1] - s["a"][1])
            L = math.hypot(*d)
            n = (d[1] / L * k * dist, -d[0] / L * k * dist)  # right normal, scaled
            out.append({**s, "a": (s["a"][0] + n[0], s["a"][1] + n[1]), "b": (s["b"][0] + n[0], s["b"][1] + n[1]), "dir": (d[0] / L, d[1] / L)})
        else:
            # traversed counterclockwise, the centre is on the left: the right side is outward
            r = s["r"] + (k * dist if not s["rev"] else -k * dist)
            if r <= TOL:
                raise OffsetError(f"arc {s['id']!r} (radius {s['r']:g}) is smaller than the offset {dist:g} on that side")
            grow = r / s["r"]
            scale = lambda p, c=s["c"], g=grow: (c[0] + (p[0] - c[0]) * g, c[1] + (p[1] - c[1]) * g)
            out.append({**s, "r": r, "a": scale(s["a"]), "b": scale(s["b"])})
    # joins: tangent joins meet already; sharp ones are cut back or extended to where the offsets cross
    n = len(out)
    for i in range(n if closed else n - 1):
        s, t = out[i], out[(i + 1) % n]
        if math.dist(s["b"], t["a"]) < 1e-7 * max(size, 1.0):
            t["a"] = s["b"]
            continue
        guess = ((s["b"][0] + t["a"][0]) / 2, (s["b"][1] + t["a"][1]) / 2)
        if s["kind"] == "line" and t["kind"] == "line":
            p = _intersect_lines(s["a"], s["dir"], t["a"], t["dir"])
            cands = [p] if p else []
        elif s["kind"] == "line":
            cands = _intersect_line_circle(s["a"], s["dir"], t["c"], t["r"])
        elif t["kind"] == "line":
            cands = _intersect_line_circle(t["a"], t["dir"], s["c"], s["r"])
        else:
            cands = _intersect_circles(s["c"], s["r"], t["c"], t["r"])
        if not cands:
            raise OffsetError(f"the offsets of {s['id']!r} and {t['id']!r} don't meet (the offset is too big for that corner)")
        p = min(cands, key=lambda q: math.dist(q, guess))
        s["b"], t["a"] = p, p
    res = []
    for s in out:
        if s["kind"] == "line":
            d = (s["b"][0] - s["a"][0], s["b"][1] - s["a"][1])
            if math.hypot(*d) < 1e-7 * max(size, 1.0) or d[0] * s["dir"][0] + d[1] * s["dir"][1] <= 0:
                raise OffsetError(f"the offset swallows line {s['id']!r} (the offset is too big for it)")
            res.append({"type": "line", "p1": list(s["a"]), "p2": list(s["b"])})
        else:
            ang = lambda p, c=s["c"]: math.degrees(math.atan2(p[1] - c[1], p[0] - c[0])) % 360
            a, b = (s["a"], s["b"]) if not s["rev"] else (s["b"], s["a"])
            res.append({"type": "arc", "center": list(s["c"]), "r": s["r"], "start_angle": ang(a), "end_angle": ang(b)})
    return res


def resolve_offsets(src: S.Sketch, env: dict, solve) -> S.Sketch:
    """Replace `offset` entities by their fixed geometry. `solve(sketch)` solves a sketch; the source is solved
    without the offsets (and without constraints that use their pieces) to get the shape they copy."""
    from .expr import evaluate
    offs = [e for e in src.entities if isinstance(e, S.Offset)]
    if not offs:
        return src
    kids = {o.id for o in offs}
    uses_kid = lambda ref: ref.split(".")[0].rsplit("_", 1)[0] in kids
    base = src.model_copy(update={"entities": [e for e in src.entities if not isinstance(e, S.Offset)],
                                  "constraints": [c for c in src.constraints if not any(uses_kid(r) for r in c.on)]})
    solved = solve(base)
    ents, cons = [e for e in src.entities if not isinstance(e, S.Offset)], []
    fix = lambda ref, uv: cons.append(S.Constraint(type="fix", on=[ref], at=(round(uv[0], 9), round(uv[1], 9))))
    for o in offs:
        try:
            pieces = offset_chain(solved, o.of, float(evaluate(o.distance, env)), o.side)
        except OffsetError as e:
            raise OffsetError(f"offset {o.id!r}: {e}") from None
        for i, p in enumerate(pieces, start=1):
            pid = f"{o.id}_{i}"
            if p["type"] == "line":
                ents.append(S.Line(id=pid, p1=tuple(p["p1"]), p2=tuple(p["p2"]), construction=o.construction))
                fix(f"{pid}.p1", p["p1"])
                fix(f"{pid}.p2", p["p2"])
            elif p["type"] == "circle":
                ents.append(S.Circle(id=pid, center=tuple(p["center"]), r=p["r"], construction=o.construction))
                fix(f"{pid}.center", p["center"])
                cons.append(S.Constraint(type="radius", on=[pid], value=p["r"]))
            else:
                ents.append(S.Arc(id=pid, center=tuple(p["center"]), r=p["r"], start_angle=p["start_angle"],
                                  end_angle=p["end_angle"], construction=o.construction))
                c, r = p["center"], p["r"]
                st, en = _pt_arc(c, r, p["start_angle"]), _pt_arc(c, r, p["end_angle"])
                fix(f"{pid}.center", c)
                fix(f"{pid}.start", st)
                # the end lies on the circle already: pin only the coordinate that moves fastest along it
                kx = 0 if abs(en[1] - c[1]) >= abs(en[0] - c[0]) else 1
                cons.append(S.Constraint(type=("distance_x", "distance_y")[kx], on=["origin", f"{pid}.end"], value=en[kx]))
    return src.model_copy(update={"entities": ents, "constraints": list(src.constraints) + cons})
