# Agent efficiency notes

Running log of what makes the design agent slow or expensive, how it was measured, and what changed. Measure with `vibecad-bench` (see the end of this file); every run records a timeline (`events.jsonl`) and metrics (`metrics.json`).

## 2026-09-25: why a "simple bracket" took 19 minutes

**Observation (Grant's interactive Claude Code run, battery mount, two parts).** 19 min 21 s total. The part files' history logs show all 10 successful edit batches landed within 2 min 12 s, 8–18 s apart. So about 17 minutes passed before the first successful edit.

**Reproduced headlessly** with the same prompt (Sonnet, `vibecad-bench`):

| setup | before first tool call | rest of the run | tool execution | tool calls | output tokens | cost |
|---|---|---|---|---|---|---|
| lean (guide + IR reference in system prompt, only CAD tools) | 302 s | 158 s | 9 s | 21 | 47k | $1.01 |
| Claude Code imitation (Claude Code system prompt + CLAUDE.md + stdio MCP) | 107 s, then 95 s more after reading the IR | stopped by usage limit | n/a | 3 | 19k | $0.31 |

**Diagnosis.**

1. **Up-front deliberation dominates.** 66% of the lean run was one silent thinking block before any tool call. Once the agent started building, it moved at about 8 s per step. The model works out every coordinate and constraint in its head, then emits one large batch (6.4k characters here), which failed on conflicting constraints anyway.
2. **Constraint bookkeeping is expensive to think through.** A rectangle is 4 lines + 11 constraints; a slot is 4 curves + 13. The model enumerates and checks these mentally.
3. **Claude Code adds overhead:** its MCP tools are deferred, so the first turn goes to `ToolSearch` to load them, then another to read the IR reference, and each turn re-thinks.
4. **The CAD tools are not the bottleneck:** 9 s of 460 s.

**Changes made.**

- Sketch shortcut ops `add_rectangle`, `add_circle`, `add_slot`: fully constrained primitives with named, param-driven dimensions, from one line of JSON.
- Guide section "Working fast": write a short plan and start building; let the solver and report catch mistakes; small batches; render only at milestones.
- Sketch coordinates accept expressions (the agent kept writing `"bolt_circle_d / 2"` as a coordinate, which was rejected).
- Rejected batches are now logged in `<part>.history.jsonl`; they were invisible before.
- The GUI and benchmark runner stream token-level events and time each phase: thinking, writing tool arguments, visible text, tool execution.
- The GUI and benchmark runner put the guide and IR reference in the system prompt, and expose only the CAD tools (no shell/file tools, no deferred tool loading).

**Still to measure** (the usage limit stopped the runs): the same task after these changes, and with `effort` low / medium (`bench/variants.json`: `lean_effort_low`, `lean_effort_medium`).

## First GUI run (flange adapter, lean setup, before the shortcut ops were live)

86 s total: 43 s thinking, 21 s writing tool arguments, 3 s tools; 10 tool calls, 2 rejected batches, $0.23. Both rejections led to the fixes above (a shortcut op the server didn't yet support, and an expression used as a coordinate).

## 2026-09-25: thinking effort (the big lever)

Grant's GUI run ("build a simple bracket to connect two 20x20 square tubes", default effort): 211 s, of which 177 s thinking, 18 s writing edits, 2 s tools.

Same prompt, headless, one run each (Sonnet, lean setup, with the shortcut ops and the "Working fast" guide section):

| setting | total | thinking | writing tool args | tool calls | rejected | output tokens | cost | result |
|---|---|---|---|---|---|---|---|---|
| default effort | 198 s | 158 s | 21 s | 5 | 0 | 19.6k | $0.36 | flat L corner plate, filleted |
| effort medium | 151 s | 98 s | 29 s | 12 | 0 | 14.7k | $0.41 | angle bracket, inside fillet |
| **effort low** | **61 s** | **19 s** | 19 s | 11 | 0 | 5.7k | **$0.26** | angle bracket, no fillet |
| thinking disabled | 107 s | 0 s | 34 s | 19 | 2 | 10.1k | $0.41 | more trial and error |

- The guide changes alone did not fix the up-front deliberation; effort did. At default effort Sonnet spends most of the run in one thinking block.
- Low effort was 3.2x faster and 28% cheaper, and passed every automated check. Its design was slightly less refined (no inside fillet, legs narrower than the tube face).
- Turning thinking off entirely is worse: the model makes more mistakes, corrects them by trial and error, and ends up slower and more expensive than low effort.
- **Decision:** the GUI defaults to effort low, with a dropdown to raise it. Revisit with more runs and harder prompts. n = 1 per setting, so treat the numbers as directional.

## 2026-09-25: first full suite (11 tasks, Sonnet, effort low) and trace review

| task | level | pass | total | thinking | tool calls | rejected | cost | review of the result |
|---|---|---|---|---|---|---|---|---|
| spacer_edit | edit, operation | yes | 17 s | 2 s | 5 | 0 | $0.05 | right: 8 holes, bore chamfer |
| motor_plate_edit | edit, parameter | yes | 20 s | 5 s | 4 | 0 | $0.06 | right: NEMA 23 pattern, bigger plate |
| bushing_longer | edit, parameter + operation | yes | 38 s | 8 s | 6 | 1 | $0.11 | longer sleeve, oil groove |
| stiffen_bracket | edit, functional | yes | 62 s | 28 s | 6 | 1 | $0.15 | added a triangular gusset between the slots: the engineer's answer |
| vents_to_front | edit, operation | yes | 71 s | 42 s | 10 | 0 | $0.18 | moved, but the slots run up into the lid's top edge (open notches) |
| uno_enclosure | create, hard | yes | 84 s | 42 s | 8 | 0 | $0.20 | tray, standoffs, USB and jack cutouts |
| tube_bracket | create, easy | yes | 124 s | 95 s | 8 | 0 | $0.23 | clamp ring on a back plate, 2 screw holes |
| tube_corner_bracket | create, easy | yes | 157 s | 96 s | 15 | 1 | $0.36 | angle bracket with fillet |
| shelf_bracket | create, medium | yes | 154 s | 107 s | 10 | 2 | $0.33 | L with triangular gusset, holes on both arms |
| vbelt_pulley | create, medium | yes | 321 s | 246 s | 12 | 2 | $0.60 | V groove, hub, set-screw hole |
| battery_mount | create, hard | yes | 333 s | 232 s | 18 | 0 | $0.67 | tray + cover bolted vertically through flanges (clamps correctly now) |

All passed the automated checks. On visual review every part was sensible, and the mechanical-review guide section visibly changed the battery design. **Edits are fast (17–71 s); new designs are slow (124–333 s), and the time is thinking.**

Findings from reading the traces (`vibecad-trace`), and what changed:

| Where time went | Evidence | Fix |
|---|---|---|
| Designing everything before the first tool call | 80–220 s of thinking before `new_part` on every creation task; the pulley worked out groove-profile coordinates in its head for 220 s | A/B test of a build-incrementally instruction (below) |
| Hand-writing polygon constraints | L profile: tried point ids as line endpoints (unhelpful error), then an over-constrained sketch, then 52 s counting DOF before giving up | `add_polygon` (vertices as param expressions, 0 DOF by construction); error hint for point ids used as coordinates |
| Sketch before its extrude in one batch | Both ops used `"after": "leg1"`, so the extrude landed first; happened twice in one run | Inserts at the same anchor keep batch order |
| Unintuitive axes on vertical faces | On a −X face sketch "up" was world −Z; holes landed outside the part | Face axes as seen from outside: up is up on vertical faces |
| An invalid solid with no reason given | Battery mount: 145 s diagnosing a self-intersecting profile from a failed render | Self-intersection check with the crossing point in the error |

### A/B: "build incrementally" instruction (effort low, current code, n = 1 per arm)

| task | baseline total | incremental total | baseline → incremental, time to first tool call |
|---|---|---|---|
| tube_bracket | 270 s | 108 s, **fail** (left a sketch at 4 DOF) | 161 s → 46 s |
| vbelt_pulley | 151 s | 217 s | 111 s → 149 s |
| shelf_bracket | 129 s | 96 s | 67 s → 47 s |

- **Inconclusive.** The instruction started building sooner in 2 of 3 tasks, but in one it rushed and shipped an under-constrained sketch.
- **Run-to-run noise is huge:** tube_bracket took 124 s in the suite and 270 s here with the same settings. Differences under about 2x need 3+ repeats per arm.
- **Not adopted yet**; kept as variant `lean_low_incremental`.
- `add_polygon` was used on its first outing (pulley, shelf bracket). Pulley total went from 321 s (before the shortcut) to 151–217 s.

## 2026-09-25: create-then-edit testing, GUI smoke test, bugs fixed

Worked through the tools by hand (the same Workspace code the agent calls): built a hex standoff, then applied a user edit ("M4: 7 mm AF, 4.5 mm bore, 15 mm long, chamfer the hex ends"). Also drove the GUI with a Playwright script. No model runs this time (see the last point).

| Finding | Evidence | Change |
|---|---|---|
| Regular polygons needed hand trig | A hex took `add_polygon` with six `cos`/`sin` vertex pairs worked out by the agent | `add_regular_polygon` (`sides`, `diameter`, `across: corners/flats`, `center`, `angle`); vertices are expressions, so it resizes with params. New example `hex_standoff` |
| One param typo wedged the session | `set_param hole_d = "2 *"` passed op validation, then regeneration raised after the doc was swapped in: every later edit failed and the GUI rollback returned 500 | `apply_ops` evaluates params and rejects the batch (also circular refs, and removing a param another uses) |
| `open_part` served a stale part | A file rewritten by another process kept its old cached session: the tree showed the old part and `render` failed with "no solid yet" | Sessions record the file's mtime/size; `open_part` reloads when it changed |
| Intermittent 500 on `/api/mesh` | The GUI asks for the mesh twice per edit; OCCT meshes in place and is not thread-safe (6/6 failures with 4 threads) | Meshing, rendering and STL export hold the workspace lock |
| GUI showed values the model didn't have | A rejected param stayed in the field; rejected edits raised uncaught promise errors | Field resets; handled errors suppressed |
| Stale intents after an edit | After the M4 edit the bore intent and design notes still said M3; nothing flags it | None yet (the guide already asks to keep intents accurate); a candidate for an automated check |

The edit itself was one batch, no rejections: the param-driven structure made it a 3-param change plus one chamfer.

**Benchmark: multi-turn tasks.** Tasks can now have `followups`, user edit requests sent in the same conversation after the design is built, each with checks (`volume_change` from `"@prev"` judges a turn against its starting point). Two tasks: `standoff_conversation` and `plate_conversation`. `tests/test_bench.py` drives them with a scripted stand-in for the model, including a case where an ignored request must fail.

**Robustness probes (same day).** Every op kind with bad input, and every example param at ×0.6 and ×1.6 (the tests sweep only ±10%). Messages that would have misled an agent, now fixed:

| Case | Before | Now |
|---|---|---|
| Holes moved off the part | "changed no volume … check its direction" (flipping can't help) | Tests the reversed tool: "would cut the other way" vs "the profile lies outside the body" |
| Bore wider than a hex's flats | Six disconnected slivers, no warning | "cut split the body into 6 separate solids" |
| Flange diameter = body diameter | `StdFail_NotDone: BRep_API: command not done` | Names the zero-length entity and the likely cause |
| Negative width | "did not solve; conflicting: none reported" | Names the dimension that is ≤ 0 |
| Fillet radius 0 | "try a smaller size" | "fillet radius must be > 0" |
| `set_param` `true` / `2wide` / `sqrt` | Accepted (width = 1 mm; unreferenceable; shadowed a function and escaped the cache's dependency tracking) | Rejected with the rule |
| `move_feature` after itself | "no feature 'base'" | "cannot move relative to itself" |
| Pattern of 5 misplaced cuts | The same warning 5 times | One line, `(x5)` |
| Hand-edited file with a bad param | Could not be opened (crash) | Opens; every feature shows the error; `set_param` fixes it |

GUI (Playwright, `scripts/gui_smoke.sh`, 66 checks; the agent panel is driven by a scripted agent, no model): a part the agent created was never framed in the 3D view (fitted while still empty), so every new design first appeared top-down and rotated. Fixed.

**Don't run `vibecad-bench` from inside a Claude Code on the web session.** The nested agent reported the parent session's id and the CLI logged "message history mutated" on it, despite `agent.py` clearing the session env vars; the account also returned a five-hour rate-limit event. Run the benchmark from a local terminal.

## 2026-09-26: realistic design workflows (holes, STEP imports), Sonnet at effort low

Ten workflows of the kind a robotics team asks for (`bench/workflows.json`, `bench/workflows2.json`), five of them around STEP models of the real parts (`examples/imports/`: controller board, 608 bearing, 2020 extrusion, NEMA 17). Checks include `clear_of_refs` (the part must not collide with the parts it is designed around) and `hole_spec` (fastener size and hole kind).

**Round 1** (before the fixes below): 4 of 6 passed.

| Workflow | Result | Time, cost | Review |
|---|---|---|---|
| controller tray (board STEP), then "8 mm standoffs + 1 mm fillets" | pass, pass | 123 s + 51 s, $0.38 | flat PETG plate, standoffs on the board's holes, inserts 4.2 × 5.5 deep |
| adapter plate, then "M4 now", then "5 mm corner radii" | pass ×3 | 40 s, 9 s, 15 s, $0.16 | clean tree; intents updated with the size |
| plate on the NEMA 17 STEP | pass | 70 s, $0.16 | hole positions typed in from `describe_import`, not projected |
| hole_plate edit: M4 tapped 10 deep, 30 mm apart | pass | 35 s, $0.11 | thickened the plate to 14 mm on its own so the threads stay blind |
| pillow block for the 608 STEP | **fail** | 122 s, $0.22 | never imported the STEP; designed from memorised sizes |
| NEMA 17 bracket beside a 2020 extrusion (two STEPs) | **fail** | 189 s, $0.32 | motor placed through the plate (10 cm³ overlap); the agent reported "no warnings" |

**Fixes** (tools first, then the guide):

| Problem | Evidence | Fix |
|---|---|---|
| Renders crashed | a visibility ray hit the apex of a blind hole's drill point (no normal): 3 failed renders in one run | treat a singular hit as hiding |
| Collisions with the reference went unseen | the agent said it "can't run a fit check against reference imports" | the import gets a warning after every batch when the part overlaps it (> 0.5 % of its volume, so press fits pass), with the overlap's extent |
| Reference holes typed as numbers | every STEP run copied coordinates, so the part wouldn't follow the reference | `describe_import` lists each round face's rims as ready-made EdgeRefs to paste into `external` entities |
| STEP ignored for a standard part | pillow block designed from memory | guide: always import a given STEP and check against it |

**Round 2** (after the fixes): 10 of 10 passed, including every follow-up.

| Workflow | Runs | Time, cost | Review |
|---|---|---|---|
| pillow block (608 STEP) | 2/2 | 175 s, 140 s; $0.38, $0.23 | imports the bearing, places it on the bore axis; M5 counterbores; no bearing shoulder (bore straight through) |
| NEMA 17 + 2020 bracket | 2/2 | 600 s, 202 s; $0.93, $0.34 | the overlap warning drove the fix in both; the 600 s run spent 5 min moving the motor and a gusset until the warning cleared |
| plate on the NEMA 17 STEP | 2/2 | 128 s, 154 s; $0.24, $0.26 | both now project the motor's hole rims (4 `external`s) |
| two-part enclosure around the board STEP | 1/1 | 339 s, $0.61 | shelled tray, standoffs with inserts, corner bosses, USB and jack cutouts in the right wall, flat lid with countersinks |
| MG996R servo bracket from datasheet sizes, then "3.5 mm pilot holes 10 deep" | 1/1, 1/1 | 230 s + 41 s, $0.46 | servo passes through a window in an upright wall, tabs screwed to it |
| shaft collar with a radial M4 set screw | 1/1 | 60 s, $0.10 | tapped hole from a datum plane tangent to the OD |
| gusset joining two 2020 extrusions (one STEP, imported twice) | 1/1 | 208 s, $0.31 | imported the extrusion a second time, rotated upright; L plate on both -Y faces |

Round 2 total: about $5.

**Remaining friction:**
- **Placement reasoning is the slow part.** Positioning a rotated reference (which way the motor hangs, which face is which) took most of the 600 s run. Candidates: an op that places a reference by mating one of its faces to a face or plane (`mate: {face, to, offset}`), and faces named by role in `describe_import` (e.g. "front face with the pilot").
- **Projection is used only when the reference face is the sketch plane.** When the plate is offset from the motor face, the agent types coordinates again. Projecting onto a parallel plane already works; the guide could show it.
- **Mechanical refinements are rare at effort low.** No bearing shoulder, no fillets unless asked. A review pass at higher effort is still an open hypothesis.

## Hypotheses to test next

- Multi-turn tasks: is a follow-up edit much cheaper than the create (it should be: the tree is already built), and does the agent keep intents and notes accurate across edits?

- The build-incrementally instruction, with 3 repeats per arm, plus a rule that `underconstrained_sketches` in a report must be fixed before moving on.

- Low effort holds up on harder prompts (two-part assemblies, edits to existing parts), measured over 3+ runs each.
- A "design review" pass at higher effort after a low-effort build catches the refinements low effort skips.
- Shortcut ops cut output tokens per part by half or more.
- An explicit "plan in at most 6 lines" instruction vs none.
- Rendering at every step vs at milestones (renders are about 1.5k input tokens each and 1–5 s).
- Claude Code with `CLAUDE.md` telling it to load the vibecad tools immediately vs the lean runner.

## How to measure

```
uv run vibecad-bench --only battery_mount --variant lean --variant lean_effort_low
uv run vibecad-bench --repeat 3                     # all tasks, lean
```

Results go to `bench/results/<stamp>_<variant>/<task>_<n>/`: the parts, `events.jsonl` (timeline), `metrics.json`, and `summary.md` per variant. The table reports time to first output, time thinking, time writing tool arguments, tool time, turns, tool calls, rejected batches, renders, output tokens and cost. Runs use your Claude login and count against its usage limits; a two-part design costs about $1 at list price. Keep suites small and run them deliberately.
