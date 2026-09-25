"""Agent benchmark: run design tasks headlessly and record time, tokens, tool use and part quality.

  vibecad-bench --tasks bench/tasks.json --variant lean --only battery_mount --repeat 1

Each run gets a fresh working directory under bench/results/<stamp>_<variant>/<task>_<n>/ holding the
parts the agent made, a timeline (events.jsonl) and metrics.json. A summary table is printed and saved
as summary.md next to them. Variants are defined in bench/variants.json.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import time
from pathlib import Path

from .agent import AgentRunner
from .regen import Regenerator, load
from .workspace import ROOT, Workspace

BENCH = ROOT / "bench"


def part_volumes(workdir: Path) -> dict[str, float | None]:
    out = {}
    for p in sorted(workdir.rglob("*.vcad.json")):
        try:
            part = Regenerator().run(load(p)).part
            out[p.name] = part.volume if part is not None else None
        except Exception:
            out[p.name] = None
    return out


def check_parts(workdir: Path, task: dict, prev: dict[str, float | None] | None = None) -> dict:
    parts = sorted(workdir.rglob("*.vcad.json"))
    out = {"parts": [], "pass": True, "problems": []}
    want = task.get("expect_parts")
    if want is not None and len(parts) != want:
        out["problems"].append(f"expected {want} part file(s), found {len(parts)}")
    if not parts:
        out["problems"].append("no part files")
    for p in parts:
        try:
            res = Regenerator().run(load(p))
        except Exception as e:
            out["problems"].append(f"{p.name}: does not load: {e}")
            continue
        s = res.summary()
        errs = [f.id for f in res.features if f.status == "error"]
        warns = [f.id for f in res.features if f.warnings]
        dof = {k: v[0].report.dof for k, v in res.sketches.items() if v[0].report.dof}
        info = {"file": p.name, "features": len(res.features), "errors": errs, "warnings": warns,
                "underconstrained": dof, "volume": s.get("volume_mm3"), "bbox": s.get("bbox_mm", {}).get("size"),
                "valid": s.get("valid")}
        out["parts"].append(info)
        if errs or warns or dof or not s.get("valid"):
            out["problems"].append(f"{p.name}: errors={errs} warnings={warns} dof={dof} valid={s.get('valid')}")
    for chk in task.get("checks", []):
        msg = _check(chk, parts, workdir, prev)
        if msg:
            out["problems"].append(msg)
    out["pass"] = not out["problems"]
    return out


def _check(chk: dict, parts: list[Path], workdir: Path, prev: dict[str, float | None] | None = None) -> str | None:
    """Task-specific checks. Returns a problem description, or None if the check passes.

    In a follow-up turn, `volume_change` with `"from": "@prev"` compares against the part as it was
    before that turn."""
    import math

    target = workdir / chk["file"] if "file" in chk else (parts[0] if parts else None)
    if target is None or not target.exists():
        return f"check {chk['kind']}: part file {chk.get('file')} missing"
    res = Regenerator().run(load(target))
    part = res.part
    if part is None:
        return f"check {chk['kind']}: {target.name} has no solid"
    kind = chk["kind"]
    if kind == "param":
        v = res.env.get(chk["name"])
        ok = v is not None and abs(v - chk["value"]) <= chk.get("tol", 0.01)
        return None if ok else f"param {chk['name']} = {v}, expected {chk['value']}"
    if kind == "param_any":  # some param has this value (the agent may name it differently)
        ok = any(abs(v - chk["value"]) <= chk.get("tol", 0.01) for v in res.env.values())
        return None if ok else f"no param with value {chk['value']} ({chk.get('why', '')})"
    if kind == "volume_change":  # relative to the setup file, or to the previous turn ("@prev")
        if chk["from"] == "@prev":
            base = (prev or {}).get(target.name)
            if not base:
                return f"check volume_change: no previous volume for {target.name}"
        else:
            base = Regenerator().run(load(ROOT / chk["from"])).part.volume
        r = part.volume / base - 1
        ok = chk.get("min", -1e9) <= r <= chk.get("max", 1e9)
        return None if ok else f"volume change {r:+.1%} outside [{chk.get('min')}, {chk.get('max')}]"
    if kind == "bbox_max":
        size = part.bounding_box().size
        ok = all(a <= b + 1e-6 for a, b in zip(sorted((size.X, size.Y, size.Z)), sorted(chk["size"])))
        return None if ok else f"bbox {size.X:.1f} x {size.Y:.1f} x {size.Z:.1f} exceeds {chk['size']}"
    if kind == "holes":  # cylindrical holes of a diameter range, counted by distinct axes
        axes = set()
        for f in part.faces():
            if f.geom_type.name == "CYLINDER":
                r = f.radius if hasattr(f, "radius") else None
                if r and chk["d_min"] / 2 - 1e-3 <= r <= chk["d_max"] / 2 + 1e-3:
                    c = f.axis_of_rotation if hasattr(f, "axis_of_rotation") else None
                    key = tuple(round(v, 1) for v in (c.position.X, c.position.Y, c.position.Z)) if c else id(f)
                    dirn = tuple(round(abs(v), 2) for v in (c.direction.X, c.direction.Y, c.direction.Z)) if c else ()
                    # project the axis point onto the plane normal to the direction so positions along the axis merge
                    if c:
                        p = c.position
                        d = c.direction
                        t = p.X * d.X + p.Y * d.Y + p.Z * d.Z
                        key = tuple(round(v, 1) for v in (p.X - t * d.X, p.Y - t * d.Y, p.Z - t * d.Z))
                    axes.add((key, dirn))
        n = len(axes)
        ok = chk.get("min", 0) <= n <= chk.get("max", math.inf)
        return None if ok else f"{n} holes of d {chk['d_min']}-{chk['d_max']} mm, expected {chk.get('min')}-{chk.get('max')}"
    if kind == "feature_type":
        types = [f.type for f in res.doc.features]
        return None if chk["type"] in types else f"no {chk['type']} feature"
    return f"unknown check {kind}"


async def run_one(task: dict, variant: dict, workdir: Path, runner_cls=AgentRunner) -> dict:
    """Run a task's prompt, then each of its `followups` (user edit requests) in the same conversation,
    checking the parts after every turn."""
    workdir.mkdir(parents=True, exist_ok=True)
    if task.get("setup_copy"):
        shutil.copy(ROOT / task["setup_copy"], workdir / Path(task["setup_copy"]).name)
    if variant.get("prompt_mode") == "claude_code":  # give it what a repo checkout would have
        shutil.copy(ROOT / "CLAUDE.md", workdir / "CLAUDE.md")
        (workdir / "agent").mkdir(exist_ok=True)
        shutil.copy(ROOT / "agent" / "GUIDE.md", workdir / "agent" / "GUIDE.md")
    events = (workdir / "events.jsonl").open("w")
    t0 = time.time()

    def on_event(e):
        if e["type"] == "agent_phase" and e.get("chars"):
            return  # progress ticks: too many to log
        e = {k: v for k, v in e.items() if k != "png_b64"}
        e["rel_s"] = round(time.time() - t0, 2)
        events.write(json.dumps(e, default=str) + "\n")
        events.flush()

    ws = Workspace(workdir)
    headless = ("This is an unattended run: nobody can answer questions. Do not ask; choose sensible values, record "
                "assumptions in design_notes, and finish the design.")
    prompt = task["prompt"] + "\n\n" + headless if variant.get("prompt_mode") == "claude_code" else task["prompt"]
    followups = []
    async with runner_cls(ws, model=variant.get("model", "sonnet"), prompt_mode=variant.get("prompt_mode", "guide+ir"),
                          extra_system=(variant.get("extra_system", "") + "\n\n" + headless).strip(), effort=variant.get("effort"),
                          thinking=variant.get("thinking"),
                          max_turns=variant.get("max_turns", 80), on_event=on_event) as r:
        m = await r.run(prompt)
        check = check_parts(workdir, task)
        for i, fu in enumerate(task.get("followups", []), start=1):
            prev = part_volumes(workdir)
            on_event({"type": "followup", "n": i, "prompt": fu["prompt"]})
            fm = await r.run(fu["prompt"])
            followups.append({"n": i, "prompt": fu["prompt"], "metrics": fm.summary(),
                              "check": check_parts(workdir, {"expect_parts": task.get("expect_parts"), **fu}, prev)})
    events.close()
    result = {"task": task["id"], "variant": variant["name"], "metrics": m.summary(), "check": check,
              "followups": followups}
    (workdir / "metrics.json").write_text(json.dumps(result, indent=1))
    return result


def table(results: list[dict]) -> str:
    rows = ["| task | variant | pass | wall s | 1st output s | thinking s | writing tool args s | text s | tool exec s | turns | tool calls | rejected ops | renders | out tok | cost $ |",
            "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in results:
        turns = [(r["task"], r["metrics"], r["check"])]
        turns += [(f"{r['task']} +{f['n']}", f["metrics"], f["check"]) for f in r.get("followups", [])]
        for name, m, c in turns:
            f1 = m.get("first_output_s")
            rows.append(f"| {name} | {r['variant']} | {'yes' if c['pass'] else 'NO'} | {m['wall_s']:.0f} | "
                        f"{f1 if f1 is None else round(f1)} | {m['thinking_s']:.0f} | {m['tool_input_s']:.0f} | {m['text_s']:.0f} | "
                        f"{m['tool_s']:.0f} | {m['turns']} | {m['n_tool_calls']} | {m['ops_rejected']} | "
                        f"{m['tool_counts'].get('render', 0)} | {m['output_tokens']} | {m['cost_usd']:.2f} |")
    return "\n".join(rows)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="vibecad-bench")
    ap.add_argument("--tasks", default=str(BENCH / "tasks.json"))
    ap.add_argument("--variants", default=str(BENCH / "variants.json"))
    ap.add_argument("--variant", action="append", help="variant name(s) to run (default: lean)")
    ap.add_argument("--only", action="append", help="task id(s) to run (default: all)")
    ap.add_argument("--repeat", type=int, default=1)
    a = ap.parse_args(argv)
    tasks = [t for t in json.loads(Path(a.tasks).read_text()) if not a.only or t["id"] in a.only]
    variants = {v["name"]: v for v in json.loads(Path(a.variants).read_text())}
    names = a.variant or ["lean"]
    stamp = time.strftime("%Y%m%d-%H%M%S")
    results = []
    for vn in names:
        out = BENCH / "results" / f"{stamp}_{vn}"
        for t in tasks:
            for i in range(a.repeat):
                print(f"running {t['id']} [{vn}] #{i + 1} ...", flush=True)
                r = asyncio.run(run_one(t, variants[vn], out / f"{t['id']}_{i + 1}"))
                results.append(r)
                m = r["metrics"]
                print(f"  {'pass' if r['check']['pass'] else 'FAIL'}  {m['wall_s']:.0f}s  {m['n_tool_calls']} tools  "
                      f"${m['cost_usd']:.2f}  {r['check']['problems'][:2]}", flush=True)
                for f in r["followups"]:
                    fm = f["metrics"]
                    print(f"  +{f['n']} {'pass' if f['check']['pass'] else 'FAIL'}  {fm['wall_s']:.0f}s  "
                          f"{fm['n_tool_calls']} tools  ${fm['cost_usd']:.2f}  {f['check']['problems'][:2]}", flush=True)
        (out / "summary.md").write_text(table([r for r in results if r["variant"] == vn]) + "\n")
    print()
    print(table(results))


if __name__ == "__main__":
    main()
