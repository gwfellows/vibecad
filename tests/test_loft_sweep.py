"""Loft through sketch sections, and sweep a profile along a sketch path."""
from math import pi, sqrt

import pytest

from vibecad import schema as S
from vibecad.regen import Regenerator
from test_extrude_extras import _circle, _rect


def _run(feats):
    return Regenerator().run(S.Document.model_validate({"name": "t", "features": feats}))


def _ok(res):
    assert res.ok, [(f.id, f.message) for f in res.features if f.status == "error"]
    return res


def _path(sid, pts, plane=None, arcs=()):
    """A polyline path through pts (sketch coords), each segment fixed; arcs: (id, center, r, a0, a1)."""
    ents, cons = [], []
    for k, (a, b) in enumerate(zip(pts, pts[1:])):
        ents.append({"id": f"seg{k}", "type": "line", "p1": list(a), "p2": list(b)})
        cons += [{"type": "fix", "on": [f"seg{k}.p1"], "at": list(a)}, {"type": "fix", "on": [f"seg{k}.p2"], "at": list(b)}]
    for aid, c, r, a0, a1 in arcs:
        ents.append({"id": aid, "type": "arc", "center": list(c), "r": r, "start_angle": a0, "end_angle": a1})
        cons += [{"type": "fix", "on": [f"{aid}.center"], "at": list(c)}, {"type": "radius", "on": [aid], "value": r}]
    return {"id": sid, "type": "sketch", "plane": plane or {"datum": "XZ"}, "entities": ents, "constraints": cons}


def test_ruled_loft_square_to_small_square():
    res = _ok(_run([_rect("a", 20, 20), _rect("b", 10, 10, {"datum": "XY", "offset": 30}),
                    {"id": "l", "type": "loft", "sections": ["a", "b"], "ruled": True}]))
    assert res.part.volume == pytest.approx(30 / 3 * (400 + 100 + sqrt(400 * 100)), rel=1e-6)
    labels = {str(l) for _, l in res.body.labels}
    assert {"l.start", "l.end", "l.side[b]", "l.side[r]", "l.side[t]", "l.side[l]"} <= labels, labels


def test_loft_circle_to_circle_is_a_frustum_and_square_to_round_adapter():
    res = _ok(_run([_circle("a", 10), _circle("b", 5, {"datum": "XY", "offset": 30}),
                    {"id": "l", "type": "loft", "sections": ["a", "b"]}]))
    assert res.part.volume == pytest.approx(pi * 30 / 3 * (100 + 25 + 50), rel=1e-5)
    # a square duct to a round one: between the two prisms' volumes
    res = _ok(_run([_rect("a", 20, 20), _circle("b", 8, {"datum": "XY", "offset": 25}),
                    {"id": "l", "type": "loft", "sections": ["a", "b"]}]))
    v = res.part.volume
    assert pi * 64 * 25 < v < 400 * 25 and res.part.bounding_box().size.Z == pytest.approx(25)


def test_three_section_smooth_loft_and_errors():
    feats = [_circle("a", 10), _circle("m", 4, {"datum": "XY", "offset": 20}), _circle("b", 10, {"datum": "XY", "offset": 40})]
    smooth = _ok(_run(feats + [{"id": "l", "type": "loft", "sections": ["a", "m", "b"]}])).part.volume
    ruled = _ok(_run(feats + [{"id": "l", "type": "loft", "sections": ["a", "m", "b"], "ruled": True}])).part.volume
    cone = pi * 20 / 3 * (100 + 16 + 40)
    assert ruled == pytest.approx(2 * cone, rel=1e-5) and smooth != pytest.approx(ruled, rel=1e-3)
    bad = _run([_circle("a", 10), _circle("b", 5), {"id": "l", "type": "loft", "sections": ["a", "b"]}])
    assert not bad.ok and "same plane" in bad.features[-1].message
    with pytest.raises(Exception):
        S.Loft.model_validate({"id": "l", "sections": ["a"]})


