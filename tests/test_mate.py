"""Placing an import by its faces: the rotation round trip, and mates on a real import."""
import random

import build123d as bd
import numpy as np
import pytest

from vibecad import schema as S
from vibecad.mate import mate, rot_xyz, xyz_of
from vibecad.regen import Regenerator
from test_extrude_extras import _rect


def test_angles_round_trip_including_gimbal_lock():
    rnd = random.Random(3)
    cases = [[rnd.uniform(-180, 180) for _ in range(3)] for _ in range(200)] + [[30, 90, 0], [10, -90, 0], [0, 0, 0], [180, 0, 0]]
    for ang in cases:
        assert np.allclose(rot_xyz(xyz_of(rot_xyz(ang))), rot_xyz(ang), atol=1e-9), ang


def _place(rotate, translate, p):
    return rot_xyz(rotate) @ np.asarray(p, float) + np.asarray(translate, float)


@pytest.mark.parametrize("align", ["center", "touch"])
def test_mate_puts_the_face_against_the_target(align):
    rnd = random.Random(7)
    for _ in range(50):
        r0 = [rnd.uniform(-180, 180) for _ in range(3)]
        t0 = [rnd.uniform(-50, 50) for _ in range(3)]
        local_n, local_c = np.array([0, 0, 1.0]), np.array([1.0, 2.0, 3.0])  # a face of the body, in its own frame
        n_a, c_a = rot_xyz(r0) @ local_n, _place(r0, t0, local_c)
        n_b = np.array([rnd.uniform(-1, 1) for _ in range(3)]); n_b /= np.linalg.norm(n_b)
        c_b = np.array([rnd.uniform(-20, 20) for _ in range(3)])
        r1, t1 = mate(r0, t0, n_a, c_a, n_b, c_b, gap=0.25, align=align)
        n1, c1 = rot_xyz(r1) @ local_n, _place(r1, t1, local_c)
        assert np.allclose(n1, -n_b, atol=1e-6)
        assert np.dot(c1 - c_b, n_b) == pytest.approx(0.25, abs=1e-6)  # the gap, along the target's normal
        if align == "center":
            assert np.allclose(c1, c_b + 0.25 * n_b, atol=1e-6)


def test_mate_an_imported_box_onto_the_top_of_a_plate(tmp_path):
    from vibecad.app import App
    bd.export_step(bd.Box(10, 20, 30, align=bd.Align.MIN), str(tmp_path / "cube.step"))
    feats = [_rect("sk", 60, 40), {"id": "plate", "type": "extrude", "profile": {"sketch": "sk"}, "distance": 5},
             {"id": "cube", "type": "import", "file": "cube.step", "mode": "reference", "rotate": [20, 10, 0], "translate": [100, 0, 0]}]
    (tmp_path / "p.vcad.json").write_text(S.Document.model_validate({"name": "p", "features": feats}).model_dump_json())
    a = App(tmp_path, "claude-sonnet-5")
    a.ws.open_part(path="p.vcad.json")
    res = a.ws.session().result
    cube = bd.Shape.cast(res.refs["cube"].shape)
    # the cube's largest face (10 x 30... pick its 20 x 30 side) against the plate's top
    side = max(cube.faces(), key=lambda f: f.area)
    top = [f for f in bd.Shape.cast(res.part.wrapped).faces() if f.normal_at().Z > 0.99][0]
    out = a.mate("cube", {"label": res.refs["cube"].labels_of(side.wrapped)[0].__str__(), "point": list(side.center())},
                 {"label": "plate.end", "point": list(top.center())}, gap=0, align="center")
    assert out["ok"], out
    res = a.ws.session().result
    bb = bd.Shape.cast(res.refs["cube"].shape).bounding_box()
    assert bb.min.Z == pytest.approx(5, abs=1e-6) and bb.size.Z == pytest.approx(10, abs=1e-6)  # lying on its side on the plate
    assert bb.center().X == pytest.approx(0, abs=1e-6) and bb.center().Y == pytest.approx(0, abs=1e-6)
    f = a.ws.session().doc.feature("cube")
    assert f.translate != [100, 0, 0]
    with pytest.raises(Exception, match="must be on cube"):
        a.mate("cube", {"label": "plate.side[b]", "point": [0, -20, 2]}, {"label": "plate.end", "point": [0, 0, 5]})
    lab = str(res.refs["cube"].labels[0][1])
    with pytest.raises(Exception, match="other than cube"):
        a.mate("cube", {"label": lab, "point": [0, 0, 0]}, {"label": lab, "point": [0, 0, 0]})


