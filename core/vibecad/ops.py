"""Typed edit operations on the IR.

Every change to a part, from the UI, the CLI or an AI agent, is a list of these ops applied as one
transaction: all apply and the result validates against the schema, or nothing changes.

    {"op": "set_param", "name": "width", "value": "80 mm"}
    {"op": "remove_param", "name": "width"}
    {"op": "set_meta", "set": {"design_notes": "...", "material": "...", "process": "...", "name": "..."}}
    {"op": "add_feature", "feature": {...}, "after": "<feature id>" | "before": "<feature id>"}   (default: end)
    {"op": "update_feature", "id": "<feature id>", "set": {"distance": "8 mm", "direction": "reverse"}}
    {"op": "remove_feature", "id": "<feature id>"}
    {"op": "move_feature", "id": "<feature id>", "after": "<feature id>" | "before": "<feature id>"}
    {"op": "add_entity", "sketch": "<id>", "entity": {...}}
    {"op": "update_entity", "sketch": "<id>", "id": "<entity id>", "set": {...}}
    {"op": "remove_entity", "sketch": "<id>", "id": "<entity id>"}         also removes constraints that use it
    {"op": "add_constraint", "sketch": "<id>", "constraint": {...}}
    {"op": "update_constraint", "sketch": "<id>", "match": {"id"|"name"|"index": ...}, "set": {...}}
    {"op": "remove_constraint", "sketch": "<id>", "match": {"id"|"name"|"index": ...}}
    {"op": "set_dimension", "sketch": "<id>", "name": "<dimension name>", "value": "12 mm"}
    {"op": "rename_feature", "id": "<feature id>", "to": "<new id>"}          updates every reference to it
    {"op": "rename_entity", "sketch": "<id>", "id": "<entity id>", "to": "<new id>"}   same, for a sketch entity
    {"op": "fillet_corner", "sketch": "<id>", "corner": "<line>.p1|p2", "radius": "r", "id": "<arc id>"?}
          rounds the corner where two lines meet (joined by a coincident): the lines are trimmed to the tangent
          points and a tangent arc with a named radius dimension joins them
    {"op": "mirror_entities", "sketch": "<id>", "entities": ["<id>", ...], "axis": "<line id>|x_axis|y_axis"}
          mirrored copies (ids <id>_mirror) held to the originals by symmetric constraints (coincident where a
          point is on the axis), so they add no degrees of freedom and follow every change to the originals
"""
from __future__ import annotations

import copy
from typing import Any

from pydantic import ValidationError

import keyword
import re

from . import schema as S
from .expr import CONSTS, FUNCS, ExprError, evaluate_params

RESERVED_NAMES = set(FUNCS) | set(CONSTS)


class OpError(ValueError):
    pass


OP_KINDS = {
    "set_param", "remove_param", "set_meta", "add_feature", "update_feature", "remove_feature", "move_feature",
    "add_entity", "update_entity", "remove_entity", "add_constraint", "update_constraint", "remove_constraint",
    "set_dimension", "add_rectangle", "add_circle", "add_slot", "add_polygon", "add_regular_polygon",
    "rename_feature", "rename_entity", "fillet_corner", "mirror_entities",
}
ID_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
META_FIELDS = {"name", "design_notes", "material", "process"}


def touched_features(op: dict) -> set[str]:
    """Feature ids an op writes to (for scope checks). Params/meta return {"*params*"} / {"*meta*"}."""
    k = op.get("op")
    if k in ("set_param", "remove_param"):
        return {"*params*"}
    if k == "set_meta":
        return {"*meta*"}
    if k in ("add_feature",):
        return {op.get("feature", {}).get("id", "?")}
    if k in ("update_feature", "remove_feature", "move_feature", "rename_feature"):
        return {op.get("id", "?")}
    return {op.get("sketch", "?")}