def test_sweep_round_bar_with_a_mitred_corner():
    res = _ok(_run([_circle("prof", 3), _path("path", [(0, 0), (0, 20), (20, 20)]),
                    {"id": "bar", "type": "sweep", "profile": {"sketch": "prof"}, "path": "path"}]))
    assert res.features[-1].info["path_length"] == pytest.approx(40)
    assert res.part.volume == pytest.approx(pi * 9 * 40, rel=1e-5)
    bb = res.part.bounding_box()
    assert bb.max.Z == pytest.approx(23, abs=1e-4) and bb.max.X == pytest.approx(20, abs=1e-4)
    labels = {str(l) for _, l in res.body.labels}
    assert {"bar.start", "bar.end", "bar.side[c]"} <= labels, labels


def test_sweep_tube_along_a_bend():
    ring = {"id": "prof", "type": "sketch", "plane": {"datum": "XY"}, "entities": [
        {"id": "od", "type": "circle", "center": [0, 0], "r": 3}, {"id": "id", "type": "circle", "center": [0, 0], "r": 2}],
        "constraints": [{"type": "fix", "on": ["od.center"], "at": [0, 0]}, {"type": "concentric", "on": ["od", "id"]},
                        {"type": "radius", "on": ["od"], "value": 3}, {"type": "radius", "on": ["id"], "value": 2}]}
    # up 20, then a quarter bend of radius 10 toward +X (the arc is CCW from 90 to 180 degrees about (10, 20))
    path = _path("path", [(0, 0), (0, 20)], arcs=[("bend", (10, 20), 10, 90, 180)])
    res = _ok(_run([ring, path, {"id": "tube", "type": "sweep", "profile": {"sketch": "prof"}, "path": "path"}]))
    L = 20 + pi * 10 / 2
    assert res.features[-1].info["path_length"] == pytest.approx(L)
    assert res.part.volume == pytest.approx(pi * (9 - 4) * L, rel=1e-4)  # Pappus: the ring's centroid is on the path
    labels = {str(l) for _, l in res.body.labels}
    assert {"tube.side[od]", "tube.side[id]", "tube.start", "tube.end"} <= labels, labels


def test_sweep_errors():
    res = _run([_circle("prof", 3, {"datum": "XZ"}), _path("path", [(0, 0), (20, 0)]),
                {"id": "s", "type": "sweep", "profile": {"sketch": "prof"}, "path": "path"}])
    assert not res.ok and "along the path" in res.features[-1].message
    res = _run([_circle("prof", 3), _path("path", [(0, 0), (0, 20)], arcs=[("far", (50, 50), 5, 0, 90)]),
                {"id": "s", "type": "sweep", "profile": {"sketch": "prof"}, "path": "path"}])
    assert not res.ok and "separate chains" in res.features[-1].message


def test_renames_follow_loft_sections_sweep_paths_and_hole_sketches():
    from vibecad.ops import apply_ops
    doc = S.Document.model_validate({"name": "t", "features": [
        _rect("a", 20, 20), _rect("b", 10, 10, {"datum": "XY", "offset": 30}),
        {"id": "l", "type": "loft", "sections": ["a", "b"]},
        _circle("prof", 3), _path("path", [(0, 0), (0, 20)]),
        {"id": "s", "type": "sweep", "profile": {"sketch": "prof"}, "path": "path"},
        {"id": "h", "type": "hole", "sketch": "prof", "diameter": 1},
        {"id": "f", "type": "fillet", "edges": [{"between": [{"feature": "l", "role": "side", "entity": "b"},
                                                             {"feature": "l", "role": "start"}]}], "radius": 1}]})
    doc, _ = apply_ops(doc, [{"op": "rename_feature", "id": "a", "to": "base_sec"},
                             {"op": "rename_feature", "id": "path", "to": "rail"},
                             {"op": "rename_feature", "id": "prof", "to": "disc"},
                             {"op": "rename_entity", "sketch": "base_sec", "id": "b", "to": "bottom"}])
    assert doc.feature("l").sections == ["base_sec", "b"]
    assert doc.feature("s").path == "rail" and doc.feature("s").profile.sketch == "disc"
    assert doc.feature("h").sketch == "disc"
    assert doc.feature("f").edges[0].between[0].entity == "bottom"
