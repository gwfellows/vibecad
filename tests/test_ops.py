"""Edge cases of the edit ops: every rejection names the problem, and nothing half-applies."""
import shutil
from pathlib import Path

import pytest

from vibecad import schema as S
from vibecad.ops import OpError, apply_ops
from vibecad.session import Session

EX = Path(__file__).resolve().parent.parent / "examples"


@pytest.fixture
def lb(tmp_path):
    p = tmp_path / "lb.vcad.json"
    shutil.copy(EX / "l_bracket.vcad.json", p)
    return Session(p)


@pytest.mark.parametrize("ops,msg", [
    (["set_param"], "each op must be an object"),
    ([{"op": "move_feature", "id": "base", "after": "base"}], "relative to itself"),
    ([{"op": "set_param", "name": "width", "value": True}], "number or an expression"),
    ([{"op": "set_param", "name": "width", "value": [60]}], "number or an expression"),
    ([{"op": "set_param", "name": "2wide", "value": 3}], "must be an identifier"),
    ([{"op": "set_param", "name": "lambda", "value": 3}], "must be an identifier"),
    ([{"op": "set_param", "name": "sqrt", "value": 3}], "reserved"),
    ([{"op": "set_param", "name": "pi", "value": 3}], "reserved"),
    ([{"op": "update_entity", "sketch": "base_sketch", "id": "base_front", "set": {"id": "x"}}], "cannot change"),
    ([{"op": "remove_constraint", "sketch": "base_sketch", "match": {"type": "horizontal"}}], "matched 2"),
    ([{"op": "set_dimension", "sketch": "slot_sketch", "name": "nope", "value": 3}], "matched 0"),
    ([{"op": "set_meta", "set": {"units": "in"}}], "set_meta can set"),
])
def test_rejected_ops_change_nothing(lb, ops, msg):
    before = lb.path.read_text()
    r = lb.apply(ops, "bad")
    assert not r["ok"] and r["applied"] == 0 and msg in r["error"], r
    assert lb.path.read_text() == before and not lb.undo_stack


def test_gear_module_param_named_m_is_allowed(lb):
    assert lb.apply([{"op": "set_param", "name": "m", "value": 2}], "module")["ok"]


@pytest.mark.parametrize("fid,field,value,msg", [
    ("corner_fillet", "radius", 0, "fillet radius must be > 0"),
    ("corner_fillet", "radius", -2, "fillet radius must be > 0"),
    ("base", "distance", -5, "extrude distance must be > 0"),
])
def test_nonpositive_sizes_explained(lb, fid, field, value, msg):
    r = lb.apply([{"op": "update_feature", "id": fid, "set": {field: value}}], "bad size")
    assert any(msg in e for e in r["errors"]), r["errors"]


def test_negative_dimension_named_when_sketch_fails(lb):
    r = lb.apply([{"op": "set_param", "name": "width", "value": "-60"}], "negative")
    err = next(e for e in r["errors"] if e.startswith("base_sketch"))
    assert "<= 0" in err and "width" in err, err


def test_undo_restores_exact_document_after_every_op_kind(lb):
    from vibecad.session import dump_doc

    original = lb.doc
    batches = [
        [{"op": "set_param", "name": "width", "value": "70 mm"}],
        [{"op": "set_dimension", "sketch": "slot_sketch", "name": "slot_len", "value": 12}],
        [{"op": "update_feature", "id": "wall", "set": {"intent": "changed"}}],
        [{"op": "update_constraint", "sketch": "hole_sketch", "match": {"name": "hole_z"}, "set": {"value": 30}}],
        [{"op": "add_entity", "sketch": "hole_sketch", "entity": {"id": "p", "type": "point", "at": [0, 0]}}],
        [{"op": "remove_entity", "sketch": "hole_sketch", "id": "p"}],
        [{"op": "move_feature", "id": "corner_fillet", "after": "wall"}],
        [{"op": "set_meta", "set": {"material": "steel"}}],
        [{"op": "remove_feature", "id": "corner_fillet"}],
    ]
    for b in batches:
        r = lb.apply(b, "step")
        assert r["applied"] == len(b), (b, r)
    for _ in batches:
        lb.undo()
    assert lb.doc == original
    assert lb.path.read_text() == dump_doc(original)
    assert lb.result.part.volume == pytest.approx(Session(lb.path).result.part.volume)


@pytest.fixture
def standoff(tmp_path):
    p = tmp_path / "hs.vcad.json"
    shutil.copy(EX / "hex_standoff.vcad.json", p)
    return Session(p)


