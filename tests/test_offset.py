"""Sketch offsets: derived, fixed copies of a chain of lines and arcs."""
from math import pi

import pytest

from vibecad import schema as S
from vibecad.ops import apply_ops
from vibecad.regen import Regenerator


def _doc(extra_ops, params=None, extrude="all"):
    doc = S.Document(name="t", params=params or {"gap": 2})
    doc, _ = apply_ops(doc, [
        {"op": "add_feature", "feature": {"id": "sk", "type": "sketch", "plane": {"datum": "XY"}}},
        {"op": "add_rectangle", "sketch": "sk", "id": "r", "width": 40, "height": 20, "center": [0, 0]},
        *extra_ops,
        {"op": "add_feature", "feature": {"id": "ex", "type": "extrude", "profile": {"sketch": "sk", "regions": extrude}, "distance": 1}},
    ])
    return doc


def test_outside_offset_of_a_rectangle_is_mitred_and_adds_no_dof():
    doc = _doc([{"op": "add_entity", "sketch": "sk", "entity": {"id": "o", "type": "offset", "of": ["r_bottom", "r_right", "r_top", "r_left"], "distance": "gap"}}])
    res = Regenerator().run(doc)
    assert res.ok, [(f.id, f.message) for f in res.features]
    sk = res.sketches["sk"][0]
    assert sk.report.dof == 0 and {f"o_{i}" for i in range(1, 5)} <= set(sk.entities)
    assert res.part.volume == pytest.approx(44 * 24 - 40 * 20)  # the ring between the offset and the rectangle
    doc, _ = apply_ops(doc, [{"op": "set_param", "name": "gap", "value": 3}])
    assert Regenerator().run(doc).part.volume == pytest.approx(46 * 26 - 40 * 20)


def test_inside_offset_of_a_rounded_outline_keeps_the_arcs_concentric():
    ops = [{"op": "fillet_corner", "sketch": "sk", "corner": c, "radius": 5} for c in ("r_bottom.p2", "r_right.p2", "r_top.p2", "r_left.p2")]
    ids = ["r_bottom", "r_right", "r_top", "r_left", "r_bottom_r_right_round", "r_right_r_top_round", "r_top_r_left_round", "r_left_r_bottom_round"]
    doc = _doc(ops + [{"op": "add_entity", "sketch": "sk", "entity": {"id": "o", "type": "offset", "of": ids, "distance": 2, "side": "inside"}}])
    res = Regenerator().run(doc)
    assert res.ok, [(f.id, f.message) for f in res.features]
    arcs = [e for e in res.sketches["sk"][0].entities.values() if e.id.startswith("o_") and e.type == "arc"]
    assert len(arcs) == 4 and all(abs(a.r - 3) < 1e-9 for a in arcs)
    outer = 40 * 20 - 4 * 25 * (1 - pi / 4)
    inner = 36 * 16 - 4 * 9 * (1 - pi / 4)
    assert res.part.volume == pytest.approx(outer - inner, rel=1e-6)


def test_offset_errors():
    bad = _doc([{"op": "add_entity", "sketch": "sk", "entity": {"id": "o", "type": "offset", "of": ["r_bottom", "r_top"], "distance": 1}}])
    res = Regenerator().run(bad)
    assert not res.ok and "don't join" in res.features[0].message
    open_side = _doc([{"op": "add_entity", "sketch": "sk", "entity": {"id": "o", "type": "offset", "of": ["r_bottom", "r_right"], "distance": 1,
                                                                       "construction": True}}])
    res = Regenerator().run(open_side)
    assert not res.ok and "closed loop" in res.features[0].message
    too_big = _doc([{"op": "add_entity", "sketch": "sk", "entity": {"id": "o", "type": "offset", "of": ["r_bottom", "r_right", "r_top", "r_left"],
                                                                     "distance": 15, "side": "inside"}}])
    res = Regenerator().run(too_big)
    assert not res.ok and "offset" in res.features[0].message