def apply_ops(doc: S.Document, ops: list[dict[str, Any]]) -> tuple[S.Document, list[str]]:
    """Apply ops to a copy of doc. Returns (new_doc, notes). Raises OpError; the input is never modified."""
    if not isinstance(ops, list) or not ops:
        raise OpError("ops must be a non-empty list")
    raw = doc.model_dump(mode="json", exclude_none=True)
    notes: list[str] = []
    raw["_chain"] = {}  # anchor -> last feature inserted at it in this batch (keeps batch order)
    for i, op in enumerate(ops):
        if not isinstance(op, dict):
            raise OpError(f"op {i}: each op must be an object like {{\"op\": \"set_param\", ...}}, got {op!r:.80}")
        try:
            _apply_one(raw, op, notes)
        except OpError as e:
            raise OpError(f"op {i} ({op.get('op')}): {e}") from None
        except (KeyError, TypeError) as e:
            raise OpError(f"op {i} ({op.get('op')}): malformed op, missing or wrong field {e}") from None
    raw.pop("_chain", None)
    try:
        new = S.Document.model_validate(raw)
    except ValidationError as e:
        raise OpError("result does not validate:\n" + _short_validation(e)) from None
    try:  # a param that doesn't evaluate would break every later regeneration of the part
        evaluate_params(new.params)
    except ExprError as e:
        raise OpError(f"params do not evaluate: {e}") from None
    return new, notes


_SHORTCUT_OPS = {"add_rectangle", "add_circle", "add_slot", "add_polygon", "add_regular_polygon", "fillet_corner",
                 "mirror_entities"}
_OP_NAMES = {"add_entity", "add_constraint", "add_feature", "update_feature", "set_param"}


def _short_validation(e: ValidationError) -> str:
    lines, hints = [], set()
    for err in e.errors()[:8]:
        loc = ".".join(str(x) for x in err["loc"])
        lines.append(f"  {loc}: {err['msg']}")
        last = str(err["loc"][-1]) if err["loc"] else ""
        if last in ("p1", "p2", "center", "at") and isinstance(err.get("input"), str):
            hints.add("Sketch coordinates are [x, y] pairs (numbers or expressions), not point ids. Join lines with "
                      "`coincident` constraints on `line.p1` / `line.p2`, or use add_polygon / add_rectangle.")
        if err["type"] == "extra_forbidden":
            if last in _SHORTCUT_OPS or last in _OP_NAMES:
                hints.add(f"`{last}` is an op, not a field: send it as its own op in apply_ops, "
                          f'{{"op": "{last}", "sketch": "<sketch id>", ...}}, after the op that adds the sketch.')
            else:
                hints.add(f"Unknown field `{last}`; check the field names in the IR reference.")
    return "\n".join(lines + [f"hint: {h}" for h in sorted(hints)])


def _feat_index(raw, fid) -> int:
    for i, f in enumerate(raw["features"]):
        if f["id"] == fid:
            return i
    raise OpError(f"no feature {fid!r}; features: {[f['id'] for f in raw['features']]}")


def _sketch(raw, sid) -> dict:
    f = raw["features"][_feat_index(raw, sid)]
    if f["type"] != "sketch":
        raise OpError(f"{sid!r} is a {f['type']}, not a sketch")
    f.setdefault("entities", [])
    f.setdefault("constraints", [])
    return f


def _position(raw, op) -> int:
    if "after" in op and op["after"] is not None:
        return _feat_index(raw, op["after"]) + 1
    if "before" in op and op["before"] is not None:
        return _feat_index(raw, op["before"])
    return len(raw["features"])


def _match_constraint(sk, match) -> int:
    cons = sk["constraints"]
    if not isinstance(match, dict) or len(match) != 1:
        raise OpError('match must be one of {"id": ...}, {"name": ...}, {"index": n}')
    (k, v), = match.items()
    if k == "index":
        if not 0 <= int(v) < len(cons):
            raise OpError(f"constraint index {v} out of range (sketch has {len(cons)})")
        return int(v)
    hits = [i for i, c in enumerate(cons) if c.get(k) == v]
    if len(hits) != 1:
        raise OpError(f"constraint {k}={v!r} matched {len(hits)} constraints")
    return hits[0]


