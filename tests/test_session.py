"""Edit ops, sessions and the MCP server."""
import asyncio
import json
import shutil
from math import pi, sin, tan
from pathlib import Path

import pytest

from vibecad.session import Session

EX = Path(__file__).resolve().parent.parent / "examples"


@pytest.fixture
def lb(tmp_path):
    p = tmp_path / "lb.vcad.json"
    shutil.copy(EX / "l_bracket.vcad.json", p)
    return Session(p)


def vol(s):
    return s.result.part.volume


def test_param_driven_dimension_updates_param(lb):
    r = lb.apply([{"op": "set_dimension", "sketch": "hole_sketch", "name": "hole_spacing", "value": "40 mm"}], "spread")
    assert r["ok"]
    assert lb.doc.params["hole_spacing"] == "40 mm"
    assert any("driven by param" in n for n in r["notes"])


def test_failed_batch_changes_nothing(lb):
    before = lb.path.read_text()
    v = vol(lb)
    r = lb.apply([{"op": "set_param", "name": "width", "value": "90"},
                  {"op": "update_feature", "id": "wall", "set": {"distanc": 3}}], "typo")
    assert not r["ok"] and r["applied"] == 0 and "distanc" in r["error"]
    assert lb.path.read_text() == before and vol(lb) == v


@pytest.mark.parametrize("ops,why", [
    ([{"op": "set_param", "name": "hole_d", "value": "2 *"}], "cannot parse"),
    ([{"op": "set_param", "name": "a", "value": "b + 1"}, {"op": "set_param", "name": "b", "value": "a"}], "circular"),
    ([{"op": "set_param", "name": "hole_r", "value": "hole_d / 2"}, {"op": "remove_param", "name": "hole_d"}], "unknown"),
])
def test_bad_params_rejected_without_wedging_session(lb, ops, why):
    v = vol(lb)
    r = lb.apply(ops, "bad params")
    assert not r["ok"] and r["applied"] == 0 and why in r["error"]
    assert lb.doc.params["hole_d"] == "5.5 mm" and not lb.undo_stack
    assert lb.apply([{"op": "set_param", "name": "width", "value": "70 mm"}], "next edit")["ok"]
    assert vol(lb) > v


def test_undo_redo(lb):
    v0 = vol(lb)
    lb.apply([{"op": "set_param", "name": "width", "value": "80 mm"}], "wider")
    v1 = vol(lb)
    assert v1 > v0
    lb.undo()
    assert vol(lb) == pytest.approx(v0)
    lb.redo()
    assert vol(lb) == pytest.approx(v1)
    log = lb.history_path.read_text().splitlines()
    assert [json.loads(x)["author"] for x in log] == ["agent", "undo", "redo"]


def test_scope(lb):
    lb.scope = {"hole_sketch", "hole_cut"}
    r = lb.apply([{"op": "set_dimension", "sketch": "slot_sketch", "name": "slot_w", "value": 8}], "x")
    assert not r["ok"] and "outside the current scope" in r["error"]
    assert lb.apply([{"op": "update_feature", "id": "hole_cut", "set": {"intent": "holes"}}], "ok")["ok"]


def test_remove_entity_cascades(lb):
    r = lb.apply([{"op": "remove_entity", "sketch": "slot_sketch", "id": "slot_left"}], "break")
    assert any("also removed 5 constraint" in n for n in r["notes"])
    assert not r["ok"]  # the slot is now open; the report says so


def test_zero_volume_cut_warns(lb):
    r = lb.apply([{"op": "update_feature", "id": "hole_cut", "set": {"direction": "normal"}}], "wrong way")
    assert not r["ok"]
    assert any("changed no volume" in w and "reverse" in w for w in r["warnings"])


def test_new_part_from_scratch(tmp_path):
    s = Session(tmp_path / "blk.vcad.json", create_name="block")
    r = s.apply([
        {"op": "set_param", "name": "a", "value": "10 mm"},
        {"op": "add_feature", "feature": {"id": "sk", "type": "sketch", "plane": {"datum": "XY"},
                                          "entities": [{"id": "c", "type": "circle", "center": [0, 0], "r": 4}],
                                          "constraints": [{"type": "coincident", "on": ["c.center", "origin"]},
                                                          {"type": "diameter", "on": ["c"], "value": "a"}]}},
        {"op": "add_feature", "feature": {"id": "rod", "type": "extrude", "profile": {"sketch": "sk"}, "distance": 20}},
    ], "rod")
    assert r["ok"] and vol(s) == pytest.approx(3.14159265 * 25 * 20, rel=1e-6)
    reopened = Session(tmp_path / "blk.vcad.json")
    assert reopened.result.part.volume == pytest.approx(vol(s))


