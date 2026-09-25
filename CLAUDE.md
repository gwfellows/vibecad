# VibeCAD

AI-assisted parametric B-rep CAD. Parts are `*.vcad.json` feature trees (params, constrained sketches, features) regenerated into solids with build123d/OpenCascade; sketches are solved with PlaneGCS.

Design doc: https://claude.ai/code/artifact/143cbf50-54de-4302-849d-9ac99055b488

## Two kinds of work in this repo

**Designing or editing a part** (the user asks for a bracket, a change to a part, a question about a feature): use the `vibecad` MCP tools and follow the guide below. Don't write build123d/CadQuery code, and don't edit `*.vcad.json` by hand when an op exists.

**Developing VibeCAD itself** (changing code in `core/vibecad/`):
- After GUI changes (`app.py`, `static/`), run `scripts/gui_smoke.sh`: a Playwright browser test of the main GUI flows.
- `uv run pytest` must pass. Every example part in `examples/` is checked against a hand-calculated volume, and every parameter is swept ±10%. A new feature type needs an example that exercises it plus its volume formula in `tests/test_examples.py`.
- Layout: `workspace.py` all agent tools (shared by MCP server, GUI and benchmark) · `agent.py` Agent SDK runner with metrics · `app.py` + `static/` GUI · `bench.py` agent benchmark · `macros.py` sketch shortcut ops · `schema.py` IR models · `expr.py` params/units · `sketch.py` PlaneGCS + regions · `frames.py` plane conventions · `topo.py` face labels and FaceRef/EdgeRef resolution · `features.py` feature builders · `regen.py` regeneration + cache · `ops.py` edit ops · `session.py` undo/history/autosave · `mcp_server.py` agent tools · `render.py` PNGs · `cli.py`.
- Face labels must be carried through every operation with OCCT history (`propagate`); never select topology by index.
- `docs/IR.md` is the user- and agent-facing format reference. Keep it in sync with `schema.py`.
- Agent speed and cost matter as much as correctness. Log findings in `docs/agent-efficiency.md` and measure changes to the guide or tools with `vibecad-bench` (runs cost real usage; keep them small).

@agent/GUIDE.md
