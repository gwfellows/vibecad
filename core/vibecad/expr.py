"""Safe, unit-aware expression evaluation for parameters and dimensions.

Lengths are in mm and angles in degrees unless a unit suffix is given.
Examples: "6", "6 mm", "0.25 in", "plate_t * 2 + 1", "sqrt(2) * hole_d", "30 deg", "0.5 rad".
"""
from __future__ import annotations

import ast
import math
import re

UNIT_FACTORS = {
    "mm": 1.0, "cm": 10.0, "m": 1000.0, "in": 25.4, "inch": 25.4, "ft": 304.8,
    "deg": 1.0, "rad": 180.0 / math.pi,
}
FUNCS = {
    "sqrt": math.sqrt, "sin": lambda d: math.sin(math.radians(d)), "cos": lambda d: math.cos(math.radians(d)),
    "tan": lambda d: math.tan(math.radians(d)), "atan2": lambda y, x: math.degrees(math.atan2(y, x)),
    "min": min, "max": max, "abs": abs, "round": round, "floor": math.floor, "ceil": math.ceil,
}
CONSTS = {"pi": math.pi}

_UNIT_RE = re.compile(r"(?<![\w.])(\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?\s*(mm|cm|m|inch|in|ft|deg|rad)\b")
_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class ExprError(ValueError):
    pass


def _strip_units(src: str) -> str:
    return _UNIT_RE.sub(lambda m: f"({m.group(0)[: m.start(2) - m.start(0)].strip()}*{UNIT_FACTORS[m.group(2)]!r})", src)


def names_in(src: float | int | str) -> set[str]:
    """Parameter names referenced by an expression (units, functions and constants excluded)."""
    if not isinstance(src, str):
        return set()
    return {n for n in _NAME_RE.findall(_strip_units(src)) if n not in FUNCS and n not in CONSTS}


def evaluate(src: float | int | str, env: dict[str, float]) -> float:
    if isinstance(src, bool):
        raise ExprError(f"expected a number or expression, got {src!r}")
    if isinstance(src, (int, float)):
        return float(src)
    try:
        tree = ast.parse(_strip_units(src.strip()), mode="eval")
    except SyntaxError as e:
        raise ExprError(f"cannot parse expression {src!r}: {e.msg}") from None
    return float(_eval(tree.body, env, src))


def _eval(node: ast.AST, env: dict[str, float], src: str) -> float:
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
        return node.value
    if isinstance(node, ast.Name):
        if node.id in env:
            return env[node.id]
        if node.id in CONSTS:
            return CONSTS[node.id]
        raise ExprError(f"unknown name {node.id!r} in {src!r}")
    if isinstance(node, ast.BinOp):
        a, b = _eval(node.left, env, src), _eval(node.right, env, src)
        ops = {ast.Add: lambda: a + b, ast.Sub: lambda: a - b, ast.Mult: lambda: a * b,
               ast.Div: lambda: a / b, ast.Pow: lambda: a ** b, ast.Mod: lambda: a % b, ast.FloorDiv: lambda: a // b}
        if type(node.op) in ops:
            try:
                return ops[type(node.op)]()
            except ZeroDivisionError:
                raise ExprError(f"division by zero in {src!r}") from None
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        v = _eval(node.operand, env, src)
        return -v if isinstance(node.op, ast.USub) else v
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in FUNCS and not node.keywords:
        return FUNCS[node.func.id](*[_eval(a, env, src) for a in node.args])
    raise ExprError(f"unsupported syntax in {src!r}")


def evaluate_params(params: dict[str, float | int | str]) -> dict[str, float]:
    """Evaluate params in dependency order; params may reference each other."""
    out: dict[str, float] = {}
    pending = dict(params)
    while pending:
        progressed = False
        for name, src in list(pending.items()):
            deps = names_in(src)
            unknown = deps - set(params)
            if unknown:
                raise ExprError(f"param {name!r} references unknown name(s) {sorted(unknown)}")
            if deps <= set(out):
                out[name] = evaluate(src, out)
                del pending[name]
                progressed = True
        if not progressed:
            raise ExprError(f"circular parameter references among {sorted(pending)}")
    return out