def test_place_import_tool_for_the_agent(tmp_path):
    import json

    from vibecad.workspace import ToolError, Workspace, call
    bd.export_step(bd.Box(10, 20, 30, align=bd.Align.MIN), str(tmp_path / "cube.step"))
    feats = [_rect("sk", 60, 40), {"id": "plate", "type": "extrude", "profile": {"sketch": "sk"}, "distance": 5},
             {"id": "cube", "type": "import", "file": "cube.step", "mode": "reference", "translate": [100, 0, 0]}]
    (tmp_path / "p.vcad.json").write_text(S.Document.model_validate({"name": "p", "features": feats}).model_dump_json())
    ws = Workspace(tmp_path)
    ws.open_part(path="p.vcad.json")
    labels = sorted({str(l) for _, l in ws.session().result.refs["cube"].labels})
    # the cube's bottom (z = 0 in its file) sits 2 mm above the plate top, centred
    bottom = next(l for f, l in ws.session().result.refs["cube"].labels if bd.Face(f).normal_at().Z < -0.99)
    text, _ = call(ws, "place_import", {"import_id": "cube", "face": {"feature": "cube", "role": bottom.role, "entity": bottom.entity},
                                        "target": {"feature": "plate", "role": "end"}, "gap": 2})
    out = json.loads(text)
    assert out["ok"], out
    bb = bd.Shape.cast(ws.session().result.refs["cube"].shape).bounding_box()
    assert bb.min.Z == pytest.approx(7) and bb.center().X == pytest.approx(0) and bb.center().Y == pytest.approx(0)
    assert bb.size.X == pytest.approx(20) and bb.size.Y == pytest.approx(10)  # its long side along the plate's (60 x 40)
    assert out["contact"].startswith("the import's face now points -Z (down), against a face pointing +Z (up)")
    # the part's faces with outward normals, so a misnamed sketch edge is visible before placing against it
    rows = ws.face_labels("plate").splitlines()
    assert "normals point out" in rows[0]
    assert any(r.startswith("plate.end ") and "normal (0, 0, 1)" in r for r in rows)
    assert any(r.startswith("plate.start ") and "normal (0, 0, -1)" in r for r in rows)
    with pytest.raises(ToolError, match="must be on cube"):
        ws.place_import("cube", {"feature": "plate", "role": "end"}, {"feature": "plate", "role": "end"})
    with pytest.raises(ToolError, match="matched"):
        ws.place_import("cube", {"feature": "cube", "role": "face"}, {"feature": "plate", "role": "end"})
    assert len(labels) >= 6


