"""The text feature: engraved or raised lettering on a sketch plane."""
import pytest

from vibecad import schema as S
from vibecad.regen import Regenerator


def _plate(text_feature, t=5):
    doc = S.Document.model_validate({"name": "t", "features": [
        {"id": "sk", "type": "sketch", "plane": {"datum": "XY"}, "entities": [
            {"id": "b", "type": "line", "p1": [-30, -10], "p2": [30, -10]}, {"id": "r", "type": "line", "p1": [30, -10], "p2": [30, 10]},
            {"id": "tp", "type": "line", "p1": [30, 10], "p2": [-30, 10]}, {"id": "l", "type": "line", "p1": [-30, 10], "p2": [-30, -10]}],
         "constraints": [{"type": "fix", "on": [p], "at": a} for p, a in (("b.p1", [-30, -10]), ("b.p2", [30, -10]), ("r.p2", [30, 10]), ("tp.p2", [-30, 10]))]
         + [{"type": "coincident", "on": [a, b]} for a, b in (("b.p2", "r.p1"), ("r.p2", "tp.p1"), ("tp.p2", "l.p1"), ("l.p2", "b.p1"))]},
        {"id": "plate", "type": "extrude", "profile": {"sketch": "sk"}, "distance": t},
        {"id": "lbl", "type": "sketch", "plane": {"face": {"feature": "plate", "role": "end"}},
         "entities": [{"id": "c", "type": "point", "at": [0, 0]}], "constraints": [{"type": "coincident", "on": ["c", "origin"]}]},
        text_feature,
    ]})
    return Regenerator().run(doc), 60 * 20 * t


def test_engraved_and_raised_text():
    cut, v0 = _plate({"id": "engrave", "type": "text", "sketch": "lbl", "at": "c", "text": "V42", "size": 8, "depth": 0.6})
    assert cut.ok, [(f.id, f.message) for f in cut.features]
    removed = v0 - cut.part.volume
    assert 0 < removed < 60 * 20 * 0.6  # some letters' area times the depth
    bb = cut.part.bounding_box()
    assert round(bb.size.Z, 6) == 5  # cut into the plate: no taller
    labels = {str(l) for _, l in cut.body.labels}
    assert "engrave.end" in labels and "engrave.side" in labels  # the letters' floors and walls
    add, _ = _plate({"id": "emboss", "type": "text", "sketch": "lbl", "at": "c", "text": "V42", "size": 8, "depth": 0.6, "mode": "add"})
    assert add.ok and round(add.part.bounding_box().size.Z, 6) == 5.6
    assert add.part.volume - v0 == pytest.approx(removed, rel=1e-6)  # the same letters, raised


def test_text_errors():
    res, _ = _plate({"id": "t", "type": "text", "sketch": "lbl", "at": "nope", "text": "A"})
    assert not res.ok and "must be a point" in res.features[-1].message
    res, _ = _plate({"id": "t", "type": "text", "sketch": "lbl", "at": "c", "text": "   "})
    assert not res.ok and "empty" in res.features[-1].message