def test_mcp_server_tools(tmp_path):
    from vibecad.mcp_server import server

    p = tmp_path / "pb.vcad.json"
    shutil.copy(EX / "pillow_block.vcad.json", p)

    async def run():
        names = {t.name for t in await server.list_tools()}
        assert {"open_part", "apply_ops", "render", "get_sketch", "undo"} <= names
        tree = (await server.call_tool("open_part", {"path": str(p)})).content[0].text
        assert "pillow_block" in tree
        img = (await server.call_tool("render", {"views": ["iso"]})).content[0]
        assert len(img.data) > 1000
        rep = json.loads((await server.call_tool("apply_ops", {
            "ops": [{"op": "set_param", "name": "bolt_spacing", "value": "46 mm"}], "message": "wider bolts"})).content[0].text)
        assert rep["ok"]
        sk = json.loads((await server.call_tool("get_sketch", {"sketch_id": "bolt_sketch"})).content[0].text)
        assert sk["dof"] == 0

    asyncio.run(run())


@pytest.mark.parametrize("op,expected", [
    ({"op": "add_rectangle", "sketch": "sk", "id": "p", "width": "W", "height": 40, "center": [0, 0]}, 60 * 40 * 2),
    ({"op": "add_rectangle", "sketch": "sk", "id": "p", "width": "W", "height": 40, "corner": [5, 5]}, 60 * 40 * 2),
    ({"op": "add_circle", "sketch": "sk", "id": "c", "diameter": "W / 2", "center": [3, "W"]}, 3.14159265 * 15 ** 2 * 2),
    ({"op": "add_slot", "sketch": "sk", "id": "s", "length": 12, "width": 4, "center": [3, -2], "angle": 30},
     (3.14159265 * 4 + 12 * 4) * 2),
])
def test_sketch_shortcuts_fully_constrained(tmp_path, op, expected):
    s = Session(tmp_path / "m.vcad.json", create_name="m")
    r = s.apply([{"op": "set_param", "name": "W", "value": "60 mm"},
                 {"op": "add_feature", "feature": {"id": "sk", "type": "sketch", "plane": {"datum": "XY"}}}, op,
                 {"op": "add_feature", "feature": {"id": "ex", "type": "extrude", "profile": {"sketch": "sk"}, "distance": 2}}],
                "shortcut")
    assert r["ok"], r
    assert s.result.sketches["sk"][0].report.dof == 0
    assert s.result.part.volume == pytest.approx(expected, rel=1e-6)
    # dimensions stay driven by params
    s.apply([{"op": "set_param", "name": "W", "value": "70 mm"}], "resize")
    assert s.result.sketches["sk"][0].report.dof == 0


def test_batch_inserts_at_same_anchor_keep_order(lb):
    r = lb.apply([
        {"op": "add_feature", "after": "wall", "feature": {"id": "rib_sk", "type": "sketch", "plane": {"datum": "YZ", "offset": 30}}},
        {"op": "add_rectangle", "sketch": "rib_sk", "id": "rib", "width": 10, "height": 10, "corner": [5, 5]},
        {"op": "add_feature", "after": "wall", "feature": {"id": "rib", "type": "extrude", "profile": {"sketch": "rib_sk"}, "distance": 2}},
    ], "rib")
    ids = [f.id for f in lb.doc.features]
    assert ids.index("wall") + 1 == ids.index("rib_sk") and ids.index("rib_sk") + 1 == ids.index("rib")
    assert r["applied"] == 3 and not r.get("errors"), r


def test_add_polygon_l_profile(tmp_path):
    s = Session(tmp_path / "m.vcad.json", create_name="m")
    r = s.apply([{"op": "set_param", "name": "leg", "value": "40 mm"}, {"op": "set_param", "name": "t", "value": "4 mm"},
                 {"op": "add_feature", "feature": {"id": "sk", "type": "sketch", "plane": {"datum": "XY"}}},
                 {"op": "add_polygon", "sketch": "sk", "id": "l",
                  "points": [[0, 0], ["leg", 0], ["leg", "t"], ["t", "t"], ["t", "leg"], [0, "leg"]]},
                 {"op": "add_feature", "feature": {"id": "ex", "type": "extrude", "profile": {"sketch": "sk"}, "distance": 20}}], "L")
    assert r["ok"], r
    assert s.result.sketches["sk"][0].report.dof == 0
    assert s.result.part.volume == pytest.approx((40 * 4 + 36 * 4) * 20)
    s.apply([{"op": "set_param", "name": "leg", "value": "50 mm"}], "longer")
    assert s.result.part.volume == pytest.approx((50 * 4 + 46 * 4) * 20)


