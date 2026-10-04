"""mirror_entities: mirrored sketch copies held by symmetric constraints, adding no degrees of freedom."""
from math import pi

import pytest

from vibecad import schema as S
from vibecad.ops import OpError, apply_ops
from vibecad.regen import Regenerator


def _build(ops, extrude=True):
    doc = S.Document.model_validate({"name": "t", "params": {"t": 4}, "features": [{"id": "sk", "type": "sketch", "plane": {"datum": "XY"}}]})
    doc, notes = apply_ops(doc, ops)
    if extrude:
        doc, _ = apply_ops(doc, [{"op": "add_feature", "feature": {"id": "e", "type": "extrude", "profile": {"sketch": "sk"}, "distance": "t"}}])
    return doc, Regenerator().run(doc), notes


def test_half_tee_mirrored_across_y_axis_closes_the_profile():
    half = [(0, 0), (20, 0), (20, 5), (5, 5), (5, 30), (0, 30)]  # the right half of a T, ends on the y axis
    doc, res, notes = _build([{"op": "add_polygon", "sketch": "sk", "id": "half", "closed": False, "points": half},
                              {"op": "mirror_entities", "sketch": "sk", "entities": [f"half_{i}" for i in range(1, 6)], "axis": "y_axis"}])
    assert res.ok, [f.message for f in res.features]
    assert res.features[0].info["dof"] == 0
    half_area = 20 * 5 + 5 * 25
    assert res.part.volume == pytest.approx(2 * half_area * 4, rel=1e-9)
    assert "half_1_mirror" in notes[-1]
    sk = doc.feature("sk")
    # the ends on the axis join the copies with coincident constraints; the rest are symmetric
    kinds = [c.type for c in sk.constraints if any("_mirror" in r for r in c.on)]
    assert kinds.count("coincident") == 2 and kinds.count("symmetric") == 8


def test_mirror_follows_the_original_and_handles_arcs_circles_and_a_line_axis():
    ops = [{"op": "add_entity", "sketch": "sk", "entity": {"id": "axis", "type": "line", "p1": [0, -10], "p2": [10, 0], "construction": True}},
           {"op": "add_constraint", "sketch": "sk", "constraint": {"type": "fix", "on": ["axis.p1"], "at": [0, -10]}},
           {"op": "add_constraint", "sketch": "sk", "constraint": {"type": "fix", "on": ["axis.p2"], "at": [10, 0]}},
           {"op": "add_circle", "sketch": "sk", "id": "hole", "diameter": 6, "center": [0, 5]},
           {"op": "add_entity", "sketch": "sk", "entity": {"id": "hook", "type": "arc", "center": [-10, 10], "r": 4, "start_angle": 0, "end_angle": 90,
                                                          "construction": True}},
           {"op": "add_constraint", "sketch": "sk", "constraint": {"type": "fix", "on": ["hook.center"], "at": [-10, 10]}},
           {"op": "add_constraint", "sketch": "sk", "constraint": {"type": "radius", "on": ["hook"], "value": 4}},
           {"op": "add_constraint", "sketch": "sk", "constraint": {"type": "fix", "on": ["hook.start"], "at": [-6, 10]}},
           {"op": "add_constraint", "sketch": "sk", "constraint": {"type": "fix", "on": ["hook.end"], "at": [-10, 14]}},
           {"op": "mirror_entities", "sketch": "sk", "entities": ["hole", "hook"], "axis": "axis"}]
    doc, res, _ = _build(ops, extrude=False)
    assert res.ok and res.features[0].info["dof"] == 0, res.features[0].info
    solved = res.sketches["sk"][0].entities
    # the axis is y = x - 10: (x, y) reflects to (y + 10, x - 10)
    assert solved["hole_mirror"].center == pytest.approx((15, -10), abs=1e-6)
    assert solved["hole_mirror"].r == pytest.approx(3)
    assert solved["hook_mirror"].center == pytest.approx((20, -20), abs=1e-6)
    assert solved["hook_mirror"].r == pytest.approx(4)
    # growing the original moves the copy
    doc, _ = apply_ops(doc, [{"op": "set_dimension", "sketch": "sk", "name": "hole_diameter", "value": 10}])
    res = Regenerator().run(doc)
    assert res.sketches["sk"][0].entities["hole_mirror"].r == pytest.approx(5)


def test_mirror_errors():
    base = [{"op": "add_circle", "sketch": "sk", "id": "c", "diameter": 4, "center": [5, 5]}]
    for bad, msg in [({"axis": "nope", "entities": ["c"]}, "mirror axis"), ({"axis": "x_axis", "entities": ["zz"]}, "no entity"),
                     ({"axis": "x_axis", "entities": []}, "needs entities")]:
        with pytest.raises(OpError, match=msg):
            _build(base + [{"op": "mirror_entities", "sketch": "sk", **bad}])
    # mirroring twice names the second copy distinctly
    doc, res, notes = _build(base + [{"op": "mirror_entities", "sketch": "sk", "entities": ["c"], "axis": "x_axis"},
                                     {"op": "mirror_entities", "sketch": "sk", "entities": ["c"], "axis": "y_axis"}], extrude=False)
    assert "c_mirror2" in notes[-1] and res.ok