def test_open_chain_offset_left_as_construction_and_circle():
    doc = _doc([
        {"op": "add_entity", "sketch": "sk", "entity": {"id": "o", "type": "offset", "of": ["r_bottom", "r_right"], "distance": 1, "side": "left", "construction": True}},
        {"op": "add_circle", "sketch": "sk", "id": "c", "diameter": 4, "center": [0, 0]},
        {"op": "add_entity", "sketch": "sk", "entity": {"id": "oc", "type": "offset", "of": ["c"], "distance": 1}},
    ])
    res = Regenerator().run(doc)
    assert res.ok, [(f.id, f.message) for f in res.features]
    e = res.sketches["sk"][0].entities
    assert e["o_1"].construction and abs(e["oc_1"].r - 3) < 1e-9
    # the bottom goes left to right; its left side is inward (y up): y = -10 + 1
    assert abs(e["o_1"].p1[1] - (-9)) < 1e-9
    # the rectangle with a ring hole cut by the circle offsets: the circle (r 2) is an island in the ring (r 3)
    assert res.part.volume == pytest.approx(40 * 20 - pi * 9 + pi * 4, rel=1e-6)


def test_offset_of_projected_edges_follows_the_part(tmp_path):
    """A lip 2 mm outside a plate's top outline: the offset copies projected edges and follows the plate."""
    from vibecad.app import App
    import shutil
    from pathlib import Path
    ex = Path(__file__).resolve().parent.parent / "examples"
    shutil.copy(ex / "l_bracket.vcad.json", tmp_path / "b.vcad.json")
    a = App(tmp_path, "sonnet")
    a.ws.open_part("b.vcad.json")
    a.ws.apply_ops([{"op": "add_feature", "feature": {"id": "lip_sk", "type": "sketch", "plane": {"face": {"feature": "base", "role": "start"}}}}], "sk", "user")
    out = a.face_outline("lip_sk")
    # the outer loop only (the bottom face also has the two slots' outlines)
    ids = [e["id"] for e in out["entities"] if e["edge"]["between"][1]["feature"] == "base" and e["edge"]["between"][1]["role"] == "side"]
    assert len(ids) == 4
    rep = a.ws.apply_ops([{"op": "add_entity", "sketch": "lip_sk", "entity": e} for e in out["entities"]]
                         + [{"op": "add_entity", "sketch": "lip_sk", "entity": {"id": "lip", "type": "offset", "of": ids, "distance": 2}},
                            {"op": "add_feature", "feature": {"id": "lip_ex", "type": "extrude", "profile": {"sketch": "lip_sk"}, "distance": 1, "mode": "new"}}],
                         "lip", "user")
    import json
    rep = json.loads(rep)
    assert rep["ok"], rep
    res = a.ws.session().result
    assert res.sketches["lip_sk"][0].report.dof == 0
    bb = res.part.bounding_box()
    assert round(bb.min.X, 6) == -2 and round(bb.max.X, 6) == 62  # 60 wide base, 2 mm each side
    a.ws.apply_ops([{"op": "set_param", "name": "width", "value": 80}], "wider", "user")
    assert round(a.ws.session().result.part.bounding_box().max.X, 6) == 82
    # renaming a copied entity updates the offset; removing it is refused
    rep = json.loads(a.ws.apply_ops([{"op": "rename_entity", "sketch": "lip_sk", "id": ids[0], "to": "first_edge"}], "rename", "user"))
    assert rep["ok"] and "first_edge" in a.ws.session().doc.feature("lip_sk").entities[-1].of
    rep = json.loads(a.ws.apply_ops([{"op": "remove_entity", "sketch": "lip_sk", "id": "first_edge"}], "rm", "user"))
    assert not rep["ok"] and "copied by offset" in rep["error"]


def test_the_gasket_follows_the_lid(tmp_path):
    """examples/lid_gasket imports enclosure_lid live and offsets its projected rim: widen the lid, the gasket follows."""
    import json
    import shutil
    from math import pi
    from pathlib import Path

    from vibecad.regen import load
    ex = Path(__file__).resolve().parent.parent / "examples"
    for f in ("enclosure_lid.vcad.json", "lid_gasket.vcad.json"):
        shutil.copy(ex / f, tmp_path / f)
    lid = json.loads((tmp_path / "enclosure_lid.vcad.json").read_text())
    lid["params"]["width"] = 100
    (tmp_path / "enclosure_lid.vcad.json").write_text(json.dumps(lid))
    res = Regenerator(tmp_path).run(load(tmp_path / "lid_gasket.vcad.json"))
    assert res.ok, [(f.id, f.message) for f in res.features]
    c = lambda r: r * r * (1 - pi / 4)
    outer, inner = 99 * 49 - 4 * c(4.5), 97 * 47 - 4 * c(3.5)
    assert res.part.volume == pytest.approx((outer - inner) * 1.5, rel=1e-6)
