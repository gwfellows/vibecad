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
