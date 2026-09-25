"""Sketch shortcut ops: expand a common shape into fully constrained primitive geometry.

The stored IR stays primitive (lines, arcs, circles + constraints), so editing, the GUI and the solver
see ordinary entities. The shortcuts exist to save the agent from hand-writing a dozen constraints.

  {"op": "add_rectangle", "sketch": S, "id": "plate", "width": "base_w", "height": "depth",
   "center": [0, 0]            # or "corner": [x, y] (bottom-left); omit both to leave it free (2 DOF)
  }
  -> lines plate_bottom, plate_right, plate_top, plate_left; dimensions named plate_width, plate_height

  {"op": "add_circle", "sketch": S, "id": "bore", "diameter": "bore_d", "center": [0, "shaft_h"]}
  -> circle bore; dimension bore_d named bore_diameter

  {"op": "add_slot", "sketch": S, "id": "slot", "length": "slot_len", "width": "slot_w",
   "center": [x, y], "angle": 90}  # length = center-to-center; angle of the slot axis from sketch +x
  -> arcs slot_end1, slot_end2; lines slot_side1, slot_side2; construction slot_axis, point slot_mid

  {"op": "add_polygon", "sketch": S, "id": "l", "points": [[0, 0], ["leg", 0], ["leg", "t"], ["t", "t"], ["t", "leg"], [0, "leg"]],
   "names": ["bottom", "outer_right", "inner_h", "inner_v", "top", "outer_left"]}   # names optional
  -> one line per edge (closed by default); every vertex is fixed at its given coordinates, so the
     profile is fully constrained and resizes when the params in its coordinates change

Positions are measured from the sketch origin and accept expressions.
"""
from __future__ import annotations

import math

from .expr import ExprError, evaluate, evaluate_params


def _num(v, env, default):
    try:
        return evaluate(v, env)
    except (ExprError, TypeError):
        return default


def _sub(a, b):
    return f"({a}) - ({b})"


