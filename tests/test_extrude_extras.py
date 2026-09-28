"""Extrude with a draft angle, and extruding up to a face."""
from math import pi, radians, tan

import pytest

from vibecad import schema as S
from vibecad.regen import Regenerator


def _rect(sid, w, d, plane=None, cx=0.0, cy=0.0):
    x0, y0, x1, y1 = cx - w / 2, cy - d / 2, cx + w / 2, cy + d / 2
    return {"id": sid, "type": "sketch", "plane": plane or {"datum": "XY"}, "entities": [
        {"id": "b", "type": "line", "p1": [x0, y0], "p2": [x1, y0]}, {"id": "r", "type": "line", "p1": [x1, y0], "p2": [x1, y1]},
        {"id": "t", "type": "line", "p1": [x1, y1], "p2": [x0, y1]}, {"id": "l", "type": "line", "p1": [x0, y1], "p2": [x0, y0]}],
        "constraints": [{"type": "fix", "on": [p], "at": a} for p, a in
                        (("b.p1", [x0, y0]), ("b.p2", [x1, y0]), ("r.p2", [x1, y1]), ("t.p2", [x0, y1]))]
        + [{"type": "coincident", "on": ["b.p2", "r.p1"]}, {"type": "coincident", "on": ["r.p2", "t.p1"]},
           {"type": "coincident", "on": ["t.p2", "l.p1"]}, {"type": "coincident", "on": ["l.p2", "b.p1"]}]}


def _circle(sid, r, plane=None):
    return {"id": sid, "type": "sketch", "plane": plane or {"datum": "XY"},
            "entities": [{"id": "c", "type": "circle", "center": [0, 0], "r": r}],
            "constraints": [{"type": "fix", "on": ["c.center"], "at": [0, 0]}, {"type": "radius", "on": ["c"], "value": r}]}


def _run(feats):
    res = Regenerator().run(S.Document.model_validate({"name": "t", "features": feats}))
    return res


def _drafted_box(w, d, h, a):
    """Prismatoid: the section shrinks linearly on every side, so its area is quadratic in height."""
    s = h * tan(radians(a))
    area = lambda k: (w - 2 * s * k) * (d - 2 * s * k)
    return h / 6 * (area(0) + 4 * area(0.5) + area(1))


def test_drafted_boss():
    res = _run([_rect("sk", 40, 30, None), {"id": "boss", "type": "extrude", "profile": {"sketch": "sk"}, "distance": 20, "draft": 5}])
    assert res.ok, [f.message for f in res.features]
    assert res.part.volume == pytest.approx(_drafted_box(40, 30, 20, 5), rel=1e-6)
    bb = res.part.bounding_box()
    assert bb.max.Z == pytest.approx(20) and bb.size.X == pytest.approx(40)
    labels = {str(l) for _, l in res.body.labels}
    assert {"boss.start", "boss.end", "boss.side[b]", "boss.side[r]", "boss.side[t]", "boss.side[l]"} <= labels, labels


def test_drafted_round_boss_and_reverse():
    res = _run([_circle("sk", 10), {"id": "p", "type": "extrude", "profile": {"sketch": "sk"}, "distance": 10, "draft": -10,
                                     "direction": "reverse"}])
    r2 = 10 + 10 * tan(radians(10))
    assert res.part.volume == pytest.approx(pi * 10 / 3 * (100 + r2 * r2 + 10 * r2), rel=1e-5)
    assert res.part.bounding_box().min.Z == pytest.approx(-10)


def test_drafted_pocket_from_a_face():
    feats = [_rect("sk", 60, 40), {"id": "plate", "type": "extrude", "profile": {"sketch": "sk"}, "distance": 15},
             _rect("pk", 30, 20, {"face": {"feature": "plate", "role": "end"}}),
             {"id": "pocket", "type": "extrude", "profile": {"sketch": "pk"}, "distance": 10, "direction": "reverse",
              "mode": "cut", "draft": 8}]
    res = _run(feats)
    assert res.ok, [f.message for f in res.features]
    assert res.part.volume == pytest.approx(60 * 40 * 15 - _drafted_box(30, 20, 10, 8), rel=1e-6)


