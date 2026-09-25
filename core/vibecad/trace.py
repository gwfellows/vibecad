"""Turn a benchmark run's events.jsonl into a readable trace for review.

  vibecad-trace bench/results/<stamp>_<variant>/<task>_1            -> prints markdown
  vibecad-trace <run dir> --out trace.md

The trace shows, in order and with timestamps: thinking summaries, visible text, each tool call (edit
batches summarized op by op), and each result (ok / rejected with reason / warnings / volume change).
Reviewing these is how we find heuristics the agent was missing (see docs/testing-plan.md).
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def summarize_ops(ops: list[dict]) -> list[str]:
    out = []
    for o in ops:
        k = o.get("op")
        if k == "add_feature":
            f = o.get("feature", {})
            extra = ""
            if f.get("type") == "sketch":
                pl = f.get("plane", {})
                where = pl.get("datum") or f"face {pl.get('face', {}).get('feature')}.{pl.get('face', {}).get('role')}"
                extra = f" on {where}, {len(f.get('entities', []))} entities, {len(f.get('constraints', []))} constraints"
            elif f.get("type") == "extrude":
                extra = f" {f.get('mode', 'add')} {f.get('profile', {}).get('sketch')} {f.get('extent', f.get('distance'))} {f.get('direction', 'normal')}"
            out.append(f"add {f.get('type')} `{f.get('id')}`{extra}" + (f" — {f['intent']}" if f.get("intent") else ""))
        elif k == "set_param":
            out.append(f"param {o.get('name')} = {o.get('value')}")
        elif k in ("add_rectangle", "add_circle", "add_slot", "add_polygon", "add_regular_polygon"):
            out.append(f"{k} `{o.get('id')}` in {o.get('sketch')}")
        elif k == "update_feature":
            out.append(f"update `{o.get('id')}`: {json.dumps(o.get('set'))[:160]}")
        elif k in ("add_constraint", "add_entity"):
            out.append(f"{k} in {o.get('sketch')}: {json.dumps(o.get('constraint') or o.get('entity'))[:140]}")
        else:
            out.append(f"{k} {json.dumps({x: y for x, y in o.items() if x != 'op'})[:160]}")
    # collapse runs of params
    params = [x for x in out if x.startswith("param ")]
    rest = [x for x in out if not x.startswith("param ")]
    return ([f"params: {', '.join(p[6:] for p in params)}"] if params else []) + rest


def result_line(text: str, is_error: bool) -> str:
    if is_error:
        return f"**ERROR** {text[:300]}"
    try:
        j = json.loads(text)
    except ValueError:
        return text[:200].replace("\n", " ")
    if not isinstance(j, dict):
        return str(j)[:200]
    if j.get("error"):
        return f"**REJECTED** {j['error'][:400]}"
    bits = []
    if j.get("errors"):
        bits.append("**errors:** " + "; ".join(j["errors"])[:400])
    if j.get("warnings"):
        bits.append("**warnings:** " + "; ".join(j["warnings"])[:300])
    if j.get("underconstrained_sketches"):
        bits.append("underconstrained: " + ", ".join(j["underconstrained_sketches"]))
    if any(n.startswith("sizes changed") for n in j.get("notes", [])):
        bits.append("note: " + next(n for n in j["notes"] if n.startswith("sizes changed"))[:300])
    if j.get("change"):
        v = j["change"].get("volume_mm3")
        b = j["change"].get("bbox_size_mm")
        bits.append(f"volume {v[0]} → {v[1]}, bbox → {b[1]}" if v else "")
    return ("ok " if j.get("ok") else "") + " · ".join(x for x in bits if x) or text[:200]


def render(run: Path) -> str:
    ev = [json.loads(l) for l in (run / "events.jsonl").read_text().splitlines()]
    m = json.loads((run / "metrics.json").read_text()) if (run / "metrics.json").exists() else {}
    lines = [f"# Trace: {run.parent.name}/{run.name}", ""]
    if m:
        mm, c = m["metrics"], m["check"]
        lines += [f"**Prompt:** {mm['prompt'].splitlines()[0]}", "",
                  f"**Result:** {'pass' if c['pass'] else 'FAIL'} · {mm['wall_s']:.0f} s (thinking {mm.get('thinking_s', 0):.0f} s, "
                  f"writing tool args {mm.get('tool_input_s', 0):.0f} s, tools {mm['tool_s']:.0f} s) · {mm['n_tool_calls']} tool calls, "
                  f"{mm['ops_rejected']} rejected · {mm['output_tokens']} output tokens · ${mm['cost_usd']:.2f} · {mm['model']}", ""]
        if c["problems"]:
            lines += ["**Check problems:** " + "; ".join(c["problems"]), ""]
        for f in m.get("followups", []):
            fm, fc = f["metrics"], f["check"]
            lines += [f"**Follow-up {f['n']}:** {f['prompt']} → {'pass' if fc['pass'] else 'FAIL'} · {fm['wall_s']:.0f} s · "
                      f"{fm['n_tool_calls']} tool calls, {fm['ops_rejected']} rejected · ${fm['cost_usd']:.2f}"
                      + (f" · problems: {'; '.join(fc['problems'])}" if fc["problems"] else ""), ""]
    calls = {}
    for e in ev:
        t = f"`{e.get('rel_s', 0):6.1f}s`"
        k = e["type"]
        if k == "agent_thinking" and e.get("text"):
            lines += [f"{t} **thinking**", "", "> " + e["text"].strip().replace("\n", "\n> "), ""]
        elif k == "agent_text" and e.get("text", "").strip():
            lines += [f"{t} **says:** {e['text'].strip()}", ""]
        elif k == "tool_call":
            calls[e["id"]] = e["name"]
            inp = e.get("input", {})
            if e["name"] == "apply_ops":
                lines.append(f"{t} **apply_ops** ({len(json.dumps(inp))} chars) — {inp.get('message', '')}")
                lines += [f"    - {x}" for x in summarize_ops(inp.get("ops", []))]
            else:
                lines.append(f"{t} **{e['name']}** {json.dumps(inp)[:200]}")
        elif k == "tool_result":
            lines += [f"{t} → {result_line(e.get('text', ''), e.get('is_error', False))}", ""]
        elif k == "followup":
            lines += [f"## Follow-up {e['n']}: {e['prompt']}", ""]
    return "\n".join(lines) + "\n"


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="vibecad-trace")
    ap.add_argument("run", help="a run directory containing events.jsonl")
    ap.add_argument("--out")
    a = ap.parse_args(argv)
    md = render(Path(a.run))
    if a.out:
        Path(a.out).write_text(md)
    else:
        print(md)


if __name__ == "__main__":
    main()
