"""Loops that touch along part of an edge (an L or T drawn as stacked rectangles) extrude as their union.

A benchmark agent drew a phone stand's side profile as three touching rectangles and got a 'crosses itself'
error; it spent minutes on a scratch part finding out why."""
from math import pi

import pytest

from vibecad import schema as S
from vibecad.ops import apply_ops
from vibecad.regen import Regenerator
from vibecad.topo import resolve_faces


def _build(ops, regions="all"):
    doc = S.Document.model_validate({"name": "t", "features": [{"id": "sk", "type": "sketch", "plane": {"datum": "XY"}}]})
    doc, _ = apply_ops(doc, ops + [{"op": "add_feature", "feature": {
        "id": "e", "type": "extrude", "profile": {"sketch": "sk", "regions": regions}, "distance": 10}}])
    return Regenerator().run(doc)


def _rect(rid, corner, w, h):
    return {"op": "add_rectangle", "sketch": "sk", "id": rid, "corner": corner, "width": w, "height": h}


def test_l_from_two_touching_rectangles():
    res = _build([_rect("base", [0, 0], 100, 5), _rect("back", [0, 5], 5, 60)])
    assert res.ok, [f.message for f in res.features]
    assert res.part.volume == pytest.approx((100 * 5 + 5 * 60) * 10, rel=1e-9)
    assert len(res.part.solids()) == 1
    # side faces keep their entity labels, so later features can refer to them
    assert resolve_faces(res.body, S.FaceRef(feature="e", role="side", entity="back_top"))


def test_t_of_three_rectangles_with_a_hole_in_the_base():
    res = _build([_rect("base", [0, 0], 100, 10), _rect("post", [45, 10], 10, 40), _rect("tab", [0, 10], 10, 5),
                  {"op": "add_circle", "sketch": "sk", "id": "hole", "diameter": 4, "center": [80, 5]}])
    assert res.ok, [f.message for f in res.features]
    area = 100 * 10 + 10 * 40 + 10 * 5 - pi * 4
    assert res.part.volume == pytest.approx(area * 10, rel=1e-6)
    assert len(res.part.solids()) == 1


def test_region_selection_picks_the_merged_outline():
    res = _build([_rect("base", [0, 0], 100, 5), _rect("back", [0, 5], 5, 60)], regions=["back_top"])
    assert res.ok, [f.message for f in res.features]
    assert res.part.volume == pytest.approx((100 * 5 + 5 * 60) * 10, rel=1e-9)


def test_crossing_loops_stay_separately_selectable():
    """A panel sunk into a slab (the loops cross, sharing no corner): each can still be extruded on its own."""
    ops = [_rect("slab", [0, 0], 60, 8), _rect("panel", [20, -5], 6, 50)]
    assert _build(ops, regions=["panel_top"]).part.volume == pytest.approx(6 * 50 * 10, rel=1e-9)
    assert _build(ops, regions=["slab_bottom"]).part.volume == pytest.approx(60 * 8 * 10, rel=1e-9)
    assert _build(ops).part.volume == pytest.approx((60 * 8 + 6 * 50 - 6 * 8) * 10, rel=1e-9)
