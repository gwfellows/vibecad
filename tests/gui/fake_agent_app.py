"""vibecad-app with a scripted agent instead of a model, for browser tests of the agent panel.

  uv run python tests/gui/fake_agent_app.py --root DIR --port 8792

Each prompt runs the next scripted turn: it emits the same events AgentRunner does (thinking, tool calls with
real tool results, images, text, metrics) and calls the real Workspace tools, so the tree and 3D view update
exactly as they would for a model. The first line of every reply echoes the prompt's context prefix; a prompt
containing "(echo)" runs no tools and only echoes.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
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


DEMO_REPLIES = [
    "Made `fake_plate`: a 60 × 40 mm plate, 4 mm thick, with the width as parameter `w`.",
    "Added a 10 mm hole through the middle (my first param edit had a typo and was rejected; fixed).",
    "Made the plate 6 mm thick.",
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

    async def run(self, prompt: str, attachments: list[dict] | None = None) -> RunMetrics:
        pub = self.app.publish
        m = RunMetrics(prompt=prompt, model="scripted")
        t0 = time.perf_counter()
        pub({"type": "run_start", "prompt": prompt, "t": time.time()})
        pub({"type": "agent_thinking", "text": "Planning the change."})
        turn = [] if "(echo)" in prompt else TURNS[self.n % len(TURNS)]  # "(echo)": just report the context
        for k, (name, args) in enumerate(turn):
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
        att = f"\nAttachment blocks: {', '.join(b['type'] for b in attachments)}" if attachments else ""
        if os.environ.get("VIBECAD_DEMO") and turn:  # README recordings: a plain reply instead of the context echo
            pub({"type": "agent_text", "text": DEMO_REPLIES[self.n % len(DEMO_REPLIES)], "t": time.time()})
        else:
            pub({"type": "agent_text", "text": "Context: " + (" ".join(context) or "none") + att + "\nDone.", "t": time.time()})
        self.n += bool(turn)
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
