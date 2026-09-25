"""Regenerate a document into a solid, feature by feature, with a per-feature cache."""
from __future__ import annotations

import copy
import hashlib
import json
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path

import build123d as bd

from . import schema as S
from .expr import ExprError, evaluate_params, names_in
from .features import BUILDERS, Ctx, FeatureError
from .sketch import SketchError, SolvedSketch
from .frames import Frame
from .topo import Body, RefError

EXPECTED = (FeatureError, RefError, SketchError, ExprError, ValueError)


@dataclass
class FeatureResult:
    id: str
    type: str
    status: str  # ok | error | suppressed
    message: str | None = None
    info: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    seconds: float = 0.0
    cached: bool = False


@dataclass
class RegenResult:
    doc: S.Document
    env: dict[str, float]
    body: Body
    features: list[FeatureResult]
    sketches: dict[str, tuple[SolvedSketch, Frame]]
    seconds: float

    @property
    def ok(self) -> bool:
        return all(f.status != "error" for f in self.features)

    @property
    def part(self) -> bd.Shape | None:
        return bd.Shape.cast(self.body.shape) if self.body.shape is not None else None

    def summary(self) -> dict:
        out = {"name": self.doc.name, "ok": self.ok, "seconds": round(self.seconds, 3), "params": self.env}
        p = self.part
        if p is not None:
            bb = p.bounding_box()
            out["volume_mm3"] = round(p.volume, 3)
            out["bbox_mm"] = {"min": [round(v, 4) for v in (bb.min.X, bb.min.Y, bb.min.Z)],
                              "max": [round(v, 4) for v in (bb.max.X, bb.max.Y, bb.max.Z)],
                              "size": [round(v, 4) for v in (bb.size.X, bb.size.Y, bb.size.Z)]}
            out["faces"] = len(self.body.faces())
            out["valid"] = p.is_valid
        out["features"] = [
            {k: v for k, v in f.__dict__.items() if v not in (None, {}, [], False) or k == "status"} for f in self.features
        ]
        labs: dict[str, int] = {}
        for _, l in self.body.labels:
            labs[str(l)] = labs.get(str(l), 0) + 1
        out["face_labels"] = dict(sorted(labs.items()))
        return out


class Regenerator:
    def __init__(self) -> None:
        self._cache: dict[str, tuple[Body, dict, dict, FeatureResult]] = {}

    def run(self, doc: S.Document, overrides: dict[str, float | str] | None = None) -> RegenResult:
        t0 = time.perf_counter()
        params = {**doc.params, **(overrides or {})}
        try:
            env = evaluate_params(params)
        except ExprError as e:  # e.g. a hand-edited file: report it, so the part can still be opened and fixed
            msg = f"params do not evaluate: {e}"
            return RegenResult(doc, {}, Body(), [FeatureResult(f.id, f.type, "error", message=msg) for f in doc.features],
                               {}, time.perf_counter() - t0)
        ctx = Ctx(env=env)
        results: list[FeatureResult] = []
        debug_sketches: dict[str, tuple[SolvedSketch, Frame]] = {}
        key = hashlib.sha256(b"vibecad-0.1").hexdigest()
        for feat in doc.features:
            if feat.suppressed:
                results.append(FeatureResult(feat.id, feat.type, "suppressed"))
                continue
            dump = feat.model_dump(mode="json")
            used = sorted(set().union(*[names_in(s) for s in _strings(dump)]) & set(env))
            key = hashlib.sha256(
                (key + json.dumps(dump, sort_keys=True) + json.dumps({k: env[k] for k in used})).encode()
            ).hexdigest()
            if key in self._cache:
                body, sketches, tools, res = self._cache[key]
                ctx.body, ctx.sketches, ctx.tools = body, dict(sketches), dict(tools)
                r = copy.copy(res)
                r.cached = True
                results.append(r)
                debug_sketches.update(sketches)
                continue
            before = (ctx.body, dict(ctx.sketches), dict(ctx.tools))
            t1 = time.perf_counter()
            ctx.warnings = []
            try:
                info = BUILDERS[feat.type](ctx, feat) or {}
                res = FeatureResult(feat.id, feat.type, "ok", info=info, warnings=_collapse(ctx.warnings))
            except EXPECTED as ex:
                res = FeatureResult(feat.id, feat.type, "error", message=str(ex))
            except Exception as ex:  # kernel failures and bugs: report, keep going
                res = FeatureResult(feat.id, feat.type, "error",
                                    message=f"{type(ex).__name__}: {ex}",
                                    info={"trace": traceback.format_exc(limit=3)})
            res.seconds = round(time.perf_counter() - t1, 4)
            if feat.id in ctx.sketches:
                debug_sketches[feat.id] = ctx.sketches[feat.id]
            if res.status == "error":
                if isinstance(feat, S.Sketch) and feat.id in ctx.sketches:
                    res.info = {**res.info, "dof": ctx.sketches[feat.id][0].report.dof}
                ctx.body, ctx.sketches, ctx.tools = before
            else:
                self._cache[key] = (ctx.body, dict(ctx.sketches), dict(ctx.tools), res)
            results.append(res)
        return RegenResult(doc, env, ctx.body, results, debug_sketches, time.perf_counter() - t0)


def _collapse(warnings: list[str]) -> list[str]:
    """One line per distinct warning (a pattern repeats its tool's warning for every copy)."""
    counts: dict[str, int] = {}
    for w in warnings:
        counts[w] = counts.get(w, 0) + 1
    return [w if n == 1 else f"{w} (x{n})" for w, n in counts.items()]


def _strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _strings(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _strings(v)


def load(path: str | Path) -> S.Document:
    return S.Document.model_validate_json(Path(path).read_text())


def write_back_solved(path: str | Path, result: RegenResult) -> int:
    """Replace sketch entity guesses in the file with solved coordinates. Returns entities updated."""
    path = Path(path)
    raw = json.loads(path.read_text())
    n = 0
    for f in raw.get("features", []):
        if f.get("type") != "sketch" or f["id"] not in result.sketches:
            continue
        solved = {e["id"]: e for e in result.sketches[f["id"]][0].to_ir_entities()}
        for e in f.get("entities", []):
            if e["id"] in solved:
                e.update({k: v for k, v in solved[e["id"]].items() if k != "id"})
                n += 1
    path.write_text(json.dumps(raw, indent=2) + "\n")
    return n
