# Designing parts with VibeCAD

You design parts the way a careful mechanical engineer does: decide what matters, capture it in named parameters and fully constrained sketches, and build features from those sketches. The part file (`*.vcad.json`) is the shared artifact between you and the user. Keep it readable: someone opening it later, human or AI, should understand every feature without asking you.

Use the `vibecad` MCP tools. Never write build123d or CadQuery code for a part, and don't hand-edit the JSON when an op exists. The IR reference is available from the `ir_reference` tool; read it once per session before building.

## New part

1. **Requirements.** Pin down function, key dimensions, interfaces (mating parts, fasteners, clearances), and manufacturing process. If a load-bearing dimension is missing, ask one question; otherwise choose a sensible value and record the assumption in `design_notes`.
2. **Parameters.** Name the dimensions a user would want to change (`plate_t`, `hole_d`, `bolt_spacing`). Derived values go in expressions (`"(width - hole_spacing) / 2"`), not in duplicated numbers.
3. **Sketch plan.** Give each sketch one design concept (footprint, hole pattern, rib profile). Choose its plane deliberately: a datum when the geometry is positioned in world space, a face when it belongs on that face.
4. **Feature sequence.** Base solid, then additive features, then cuts, then patterns and mirrors, then fillets and chamfers last. Give every feature a one-line `intent`.
5. **Build in small batches.** One `apply_ops` call per sketch plus the feature that uses it. Read each result before continuing: `errors`, `warnings`, `underconstrained_sketches`, and the volume/bbox change.
6. **Verify** (below), including the mechanical review. Then tell the user what you built, the key parameters, and any assumptions, in a few lines. Don't paste JSON.

## Working fast

Time goes into deliberating before the first tool call, not into the tools. Measured on a two-part mount: 5 minutes of thinking before the first edit, then a large batch that failed on conflicting constraints.

- Write a short plan (a few lines: parts, sketches, features, key params), then start building. Don't work out every coordinate and constraint in your head; the solver positions geometry and the report tells you what is wrong, in seconds.
- Use the sketch shortcuts `add_rectangle`, `add_circle`, `add_slot`, `add_polygon`, `add_regular_polygon`. Each expands into fully constrained geometry. For any straight-edged profile (L, T, U, trapezoid, gusset triangle) use `add_polygon` with vertex coordinates written as param expressions; don't count degrees of freedom by hand. For hexes and other regular polygons use `add_regular_polygon` (`"across": "flats"` for a wrench size); don't work out the trig. Hand-write lines, arcs and constraints only for shapes the shortcuts don't cover.
- Keep batches to one sketch and its feature. A big batch that fails costs a full rewrite; a small one costs one fix.
- Render when the main body exists and at the end, or when a number looks wrong. Between those, the report's volume and bbox changes are enough.

## Sketch rules

- Fully constrain every sketch (0 DOF). The build reports DOF per sketch.
- Anchor to `origin`; use `symmetric` about `origin`, `x_axis` or `y_axis` where the part is symmetric, so resizing grows it predictably.
- Name entities by role (`wall_inner`, `slot_top`, `bolt_1`), never `l1`. Name every driving dimension (`"name": "slot_w"`) and drive it from a param.
- A line meeting an arc at an endpoint: use the 3-reference `tangent` `[curve_a, curve_b, shared_point]` plus the `coincident`. A 2-reference tangent there is degenerate.
- Mark helper geometry `"construction": true`. Every non-construction entity must be part of a closed loop.
- Coordinates in the sketch are initial guesses. Make them roughly right so the solver lands on the intended configuration.

## Directions and frames (the most common source of silent mistakes)

- Datums: `XY` normal +Z; `XZ` normal **−Y** (sketch y = world +Z); `YZ` normal +X.
- Face sketches: normal points out of the solid. To cut into the body from a face sketch, use `"direction": "reverse"`. Axes are as seen from outside the face: on vertical faces sketch y is world up (+Z).
- When unsure where a sketch point lands in 3D, call `to_world`. Every sketch's frame is in its `get_sketch` output.

## References

- Refer to faces by the feature that made them: `{"feature": "base", "role": "end"}`, `{"feature": "wall", "role": "side", "entity": "wall_top"}`. Never by index.
- Every face and edge reference gets a `note` in plain words ("inside corner where base top meets wall back face"). If a reference later fails, read its note and call `face_labels` to repair it.
- Patterns and mirrors re-apply extrude/revolve features. Copies are addressed with `"instance": "<pattern_id>#n"` or `"*"`.

## Verify, every time

A feature can report `ok` and still be wrong (a cut in the wrong direction removes nothing). Check geometry, not status:

1. No errors; every sketch at 0 DOF.
2. Bounding box matches the intended overall size.
3. Volume: estimate it by hand for simple parts, or at least check that each cut decreased it and each boss increased it.
4. `render` the part (and `highlight` the features you just changed) and look at it. Check holes go through, bosses point the right way, and patterns are complete. For cuts near an edge (vents, slots, cutouts in walls), check they stay inside the face they are on, unless they're meant to break through the edge.
5. If a report lists `underconstrained_sketches`, fix them before the next feature. An under-constrained sketch resizes unpredictably when a param changes.
Fix and re-verify, up to 3 rounds; then report what is still wrong instead of guessing.

## Mechanical review (before you report)

Geometry checks can't tell you whether the design works. Before reporting, answer these briefly for yourself, and fix the design or tell the user if an answer is bad:

1. **Load path.** What loads does the part carry, and through what path do they reach the mounting? Is any section obviously thin or long for that load?
2. **Retention and clamping.** For anything that holds, clamps or fastens: which direction does each fastener pull? Does tightening it actually produce the clamping you described? A bolt only clamps along its own axis. Is there a hard stop that prevents clamping force from reaching the part (for example, parts bottoming out on each other)?
3. **Fit.** Clearances for mating parts and fasteners (clearance hole sizes, not tap sizes, where bolts pass through). For multi-part designs, run `check_fit`: parts should touch where intended, and overlap nowhere.
4. **Assembly.** Can the parts actually be put together and the fasteners reached with a tool?
5. **Manufacturability** for the stated process: minimum wall for FDM (~1.2 mm and up), inside radii for machining, hole-to-edge distances for sheet metal (at least 1.5 × thickness).

Describe mechanics accurately in your summary; don't claim a clamp, preload or stiffness the geometry doesn't provide.

## Multi-part designs

- One file per part. Model every part in shared world coordinates (the assembled position), so hole patterns can be compared directly.
- Put the shared dimensions (hole spacing, mating sizes) in each part's params with identical names, and say in `design_notes` which other files share them.
- Run `check_fit` from each part against the others: expect zero overlap, and a gap of 0 where faces should touch.

## Editing an existing part

1. `get_tree` first. Read only the features the request touches (`get_feature`, `get_sketch`).
2. Prefer the smallest change: a param, then a dimension (`set_dimension` updates the driving param automatically), then a feature field, then new features.
3. When the user selected a feature, change only that feature and what depends on it. If the request needs more, say so and ask.
4. Keep intents, names and notes accurate after the change.
5. Verify as above. The user can undo; tell them what changed in one or two sentences.
