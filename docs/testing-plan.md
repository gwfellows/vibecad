# Agent testing plan

The design agent should work the way a careful engineer does, so a person can follow and edit its models. It should also be fast, cheap, and mechanically right. This plan covers how we test that, and how test results turn into better prompting, tools and IR.

## 1. What already exists (and what we can borrow)

| Work | What it is | What we take |
|---|---|---|
| [Text2CAD](https://github.com/SadilKhan/Text2CAD) (NeurIPS 2024) | DeepCAD models annotated with text prompts from beginner ("a ring") to expert (exact sketch dimensions) | Creation prompts at graded levels of detail |
| [Text2CAD-Bench](https://arxiv.org/html/2605.18430) (2026) | CadQuery generation, levels L1–L3; 70–90% invalid at L3; shape-description prompts beat step-by-step prompts | Difficulty tiers |
| [BenchCAD](https://arxiv.org/abs/2605.10865) (2026) | 17,900 CadQuery programs across 106 industrial part families, including instruction-guided code editing | Part families for coverage (gears, springs, drills); editing tasks |
| [CADTests / CADTestBench](https://arxiv.org/html/2605.07807v1) (2026) | Executable B-rep tests per part (topology, dimensions, volume, symmetry, validity); 5,937 tests over 200 parts; tests hardened with mutated models | Our check style: small executable geometric assertions, validated against deliberately broken parts |
| [CADEngBench](https://arxiv.org/html/2608.09296) (2026) | Parameter integrity under value changes, functional edits without side effects, manufacturability, FEA | 58% of executable outputs violated engineering requirements. Test the engineering, not just the shape |
| [CAD-Editor](https://cad-editor.github.io/) (ICML 2025) | Synthetic (original, instruction, edited) triplets: design variants, then a VLM describes the difference | The recipe for generating edit tasks |
| [LLM4CAD-Editor](https://arxiv.org/html/2606.20607) (2026) | Edits at three levels: parameter, operation, functional | Our edit taxonomy |
| [neuralCAD-Edit](https://arxiv.org/html/2604.16170v1) (2026) | Expert multimodal edit requests (speech, gesture, sketches on screen) | Later: sketch-region and selection prompts |

None of these datasets drop in directly. Their models are token sequences or CadQuery programs, not our feature-tree IR, and their text describes shapes, not engineering intent. We borrow their task taxonomies and check styles and write our own tasks in our IR.

## 2. Task suite

Tasks live in `bench/tasks.json`. Each has a prompt, an optional starting part (`setup_copy`), and checks.

| Axis | Values |
|---|---|
| Kind | create; edit |
| Edit level | parameter (change a size), operation (add, move or remove features), functional (a goal such as "stiffer", "fits a NEMA 23") |
| Difficulty | easy (one part, a few features); medium (revolves, patterns, several sketches, mechanical sizing); hard (multi-part, many features, interfaces to real components) |
| Ambiguity | fully specified; underspecified (the agent must choose and record assumptions) |

Current suite: 11 tasks (6 create, 5 edit). Target: 30–40, spread over the part families engineers make most (brackets, plates, enclosures, bushings and pulleys, mounts, adapters, clamps, hinges, spacers) and every edit level.

**Generating edit tasks at scale** (CAD-Editor recipe, adapted):

1. Start from a reference part.
2. Apply a known change with ops: move a hole pattern, add ribs, swap a bolt size, change a count.
3. Describe the change in words at each of the three levels.
4. The task is (original part, instruction). Check the result against the known edited part: volume within tolerance, key params, hole counts and positions.

## 3. Checks (automated)

**Every run:**

- all part files load
- no feature errors or warnings
- every sketch at 0 DOF
- valid solid
- expected number of parts

**Per task (`checks`):**

- param values, where the agent may name the param anything
- volume change relative to the starting part
- bounding-box limits
- hole counts by diameter range
- required feature types

**Parameter integrity** (from CADEngBench): rebuild the result with each param changed by ±10%. It must still build. This is the same sweep the reference parts already pass.

**To add:** for each check, a mutated part that must fail it (the CADTests method), so checks can't pass vacuously.

## 4. Quality review (engineer-likeness)

Automated checks can't see these, so they're reviewed from renders and the part file, by me now and later by an LLM judge with a fixed rubric:

1. **Structure a person can follow:** one design concept per sketch, named params for the sizes that matter, an intent on every feature, notes on references.
2. **Mechanical sense:** load path, fastener directions, clearances, manufacturability for the stated process.
3. **Faithful to the request:** did it make what was asked? Are its assumptions stated, and reasonable?
4. **Minimal edits:** for edit tasks, only what was needed changed.

## 5. Efficiency metrics (per run)

- wall time
- time to first tool call
- thinking time
- time writing tool arguments
- tool execution time
- turns, tool calls, rejected batches, renders
- output tokens, cost

All are recorded by `vibecad-bench` in `metrics.json`, with the full timeline in `events.jsonl`.

## 6. The improvement loop (the main point)

1. **Run** a slice of the suite (`vibecad-bench`).
2. **Read the traces** (`vibecad-trace <run dir>`): thinking summaries, each edit batch op by op, each result. Look for where time went and where the agent went wrong.
3. **Classify** each problem:

| Failure mode | Example seen | Typical fix |
|---|---|---|
| Over-planning | 5 min of thinking before the first tool call | Effort setting, guide, shortcuts |
| Interface friction | Sketch inserted after its extrude in one batch | Change the op semantics |
| API misuse | Expression used as a coordinate | Accept it, or a better error message |
| Frame / direction errors | Cut in the wrong direction | Zero-volume warning, frame in the report |
| Constraint errors | Tangent plus coincident flagged redundant | Point-tangent constraint, shortcuts |
| Reference errors | Face ref matches 0 or 2 faces | Label hints in the error, coplanar tolerance |
| Verification gaps | Reported success without checking | Guide checklist |
| Mechanical reasoning | Bolts claimed to clamp along the wrong axis | Mechanical-review section |
| Wasted steps | Rendering after every batch | Guide |

4. **Fix**, preferring in this order:
   1. a tool or op that makes the mistake impossible
   2. a better error message
   3. a guide heuristic
   4. a model or effort setting

   Code fixes are deterministic; prompt fixes are probabilistic.
5. **Re-run** the same tasks and compare (A/B). Keep changes that help; log everything in `docs/agent-efficiency.md`.

## 7. Models and settings

Test Haiku, Sonnet and Opus at effort low and medium on a fixed slice (easy create, medium create, parameter edit, functional edit). Pick defaults per task kind: a cheap model may be enough for parameter edits, and a stronger one worth it for new designs.

## 8. Budget and cadence

Runs use the Claude login's usage.

| Run | When | Size | Cost |
|---|---|---|---|
| Smoke | after any guide or tool change | 4 tasks × 1, low effort | ~$1 |
| Suite | weekly or before a release | all tasks × 3 repeats | ~$10–15 at low effort |
| Model matrix | after major changes | slice × 3 models × 2 efforts | ~$5 |

Single runs are noisy: judge changes on 3+ repeats before trusting a difference smaller than about 30%.