def test_place_then_slide_onto_a_second_face(tmp_path):
    """A box against a wall, then slid down the wall until it sits on the base: two contacts in one call."""
    from vibecad.workspace import Workspace
    bd.export_step(bd.Box(10, 20, 30, align=bd.Align.MIN), str(tmp_path / "cube.step"))
    feats = [_rect("sk", 60, 40), {"id": "base", "type": "extrude", "profile": {"sketch": "sk"}, "distance": 5},
             _rect("wsk", 4, 40, cx=-28), {"id": "wall", "type": "extrude", "profile": {"sketch": "wsk"}, "distance": 60},
             {"id": "cube", "type": "import", "file": "cube.step", "mode": "reference", "translate": [100, 0, 50]}]
    (tmp_path / "p.vcad.json").write_text(S.Document.model_validate({"name": "p", "features": feats}).model_dump_json())
    ws = Workspace(tmp_path)
    ws.open_part(path="p.vcad.json")
    refs = ws.session().result.refs["cube"].labels
    lab = lambda test: next(l for f, l in refs if test(bd.Face(f).normal_at()))
    minus_x, minus_z = lab(lambda n: n.X < -0.99), lab(lambda n: n.Z < -0.99)
    ref = lambda l: {"feature": "cube", "role": l.role, "entity": l.entity}
    out = ws.place_import("cube", ref(minus_x), {"feature": "wall", "role": "side", "entity": "r"},
                          then_face=ref(minus_z), then_target={"feature": "base", "role": "end"})
    assert '"ok": true' in out, out
    bb = bd.Shape.cast(ws.session().result.refs["cube"].shape).bounding_box()
    assert bb.min.X == pytest.approx(-26, abs=1e-6) and bb.min.Z == pytest.approx(5, abs=1e-6), (bb.min, bb.max)
    with pytest.raises(Exception, match="go together"):
        ws.place_import("cube", ref(minus_x), {"feature": "wall", "role": "side", "entity": "r"}, then_face=ref(minus_z))


def test_second_face_pair_sets_the_spin(tmp_path):
    """Back on the wall, a long side on the base: the box turns so that side is down (landscape), whatever
    orientation the smallest turn would have left it in."""
    from vibecad.workspace import Workspace
    bd.export_step(bd.Box(10, 20, 30, align=bd.Align.MIN), str(tmp_path / "cube.step"))
    feats = [_rect("sk", 60, 60), {"id": "base", "type": "extrude", "profile": {"sketch": "sk"}, "distance": 5},
             _rect("wsk", 4, 60, cx=-28), {"id": "wall", "type": "extrude", "profile": {"sketch": "wsk"}, "distance": 60},
             {"id": "cube", "type": "import", "file": "cube.step", "mode": "reference", "translate": [100, 0, 50]}]
    (tmp_path / "p.vcad.json").write_text(S.Document.model_validate({"name": "p", "features": feats}).model_dump_json())
    ws = Workspace(tmp_path)
    ws.open_part(path="p.vcad.json")
    refs = ws.session().result.refs["cube"].labels
    lab = lambda test: next(l for f, l in refs if test(bd.Face(f).normal_at()))
    ref = lambda l: {"feature": "cube", "role": l.role, "entity": l.entity}
    out = ws.place_import("cube", ref(lab(lambda n: n.X < -0.99)), {"feature": "wall", "role": "side", "entity": "r"},
                          then_face=ref(lab(lambda n: n.Y < -0.99)), then_target={"feature": "base", "role": "end"})
    assert '"ok": true' in out, out
    bb = bd.Shape.cast(ws.session().result.refs["cube"].shape).bounding_box()
    assert bb.size.Z == pytest.approx(20, abs=1e-6) and bb.size.Y == pytest.approx(30, abs=1e-6), bb.size
    assert bb.min.Z == pytest.approx(5, abs=1e-6) and bb.min.X == pytest.approx(-26, abs=1e-6)