def test_draft_that_pinches_off_is_an_error():
    res = _run([_rect("sk", 10, 10), {"id": "e", "type": "extrude", "profile": {"sketch": "sk"}, "distance": 50, "draft": 20}])
    assert not res.ok


def test_up_to_face():
    feats = [_rect("base_sk", 60, 40), {"id": "base", "type": "extrude", "profile": {"sketch": "base_sk"}, "distance": 5},
             # a raised shelf, then a post sketched on the XY datum that grows up to the shelf's top
             _rect("shelf_sk", 60, 10, {"datum": "XY", "offset": 5}, cy=15),
             {"id": "shelf", "type": "extrude", "profile": {"sketch": "shelf_sk"}, "distance": 25},
             _rect("post_sk", 8, 8, {"datum": "XY", "offset": 5}, cx=20, cy=-10),
             {"id": "post", "type": "extrude", "profile": {"sketch": "post_sk"}, "extent": "up_to_face",
              "to_face": {"feature": "shelf", "role": "end"}}]
    res = _run(feats)
    assert res.ok, [f.message for f in res.features]
    assert res.features[-1].info["length"] == pytest.approx(25)
    assert res.part.volume == pytest.approx(60 * 40 * 5 + 60 * 10 * 25 + 8 * 8 * 25, rel=1e-6)
    # it follows the shelf when the shelf grows, and turns around for a face below the sketch
    feats[3]["distance"] = 35
    feats[5]["distance"] = 2  # past the face
    res = _run(feats)
    assert res.features[-1].info["length"] == pytest.approx(37)
    below = feats[:4] + [_rect("post_sk", 8, 8, {"datum": "XY", "offset": 40}, cx=20, cy=-10),
                         {"id": "post", "type": "extrude", "profile": {"sketch": "post_sk"}, "extent": "up_to_face",
                          "to_face": {"feature": "base", "role": "end"}}]
    res = _run(below)
    assert res.ok and res.features[-1].info["length"] == pytest.approx(35)


def test_up_to_face_rejects_non_parallel_and_missing():
    feats = [_rect("sk", 20, 20), {"id": "b", "type": "extrude", "profile": {"sketch": "sk"}, "distance": 20},
             _rect("s2", 4, 4, {"datum": "XY", "offset": -10}),
             {"id": "e", "type": "extrude", "profile": {"sketch": "s2"}, "extent": "up_to_face",
              "to_face": {"feature": "b", "role": "side", "entity": "b"}}]
    res = _run(feats)
    assert not res.ok and "parallel" in res.features[-1].message
    with pytest.raises(Exception, match="to_face"):
        S.Extrude.model_validate({"id": "e", "profile": {"sketch": "s"}, "extent": "up_to_face"})


def test_draft_on_a_tilted_face():
    # a wedge's sloped face: the sketch normal is not along an axis
    feats = [{"id": "sk", "type": "sketch", "plane": {"datum": "XZ"}, "entities": [
        {"id": "b", "type": "line", "p1": [0, 0], "p2": [40, 0]}, {"id": "s", "type": "line", "p1": [40, 0], "p2": [0, 40]},
        {"id": "l", "type": "line", "p1": [0, 40], "p2": [0, 0]}],
        "constraints": [{"type": "fix", "on": ["b.p1"], "at": [0, 0]}, {"type": "fix", "on": ["b.p2"], "at": [40, 0]},
                        {"type": "fix", "on": ["l.p1"], "at": [0, 40]}, {"type": "coincident", "on": ["b.p2", "s.p1"]},
                        {"type": "coincident", "on": ["s.p2", "l.p1"]}, {"type": "coincident", "on": ["l.p2", "b.p1"]}]},
        {"id": "wedge", "type": "extrude", "profile": {"sketch": "sk"}, "distance": 30, "direction": "reverse"},
        _circle("c", 5, {"face": {"feature": "wedge", "role": "side", "entity": "s"}}),
        {"id": "boss", "type": "extrude", "profile": {"sketch": "c"}, "distance": 6, "draft": 10}]
    res = _run(feats)
    assert res.ok, [f.message for f in res.features]
    r2 = 5 - 6 * tan(radians(10))
    boss = pi * 6 / 3 * (25 + r2 * r2 + 5 * r2)
    assert res.part.volume == pytest.approx(40 * 40 / 2 * 30 + boss, rel=1e-5)
