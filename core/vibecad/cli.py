"""vibecad command line.

  vibecad build part.vcad.json [--out DIR] [--set name=value ...] [--show] [--no-render] [--write-solved]
  vibecad watch part.vcad.json          rebuild + push to ocp_viewer whenever the file changes
  vibecad tree  part.vcad.json          compact feature tree with status (what an agent reads first)
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import build123d as bd
from pydantic import ValidationError

from .regen import Regenerator, RegenResult, load, write_back_solved
from .render import render_sketch, render_view

DEFAULT_VIEWS = ("iso", "iso_back", "iso_below", "front", "top")


def tree_text(res: RegenResult) -> str:
    doc = res.doc
    lines = [f"part {doc.name}" + (f"  ({doc.material}, {doc.process})" if doc.material else "")]
    if doc.design_notes:
        lines.append(f"notes: {doc.design_notes}")
    if res.env:
        lines.append("params: " + ", ".join(f"{k}={_fmt(v)}" for k, v in res.env.items()))
    for f, r in zip(doc.features, res.features):
        tag = {"ok": "ok ", "error": "ERR", "suppressed": "-- "}[r.status]
        if r.status == "ok" and r.warnings:
            tag = "WRN"
        extra = ""
        if f.type == "sketch":
            plane = f.plane.datum + (f"+{f.plane.offset}" if getattr(f.plane, "offset", 0) not in (0, 0.0, "0") else "") \
                if hasattr(f.plane, "datum") else f"face {f.plane.face.feature}.{f.plane.face.role}"
            dims = [c.name for c in f.constraints if c.name]
            extra = f"on {plane}; {len(f.entities)} entities; dof {r.info.get('dof', '?')}" + \
                (f"; dims {', '.join(dims)}" if dims else "")
        elif f.type in ("extrude", "revolve"):
            extra = f"{f.mode} {f.profile.sketch}" + (
                f" {f.extent if f.extent != 'blind' else f.distance} {f.direction}" if f.type == "extrude" else f" axis {f.axis} {f.angle} deg")
        elif f.type in ("fillet", "chamfer"):
            extra = f"{getattr(f, 'radius', None) or getattr(f, 'distance', None)} on {r.info.get('edges', '?')} edges"
        elif f.type in ("linear_pattern", "circular_pattern", "mirror"):
            extra = f"of {', '.join(f.features)}"
        elif f.type == "shell":
            extra = f"t={f.thickness}"
        line = f" {tag} {f.id:<18} {f.type:<16} {extra}"
        if f.intent:
            line += f"  | {f.intent}"
        lines.append(line)
        if r.status == "error":
            lines.append(f"      error: {r.message}")
        for w in r.warnings:
            lines.append(f"      warning: {w}")
    s = res.summary()
    if "volume_mm3" in s:
        lines.append(f"volume {s['volume_mm3']} mm^3, bbox {' x '.join(_fmt(v) for v in s['bbox_mm']['size'])} mm, "
                     f"{s['faces']} faces, valid={s['valid']}, {s['seconds']} s")
    return "\n".join(lines)


def _fmt(v: float) -> str:
    return f"{v:g}"


def _parse_sets(sets: list[str]) -> dict[str, str]:
    out = {}
    for s in sets or []:
        if "=" not in s:
            raise SystemExit(f"--set expects name=value, got {s!r}")
        k, v = s.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def _outputs(res: RegenResult, out: Path, render: bool) -> list[Path]:
    out.mkdir(parents=True, exist_ok=True)
    written = []
    (out / "report.json").write_text(json.dumps(res.summary(), indent=2) + "\n")
    written.append(out / "report.json")
    part = res.part
    if part is not None:
        bd.export_step(part, str(out / f"{res.doc.name}.step"))
        bd.export_stl(part, str(out / f"{res.doc.name}.stl"))
        written += [out / f"{res.doc.name}.step", out / f"{res.doc.name}.stl"]
        if render:
            for v in DEFAULT_VIEWS:
                p = out / f"view_{v}.png"
                render_view(res.body, p, v, f"{res.doc.name}  {v}")
                written.append(p)
    if render:
        for sid, (solved, frame) in res.sketches.items():
            p = out / f"sketch_{sid}.png"
            render_sketch(solved, frame, p, res.doc.feature(sid).constraints)
            written.append(p)
    return written


def _show(res: RegenResult) -> None:
    try:
        from ocp_viewer import show
    except ImportError:
        print("ocp_viewer not installed", file=sys.stderr)
        return
    if res.part is not None:
        show(res.part, names=[res.doc.name])


def cmd_build(a) -> int:
    doc = _load(a.file)
    res = Regenerator().run(doc, _parse_sets(a.set))
    print(tree_text(res))
    out = Path(a.out) if a.out else Path("out") / doc.name
    written = _outputs(res, out, render=not a.no_render)
    print(f"wrote {len(written)} files to {out}/")
    if a.write_solved:
        n = write_back_solved(a.file, res)
        print(f"updated {n} sketch entities in {a.file} with solved coordinates")
    if a.show:
        _show(res)
    return 0 if res.ok else 1


def cmd_tree(a) -> int:
    res = Regenerator().run(_load(a.file), _parse_sets(a.set))
    print(tree_text(res))
    return 0 if res.ok else 1


def cmd_watch(a) -> int:
    path, regen, last = Path(a.file), Regenerator(), 0.0
    print(f"watching {path} (Ctrl-C to stop); run `python -m ocp_viewer` to see the part")
    while True:
        m = path.stat().st_mtime
        if m != last:
            last = m
            try:
                res = regen.run(_load(a.file), _parse_sets(a.set))
                print("\n" + time.strftime("%H:%M:%S ") + tree_text(res))
                _show(res)
            except SystemExit as e:
                print(e)
        time.sleep(0.4)


def _load(file):
    try:
        return load(file)
    except ValidationError as e:
        raise SystemExit(f"{file}: schema errors\n{e}")
    except (OSError, ValueError) as e:
        raise SystemExit(f"{file}: {e}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="vibecad")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="regenerate and write STEP, STL, PNG views, report.json")
    b.add_argument("file")
    b.add_argument("--out")
    b.add_argument("--set", action="append", metavar="NAME=VALUE", help="override a parameter")
    b.add_argument("--show", action="store_true", help="push to a running ocp_viewer")
    b.add_argument("--no-render", action="store_true")
    b.add_argument("--write-solved", action="store_true", help="save solved sketch coordinates back into the file")
    b.set_defaults(fn=cmd_build)
    t = sub.add_parser("tree", help="print the feature tree with status")
    t.add_argument("file")
    t.add_argument("--set", action="append", metavar="NAME=VALUE")
    t.set_defaults(fn=cmd_tree)
    w = sub.add_parser("watch", help="rebuild and show on every save")
    w.add_argument("file")
    w.add_argument("--set", action="append", metavar="NAME=VALUE")
    w.set_defaults(fn=cmd_watch)
    a = ap.parse_args(argv)
    try:
        return a.fn(a)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