def _apply_one(raw: dict, op: dict, notes: list[str]) -> None:
    kind = op.get("op")
    if kind not in OP_KINDS:
        raise OpError(f"unknown op {kind!r}; valid: {sorted(OP_KINDS)}")
    raw.setdefault("params", {})
    raw.setdefault("features", [])

    if kind == "set_param":
        name, value = op["name"], op["value"]
        if not isinstance(name, str) or not name.isidentifier() or keyword.iskeyword(name):
            raise OpError(f"param name {name!r} must be an identifier (letters, digits, underscores; not starting "
                          "with a digit) so expressions can refer to it")
        if name in RESERVED_NAMES:
            raise OpError(f"param name {name!r} is reserved (a function or constant in expressions); pick another")
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            raise OpError(f"param value must be a number or an expression string like \"12 mm\", got {value!r}")
        raw["params"][name] = value
    elif kind == "remove_param":
        if op["name"] not in raw["params"]:
            raise OpError(f"no param {op['name']!r}")
        del raw["params"][op["name"]]
    elif kind == "set_meta":
        bad = set(op["set"]) - META_FIELDS
        if bad:
            raise OpError(f"set_meta can set {sorted(META_FIELDS)}, not {sorted(bad)}")
        raw.update(op["set"])
    elif kind == "add_feature":
        f = copy.deepcopy(op["feature"])
        if any(x["id"] == f.get("id") for x in raw["features"]):
            raise OpError(f"feature id {f.get('id')!r} already exists")
        pos_op = op
        anchor = ("after", op["after"]) if op.get("after") else ("before", op["before"]) if op.get("before") else None
        if anchor and anchor in raw["_chain"]:
            # several features added at the same spot in one batch stay in batch order (sketch before its extrude)
            pos_op = {"after": raw["_chain"][anchor]}
        raw["features"].insert(_position(raw, pos_op), f)
        if anchor:
            raw["_chain"][anchor] = f.get("id")
    elif kind == "update_feature":
        f = raw["features"][_feat_index(raw, op["id"])]
        for k, v in op["set"].items():
            if k in ("id", "type"):
                raise OpError(f"cannot change a feature's {k}; remove and re-add it")
            if v is None:
                f.pop(k, None)
            else:
                f[k] = copy.deepcopy(v)
    elif kind == "remove_feature":
        i = _feat_index(raw, op["id"])
        users = _users_of(raw, op["id"])
        if users:
            notes.append(f"removed {op['id']!r}; these features still reference it and will fail until updated: {users}")
        raw["features"].pop(i)
    elif kind == "move_feature":
        if op["id"] in (op.get("after"), op.get("before")):
            raise OpError(f"cannot move {op['id']!r} relative to itself")
        f = raw["features"].pop(_feat_index(raw, op["id"]))
        raw["features"].insert(_position(raw, op), f)
    elif kind == "add_entity":
        sk = _sketch(raw, op["sketch"])
        e = copy.deepcopy(op["entity"])
        if any(x["id"] == e.get("id") for x in sk["entities"]):
            raise OpError(f"entity id {e.get('id')!r} already exists in {op['sketch']!r}")
        sk["entities"].append(e)
    elif kind == "update_entity":
        sk = _sketch(raw, op["sketch"])
        ent = next((e for e in sk["entities"] if e["id"] == op["id"]), None)
        if ent is None:
            raise OpError(f"no entity {op['id']!r} in sketch {op['sketch']!r}")
        for k, v in op["set"].items():
            if k in ("id", "type"):
                raise OpError(f"cannot change an entity's {k}; remove and re-add it")
            ent[k] = v
    elif kind == "remove_entity":
        sk = _sketch(raw, op["sketch"])
        before = len(sk["entities"])
        sk["entities"] = [e for e in sk["entities"] if e["id"] != op["id"]]
        if len(sk["entities"]) == before:
            raise OpError(f"no entity {op['id']!r} in sketch {op['sketch']!r}")
        users = [e["id"] for e in sk["entities"] if e.get("type") == "offset" and op["id"] in e.get("of", [])]
        if users:
            raise OpError(f"{op['id']!r} is copied by offset {', '.join(users)}; remove or change the offset first")
        pre = op["id"] + "."
        dropped = [c for c in sk["constraints"] if any(r == op["id"] or r.startswith(pre) for r in c["on"])]
        sk["constraints"] = [c for c in sk["constraints"] if c not in dropped]
        if dropped:
            notes.append(f"removing {op['id']!r} also removed {len(dropped)} constraint(s) that used it: "
                         + "; ".join(f"{c['type']}({', '.join(c['on'])})" for c in dropped))
    elif kind == "add_constraint":
        _sketch(raw, op["sketch"])["constraints"].append(copy.deepcopy(op["constraint"]))
    elif kind == "update_constraint":
        sk = _sketch(raw, op["sketch"])
        c = sk["constraints"][_match_constraint(sk, op["match"])]
        for k, v in op["set"].items():
            if v is None:
                c.pop(k, None)
            else:
                c[k] = v
    elif kind == "remove_constraint":
        sk = _sketch(raw, op["sketch"])
        sk["constraints"].pop(_match_constraint(sk, op["match"]))
    elif kind in ("add_rectangle", "add_circle", "add_slot", "add_polygon", "add_regular_polygon"):
        from .macros import expand

        sk = _sketch(raw, op["sketch"])
        try:
            ents, cons = expand(op, raw)
        except ValueError as e:
            raise OpError(str(e)) from None
        taken = {e["id"] for e in sk["entities"]} & {e["id"] for e in ents}
        if taken:
            raise OpError(f"entity ids already exist in {op['sketch']!r}: {sorted(taken)}; pick another id")
        sk["entities"] += ents
        sk["constraints"] += cons
        notes.append(f"{kind} {op['id']!r}: added {', '.join(e['id'] for e in ents)}")
    elif kind == "rename_feature":
        n = _rename_feature(raw, op["id"], op["to"])
        notes.append(f"renamed feature {op['id']!r} to {op['to']!r}; updated {n} reference(s)")
    elif kind == "rename_entity":
        n = _rename_entity(raw, op["sketch"], op["id"], op["to"])
        notes.append(f"renamed {op['sketch']}.{op['id']} to {op['to']!r}; updated {n} reference(s)")
    elif kind == "fillet_corner":
        notes.append(_fillet_corner(raw, op))
    elif kind == "mirror_entities":
        notes.append(_mirror_entities(raw, op))
    elif kind == "set_dimension":
        sk = _sketch(raw, op["sketch"])
        c = sk["constraints"][_match_constraint(sk, {"name": op["name"]})]
        cur = c.get("value")
        if isinstance(cur, str) and cur.strip() in raw["params"]:
            # driven by a param: change the param so every other use stays consistent
            raw["params"][cur.strip()] = op["value"]
            notes.append(f"dimension {op['name']!r} is driven by param {cur.strip()!r}; set that param to {op['value']!r}")
        else:
            c["value"] = op["value"]


