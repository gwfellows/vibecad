# VibeCAD

AI-assisted parametric CAD. The AI and the engineer edit the same artifact: a readable feature tree of constrained sketches and operations, regenerated into a B-rep solid.

Design doc: https://claude.ai/code/artifact/143cbf50-54de-4302-849d-9ac99055b488

## Stack

- Geometry: build123d on OCP (OpenCascade)
- Sketch solver: PlaneGCS (FreeCAD's solver) via `planegcs`
- Viewer (Phase 1): `ocp_viewer` standalone; later `three-cad-viewer` in a React app
- Sketcher UI (Phase 4): BREP.io `Sketcher2DEmbed`, pinned version

## Setup (macOS)

```
./scripts/setup_mac.sh
```

Linux/Windows: `uv sync` (planegcs ships wheels there).

## Status

Phase 2 (agent tools, benchmark) and a first GUI done; Phase 1 (IR, regenerator, CLI) done. Six reference parts in `examples/` regenerate with every sketch fully constrained and volumes matching hand calculations. Each single-parameter change of ±10% still rebuilds (`uv run pytest`).

```
uv run vibecad build examples/l_bracket.vcad.json            # STEP, STL, 5 PNG views, sketch PNGs, report.json -> out/l_bracket/
uv run vibecad build examples/l_bracket.vcad.json --set width=80 --show
uv run vibecad tree examples/enclosure_lid.vcad.json         # feature tree with status
uv run vibecad watch examples/nema17_mount.vcad.json         # rebuild + show on every save (run `uv run python -m ocp_viewer` first)
```

IR reference: [docs/IR.md](docs/IR.md).

## The app (GUI)

```
uv run vibecad-app            # then open http://127.0.0.1:8765
```

- **Right panel:** talk to the design agent. Every tool call appears as it happens with its result (volume change, errors, warnings), along with renders the agent looks at, a live indicator of what the model is doing (thinking, writing a batch, running a tool), and timing, token and cost numbers when it finishes.
- **Left panel:** the feature tree (updates live; changed features flash), editable parameters, and the selected feature's JSON (editable).
- **Center:** 3D view (hover a face to see its labels; click to select its feature), a Renders tab, a Sketch tab, and a History tab (every batch by you or the agent, including rejected ones).
- Select a feature and tick "Limit edits to the selected feature" to scope the agent's changes.
- The agent runs through your Claude login (Claude Pro/Max works, no API key needed) and counts against its usage limits.

`--root DIR` picks the folder of parts (default: current directory); new parts go in `parts/`. `--model` picks the model (default sonnet).

## Designing parts with Claude Code

Register the MCP server once: `claude mcp add --scope project vibecad -- uv run --quiet vibecad-mcp`. Then ask Claude Code for a part in this folder. `CLAUDE.md` loads the design guide (`agent/GUIDE.md`).

## Measuring the agent

`uv run vibecad-bench --only battery_mount --variant lean` runs design tasks headlessly (`bench/tasks.json`) under setup variants (`bench/variants.json`), checks the resulting parts, and records time to first output, thinking time, tool time, tool calls, rejected batches, tokens and cost. Findings so far: [docs/agent-efficiency.md](docs/agent-efficiency.md). Runs use your Claude login's usage.

## License

MIT
