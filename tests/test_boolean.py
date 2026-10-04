"""The boolean feature: cut / add / intersect a reference body, grown by a clearance."""
from math import pi

import build123d as bd
import pytest

from vibecad import schema as S
from vibecad.ops import apply_ops
from vibecad.regen import Regenerator
from test_extrude_extras import _rect


def _steiner_box(a, b, h, c):
    """Volume of a box grown by c with rounded edges and corners (a constant-distance offset)."""
    return a * b * h + 2 * (a * b + b * h + h * a) * c + pi * (a + b + h) * c * c + 4 / 3 * pi * c**3


@pytest.fixture
def cube_step(tmp_path):
    bd.export_step(bd.Box(10, 20, 30, align=bd.Align.MIN), str(tmp_path / "cell.step"))
    return tmp_path


def _run(base, feats):
    return Regenerator(base).run(S.Document.model_validate({"name": "t", "features": feats}))


def _block_and_ref(z=20, **bool_kw):
    # a 40 x 40 x 30 block; the 10 x 20 x 30 reference box stands in it, sunk from z = 20 (10 mm into the block top)
    return [_rect("sk", 40, 40), {"id": "block", "type": "extrude", "profile": {"sketch": "sk"}, "distance": 30},
            {"id": "cell", "type": "import", "file": "cell.step", "mode": "reference", "translate": [-5, -10, z]},
            {"id": "nest", "type": "boolean", "tool": "cell", **bool_kw}]


def test_cut_without_clearance_and_labels(cube_step):
    res = _run(cube_step, _block_and_ref())
    assert res.ok, [(f.id, f.message) for f in res.features if f.status == "error"]
    assert res.part.volume == pytest.approx(40 * 40 * 30 - 10 * 20 * 10, rel=1e-9)
    labels = {str(l) for _, l in res.body.labels}
    assert "nest.wall" in labels and any(l.startswith("nest.wall[cell.face") for l in labels), labels


def test_cut_with_clearance_matches_the_rounded_offset(cube_step):
    c = 0.4
    res = _run(cube_step, _block_and_ref(clearance=c))
    assert res.ok, [(f.id, f.message) for f in res.features if f.status == "error"]
    assert res.features[-1].info["corners"] == "rounded"
    assert res.features[-1].info["tool_volume"] == pytest.approx(_steiner_box(10, 20, 30, c), rel=1e-7)
    # the part of the grown box below the block top (z = 30): its bottom slab, sides and rounded bottom edges
    a, b, d = 10, 20, 10  # footprint, depth sunk
    below = (a * b * c + (a + 2 * c) * (b + 2 * c) * d - 4 * c * c * d * (1 - pi / 4)  # prism (rounded vertical edges)
             + 2 * (a + b) * pi * c * c / 4 + 4 * (4 / 3 * pi * c**3) / 8)  # bottom edge quarter-rounds, four corner eighths
    assert res.part.volume == pytest.approx(40 * 40 * 30 - below, rel=1e-7)


def test_add_and_intersect_and_errors(cube_step):
    add = _run(cube_step, _block_and_ref(mode="add"))
    assert add.part.volume == pytest.approx(40 * 40 * 30 + 10 * 20 * 20, rel=1e-9)
    inter = _run(cube_step, _block_and_ref(mode="intersect"))
    assert inter.part.volume == pytest.approx(10 * 20 * 10, rel=1e-9)
    bad = _run(cube_step, _block_and_ref()[:2] + [{"id": "nest", "type": "boolean", "tool": "block"}])
    assert not bad.ok and "reference import" in bad.features[-1].message
    neg = _run(cube_step, _block_and_ref(clearance=-1))
    assert not neg.ok and ">= 0" in neg.features[-1].message


def test_pattern_repeats_the_nest_and_rename_follows(cube_step):
    feats = _block_and_ref(z=25) + [{"id": "row", "type": "linear_pattern", "features": ["nest"], "direction": "X", "spacing": 12, "count": 2}]
    feats[0] = _rect("sk", 60, 40)
    res = _run(cube_step, feats)
    assert res.ok and res.part.volume == pytest.approx(60 * 40 * 30 - 2 * 10 * 20 * 5, rel=1e-9)
    doc, _ = apply_ops(S.Document.model_validate({"name": "t", "features": feats}), [{"op": "rename_feature", "id": "cell", "to": "battery"}])
    assert doc.feature("nest").tool == "battery"


def test_nest_around_a_rounded_body_leaves_exactly_the_clearance(tmp_path):
    """A rounded-rectangle slab (a phone) nested into a plate: the smallest gap equals the clearance, although OCCT's
    whole-shape distance fails on the concentric offset surfaces."""
    from vibecad.measure import min_distance
    with bd.BuildPart() as p:
        with bd.BuildSketch():
            bd.RectangleRounded(70, 140, 9)
        bd.extrude(amount=8)
    bd.export_step(p.part, str(tmp_path / "phone.step"))
    feats = [{"id": "phone", "type": "import", "file": "phone.step", "mode": "reference"},
             _rect("sk", 100, 170), {"id": "plate", "type": "extrude", "profile": {"sketch": "sk"}, "distance": 3, "direction": "reverse"},
             {"id": "nest", "type": "boolean", "tool": "phone", "clearance": 0.5}]
    res = _run(tmp_path, feats)
    assert res.ok
    d, pa, pb = min_distance(res.part.wrapped, res.refs["phone"].shape)
    assert d == pytest.approx(0.5, abs=1e-6)
    # the pocket is the part of the grown phone below its underside: at depth t the section is the footprint grown by
    # s = sqrt(c² - t²), area A + P s + π s²; integrated over t from 0 to c
    c, A, P = 0.5, 70 * 140 - (4 - pi) * 81, 2 * (70 + 140) - 8 * 9 + 2 * pi * 9
    pocket = A * c + P * pi * c * c / 4 + 2 * pi * c**3 / 3
    assert res.part.volume == pytest.approx(100 * 170 * 3 - pocket, rel=1e-7)