def test_one_face_pair_lines_up_long_sides(tmp_path):
    """A 10 x 20 x 30 box's 20 x 30 face against a wall that is long horizontally: the 30 side ends up horizontal."""
    from vibecad.workspace import Workspace
    bd.export_step(bd.Box(10, 20, 30, align=bd.Align.MIN), str(tmp_path / "cube.step"))
    feats = [_rect("wsk", 4, 120, cx=-28), {"id": "wall", "type": "extrude", "profile": {"sketch": "wsk"}, "distance": 40},
             {"id": "cube", "type": "import", "file": "cube.step", "mode": "reference", "translate": [100, 0, 50]}]
    (tmp_path / "p.vcad.json").write_text(S.Document.model_validate({"name": "p", "features": feats}).model_dump_json())
    ws = Workspace(tmp_path)
    ws.open_part(path="p.vcad.json")
    refs = ws.session().result.refs["cube"].labels
    minus_x = next(l for f, l in refs if bd.Face(f).normal_at().X < -0.99)
    ws.place_import("cube", {"feature": "cube", "role": minus_x.role, "entity": minus_x.entity}, {"feature": "wall", "role": "side", "entity": "r"})
    bb = bd.Shape.cast(ws.session().result.refs["cube"].shape).bounding_box()
    assert bb.size.Y == pytest.approx(30, abs=1e-6) and bb.size.Z == pytest.approx(20, abs=1e-6), bb.size
    assert bb.min.X == pytest.approx(-26, abs=1e-6)


def test_missing_import_lists_the_files_there(tmp_path):
    from vibecad.workspace import Workspace
    bd.export_step(bd.Box(1, 1, 1), str(tmp_path / "phone.step"))
    ws = Workspace(tmp_path)
    ws.new_part(path="t.vcad.json", name="t")
    rep = ws.apply_ops([{"op": "add_feature", "feature": {"id": "p", "type": "import", "file": "../phone.step", "mode": "reference"}}], "p")
    assert "phone.step" in rep and "files in" in rep, rep


def test_touch_alignment_also_lines_up_long_sides(tmp_path):
    from vibecad.workspace import Workspace
    bd.export_step(bd.Box(10, 20, 30, align=bd.Align.MIN), str(tmp_path / "cube.step"))
    feats = [_rect("wsk", 4, 120, cx=-28), {"id": "wall", "type": "extrude", "profile": {"sketch": "wsk"}, "distance": 40},
             {"id": "cube", "type": "import", "file": "cube.step", "mode": "reference", "translate": [100, 0, 5]}]
    (tmp_path / "p.vcad.json").write_text(S.Document.model_validate({"name": "p", "features": feats}).model_dump_json())
    ws = Workspace(tmp_path)
    ws.open_part(path="p.vcad.json")
    refs = ws.session().result.refs["cube"].labels
    minus_x = next(l for f, l in refs if bd.Face(f).normal_at().X < -0.99)
    ws.place_import("cube", {"feature": "cube", "role": minus_x.role, "entity": minus_x.entity},
                    {"feature": "wall", "role": "side", "entity": "r"}, align="touch")
    bb = bd.Shape.cast(ws.session().result.refs["cube"].shape).bounding_box()
    assert bb.size.Y == pytest.approx(30, abs=1e-6) and bb.min.X == pytest.approx(-26, abs=1e-6), (bb.size, bb.min)


def test_place_import_on_a_datum_plane(tmp_path):
    """Stand an import that arrived lying on its side on a datum, before the part has any geometry."""
    from vibecad.workspace import Workspace
    # a 10 x 20 x 30 block exported lying down: its 'bottom' (local -Z) now faces +Y
    bd.export_step(bd.Rot(90, 0, 0) * bd.Box(10, 20, 30, align=bd.Align.MIN), str(tmp_path / "blk.step"))
    feats = [{"id": "blk", "type": "import", "file": "blk.step", "mode": "reference", "translate": [40, 50, 60]}]
    (tmp_path / "p.vcad.json").write_text(S.Document.model_validate({"name": "p", "features": feats}).model_dump_json())
    ws = Workspace(tmp_path)
    ws.open_part(path="p.vcad.json")
    ref = ws.session().result.refs["blk"]
    bottom = next(l for f, l in ref.labels if bd.Face(f).normal_at().Y > 0.99 and bd.Face(f).area == pytest.approx(200))
    out = ws.place_import("blk", {"feature": "blk", "role": bottom.role, "entity": bottom.entity}, {"datum": "XY", "offset": 5})
    bb = bd.Shape.cast(ws.session().result.refs["blk"].shape).bounding_box()
    assert bb.min.Z == pytest.approx(5) and bb.size.Z == pytest.approx(30)  # standing on its bottom, on z = 5
    assert bb.center().X == pytest.approx(0) and bb.center().Y == pytest.approx(0)
    assert bb.size.X == pytest.approx(20)  # long side along the datum's x
    assert "points -Z (down)" in __import__("json").loads(out)["contact"]