def _mirror_entities(raw: dict, op: dict) -> str:
    import math

    from .expr import evaluate
    sk = _sketch(raw, op["sketch"])
    env = evaluate_params(raw.get("params", {}))
    ents = {e["id"]: e for e in sk["entities"]}
    xy = lambda p: (evaluate(p[0], env), evaluate(p[1], env))
    axis = op.get("axis")
    if axis in ("x_axis", "y_axis"):
        o, d = (0.0, 0.0), ((1.0, 0.0) if axis == "x_axis" else (0.0, 1.0))
    elif ents.get(axis, {}).get("type") == "line":
        a, b = xy(ents[axis]["p1"]), xy(ents[axis]["p2"])
        L = math.dist(a, b)
        if L < 1e-9:
            raise OpError(f"mirror axis {axis!r} has no length")
        o, d = a, ((b[0] - a[0]) / L, (b[1] - a[1]) / L)
    else:
        raise OpError(f"mirror axis must be a line in sketch {op['sketch']!r}, x_axis or y_axis; got {axis!r}")
    todo = op.get("entities") or []
    if not todo:
        raise OpError("mirror_entities needs entities to mirror")

    def refl(p):
        v = (p[0] - o[0], p[1] - o[1])
        t = v[0] * d[0] + v[1] * d[1]
        f = (o[0] + t * d[0], o[1] + t * d[1])
        return [round(2 * f[0] - p[0], 9), round(2 * f[1] - p[1], 9)]

    def on_axis(p):
        return abs((p[0] - o[0]) * d[1] - (p[1] - o[1]) * d[0]) < 1e-6

    def hold(old_ref, new_ref, p):  # the copy's point mirrors the original's; on the axis they are one point
        if on_axis(p):
            return {"type": "coincident", "on": [new_ref, old_ref]}
        return {"type": "symmetric", "on": [old_ref, new_ref, axis]}

    theta = math.degrees(math.atan2(d[1], d[0]))
    taken = set(ents)
    made, cons = [], []
    for eid in todo:
        e = ents.get(eid)
        if e is None:
            raise OpError(f"no entity {eid!r} in sketch {op['sketch']!r}")
        if eid == axis:
            raise OpError(f"{eid!r} is the mirror axis; it can't be mirrored across itself")
        if e["type"] not in ("line", "arc", "circle", "point"):
            raise OpError(f"can't mirror {e['type']} {eid!r}; mirror lines, arcs, circles and points")
        nid = f"{eid}_mirror"
        k = 2
        while nid in taken:
            nid, k = f"{eid}_mirror{k}", k + 1
        taken.add(nid)
        n = {"id": nid, "type": e["type"]}
        if e.get("construction"):
            n["construction"] = True
        if e["type"] == "line":
            p1, p2 = xy(e["p1"]), xy(e["p2"])
            n.update(p1=refl(p1), p2=refl(p2))
            cons += [hold(f"{eid}.p1", f"{nid}.p1", p1), hold(f"{eid}.p2", f"{nid}.p2", p2)]
        elif e["type"] == "point":
            p = xy(e["at"])
            n["at"] = refl(p)
            cons.append(hold(eid, nid, p))
        elif e["type"] == "circle":
            c = xy(e["center"])
            n.update(center=refl(c), r=evaluate(e["r"], env))
            cons += [hold(f"{eid}.center", f"{nid}.center", c), {"type": "equal", "on": [eid, nid]}]
        else:  # arc: mirroring reverses its direction, so the copy runs from the image of the end to that of the start
            c, r = xy(e["center"]), evaluate(e["r"], env)
            a0, a1 = evaluate(e["start_angle"], env), evaluate(e["end_angle"], env)
            n.update(center=refl(c), r=r, start_angle=round((2 * theta - a1) % 360, 9), end_angle=round((2 * theta - a0) % 360, 9))
            end = (c[0] + r * math.cos(math.radians(a1)), c[1] + r * math.sin(math.radians(a1)))
            start = (c[0] + r * math.cos(math.radians(a0)), c[1] + r * math.sin(math.radians(a0)))
            cons += [hold(f"{eid}.end", f"{nid}.start", end), hold(f"{eid}.start", f"{nid}.end", start),
                     {"type": "equal", "on": [eid, nid]}]
        made.append(n)
    sk["entities"].extend(made)
    sk["constraints"].extend(cons)
    return f"mirrored {', '.join(todo)} across {axis}: added {', '.join(m['id'] for m in made)}"