def test_size_change_points_at_texts_that_quote_numbers(standoff):
    r = standoff.apply([{"op": "set_param", "name": "bore_d", "value": "4.5 mm"}], "M4")
    note = next(n for n in r.get("notes", []) if n.startswith("sizes changed"))
    assert "bore_d 3.4 -> 4.5" in note and "bore_sk ('M3 clearance bore" in note and "design_notes" in note
    assert "base_sk" not in note  # its intent doesn't use bore_d


def test_no_stale_text_note_when_the_batch_rewrites_it(standoff):
    r = standoff.apply([{"op": "set_param", "name": "bore_d", "value": "4.5 mm"},
                        {"op": "update_feature", "id": "bore_sk", "set": {"intent": "M4 clearance bore"}},
                        {"op": "set_meta", "set": {"design_notes": "Hex standoff, M4 clearance bore."}}], "M4")
    assert not any(n.startswith("sizes changed") for n in r.get("notes", [])), r.get("notes")


def test_no_stale_text_note_for_unrelated_param(standoff):
    r = standoff.apply([{"op": "set_meta", "set": {"design_notes": "Hex standoff."}},
                        {"op": "set_param", "name": "length", "value": "12 mm"}], "longer")
    assert not any(n.startswith("sizes changed") for n in r.get("notes", [])), r.get("notes")


def test_tool_errors_name_the_problem(tmp_path):
    from vibecad.workspace import ToolError, Workspace

    shutil.copy(EX / "l_bracket.vcad.json", tmp_path / "lb.vcad.json")
    ws = Workspace(tmp_path)
    ws.open_part("lb.vcad.json")
    with pytest.raises(ToolError, match="built sketches"):
        ws.to_world("nope", 0, 0)
    with pytest.raises(ToolError, match="no part file"):
        ws.check_fit(["missing.vcad.json"])
    assert ws.to_world("base_sketch", 1, 2) == [1, 2, 0]


def test_rename_feature_updates_every_reference(lb):
    v = lb.result.part.volume
    r = lb.apply([{"op": "rename_feature", "id": "base", "to": "plate"},
                  {"op": "rename_feature", "id": "slot_cut", "to": "slot"},
                  {"op": "rename_feature", "id": "slot_sketch", "to": "slot_profile"}], "rename")
    assert r["ok"], r
    ids = [f.id for f in lb.doc.features]
    assert "base" not in ids and {"plate", "slot", "slot_profile"} <= set(ids)
    assert lb.doc.feature("slot_profile").plane.face.feature == "plate"
    assert lb.doc.feature("slot").profile.sketch == "slot_profile"
    assert lb.doc.feature("slot_mirror").features == ["slot"]
    assert lb.doc.feature("corner_fillet").edges[0].between[0].feature == "plate"
    assert lb.result.part.volume == pytest.approx(v)


def test_rename_pattern_updates_instance_refs(lb):
    lb.apply([{"op": "add_feature", "feature": {"id": "mirror_ch", "type": "chamfer", "distance": 0.5, "edges": [
        {"of": {"feature": "slot_cut", "role": "side", "instance": "slot_mirror#1"}, "filter": {"type": "line"}}]}}], "chamfer copy")
    v = lb.result.part.volume
    r = lb.apply([{"op": "rename_feature", "id": "slot_mirror", "to": "slot_copy"}], "rename")
    assert r["ok"], r
    assert lb.doc.feature("mirror_ch").edges[0].of.instance == "slot_copy#1"
    assert lb.result.part.volume == pytest.approx(v)


def test_rename_entity_updates_constraints_and_face_refs(lb):
    lb.apply([{"op": "add_feature", "feature": {"id": "top_ch", "type": "chamfer", "distance": 1, "edges": [
        {"of": {"feature": "wall", "role": "side", "entity": "wall_top"}, "filter": {"parallel_to": "X"}}]}}], "chamfer")
    v = lb.result.part.volume
    r = lb.apply([{"op": "rename_entity", "sketch": "wall_sketch", "id": "wall_top", "to": "wall_crest"}], "rename")
    assert r["ok"], r
    sk = lb.doc.feature("wall_sketch")
    assert "wall_top" not in {e.id for e in sk.entities}
    assert ["wall_right.p2", "wall_crest.p1"] in [c.on for c in sk.constraints]
    assert lb.doc.feature("top_ch").edges[0].of.entity == "wall_crest"
    assert lb.result.part.volume == pytest.approx(v)


@pytest.mark.parametrize("op,msg", [
    ({"op": "rename_feature", "id": "base", "to": "wall"}, "already exists"),
    ({"op": "rename_feature", "id": "base", "to": "base.top"}, "letters, digits"),
    ({"op": "rename_feature", "id": "nope", "to": "x"}, "no feature"),
    ({"op": "rename_entity", "sketch": "wall_sketch", "id": "wall_top", "to": "wall_left"}, "already exists"),
    ({"op": "rename_entity", "sketch": "wall_sketch", "id": "wall_top", "to": "origin"}, "reserved"),
])
def test_bad_renames_rejected(lb, op, msg):
    r = lb.apply([op], "bad rename")
    assert not r["ok"] and msg in r["error"], r