def expand(op: dict, raw: dict) -> tuple[list[dict], list[dict]]:
    """Returns (entities, constraints) to add to the sketch."""
    try:
        env = evaluate_params(raw.get("params", {}))
    except ExprError:
        env = {}
    kind, pid = op["op"], op["id"]
    ents, cons = [], []

    def at(ref, xy):
        x, y = xy
        cons.append({"type": "distance_x", "on": ["origin", ref], "value": x})
        cons.append({"type": "distance_y", "on": ["origin", ref], "value": y})

    if kind == "add_rectangle":
        W, H = op["width"], op["height"]
        w, h = _num(W, env, 10.0), _num(H, env, 10.0)
        if "center" in op:
            cx, cy = (_num(v, env, 0.0) for v in op["center"])
            x0, y0 = cx - w / 2, cy - h / 2
        elif "corner" in op:
            x0, y0 = (_num(v, env, 0.0) for v in op["corner"])
        else:
            x0, y0 = 0.0, 0.0
        c = [(x0, y0), (x0 + w, y0), (x0 + w, y0 + h), (x0, y0 + h)]
        names = [f"{pid}_bottom", f"{pid}_right", f"{pid}_top", f"{pid}_left"]
        for i, n in enumerate(names):
            ents.append({"id": n, "type": "line", "p1": list(c[i]), "p2": list(c[(i + 1) % 4]),
                         "construction": bool(op.get("construction", False))})
        for i in range(4):
            cons.append({"type": "coincident", "on": [f"{names[i]}.p2", f"{names[(i + 1) % 4]}.p1"]})
        cons += [{"type": "horizontal", "on": [names[0]]}, {"type": "horizontal", "on": [names[2]]},
                 {"type": "vertical", "on": [names[1]]}, {"type": "vertical", "on": [names[3]]},
                 {"type": "distance", "on": [names[0]], "value": W, "name": f"{pid}_width"},
                 {"type": "distance", "on": [names[1]], "value": H, "name": f"{pid}_height"}]
        if "center" in op:
            cx, cy = op["center"]
            at(f"{names[0]}.p1", [_sub(cx, f"({W}) / 2"), _sub(cy, f"({H}) / 2")])
        elif "corner" in op:
            at(f"{names[0]}.p1", op["corner"])
    elif kind == "add_circle":
        D = op["diameter"]
        cx, cy = (_num(v, env, 0.0) for v in op.get("center", [0, 0]))
        ents.append({"id": pid, "type": "circle", "center": [cx, cy], "r": _num(D, env, 10.0) / 2,
                     "construction": bool(op.get("construction", False))})
        cons.append({"type": "diameter", "on": [pid], "value": D, "name": f"{pid}_diameter"})
        if "center" in op:
            if all(_num(v, env, 1.0) == 0 and not isinstance(v, str) for v in op["center"]):
                cons.append({"type": "coincident", "on": [f"{pid}.center", "origin"]})
            else:
                at(f"{pid}.center", op["center"])
    elif kind == "add_slot":
        L, Wd = op["length"], op["width"]
        l, r = _num(L, env, 10.0), _num(Wd, env, 4.0) / 2
        ang = _num(op.get("angle", 0), env, 0.0)
        cx, cy = (_num(v, env, 0.0) for v in op.get("center", [0, 0]))
        ux, uy = math.cos(math.radians(ang)), math.sin(math.radians(ang))
        c1 = (cx - ux * l / 2, cy - uy * l / 2)
        c2 = (cx + ux * l / 2, cy + uy * l / 2)
        nx, ny = -uy, ux
        e1, e2 = f"{pid}_end1", f"{pid}_end2"
        s1, s2, ax, mid = f"{pid}_side1", f"{pid}_side2", f"{pid}_axis", f"{pid}_mid"
        ents += [
            {"id": e1, "type": "arc", "center": list(c1), "r": r, "start_angle": ang + 90, "end_angle": ang + 270},
            {"id": e2, "type": "arc", "center": list(c2), "r": r, "start_angle": ang - 90, "end_angle": ang + 90},
            {"id": s1, "type": "line", "p1": [c2[0] + nx * r, c2[1] + ny * r], "p2": [c1[0] + nx * r, c1[1] + ny * r]},
            {"id": s2, "type": "line", "p1": [c1[0] - nx * r, c1[1] - ny * r], "p2": [c2[0] - nx * r, c2[1] - ny * r]},
            {"id": ax, "type": "line", "p1": list(c1), "p2": list(c2), "construction": True},
            {"id": mid, "type": "point", "at": [cx, cy]},
        ]
        cons += [
            {"type": "coincident", "on": [f"{e2}.end", f"{s1}.p1"]},
            {"type": "coincident", "on": [f"{s1}.p2", f"{e1}.start"]},
            {"type": "coincident", "on": [f"{e1}.end", f"{s2}.p1"]},
            {"type": "coincident", "on": [f"{s2}.p2", f"{e2}.start"]},
            {"type": "tangent", "on": [e2, s1, f"{s1}.p1"]},
            {"type": "tangent", "on": [s1, e1, f"{s1}.p2"]},
            {"type": "tangent", "on": [e1, s2, f"{s2}.p1"]},
            {"type": "tangent", "on": [s2, e2, f"{s2}.p2"]},
            {"type": "coincident", "on": [f"{ax}.p1", f"{e1}.center"]},
            {"type": "coincident", "on": [f"{ax}.p2", f"{e2}.center"]},
            {"type": "equal", "on": [e1, e2]},
            {"type": "diameter", "on": [e1], "value": Wd, "name": f"{pid}_width"},
            {"type": "distance", "on": [ax], "value": L, "name": f"{pid}_length"},
            {"type": "angle", "on": [ax], "value": op.get("angle", 0)},
            {"type": "midpoint", "on": [mid, ax]},
        ]
        if "center" in op:
            at(mid, op["center"])
    elif kind == "add_polygon":
        pts = op["points"]
        closed = op.get("closed", True)
        n_edges = len(pts) if closed else len(pts) - 1
        if len(pts) < (3 if closed else 2):
            raise ValueError("add_polygon needs at least 3 points (2 for an open polyline)")
        names = op.get("names") or [f"{pid}_{i + 1}" for i in range(n_edges)]
        if len(names) != n_edges:
            raise ValueError(f"add_polygon: {n_edges} edges but {len(names)} names")
        guess = [(_num(x, env, 0.0), _num(y, env, 0.0)) for x, y in pts]
        for i in range(n_edges):
            a, b = guess[i], guess[(i + 1) % len(pts)]
            ents.append({"id": names[i], "type": "line", "p1": list(a), "p2": list(b),
                         "construction": bool(op.get("construction", False))})
        for i in range(n_edges - 1 + (1 if closed else 0)):
            cons.append({"type": "coincident", "on": [f"{names[i]}.p2", f"{names[(i + 1) % n_edges]}.p1"]})
        for i, (x, y) in enumerate(pts):  # fix every vertex: fully constrained, no redundancy
            ref = f"{names[i]}.p1" if i < n_edges else f"{names[-1]}.p2"
            at(ref, [x, y])
    else:
        raise KeyError(kind)
    return ents, cons


MACROS = {"add_rectangle", "add_circle", "add_slot", "add_polygon"}