def _fillet_corner(raw: dict, op: dict) -> str:
    import math

    from .expr import evaluate
    sk = _sketch(raw, op["sketch"])
    env = evaluate_params(raw.get("params", {}))
    ents = {e["id"]: e for e in sk["entities"]}
    num = lambda v: evaluate(v, env)
    xy = lambda p: (num(p[0]), num(p[1]))
    corner = op["corner"]
    eid, _, end = corner.partition(".")
    if ents.get(eid, {}).get("type") != "line" or end not in ("p1", "p2"):
        raise OpError(f"corner {corner!r} must be a line endpoint like 'side.p2'")
    partner, ci = None, None
    for i, c in enumerate(sk["constraints"]):
        if c["type"] == "coincident" and corner in c["on"] and len(c["on"]) == 2:
            other = c["on"][1] if c["on"][0] == corner else c["on"][0]
            oid, _, oend = other.partition(".")
            if ents.get(oid, {}).get("type") == "line" and oend in ("p1", "p2") and oid != eid:
                partner, ci = other, i
                break
    if partner is None:
        raise OpError(f"{corner} is not joined to another line's end by a coincident constraint; fillet_corner "
                      "rounds the corner where two lines meet")
    oid, _, oend = partner.partition(".")
    l1, l2 = ents[eid], ents[oid]
    p = xy(l1[end])
    a = xy(l1["p2" if end == "p1" else "p1"])
    b = xy(l2["p2" if oend == "p1" else "p1"])
    r = num(op["radius"])
    if r <= 0:
        raise OpError(f"fillet radius must be > 0, got {r:g}")
    la, lb = math.dist(a, p), math.dist(b, p)
    if la < 1e-9 or lb < 1e-9:
        raise OpError("a line at the corner has no length")
    u = ((a[0] - p[0]) / la, (a[1] - p[1]) / la)
    v = ((b[0] - p[0]) / lb, (b[1] - p[1]) / lb)
    theta = math.acos(max(-1.0, min(1.0, u[0] * v[0] + u[1] * v[1])))
    if theta < 1e-6 or math.pi - theta < 1e-6:
        raise OpError("the two lines are in line at that corner: nothing to round")
    t = r / math.tan(theta / 2)
    if t >= la - 1e-9 or t >= lb - 1e-9:
        raise OpError(f"radius {r:g} is too big for these lines: it needs {t:.3g} of each, and they are "
                      f"{la:.3g} and {lb:.3g} long")
    t1 = (p[0] + u[0] * t, p[1] + u[1] * t)
    t2 = (p[0] + v[0] * t, p[1] + v[1] * t)
    bis = (u[0] + v[0], u[1] + v[1])
    bl = math.hypot(*bis)
    d = r / math.sin(theta / 2)
    ctr = (p[0] + bis[0] / bl * d, p[1] + bis[1] / bl * d)
    ang = lambda q: math.degrees(math.atan2(q[1] - ctr[1], q[0] - ctr[0])) % 360
    s1, s2 = ang(t1), ang(t2)
    first, second = (corner, partner) if (s2 - s1) % 360 <= 180 else (partner, corner)  # arcs run counterclockwise
    if first != corner:
        s1, s2 = s2, s1
    aid = op.get("id") or f"{eid}_{oid}_round"
    _check_new_id(aid, set(ents), "entity")
    r6 = lambda q: [round(q[0], 6), round(q[1], 6)]
    # dimensions to the corner, and the lines' lengths, keep measuring to the sharp corner (as CAD does): a
    # construction point held at the lines' intersection stands in for it
    vc = f"{aid}_corner"
    _check_new_id(vc, set(ents) | {aid}, "entity")
    far1, far2 = f"{eid}.{'p2' if end == 'p1' else 'p1'}", f"{oid}.{'p2' if oend == 'p1' else 'p1'}"
    del sk["constraints"][ci]
    moved = []
    for c in sk["constraints"]:
        on = c["on"]
        if len(on) == 1 and on[0] in (eid, oid) and c["type"] in ("distance", "distance_x", "distance_y"):
            corner_end, far = (end, far1) if on[0] == eid else (oend, far2)
            c["on"] = [far, vc] if corner_end == "p2" else [vc, far]  # p2 - p1 keeps its sign
            moved.append(c)
        elif corner in on or partner in on:
            c["on"] = [vc if r in (corner, partner) else r for r in on]
            moved.append(c)
    l1[end], l2[oend] = r6(t1), r6(t2)
    sk["entities"].append({"id": vc, "type": "point", "at": r6(p), "construction": True})
    sk["constraints"] += [{"type": "point_on", "on": [vc, eid]}, {"type": "point_on", "on": [vc, oid]}]
    sk["entities"].append({"id": aid, "type": "arc", "center": r6(ctr), "r": round(r, 6),
                           "start_angle": round(s1, 6), "end_angle": round(s2, 6)})
    sk["constraints"] += [
        {"type": "coincident", "on": [first, f"{aid}.start"]},
        {"type": "coincident", "on": [second, f"{aid}.end"]},
        {"type": "tangent", "on": [first.split(".")[0], aid, first]},
        {"type": "tangent", "on": [second.split(".")[0], aid, second]},
        {"type": "radius", "on": [aid], "value": op["radius"], "name": op.get("name") or f"{aid}_r"},
    ]
    note = f"rounded {corner} / {partner} with arc {aid!r} (radius {op['radius']})"
    if moved:
        note += (f"; {len(moved)} constraint(s) that used the corner now use {vc!r}, the sharp corner the lines "
                 "still meet at: " + ", ".join(c.get("name") or c["type"] for c in moved))
    return note


