# VibeCAD IR reference (v0.1)

A part is one JSON file (`*.vcad.json`): named `params` plus an ordered list of `features`. Units are mm and degrees. Any numeric field marked *Num* takes a number or an expression over params: `"plate_t * 2"`, `"0.25 in"`, `"(width - hole_spacing) / 2"`, `"sqrt(2) * d"`. Examples: `examples/`. Param names are identifiers (`plate_t`, `m`), and can't be the expression functions or constants (`sqrt`, `sin`, `cos`, `tan`, `atan2`, `min`, `max`, `abs`, `round`, `floor`, `ceil`, `pi`).

## Conventions

- **Datum planes** (sketch x, sketch y, normal): `XY` = (+X, +Y, +Z); `XZ` = (+X, +Z, **−Y**); `YZ` = (+Y, +Z, +X). `offset` moves along the normal.
- **Face planes**: normal points out of the solid; origin is the world origin projected onto the plane. Axes are as seen looking at the face from outside: on vertical faces sketch y is world +Z (up) and x is to the right; on top/bottom faces sketch x is world +X. Check `get_sketch` → frame when unsure. Every build reports each sketch's world frame in `report.json` and on its sketch PNG.
- **Extrude direction**: `normal` goes along the plane normal, `reverse` goes against it, `symmetric` goes half each way. To cut into a face-sketched body, use `reverse`.
- **Sketch coordinates are initial guesses** (numbers or expressions). The solver (PlaneGCS) owns final positions. `vibecad build --write-solved` writes solved values back.

## Sketch

```json
{"id": "slot_sketch", "type": "sketch", "plane": {"datum": "XY", "offset": 0} | {"face": FaceRef, "offset": 0},
 "entities": [...], "constraints": [...]}
```

Entities (non-construction geometry forms profiles; set `"construction": true` for helpers):

| type | fields | sub-references |
|---|---|---|
| point | `at: [x, y]` (construction by default) | the point itself |
| line | `p1`, `p2` | `id.p1`, `id.p2` |
| circle | `center`, `r` | `id.center` |
| arc | `center`, `r`, `start_angle`, `end_angle` (CCW, degrees) | `id.center`, `id.start`, `id.end` |
| external | `edge: EdgeRef` naming one edge of the part built so far | as the projected line (`id.p1`, `id.p2`), circle or arc; a point if the edge is perpendicular to the plane |

Built-in references: `origin`, `x_axis`, `y_axis`. `external` entities are re-projected on every build: fixed construction geometry that follows the part, so constraints to them (a hole centred on a projected corner, a slot a set distance from a projected edge) stay attached when upstream params change. The edge ref must match exactly one edge (usually `between` two faces).

Constraints: `{"type": ..., "on": [refs], "value": Num, "name": "shown_to_user", "note": "..."}`

| type | on | value |
|---|---|---|
| coincident | point, point (or point, line) | |
| horizontal / vertical | line, or point, point | |
| parallel / perpendicular | line, line | |
| equal | line, line (length) or circle/arc pair (radius) | |
| tangent | curve, curve; or curve, curve, shared_point for endpoint tangency | |
| point_on | point, line / circle / arc | |
| midpoint | point, line | |
| symmetric | point, point, line or point | |
| concentric | circle/arc, circle/arc | |
| fix | point, with `at: [x, y]` | |
| distance | line (length); point, point; point, line | yes |
| distance_x / distance_y | point, point (signed: second minus first), or line | yes |
| radius / diameter | circle or arc | yes |
| angle | line, line (CCW from first to second), or line (from x_axis) | yes |

Rules: fully constrain every sketch (build reports DOF). Where a line meets an arc at an endpoint, use the 3-reference `tangent` with the shared point, never a 2-reference tangent plus coincident: that combination is degenerate and the solver flags it redundant.

## Features

