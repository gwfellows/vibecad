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
"""
from __future__ import annotations

import copy
from typing import Any

from pydantic import ValidationError

from . import schema as S
from .expr import ExprError, evaluate_params


class OpError(ValueError):
    pass


OP_KINDS = {
    "set_param", "remove_param", "set_meta", "add_feature", "update_feature", "remove_feature", "move_feature",
    "add_entity", "update_entity", "remove_entity", "add_constraint", "update_constraint", "remove_constraint",
    "set_dimension", "add_rectangle", "add_circle", "add_slot", "add_polygon", "add_regular_polygon",
}
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
    if k in ("update_feature", "remove_feature", "move_feature"):
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
        raw["params"][op["name"]] = op["value"]
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
