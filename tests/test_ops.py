"""Edge cases of the edit ops: every rejection names the problem, and nothing half-applies."""
import shutil
from pathlib import Path

import pytest

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
