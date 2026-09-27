"""The hole feature: simple, counterbore, countersink, blind with a drill point, tapped; labels and patterns."""
from math import pi, tan

import pytest

from vibecad import schema as S
from vibecad.regen import Regenerator


def _plate(extra, w=40, d=30, t=10):
    feats = [
        {"id": "sk", "type": "sketch", "plane": {"datum": "XY"}, "entities": [
            {"id": "b", "type": "line", "p1": [0, 0], "p2": [w, 0]}, {"id": "r", "type": "line", "p1": [w, 0], "p2": [w, d]},
            {"id": "t", "type": "line", "p1": [w, d], "p2": [0, d]}, {"id": "l", "type": "line", "p1": [0, d], "p2": [0, 0]}],
         "constraints": [{"type": "fix", "on": [p], "at": a} for p, a in
                         (("b.p1", [0, 0]), ("b.p2", [w, 0]), ("r.p2", [w, d]), ("t.p2", [0, d]))]
         + [{"type": "coincident", "on": ["b.p2", "r.p1"]}, {"type": "coincident", "on": ["r.p2", "t.p1"]},
            {"type": "coincident", "on": ["t.p2", "l.p1"]}, {"type": "coincident", "on": ["l.p2", "b.p1"]}]},
        {"id": "plate", "type": "extrude", "profile": {"sketch": "sk"}, "distance": t},
        {"id": "pts", "type": "sketch", "plane": {"face": {"feature": "plate", "role": "end"}}, "entities": [
            {"id": "p1", "type": "point", "at": [10, 10]}, {"id": "p2", "type": "point", "at": [30, 10]},
            {"id": "c", "type": "circle", "center": [20, 20], "r": 1}],
         "constraints": [{"type": "fix", "on": ["p1"], "at": [10, 10]}, {"type": "fix", "on": ["p2"], "at": [30, 10]},
                         {"type": "fix", "on": ["c.center"], "at": [20, 20]}, {"type": "radius", "on": ["c"], "value": 1}]},
    ]
    doc = S.Document.model_validate({"name": "t", "features": feats + extra})
    return Regenerator().run(doc), w * d * t


def _hole(**kw):
    return {"id": "h", "type": "hole", "sketch": "pts", **kw}


def test_through_holes_at_points_and_circle_centres():
    res, v0 = _plate([_hole(diameter=4)])
    assert res.ok, [f.message for f in res.features]
    assert res.features[-1].info["holes"] == 3  # two points and the circle's centre
    assert res.part.volume == pytest.approx(v0 - 3 * pi * 4 * 10, rel=1e-6)


def test_points_subset():
    res, v0 = _plate([_hole(diameter=4, points=["p2"])])
    assert res.part.volume == pytest.approx(v0 - pi * 4 * 10, rel=1e-6)
    bad, _ = _plate([_hole(diameter=4, points=["nope"])])
    assert not bad.ok and "nope" in bad.features[-1].message


def test_blind_with_drill_point_and_flat_bottom():
    res, v0 = _plate([_hole(diameter=5, extent="blind", depth=6, points=["p1"], thread="M6x1")])
    r = 2.5
    assert res.part.volume == pytest.approx(v0 - (pi * r * r * 6 + pi * r * r * (r / tan(59 * pi / 180)) / 3), rel=1e-6)
    flat, _ = _plate([_hole(diameter=5, extent="blind", depth=6, points=["p1"], tip_angle=0)])
    assert flat.part.volume == pytest.approx(v0 - pi * r * r * 6, rel=1e-6)
    labels = {str(l) for _, l in flat.body.labels}
    assert "h.bottom[p1]" in labels and "h.wall[p1]" in labels


def test_counterbore_and_countersink_volumes_and_labels():
    res, v0 = _plate([_hole(kind="counterbore", diameter=4, cbore_diameter=8, cbore_depth=3, points=["p1"])])
    assert res.part.volume == pytest.approx(v0 - (pi * 16 * 3 + pi * 4 * 7), rel=1e-6)
    labels = {str(l) for _, l in res.body.labels}
    assert {"h.cbore_wall[p1]", "h.cbore_floor[p1]", "h.wall[p1]"} <= labels
    res, v0 = _plate([_hole(kind="countersink", diameter=4, csk_diameter=8, points=["p1"])])
    h = 2  # 90 degrees: depth = R - r
    assert res.part.volume == pytest.approx(v0 - (pi * h / 3 * (16 + 8 + 4) + pi * 4 * (10 - h)), rel=1e-6)
    assert "h.csk[p1]" in {str(l) for _, l in res.body.labels}


@pytest.mark.parametrize("kw, msg", [
    (dict(kind="counterbore", diameter=4, cbore_diameter=3, cbore_depth=2), "larger than the hole"),
    (dict(kind="counterbore", diameter=4, cbore_diameter=8, cbore_depth=20, extent="blind", depth=5), "less than the hole depth"),
    (dict(kind="countersink", diameter=4, csk_diameter=30, extent="blind", depth=3), "deeper than the hole"),
    (dict(diameter=-1), "must be > 0"),
])
def test_bad_sizes_are_clear_errors(kw, msg):
    res, _ = _plate([_hole(**kw)])
    assert not res.ok and msg in res.features[-1].message, res.features[-1].message


def test_schema_requires_the_sizes_a_kind_needs():
    with pytest.raises(Exception, match="cbore_diameter"):
        S.Hole(id="h", sketch="s", kind="counterbore", diameter=3)
    with pytest.raises(Exception, match="depth"):
        S.Hole(id="h", sketch="s", diameter=3, extent="blind")


def test_holes_can_be_patterned_and_their_rims_filleted():
    res, v0 = _plate([
        _hole(diameter=4, points=["p1"]),
        {"id": "row", "type": "linear_pattern", "features": ["h"], "direction": "Y", "spacing": 8, "count": 3},
        {"id": "rim", "type": "chamfer", "distance": 0.5, "edges": [{"between": [
            {"feature": "plate", "role": "end"}, {"feature": "h", "role": "wall", "entity": "p1", "instance": "*"}]}]},
    ])
    assert res.ok, [(f.id, f.message) for f in res.features]
    assert res.features[-1].info["edges"] == 3
    c = 0.5
    ring = 2 * pi * (2 + c / 3) * c * c / 2
    assert res.part.volume == pytest.approx(v0 - 3 * pi * 4 * 10 - 3 * ring, rel=1e-5)


def test_hole_from_a_datum_sketch_goes_the_chosen_way():
    feats = [
        {"id": "top", "type": "sketch", "plane": {"datum": "XY", "offset": 10}, "entities": [{"id": "q", "type": "point", "at": [5, 5]}],
         "constraints": [{"type": "fix", "on": ["q"], "at": [5, 5]}]},
        {"id": "down", "type": "hole", "sketch": "top", "diameter": 2, "extent": "blind", "depth": 4, "tip_angle": 0},
    ]
    res, v0 = _plate(feats)
    assert res.part.volume == pytest.approx(v0 - pi * 4, rel=1e-6)
    up = [dict(feats[0]), {**feats[1], "direction": "normal"}]
    res, _ = _plate(up)
    assert res.ok and any("changed no volume" in w for w in res.features[-1].warnings)  # drilled upward into air
