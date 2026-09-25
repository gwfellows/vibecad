"""vibecad-app with a scripted agent instead of a model, for browser tests of the agent panel.

  uv run python tests/gui/fake_agent_app.py --root DIR --port 8792

Each prompt runs the next scripted turn: it emits the same events AgentRunner does (thinking, tool calls with
real tool results, images, text, metrics) and calls the real Workspace tools, so the tree and 3D view update
exactly as they would for a model. The first line of every reply echoes the prompt's context prefix.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path

import uvicorn

from vibecad.agent import RunMetrics, ToolCall
from vibecad.app import create_app
from vibecad.workspace import call

PLATE = [
    {"op": "set_param", "name": "w", "value": "60 mm"},
    {"op": "add_feature", "feature": {"id": "plate_sk", "type": "sketch", "plane": {"datum": "XY"}, "intent": "plate"}},
    {"op": "add_rectangle", "sketch": "plate_sk", "id": "plate", "width": "w", "height": 40, "center": [0, 0]},
    {"op": "add_feature", "feature": {"id": "plate", "type": "extrude", "profile": {"sketch": "plate_sk"}, "distance": 4,
                                      "intent": "base plate"}},
]
HOLE = [
    {"op": "add_feature", "feature": {"id": "hole_sk", "type": "sketch", "plane": {"datum": "XY"}}},
    {"op": "add_circle", "sketch": "hole_sk", "id": "hole", "diameter": 10, "center": [0, 0]},
    {"op": "add_feature", "feature": {"id": "hole", "type": "extrude", "profile": {"sketch": "hole_sk"},
                                      "extent": "through_all", "direction": "symmetric", "mode": "cut"}},
]
TURNS = [
    [("new_part", {"path": "parts/fake_plate.vcad.json", "name": "fake_plate"}),
     ("apply_ops", {"ops": PLATE, "message": "plate"}),
     ("render", {"views": ["iso"]})],
    [("apply_ops", {"ops": [{"op": "set_param", "name": "w", "value": "2 *"}], "message": "typo"}),
     ("apply_ops", {"ops": HOLE, "message": "center hole"})],
    [("apply_ops", {"ops": [{"op": "update_feature", "id": "hole", "set": {"intent": "x"}}], "message": "out of scope"}),
     ("apply_ops", {"ops": [{"op": "update_feature", "id": "plate", "set": {"distance": 6}}], "message": "thicker"})],
]


class ScriptedAgent:
    def __init__(self, app):
        self.app, self.n = app, 0

    async def __aexit__(self, *exc):
        pass

    async def interrupt(self):
        pass

    async def set_model(self, model):
        pass

    async def run(self, prompt: str) -> RunMetrics:
        pub = self.app.publish
        m = RunMetrics(prompt=prompt, model="scripted")
        t0 = time.perf_counter()
        pub({"type": "run_start", "prompt": prompt, "t": time.time()})
        pub({"type": "agent_thinking", "text": "Planning the change."})
        for k, (name, args) in enumerate(TURNS[self.n % len(TURNS)]):
            tid = f"t{self.n}_{k}"
            pub({"type": "tool_call", "id": tid, "name": name, "input": args, "t": time.time()})
            tc = ToolCall(name, len(json.dumps(args)), time.time())
            m.tool_calls.append(tc)
            await asyncio.sleep(0.2)
            text, png = await asyncio.to_thread(call, self.app.ws, name, args)
            if png is not None:
                import base64
                pub({"type": "tool_image", "name": name, "png_b64": base64.b64encode(png).decode()})
                text = ""
            elif name == "apply_ops":
                tc.ok = bool(json.loads(text).get("applied"))
            pub({"type": "tool_result", "id": tid, "is_error": False, "text": text, "t": time.time()})
        context = [ln for ln in prompt.splitlines() if ln.startswith("[")]
        pub({"type": "agent_text", "text": "Context: " + (" ".join(context) or "none") + "\nDone.", "t": time.time()})
        self.n += 1
        m.wall_s, m.turns, m.output_tokens = time.perf_counter() - t0, 3, 123
        pub({"type": "run_done", "metrics": m.summary()})
        return m


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--port", type=int, default=8792)
    a = ap.parse_args()
    api = create_app(Path(a.root).resolve())
    A = api.state.A
    agent = ScriptedAgent(A)

    async def ensure_runner():
        A.runner = agent
        return agent

    A.ensure_runner = ensure_runner
    uvicorn.run(api, host="127.0.0.1", port=a.port, log_level="warning")


if __name__ == "__main__":
    main()