def test_fillet_corner_rounds_a_rectangle_keeping_it_fully_constrained(tmp_path):
    from math import pi

    from vibecad.regen import Regenerator
    doc = S.Document(name="t", params={"r": 3})
    doc, _ = apply_ops(doc, [
        {"op": "add_feature", "feature": {"id": "sk", "type": "sketch", "plane": {"datum": "XY"}}},
        {"op": "add_rectangle", "sketch": "sk", "id": "plate", "width": 40, "height": 20, "center": [0, 0]},
        {"op": "add_feature", "feature": {"id": "ex", "type": "extrude", "profile": {"sketch": "sk"}, "distance": 2}},
    ])
    base = Regenerator().run(doc).part.volume
    corners = [("plate_bottom.p2", None), ("plate_top.p2", "r")]  # one numeric, one param-driven
    for corner, rad in corners:
        doc, notes = apply_ops(doc, [{"op": "fillet_corner", "sketch": "sk", "corner": corner, "radius": rad or 5}])
        assert "rounded" in notes[0]
    res = Regenerator().run(doc)
    assert res.ok, [(f.id, f.message) for f in res.features]
    assert res.sketches["sk"][0].report.dof == 0
    cut = lambda r: r * r * (1 - pi / 4)
    assert res.part.volume == pytest.approx(base - 2 * (cut(5) + cut(3)), rel=1e-6)
    doc, _ = apply_ops(doc, [{"op": "set_param", "name": "r", "value": 4}])
    res = Regenerator().run(doc)
    assert res.part.volume == pytest.approx(base - 2 * (cut(5) + cut(4)), rel=1e-6)


def test_fillet_corner_errors(tmp_path):
    doc = S.Document(name="t")
    doc, _ = apply_ops(doc, [
        {"op": "add_feature", "feature": {"id": "sk", "type": "sketch", "plane": {"datum": "XY"}}},
        {"op": "add_rectangle", "sketch": "sk", "id": "p", "width": 10, "height": 10, "center": [0, 0]},
        {"op": "add_circle", "sketch": "sk", "id": "c", "diameter": 2, "center": [20, 0]},
    ])
    with pytest.raises(OpError, match="too big"):
        apply_ops(doc, [{"op": "fillet_corner", "sketch": "sk", "corner": "p_bottom.p2", "radius": 20}])
    with pytest.raises(OpError, match="line endpoint"):
        apply_ops(doc, [{"op": "fillet_corner", "sketch": "sk", "corner": "c.center", "radius": 1}])


def test_fillet_all_four_corners_keeps_the_rectangle_size():
    from math import pi

    from vibecad.regen import Regenerator
    doc = S.Document(name="t", params={"w": 40, "h": 20})
    doc, _ = apply_ops(doc, [
        {"op": "add_feature", "feature": {"id": "sk", "type": "sketch", "plane": {"datum": "XY"}}},
        {"op": "add_rectangle", "sketch": "sk", "id": "p", "width": "w", "height": "h", "center": [0, 0]},
        {"op": "add_feature", "feature": {"id": "ex", "type": "extrude", "profile": {"sketch": "sk"}, "distance": 1}},
    ])
    doc, _ = apply_ops(doc, [{"op": "fillet_corner", "sketch": "sk", "corner": c, "radius": 4}
                             for c in ("p_bottom.p2", "p_right.p2", "p_top.p2", "p_left.p2")])
    res = Regenerator().run(doc)
    assert res.ok and res.sketches["sk"][0].report.dof == 0
    assert res.part.volume == pytest.approx(40 * 20 - 4 * 16 * (1 - pi / 4), rel=1e-6)
    bb = res.part.bounding_box()
    assert (round(bb.size.X, 6), round(bb.size.Y, 6)) == (40, 20)
    doc, _ = apply_ops(doc, [{"op": "set_param", "name": "w", "value": 60}])  # the rounded plate still resizes
    res = Regenerator().run(doc)
    assert round(res.part.bounding_box().size.X, 6) == 60 and res.sketches["sk"][0].report.dof == 0


def test_rejected_batches_explain_themselves():
    from vibecad.ops import OpError, apply_ops
    doc = S.Document.model_validate({"name": "t", "features": []})
    with pytest.raises(OpError, match="is an op, not a field"):
        apply_ops(doc, [{"op": "add_feature", "feature": {"id": "s", "type": "sketch", "plane": {"datum": "XY"},
                                                         "add_rectangle": {"width": 1}}}])
