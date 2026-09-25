"""Interactive sketch editing: drag previews and which geometry is still free."""
import math
from pathlib import Path

import pytest

from vibecad import schema as S
from vibecad.regen import Regenerator, load
from vibecad.sketch import drag, freedom

EX = Path(__file__).resolve().parent.parent / "examples"
LINE = [{"id": "l", "type": "line", "p1": [0, 0], "p2": [10, 0]}]
AT_ORIGIN = {"type": "coincident", "on": ["l.p1", "origin"]}


def sk(entities, constraints):
    return S.Sketch(id="s", plane={"datum": "XY"}, entities=entities, constraints=constraints)


def p2(solved):
    return solved.entities["l"].p2


@pytest.mark.parametrize("cons,expect", [
    ([AT_ORIGIN], (5, 3)),                                                       # 2 free directions: follows exactly
    ([AT_ORIGIN, {"type": "horizontal", "on": ["l"]}], (5, 0)),                  # slides along its line
    ([AT_ORIGIN, {"type": "distance", "on": ["l"], "value": 10}], (8.575, 5.145)),  # slides along a circle
])
def test_drag_point_goes_as_near_the_cursor_as_constraints_allow(cons, expect):
    out, moved = drag(sk(LINE, cons), {}, "l.p2", (5, 3))
    assert moved and out.report.ok
    assert math.dist(p2(out), expect) < 0.1, p2(out)


def test_drag_fully_constrained_point_does_not_move():
    s = sk(LINE, [AT_ORIGIN, {"type": "horizontal", "on": ["l"]}, {"type": "distance", "on": ["l"], "value": 10}])
    out, moved = drag(s, {}, "l.p2", (5, 3))
    assert not moved and p2(out) == pytest.approx((10, 0))


def test_mouse_drag_follows_a_curved_path_step_by_step():
    s = sk(LINE, [AT_ORIGIN, {"type": "distance", "on": ["l"], "value": 10}])
    guess = None
    for k in range(1, 31):
        a = math.pi * k / 30
        out, _ = drag(s, {}, "l.p2", (10 * math.cos(a), 12 * math.sin(a)), guess=guess)
        guess = out.to_ir_entities()
    assert p2(out) == pytest.approx((-10, 0), abs=1e-3)


def test_drag_free_line_translates_it():
    out, moved = drag(sk(LINE, []), {}, "l", (4, 2), grab=(3, 0))
    assert moved and out.entities["l"].p1 == pytest.approx((1, 2)) and p2(out) == pytest.approx((11, 2))


def test_drag_circle_rim_resizes_it_about_a_fixed_centre():
    s = sk([{"id": "c", "type": "circle", "center": [0, 0], "r": 5}], [{"type": "coincident", "on": ["c.center", "origin"]}])
    out, moved = drag(s, {}, "c", (0, 8))
    assert moved and out.entities["c"].r == pytest.approx(8) and out.entities["c"].center == pytest.approx((0, 0))


def test_freedom_flags():
    s = sk(LINE + [{"id": "c", "type": "circle", "center": [20, 0], "r": 2}],
           [AT_ORIGIN, {"type": "horizontal", "on": ["l"]}, {"type": "distance", "on": ["l"], "value": 10},
            {"type": "coincident", "on": ["c.center", "l.p2"]}])
    assert freedom(s, {}) == {"l": True, "c": False}  # the circle's radius is still free
    s.constraints.append(S.Constraint(type="diameter", on=["c"], value=4))
    assert freedom(s, {}) == {"l": True, "c": True}


def test_freedom_of_every_example_sketch_is_fully_constrained():
    for p in sorted(EX.glob("*.vcad.json")):
        res = Regenerator().run(load(p))
        for sid, (solved, _) in res.sketches.items():  # as built: external geometry already projected
            assert all(freedom(solved.source, res.env, solved).values()), (p.name, sid)


# ── external (projected) geometry ─────────────────────────────────
from vibecad.session import Session  # noqa: E402

FRONT_TOP = {"between": [{"feature": "base", "role": "end"}, {"feature": "base", "role": "side", "entity": "base_front"}],
             "note": "front top edge of the base"}


@pytest.fixture
def lb(tmp_path):
    import shutil
    p = tmp_path / "lb.vcad.json"
    shutil.copy(EX / "l_bracket.vcad.json", p)
    return Session(p)


def test_external_edge_is_projected_and_follows_the_part(lb):
    r = lb.apply([
        {"op": "add_feature", "after": "base", "feature": {"id": "pin_sk", "type": "sketch",
                                                          "plane": {"face": {"feature": "base", "role": "end"}}}},
        {"op": "add_entity", "sketch": "pin_sk", "entity": {"id": "front", "type": "external", "edge": FRONT_TOP}},
        {"op": "add_circle", "sketch": "pin_sk", "id": "pin", "diameter": 3},  # centre left free for the coincident
        {"op": "add_constraint", "sketch": "pin_sk", "constraint": {"type": "coincident", "on": ["pin.center", "front.p2"]}},
    ], "pin at the front right corner")
    assert r["ok"], r
    solved = lb.result.sketches["pin_sk"][0]
    assert solved.report.dof == 0
    ext = solved.entities["front"]
    assert ext.construction and {ext.p1, ext.p2} == {(0.0, 0.0), (60.0, 0.0)}
    corner = ext.p2
    assert solved.entities["pin"].center == pytest.approx(corner)
    lb.apply([{"op": "set_param", "name": "width", "value": "80 mm"}], "wider")
    solved = lb.result.sketches["pin_sk"][0]
    assert max(solved.entities["front"].p1[0], solved.entities["front"].p2[0]) == pytest.approx(80)
    assert solved.entities["pin"].center[0] in (pytest.approx(0), pytest.approx(80))  # still on the edge's end


def test_external_circle_projects_the_hole_rim(lb):
    rim = {"of": {"feature": "hole_cut", "role": "side"}, "filter": {"type": "circle"}}
    r = lb.apply([{"op": "add_feature", "feature": {"id": "rim_sk", "type": "sketch", "plane": {"datum": "XZ"}}},
                  {"op": "add_entity", "sketch": "rim_sk", "entity": {"id": "rim", "type": "external", "edge": rim}}], "rim")
    # both holes have two circular edges each: the ref must name one edge
    assert any("matched 4 edges" in e for e in r["errors"]), r
    one = {"between": [{"feature": "wall", "role": "start"}, {"feature": "hole_cut", "role": "side"}], "filter": {"type": "circle"}}
    lb.undo()
    r = lb.apply([{"op": "add_feature", "feature": {"id": "rim_sk", "type": "sketch", "plane": {"datum": "XZ"}}},
                  {"op": "add_entity", "sketch": "rim_sk", "entity": {"id": "rim", "type": "external", "edge": one}}], "rim")
    assert any("matched 2 edges" in e for e in r["errors"]), r  # still two holes on that face


def test_external_needs_a_body():
    from vibecad.regen import Regenerator

    doc = S.Document(name="x", features=[S.Sketch(id="s", plane={"datum": "XY"}, entities=[
        {"id": "e", "type": "external", "edge": {"of": {"feature": "nope", "role": "end"}}}])])
    res = Regenerator().run(doc)
    assert res.features[0].status == "error" and "needs an existing body" in res.features[0].message