def test_agent_tools_open_the_only_part_and_import_faces_need_no_role(tmp_path):
    import json

    from vibecad.workspace import Workspace, call
    bd.export_step(bd.Box(10, 20, 30, align=bd.Align.MIN), str(tmp_path / "cube.step"))
    feats = [_rect("sk", 60, 40), {"id": "plate", "type": "extrude", "profile": {"sketch": "sk"}, "distance": 5},
             {"id": "cube", "type": "import", "file": "cube.step", "mode": "reference", "translate": [100, 0, 0]}]
    (tmp_path / "p.vcad.json").write_text(S.Document.model_validate({"name": "p", "features": feats}).model_dump_json())
    ws = Workspace(tmp_path)
    text, _ = call(ws, "get_tree", {})  # no open_part first: the folder's only part is opened
    assert "plate" in text
    bottom = next(l for f, l in ws.session().result.refs["cube"].labels if bd.Face(f).normal_at().Z < -0.99)
    out = json.loads(ws.place_import("cube", {"feature": "cube", "entity": bottom.entity}, {"feature": "plate", "role": "end"}))
    assert out["ok"], out


def test_datum_placement_centres_the_whole_body(tmp_path):
    """The bench's micro servo arrives lying on its side; stood shaft-up on one tab's underside, its body is
    centred on the origin, long along x, so its tab holes are symmetric: (±13.9, 0)."""
    import shutil
    from pathlib import Path

    from vibecad.workspace import Workspace
    shutil.copy(Path(__file__).parents[1] / "bench/fixtures/servo.step", tmp_path / "servo.step")
    feats = [{"id": "servo", "type": "import", "file": "servo.step", "mode": "reference"}]
    (tmp_path / "p.vcad.json").write_text(S.Document.model_validate({"name": "p", "features": feats}).model_dump_json())
    ws = Workspace(tmp_path)
    ws.open_part(path="p.vcad.json")
    ref = ws.session().result.refs["servo"]
    shaft = next(f for f, _ in ref.labels if bd.Face(f).geom_type == bd.GeomType.CYLINDER and bd.Face(f).radius > 2.3
                 and bd.Face(f).radius < 2.5)
    up = -np.array(tuple(bd.Face(shaft).axis_of_rotation.direction))  # the shaft's axis; which sign is up is below
    # the tab undersides: small faces facing away from the shaft end
    tabs = [l for f, l in ref.labels if bd.Face(f).geom_type == bd.GeomType.PLANE and bd.Face(f).area < 60
            and abs(float(np.dot(tuple(bd.Face(f).normal_at()), up))) > 0.99]
    for lab in tabs:
        ws.place_import("servo", {"feature": "servo", "entity": lab.entity}, {"datum": "XY"})
        r = ws.session().result.refs["servo"]
        bb = bd.Shape.cast(r.shape).bounding_box()
        if bb.min.Z < -1:  # the face on the other end of the tab pair: shaft down; try the next
            continue
        holes = sorted((round(c.X, 2), round(c.Y, 2)) for c in (bd.Face(f).axis_of_rotation.position for f, _ in r.labels
                       if bd.Face(f).geom_type == bd.GeomType.CYLINDER and abs(bd.Face(f).radius - 1.0) < 1e-6))
        xs = sorted({x for x, _ in holes})
        assert xs == [-13.9, 13.9] and all(abs(y) < 1e-6 for _, y in holes)
        assert bb.center().X == pytest.approx(0) and bb.center().Y == pytest.approx(0) and bb.size.X == pytest.approx(32.3)
        return
    pytest.fail("no tab underside stood the servo shaft-up")