def _check_new_id(to, taken, what: str) -> None:
    if not isinstance(to, str) or not ID_RE.match(to):
        raise OpError(f"new {what} id {to!r} must be letters, digits and underscores, not starting with a digit")
    if to in ("origin", "x_axis", "y_axis"):
        raise OpError(f"{to!r} is reserved")
    if to in taken:
        raise OpError(f"{what} id {to!r} already exists")


def _face_refs(obj):
    """Every FaceRef-shaped dict (has "feature" and "role") inside a feature."""
    if isinstance(obj, dict):
        if "feature" in obj and "role" in obj:
            yield obj
        for v in obj.values():
            yield from _face_refs(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _face_refs(v)


def _rename_feature(raw: dict, old: str, new: str) -> int:
    f = raw["features"][_feat_index(raw, old)]
    _check_new_id(new, {x["id"] for x in raw["features"]}, "feature")
    f["id"] = new
    n = 0
    for g in raw["features"]:
        if g.get("profile", {}).get("sketch") == old:
            g["profile"]["sketch"] = new
            n += 1
        for key in ("sketch", "path", "tool"):  # hole / text points, sweep path, boolean tool body
            if g is not f and g.get(key) == old and g.get("type") != "sketch":
                g[key] = new
                n += 1
        if old in g.get("sections", []):  # loft
            g["sections"] = [new if x == old else x for x in g["sections"]]
            n += 1
        if old in g.get("features", []) and g["type"] in ("linear_pattern", "circular_pattern", "mirror"):
            g["features"] = [new if x == old else x for x in g["features"]]
            n += 1
        for ref in _face_refs(g):
            if ref["feature"] == old:
                ref["feature"] = new
                n += 1
            inst = ref.get("instance")
            if isinstance(inst, str) and inst.startswith(old + "#"):
                ref["instance"] = new + inst[len(old):]
                n += 1
    return n


def _rename_entity(raw: dict, sid: str, old: str, new: str) -> int:
    sk = _sketch(raw, sid)
    ent = next((e for e in sk["entities"] if e["id"] == old), None)
    if ent is None:
        raise OpError(f"no entity {old!r} in sketch {sid!r}")
    _check_new_id(new, {e["id"] for e in sk["entities"]}, "entity")
    ent["id"] = new
    n = 0
    for c in sk["constraints"]:
        refs = [new + r[len(old):] if r == old or r.startswith(old + ".") else r for r in c["on"]]
        n += refs != c["on"]
        c["on"] = refs
    for e in sk["entities"]:  # offsets name the chain they copy
        if e.get("type") == "offset" and old in e.get("of", []):
            e["of"] = [new if x == old else x for x in e["of"]]
            n += 1
    makers = set()  # extrudes/revolves built from this sketch: their side faces are labelled with its entities
    for g in raw["features"]:
        if g.get("sections", [None])[0] == sid:  # a loft's sides are labelled with its first section's entities
            makers.add(g["id"])
        prof = g.get("profile", {})
        if prof.get("sketch") != sid:
            continue
        makers.add(g["id"])
        if isinstance(prof.get("regions"), list) and old in prof["regions"]:
            prof["regions"] = [new if x == old else x for x in prof["regions"]]
            n += 1
        if g.get("axis") == old:
            g["axis"] = new
            n += 1
    for g in raw["features"]:
        for ref in _face_refs(g):
            if ref["feature"] in makers and ref.get("entity") == old:
                ref["entity"] = new
                n += 1
    return n


def _users_of(raw, fid) -> list[str]:
    """Features whose JSON mentions fid as a sketch, feature, or face-ref target."""
    out = []
    for f in raw["features"]:
        if f["id"] == fid:
            continue
        if _mentions(f, fid):
            out.append(f["id"])
    return out


def _mentions(obj, fid) -> bool:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in ("sketch", "feature") and v == fid:
                return True
            if k == "features" and isinstance(v, list) and fid in v:
                return True
            if _mentions(v, fid):
                return True
    elif isinstance(obj, list):
        return any(_mentions(v, fid) for v in obj)
    return False
