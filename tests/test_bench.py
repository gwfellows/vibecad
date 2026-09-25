"""The benchmark's multi-turn (create, then user edits) runs, driven by a scripted stand-in for the model."""
import asyncio
import json

from vibecad.agent import RunMetrics
from vibecad.bench import BENCH, run_one

TASK = next(t for t in json.loads((BENCH / "tasks.json").read_text()) if t["id"] == "standoff_conversation")

HEX_EDGES = [{"of": {"feature": "body", "role": role}, "filter": {"type": "line", "perpendicular_to": "Z"}}
             for role in ("start", "end")]
SCRIPT = [
    lambda ws: (ws.new_part("standoff.vcad.json", "standoff"), ws.apply_ops([
        {"op": "set_param", "name": "af", "value": "5.5 mm"},
        {"op": "set_param", "name": "length", "value": "12 mm"},
        {"op": "add_feature", "feature": {"id": "hex_sk", "type": "sketch", "plane": {"datum": "XY"}}},
        {"op": "add_regular_polygon", "sketch": "hex_sk", "id": "hex", "sides": 6, "diameter": "af",
         "across": "flats", "center": [0, 0]},
        {"op": "add_feature", "feature": {"id": "body", "type": "extrude", "profile": {"sketch": "hex_sk"},
                                          "distance": "length"}},
        {"op": "add_feature", "feature": {"id": "bore_sk", "type": "sketch",
                                          "plane": {"face": {"feature": "body", "role": "end"}}}},
        {"op": "add_circle", "sketch": "bore_sk", "id": "bore", "diameter": 3.4, "center": [0, 0]},
        {"op": "add_feature", "feature": {"id": "bore_cut", "type": "extrude", "profile": {"sketch": "bore_sk"},
                                          "extent": "through_all", "direction": "reverse", "mode": "cut"}},
    ], "standoff")),
    lambda ws: ws.apply_ops([{"op": "set_param", "name": "length", "value": "20 mm"}], "longer"),
    lambda ws: ws.apply_ops([{"op": "add_feature", "feature": {"id": "ends", "type": "chamfer", "edges": HEX_EDGES,
                                                               "distance": 0.5}}], "chamfer ends"),
]


class ScriptedRunner:
    """Stands in for AgentRunner: each run() performs the next scripted turn through the Workspace tools."""

    def __init__(self, ws, script, **_):
        self.ws, self.script, self.n = ws, list(script), 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        pass

    async def run(self, prompt):
        step = self.script[self.n]
        self.n += 1
        if step:
            step(self.ws)
        return RunMetrics(prompt=prompt, model="scripted")


def _run(tmp_path, script):
    return asyncio.run(run_one(TASK, {"name": "scripted"}, tmp_path,
                               runner_cls=lambda ws, **kw: ScriptedRunner(ws, script, **kw)))


def test_create_then_edits_all_pass(tmp_path):
    r = _run(tmp_path, SCRIPT)
    assert r["check"]["pass"], r["check"]
    assert [f["check"]["pass"] for f in r["followups"]] == [True, True], r["followups"]
    saved = json.loads((tmp_path / "metrics.json").read_text())
    assert len(saved["followups"]) == 2


def test_ignored_edit_request_fails_its_checks(tmp_path):
    r = _run(tmp_path, [SCRIPT[0], None, SCRIPT[2]])  # the agent ignores "make it 20 mm long"
    assert r["check"]["pass"]
    first, second = r["followups"]
    assert not first["check"]["pass"]
    assert any("volume change" in p or "bbox" in p for p in first["check"]["problems"])
    assert second["check"]["pass"]  # the chamfer turn is judged against its own starting point


def test_plate_conversation_checks_are_achievable(tmp_path):
    task = next(t for t in json.loads((BENCH / "tasks.json").read_text()) if t["id"] == "plate_conversation")
    x, y = "(W / 2 - inset)", "(H / 2 - inset)"
    corners = [[x, y], [x, f"-{y}"], [f"-{x}", y], [f"-{x}", f"-{y}"]]
    holes = [{"op": "add_circle", "sketch": "holes_sk", "id": f"h{i}", "diameter": "hole_d", "center": c}
             for i, c in enumerate(corners)]
    script = [
        lambda ws: (ws.new_part("plate.vcad.json", "plate"), ws.apply_ops([
            {"op": "set_param", "name": "W", "value": 80}, {"op": "set_param", "name": "H", "value": 50},
            {"op": "set_param", "name": "inset", "value": 6}, {"op": "set_param", "name": "hole_d", "value": 4.5},
            {"op": "add_feature", "feature": {"id": "sk", "type": "sketch", "plane": {"datum": "XY"}}},
            {"op": "add_rectangle", "sketch": "sk", "id": "p", "width": "W", "height": "H", "center": [0, 0]},
            {"op": "add_feature", "feature": {"id": "plate", "type": "extrude", "profile": {"sketch": "sk"}, "distance": 3}},
            {"op": "add_feature", "feature": {"id": "holes_sk", "type": "sketch", "plane": {"datum": "XY"}}},
            *holes,
            {"op": "add_feature", "feature": {"id": "holes", "type": "extrude", "profile": {"sketch": "holes_sk"},
                                              "extent": "through_all", "direction": "symmetric", "mode": "cut"}},
        ], "plate")),
        lambda ws: ws.apply_ops([{"op": "set_param", "name": "hole_d", "value": 5.5}], "M5"),
        lambda ws: ws.apply_ops([
            {"op": "add_feature", "feature": {"id": "gland_sk", "type": "sketch", "plane": {"datum": "XY"}}},
            {"op": "add_circle", "sketch": "gland_sk", "id": "gland", "diameter": 22, "center": [0, 0]},
            {"op": "add_feature", "feature": {"id": "gland", "type": "extrude", "profile": {"sketch": "gland_sk"},
                                              "extent": "through_all", "direction": "symmetric", "mode": "cut"}}],
            "gland hole"),
    ]
    r = asyncio.run(run_one(task, {"name": "scripted"}, tmp_path,
                            runner_cls=lambda ws, **kw: ScriptedRunner(ws, script, **kw)))
    assert r["check"]["pass"], r["check"]
    assert [f["check"]["pass"] for f in r["followups"]] == [True, True], r["followups"]