@pytest.mark.parametrize("sides,across,diam,expected_area", [
    (6, "flats", 20, 6 * 10 ** 2 * tan(pi / 6)),           # hex standoff, wrench size (across flats)
    (5, "corners", 20, 0.5 * 5 * 10 ** 2 * sin(2 * pi / 5)),  # pentagon, circumscribed diameter
    (3, "corners", 20, 0.5 * 3 * 10 ** 2 * sin(2 * pi / 3)),  # triangle
])
def test_add_regular_polygon_area(tmp_path, sides, across, diam, expected_area):
    s = Session(tmp_path / "m.vcad.json", create_name="m")
    r = s.apply([{"op": "set_param", "name": "D", "value": f"{diam} mm"},
                 {"op": "add_feature", "feature": {"id": "sk", "type": "sketch", "plane": {"datum": "XY"}}},
                 {"op": "add_regular_polygon", "sketch": "sk", "id": "hex", "sides": sides, "diameter": "D",
                  "across": across, "center": [3, -2], "angle": 17},
                 {"op": "add_feature", "feature": {"id": "ex", "type": "extrude", "profile": {"sketch": "sk"}, "distance": 5}}],
                "polygon")
    assert r["ok"], r
    assert s.result.sketches["sk"][0].report.dof == 0
    assert s.result.part.volume == pytest.approx(expected_area * 5, rel=1e-6)
    # resizes when the driving param changes, and stays fully constrained
    s.apply([{"op": "set_param", "name": "D", "value": f"{diam * 1.5} mm"}], "bigger")
    assert s.result.sketches["sk"][0].report.dof == 0
    assert s.result.part.volume == pytest.approx(expected_area * 1.5 ** 2 * 5, rel=1e-6)


def test_add_regular_polygon_rejects_too_few_sides(tmp_path):
    s = Session(tmp_path / "m.vcad.json", create_name="m")
    r = s.apply([{"op": "add_feature", "feature": {"id": "sk", "type": "sketch", "plane": {"datum": "XY"}}},
                 {"op": "add_regular_polygon", "sketch": "sk", "id": "p", "sides": 2, "diameter": 10}], "bad")
    assert not r["ok"] and "at least 3 sides" in r["error"]


def test_open_part_reloads_when_file_changed_on_disk(tmp_path):
    from vibecad.workspace import Workspace

    ws = Workspace(tmp_path)
    ws.new_part("probe.vcad.json", "Probe")  # caches a Session with an empty doc
    shutil.copy(EX / "l_bracket.vcad.json", tmp_path / "probe.vcad.json")  # another tool rewrites it
    tree = ws.open_part("probe.vcad.json")  # same path: must pick up the new content, not the cached one
    assert "l_bracket" in tree
    png = ws.render()  # would raise "no solid yet" on the stale cached session
    assert len(png) > 1000


def test_point_id_as_coordinate_gets_hint(lb):
    r = lb.apply([{"op": "add_feature", "feature": {"id": "sk2", "type": "sketch", "plane": {"datum": "XY"},
                   "entities": [{"id": "a", "type": "line", "p1": "pt1", "p2": "pt2"}]}}], "bad")
    assert "not point ids" in r["error"]


def test_cut_off_the_part_says_position_not_direction(lb):
    r = lb.apply([{"op": "set_param", "name": "hole_z", "value": "56 mm"}], "holes above the wall")
    w = next(x for x in r["warnings"] if x.startswith("hole_cut"))
    assert "outside the body" in w and "need 'reverse'" not in w


def test_cut_that_splits_the_part_warns(tmp_path):
    p = tmp_path / "hs.vcad.json"
    shutil.copy(EX / "hex_standoff.vcad.json", p)
    s = Session(p)
    r = s.apply([{"op": "set_param", "name": "af", "value": "3.3 mm"}], "flats narrower than the bore")
    assert any("split the body into 6 separate solids" in w for w in r["warnings"]), r.get("warnings")


def test_pattern_copies_share_one_warning_line(tmp_path):
    p = tmp_path / "sp.vcad.json"
    shutil.copy(EX / "spacer_plate.vcad.json", p)
    s = Session(p)
    r = s.apply([{"op": "set_param", "name": "pcd", "value": "200 mm"}], "bolt circle off the plate")
    pattern = [w for w in r["warnings"] if w.startswith("bolt_pattern")]
    assert len(pattern) == 1 and pattern[0].endswith("(x5)"), pattern