| type | key fields |
|---|---|
| extrude | `profile {sketch, regions: "all" or [entity ids on a region's outer loop]}`, `distance`, `direction`, `extent: blind or through_all`, `mode: add / cut / intersect / new` |
| revolve | `profile`, `axis` (a sketch line id, `x_axis` or `y_axis`), `angle` |
| import | `file` (STEP / .brep, relative to the part file), `mode: reference / add / cut / new` (default reference), `rotate: [rx, ry, rz]` degrees about world X, then Y, then Z, then `at: [x, y, z]` |
| hole | `sketch` (holes at its point entities), `points` (optional subset), `kind: simple / counterbore / countersink / tapped`, `size` ("M3", "#4-40", "1/4-20"), `fit: close / normal / loose`, `diameter` (overrides the size), `depth` (omit: through all), `direction` (default `reverse`: into a face sketch's solid), `cbore_diameter`, `cbore_depth`, `csink_diameter`, `csink_angle`, `thread_depth` |
| fillet / chamfer | `edges: [EdgeRef]`, `radius` / `distance` |
| shell | `remove_faces: [FaceRef]`, `thickness` (inward) |
| linear_pattern | `features: [extrude/revolve ids]`, `direction: X, Y, Z or [x,y,z]`, `spacing`, `count` |
| circular_pattern | `features`, `axis`, `origin`, `count`, `angle` (360 = full circle, evenly spaced) |
| mirror | `features`, `plane: {datum, offset}` |

All features take `id`, optional `name`, `intent` (one line: why it exists), `suppressed`.

### Imported geometry (design around a bought part)

An `import` in `reference` mode brings a STEP file in as geometry you design around (a motor, a PCB, a tube): it is drawn and rendered in pale green, measured by `describe_import`, but is never part of the solid, so it adds no volume and exports nothing. Its faces are labelled in file order, `{"feature": "motor", "role": "face", "entity": "f4"}`: sketch on them (`plane: {"face": ...}`), and project their edges into a sketch with `external` entities so your geometry follows the part when the import moves.

```json
{"op": "add_feature", "feature": {"id": "motor", "type": "import", "file": "imports/nema17.step", "intent": "the stepper this plate carries"}},
{"op": "add_feature", "feature": {"id": "plate_sk", "type": "sketch", "plane": {"face": {"feature": "motor", "role": "face", "entity": "f4"}}}},
{"op": "add_entity", "sketch": "screw_pts", "entity": {"id": "mh1", "type": "external",
  "edge": {"between": [{"feature": "motor", "role": "face", "entity": "f4"}, {"feature": "motor", "role": "face", "entity": "f13"}]}}}
```

`describe_import` lists the largest flat faces (normal, centre, area) and every round face (diameter, axis, position), so the motor's pilot and screw holes can be found without guessing. Modes `add` / `cut` / `new` merge the file into the part instead (to modify a vendor part). Example: `examples/motor_plate.vcad.json`.

### Holes

Use a `hole` for every fastener hole, not a circle cut: it sizes the hole from the fastener, and says what it is for. Put the centres in a sketch on the face the holes go into (`add_points`), then one `hole` feature per hole type:

```json
{"op": "add_feature", "feature": {"id": "mount_pts", "type": "sketch", "plane": {"face": {"feature": "plate", "role": "end"}}}},
{"op": "add_points", "sketch": "mount_pts", "id": "m", "points": [["-hole_spacing / 2", 0], ["hole_spacing / 2", 0]]},
{"op": "add_feature", "feature": {"id": "mount_holes", "type": "hole", "sketch": "mount_pts", "kind": "counterbore", "size": "M4",
  "intent": "M4 socket heads sit flush"}}
```

- `simple` is a clearance hole for `size` (`fit` picks close / normal / loose, ISO 273), `tapped` is the tap drill (the thread is recorded, not modelled), `counterbore` fits a socket head cap screw flush, `countersink` a flat head (90° metric, 82° inch).
- Without `depth` the hole goes through; with it, it ends in a 118° drill point.
- Faces: `hole.side[wall]`, `[cbore_wall]`, `[cbore_floor]`, `[csink]`, `[tip]`, each with `instance` = the point id (`{"feature": "mount_holes", "role": "side", "entity": "wall", "instance": "m_1"}`).
- Holes can be patterned and mirrored like extrudes. Inch sizes: #2-56 to 1/2-13 (UNC, plus #10-32 and 1/4-28).

Metric sizes (mm):

| size | clearance (normal) | tap drill | c'bore ⌀ × depth | c'sink ⌀ (90°) |
|---|---|---|---|---|
| M1.6 | 1.8 | 1.25 | 3.3 × 1.8 | 3.2 |
| M2 | 2.4 | 1.6 | 4.4 × 2.3 | 4.4 |
| M2.5 | 2.9 | 2.05 | 5.5 × 2.8 | 5.5 |
| M3 | 3.4 | 2.5 | 6.5 × 3.3 | 6.7 |
| M4 | 4.5 | 3.3 | 8 × 4.4 | 9 |
| M5 | 5.5 | 4.2 | 10 × 5.4 | 11.2 |
| M6 | 6.6 | 5 | 11 × 6.5 | 13.4 |
| M8 | 9 | 6.8 | 15 × 8.6 | 17.9 |
| M10 | 11 | 8.5 | 18 × 10.8 | 22.4 |
| M12 | 13.5 | 10.2 | 20 × 13 | 26.9 |
| M16 | 17.5 | 14 | 26 × 17.5 | 33.6 |
| M20 | 22 | 17.5 | 33 × 21.5 | 40.3 |

## References to faces and edges

Faces are labeled by the feature that made them. Labels survive regeneration because they are carried through OpenCascade's own operation history. They are never indices.

```json
FaceRef: {"feature": "wall", "role": "side", "entity": "wall_top", "instance": null, "pick": "all", "note": "top of the wall"}
EdgeRef: {"between": [FaceRef, FaceRef], "note": "..."}
       | {"of": FaceRef, "filter": {"type": "line", "parallel_to": "Z"}, "note": "..."}
```

- Roles: extrude/revolve `start` (the cap on the sketch plane), `end` (the far cap), `side` (swept from `entity`); fillet `fillet`; chamfer `chamfer`; shell `inner`; `new` for faces with no better origin.
- `instance`: omitted = the original only; `"*"` = original and all copies; `"<pattern_id>#<n>"` = one copy.
- `pick`: `largest`, `smallest`, or `nearest` (with `near: [x, y, z]`) when one face is required.
- EdgeRef `pick: "nearest"` with `near: [x, y, z]` keeps the one edge closest to that point, for two faces that meet along more than one edge.
- Always write a `note` saying in plain words what the reference means, so a later reader can repair it if it breaks.
- `report.json` → `face_labels` lists every label present on the final body.

## Edit ops

Parts are edited through ops, applied in batches as one undoable transaction (`apply_ops` in the MCP server). If any op fails or the result doesn't validate, nothing changes.

```
{"op": "set_param", "name": "width", "value": "80 mm"}
{"op": "remove_param", "name": "width"}
{"op": "set_meta", "set": {"design_notes": "...", "material": "...", "process": "...", "name": "..."}}
{"op": "add_feature", "feature": {...}, "after": "<feature id>"}          (or "before"; default: append)
{"op": "update_feature", "id": "<feature id>", "set": {"distance": "8 mm", "direction": "reverse"}}   (null deletes a field)
{"op": "remove_feature", "id": "<feature id>"}
{"op": "move_feature", "id": "<feature id>", "after": "<feature id>"}
{"op": "add_entity", "sketch": "<id>", "entity": {...}}
{"op": "update_entity", "sketch": "<id>", "id": "<entity id>", "set": {...}}
{"op": "remove_entity", "sketch": "<id>", "id": "<entity id>"}           (also removes constraints using it)
{"op": "add_constraint", "sketch": "<id>", "constraint": {...}}
{"op": "update_constraint", "sketch": "<id>", "match": {"id" | "name" | "index": ...}, "set": {...}}
{"op": "remove_constraint", "sketch": "<id>", "match": {"id" | "name" | "index": ...}}
{"op": "set_dimension", "sketch": "<id>", "name": "<dimension name>", "value": "12 mm"}
{"op": "rename_feature", "id": "<feature id>", "to": "<new id>"}          (updates profiles, face/edge refs, pattern lists)
{"op": "rename_entity", "sketch": "<id>", "id": "<entity id>", "to": "<new id>"}   (updates constraints, regions, face refs)
```

Sketch shortcuts (expand into fully constrained primitives, stored as ordinary entities and constraints):

```
{"op": "add_rectangle", "sketch": "<id>", "id": "plate", "width": "base_w", "height": "depth", "center": [0, 0]}
      (or "corner": [x, y] for the bottom-left corner) -> lines plate_bottom/right/top/left, dims plate_width, plate_height
{"op": "add_circle", "sketch": "<id>", "id": "bore", "diameter": "bore_d", "center": [0, "shaft_h"]}   -> dim bore_diameter
{"op": "add_polygon", "sketch": "<id>", "id": "l", "points": [[0, 0], ["leg", 0], ["leg", "t"], ["t", "t"], ["t", "leg"], [0, "leg"]],
 "names": ["bottom", "outer_right", "inner_h", "inner_v", "top", "outer_left"]}
      closed polygon (or "closed": false for a polyline); every vertex fixed at its (expression) coordinates -> 0 DOF
{"op": "add_slot", "sketch": "<id>", "id": "slot", "length": "slot_len", "width": "slot_w", "center": [x, y], "angle": 90}
      length is center-to-center; angle of the slot axis from sketch +x
      -> arcs slot_end1/2, lines slot_side1/2, construction slot_axis + point slot_mid; dims slot_width, slot_length
{"op": "add_regular_polygon", "sketch": "<id>", "id": "hex", "sides": 6, "diameter": "hex_af",
 "across": "flats", "center": [0, 0], "angle": 0}
      across: "corners" (default, circumscribed diameter) or "flats" (wrench size); angle rotates the
      first vertex from sketch +x -> one line per side, every vertex fixed by expression (center + R*cos/sin)
{"op": "add_points", "sketch": "<id>", "id": "m", "points": [[x, y], ...], "names": [...]}
      hole centres: points m (one) or m_1, m_2, ... each fixed by dims m_1_x, m_1_y from the origin
```

Positions are measured from the sketch origin and accept expressions. Omit `center`/`corner` to leave the shape free to position with your own constraints.

`set_dimension` on a dimension whose value is a bare param name changes that param, so every other use of it stays consistent.

The report after each batch has `ok`, `errors`, `warnings` (such as an add or cut that changed no volume), `underconstrained_sketches`, `notes`, and `change` (volume and bbox before → after). `ok` is false if there are errors or warnings. When a batch changes param values, `notes` lists the intents (and design notes) that quote numbers and depend on those params, so they can be updated in the next batch.
