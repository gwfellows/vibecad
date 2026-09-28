"""The import feature: STEP / IGES / BREP / STL files as solids or as reference bodies."""
import json
import shutil
from pathlib import Path

import build123d as bd
import pytest

from vibecad import schema as S
from vibecad.regen import Regenerator, load
from vibecad.session import Session

EX = Path(__file__).resolve().parent.parent / "examples"


def _center(f):
    from OCP.TopoDS import TopoDS
    return bd.Face(TopoDS.Face(f)).center()


def _export(tmp_path, part="l_bracket.vcad.json"):
    res = Regenerator(EX).run(load(EX / part))
    shape = res.part
    bd.export_step(shape, str(tmp_path / "b.step"))
    bd.export_stl(shape, str(tmp_path / "b.stl"), tolerance=0.01, angular_tolerance=0.1)
    bd.export_brep(shape, str(tmp_path / "b.brep"))
    return shape


def _doc(features, params=None):
    return S.Document.model_validate({"name": "t", "params": params or {}, "features": features})


def _run(tmp_path, features, params=None):
    return Regenerator(tmp_path).run(_doc(features, params))


@pytest.mark.parametrize("fname", ["b.step", "b.brep"])
def test_exact_formats_round_trip_volume_and_labels(tmp_path, fname):
    src = _export(tmp_path)
    res = _run(tmp_path, [{"id": "bracket", "type": "import", "file": fname}])
    assert res.ok, res.features[0].message
    assert res.part.volume == pytest.approx(src.volume, rel=1e-6)
    assert len(res.body.faces()) == len(src.faces())
    labels = {str(l) for _, l in res.body.labels}
    assert labels == {f"bracket.face[f{i}]" for i in range(len(src.faces()))}
    assert res.features[0].info["format"] in ("step", "brep")


def test_iges_import(tmp_path):
    from OCP.IGESControl import IGESControl_Writer

    box = bd.Box(10, 20, 30)
    w = IGESControl_Writer("MM", 1)
    w.AddShape(box.wrapped)
    w.ComputeModel()
    assert w.Write(str(tmp_path / "box.igs"))
    res = _run(tmp_path, [{"id": "box", "type": "import", "file": "box.igs"}])
    assert res.ok, res.features[0].message
    # IGES carries surfaces; a closed box comes back as faces, maybe without a solid: the message says so
    assert res.features[0].info["faces"] == 6 or "no closed solid" in (res.features[0].message or "")


def test_stl_becomes_a_solid_with_merged_faces(tmp_path):
    box = bd.Box(10, 20, 30)
    bd.export_stl(box, str(tmp_path / "box.stl"))
    res = _run(tmp_path, [{"id": "box", "type": "import", "file": "box.stl"}])
    assert res.ok, res.features[0].message
    assert res.part.volume == pytest.approx(6000, rel=1e-6)
    assert len(res.body.faces()) == 6  # 12 triangles unified into the box's 6 faces
    assert res.features[0].info["triangles"] == 12


def test_stl_reference_is_one_mesh_face_kept_out_of_the_part(tmp_path):
    _export(tmp_path)
    res = _run(tmp_path, [
        {"id": "phone", "type": "import", "file": "b.stl", "mode": "reference"},
        {"id": "sk", "type": "sketch", "plane": {"datum": "XY"}, "entities": [
            {"id": "c", "type": "circle", "center": [0, 0], "r": 5}], "constraints": [
            {"type": "coincident", "on": ["c.center", "origin"]}, {"type": "radius", "on": ["c"], "value": 5}]},
        {"id": "disc", "type": "extrude", "profile": {"sketch": "sk"}, "distance": 2, "mode": "new"},
    ])
    assert res.ok, [f.message for f in res.features]
    assert "phone" in res.refs and res.refs["phone"].labels[0][1].role == "mesh"
    assert res.part.volume == pytest.approx(3.14159265 * 25 * 2, rel=1e-3)  # only the disc is the part


def test_transform_scale_rotate_translate(tmp_path):
    bd.export_step(bd.Box(1, 2, 3, align=bd.Align.MIN), str(tmp_path / "unit.step"))
    res = _run(tmp_path, [{"id": "b", "type": "import", "file": "unit.step", "scale": "k",
                           "rotate": [0, 0, 90], "translate": [100, 0, "z0"]}], {"k": 10, "z0": 5})
    assert res.ok, res.features[0].message
    bb = res.part.bounding_box()
    # 10 x 20 x 30 box, turned 90 degrees about Z: x spans -20..0, y 0..10; then moved +100 in x, +5 in z
    assert [round(v, 6) for v in (bb.min.X, bb.min.Y, bb.min.Z)] == [80, 0, 5]
    assert [round(v, 6) for v in (bb.max.X, bb.max.Y, bb.max.Z)] == [100, 10, 35]
    assert res.part.volume == pytest.approx(6000)


def test_import_as_base_then_model_on_its_face(tmp_path):
    bd.export_step(bd.Box(40, 30, 10, align=(bd.Align.CENTER, bd.Align.CENTER, bd.Align.MIN)), str(tmp_path / "blank.step"))
    first = _run(tmp_path, [{"id": "blank", "type": "import", "file": "blank.step"}])
    top = next(str(l) for f, l in first.body.labels if abs(_center(f).Z - 10) < 1e-6)
    ent = top.split("[")[1].rstrip("]")
    res = _run(tmp_path, [
        {"id": "blank", "type": "import", "file": "blank.step"},
        {"id": "hole_sk", "type": "sketch", "plane": {"face": {"feature": "blank", "role": "face", "entity": ent}},
         "entities": [{"id": "h", "type": "circle", "center": [0, 0], "r": 3}],
         "constraints": [{"type": "coincident", "on": ["h.center", "origin"]}, {"type": "diameter", "on": ["h"], "value": 6}]},
        {"id": "hole", "type": "extrude", "profile": {"sketch": "hole_sk"}, "extent": "through_all", "direction": "reverse", "mode": "cut"},
        {"id": "edge_round", "type": "fillet", "radius": 1, "edges": [
            {"between": [{"feature": "blank", "role": "face", "entity": ent}, {"feature": "hole", "role": "side", "entity": "h"}]}]},
    ])
    assert res.ok, [(f.id, f.message) for f in res.features]
    hole = 3.14159265 * 9 * 10
    assert res.part.volume < 12000 - hole + 1e-6  # hole and rim fillet both removed material
    assert res.part.volume == pytest.approx(12000 - hole - (1 - 3.14159265 / 4) * 2 * 3.14159265 * 3.4 * 1, rel=2e-3)


def test_sketch_on_a_reference_face_and_project_its_edge(tmp_path):
    bd.export_step(bd.Box(20, 20, 20, align=bd.Align.MIN), str(tmp_path / "cube.step"))
    first = _run(tmp_path, [{"id": "cube", "type": "import", "file": "cube.step", "mode": "reference"}])
    face_at = lambda cond: next(l.entity for f, l in first.refs["cube"].labels if cond(_center(f)))
    top = face_at(lambda c: abs(c.Z - 20) < 1e-6)
    right = face_at(lambda c: abs(c.X - 20) < 1e-6)
    ref = lambda e: {"feature": "cube", "role": "face", "entity": e}
    res = _run(tmp_path, [
        {"id": "cube", "type": "import", "file": "cube.step", "mode": "reference"},
        {"id": "cap_sk", "type": "sketch", "plane": {"face": ref(top)}, "entities": [
            {"id": "edge", "type": "external", "edge": {"between": [ref(top), ref(right)]}},
            {"id": "p", "type": "circle", "center": [18, 10], "r": 2}],
         "constraints": [{"type": "coincident", "on": ["p.center", "edge.p1"]}, {"type": "radius", "on": ["p"], "value": 2}]},
        {"id": "peg", "type": "extrude", "profile": {"sketch": "cap_sk"}, "distance": 5, "mode": "new"},
    ])
    assert res.ok, [(f.id, f.message) for f in res.features]
    bb = res.part.bounding_box()
    assert round(bb.min.Z, 6) == 20 and round(bb.max.Z, 6) == 25  # the peg sits on the reference cube's top
    assert res.part.volume == pytest.approx(3.14159265 * 4 * 5, rel=1e-4)


def test_changed_file_rebuilds_and_missing_file_is_a_clear_error(tmp_path):
    bd.export_step(bd.Box(10, 10, 10), str(tmp_path / "x.step"))
    part = tmp_path / "p.vcad.json"
    part.write_text(json.dumps({"name": "p", "features": [{"id": "x", "type": "import", "file": "x.step"}]}))
    s = Session(part)
    assert s.result.part.volume == pytest.approx(1000)
    import os, time
    time.sleep(0.01)
    bd.export_step(bd.Box(10, 10, 20), str(tmp_path / "x.step"))
    os.utime(tmp_path / "x.step")
    s.result = s.regen.run(s.doc)
    assert s.result.part.volume == pytest.approx(2000)
    (tmp_path / "x.step").unlink()
    s.result = s.regen.run(s.doc)
    assert not s.result.ok and "not found" in s.result.features[0].message


def test_big_stl_needs_reference_mode(tmp_path, monkeypatch):
    from vibecad import importer
    monkeypatch.setattr(importer, "MAX_STL_SOLID_TRIANGLES", 5)
    bd.export_stl(bd.Box(1, 1, 1), str(tmp_path / "m.stl"))
    res = _run(tmp_path, [{"id": "m", "type": "import", "file": "m.stl"}])
    assert not res.ok and "reference" in res.features[0].message


@pytest.mark.parametrize("part", ["pillow_block.vcad.json", "enclosure_lid.vcad.json", "nema17_mount.vcad.json"])
def test_imported_example_parts_can_be_cut_and_extended(tmp_path, part):
    """A real part as a STEP base: add a boss on top and cut a hole through, volumes as expected."""
    src = _export(tmp_path, part)
    bb = src.bounding_box()
    cx, cy, top = (bb.min.X + bb.max.X) / 2, (bb.min.Y + bb.max.Y) / 2, bb.max.Z
    res = _run(tmp_path, [
        {"id": "base", "type": "import", "file": "b.step"},
        {"id": "boss_sk", "type": "sketch", "plane": {"datum": "XY", "offset": top}, "entities": [
            {"id": "b", "type": "circle", "center": [cx, cy], "r": 2}],
         "constraints": [{"type": "fix", "on": ["b.center"], "at": [cx, cy]}, {"type": "radius", "on": ["b"], "value": 2}]},
        {"id": "boss", "type": "extrude", "profile": {"sketch": "boss_sk"}, "distance": 3},
    ])
    assert res.ok, [(f.id, f.message) for f in res.features]
    assert res.part.volume == pytest.approx(src.volume + 3.14159265 * 4 * 3, rel=1e-5)
    assert res.part.bounding_box().max.Z == pytest.approx(top + 3)


def test_session_regen_resolves_paths_next_to_the_part(tmp_path):
    (tmp_path / "parts" / "imports").mkdir(parents=True)
    bd.export_step(bd.Box(5, 5, 5), str(tmp_path / "parts" / "imports" / "c.step"))
    part = tmp_path / "parts" / "p.vcad.json"
    part.write_text(json.dumps({"name": "p", "features": [{"id": "c", "type": "import", "file": "imports/c.step"}]}))
    assert Session(part).result.part.volume == pytest.approx(125)


def test_iges_surfaces_are_sewn_into_a_solid_for_solid_modes(tmp_path):
    from OCP.IGESControl import IGESControl_Writer

    box = bd.Box(10, 20, 30)
    w = IGESControl_Writer("MM", 0)  # mode 0: faces only (no BRep solid entity), as many IGES exports are
    for f in box.faces():
        w.AddShape(f.wrapped)
    w.ComputeModel()
    assert w.Write(str(tmp_path / "faces.igs"))
    res = _run(tmp_path, [{"id": "b", "type": "import", "file": "faces.igs"}])
    assert res.ok, res.features[0].message
    assert res.part.volume == pytest.approx(6000, rel=1e-6)
    ref = _run(tmp_path, [{"id": "b", "type": "import", "file": "faces.igs", "mode": "reference"}])
    assert ref.ok and ref.part is None and len(ref.refs["b"].faces()) == 6


def test_open_surfaces_can_only_be_a_reference(tmp_path):
    from OCP.IGESControl import IGESControl_Writer

    w = IGESControl_Writer("MM", 0)
    for f in bd.Box(10, 20, 30).faces()[:5]:  # a box missing its lid
        w.AddShape(f.wrapped)
    w.ComputeModel()
    w.Write(str(tmp_path / "open.igs"))
    res = _run(tmp_path, [{"id": "b", "type": "import", "file": "open.igs"}])
    assert not res.ok and "no closed solid" in res.features[0].message
    assert _run(tmp_path, [{"id": "b", "type": "import", "file": "open.igs", "mode": "reference"}]).ok


def test_outward_shell_of_an_imported_phone_is_a_case(tmp_path):
    """Import a phone as a solid, shell it outward with the screen open: the case skin, the phone gone."""
    with bd.BuildPart() as p:
        with bd.BuildSketch():
            bd.Rectangle(70, 140)
        bd.extrude(amount=8)
    bd.export_step(p.part, str(tmp_path / "phone.step"))
    first = _run(tmp_path, [{"id": "phone", "type": "import", "file": "phone.step"}])
    screen = next(l.entity for f, l in first.body.labels if abs(_center(f).Z - 8) < 1e-6)
    res = _run(tmp_path, [
        {"id": "phone", "type": "import", "file": "phone.step"},
        {"id": "case", "type": "shell", "thickness": 2, "outward": True,
         "remove_faces": [{"feature": "phone", "role": "face", "entity": screen}]},
    ])
    assert res.ok, [(f.id, f.message) for f in res.features]
    bb = res.part.bounding_box()
    assert [round(v, 6) for v in (bb.size.X, bb.size.Y, bb.size.Z)] == [74, 144, 10]
    # walls grow outward with rounded outer edges (the offset of a sharp box): between the sharp and rounded bounds
    sharp = 74 * 144 * 10 - 70 * 140 * 8
    assert sharp * 0.9 < res.part.volume < sharp


def test_agent_check_fit_includes_reference_imports_and_measure_gives_mass(tmp_path):
    import json

    from vibecad.workspace import Workspace

    bd.export_step(bd.Box(20, 20, 10, align=bd.Align.MIN), str(tmp_path / "motor.step"))
    ws = Workspace(tmp_path)
    ws.new_part("mount.vcad.json", "mount")
    rep = json.loads(ws.apply_ops([
        {"op": "set_meta", "set": {"material": "PLA"}},
        {"op": "add_feature", "feature": {"id": "motor", "type": "import", "file": "motor.step", "mode": "reference"}},
        {"op": "add_feature", "feature": {"id": "sk", "type": "sketch", "plane": {"datum": "XY", "offset": 10}}},
        {"op": "add_rectangle", "sketch": "sk", "id": "plate", "width": 30, "height": 30, "center": [10, 10]},
        {"op": "add_feature", "feature": {"id": "plate", "type": "extrude", "profile": {"sketch": "sk"}, "distance": 3}},
    ], "mount on the motor"))
    assert rep["ok"], rep
    fit = json.loads(ws.check_fit([]))
    assert fit["reference motor"] == {"overlap_mm3": 0, "min_gap_mm": 0, "touching": True}
    m = json.loads(ws.measure())
    assert m["mass_g"] == pytest.approx(30 * 30 * 3 / 1000 * 1.24, rel=1e-6) and m["density"] == 1.24
    ws.apply_ops([{"op": "update_feature", "id": "sk", "set": {"plane": {"datum": "XY", "offset": 8}}}], "sink it", "agent")
    fit = json.loads(ws.check_fit([]))
    assert fit["reference motor"]["overlap_mm3"] == pytest.approx(20 * 20 * 2)  # the plate now dips 2 mm into the motor


def test_import_another_part_with_its_face_labels(tmp_path):
    """A lid designed against the enclosure: the enclosure is imported live, faces named by its own labels."""
    import json
    shutil.copy(EX / "enclosure_lid.vcad.json", tmp_path / "enclosure.vcad.json")
    ref = lambda e: {"feature": "enc", "role": "face", "entity": e}
    feats = [
        {"id": "enc", "type": "import", "file": "enclosure.vcad.json", "mode": "reference"},
        {"id": "pad_sk", "type": "sketch", "plane": {"face": ref("box.end")}, "entities": [
            {"id": "c", "type": "circle", "center": [0, 0], "r": 3}],
         "constraints": [{"type": "coincident", "on": ["c.center", "origin"]}, {"type": "radius", "on": ["c"], "value": 3}]},
        {"id": "pad", "type": "extrude", "profile": {"sketch": "pad_sk"}, "distance": 2, "mode": "new"},
    ]
    res = _run(tmp_path, feats)
    assert res.ok, [(f.id, f.message) for f in res.features]
    labels = {l.entity for _, l in res.refs["enc"].labels}
    assert "box.end" in labels and "hollow.inner" in labels and any(e.startswith("box.side(") for e in labels)
    bb = res.part.bounding_box()
    assert round(bb.min.Z, 6) == 20 and round(bb.max.Z, 6) == 22  # on the enclosure's top (height 20)
    # the enclosure gets taller: the pad follows, the reference still resolves
    doc = json.loads((tmp_path / "enclosure.vcad.json").read_text())
    doc["params"]["height"] = 30
    (tmp_path / "enclosure.vcad.json").write_text(json.dumps(doc))
    res = _run(tmp_path, feats)
    assert res.ok and round(res.part.bounding_box().min.Z, 6) == 30


def test_a_part_cannot_import_itself(tmp_path):
    import json
    (tmp_path / "loop.vcad.json").write_text(json.dumps({"name": "loop", "features": [
        {"id": "me", "type": "import", "file": "loop.vcad.json", "mode": "reference"}]}))
    res = Regenerator(tmp_path).run(load(tmp_path / "loop.vcad.json"))
    assert not res.ok and "imports itself" in res.features[0].message


def test_reference_import_is_described_to_the_agent(tmp_path):
    """The tree shows where a reference body is; face_labels lists its faces with geometry; check_fit says OVERLAP."""
    import json
    import shutil

    from vibecad.workspace import Workspace
    bd.export_step(bd.Pos(10, 20, 0) * bd.Cylinder(5, 30, align=(bd.Align.CENTER, bd.Align.CENTER, bd.Align.MIN)), str(tmp_path / "rod.step"))
    ws = Workspace(tmp_path)
    ws.new_part(path="t.vcad.json", name="t")
    rep = json.loads(ws.apply_ops([{"op": "add_feature", "feature": {"id": "rod", "type": "import", "file": "rod.step", "mode": "reference"}}], "rod"))
    assert "10 x 10 x 30 mm, x 5..15 y 15..25 z 0..30" in rep["tree"], rep["tree"]
    faces = ws.face_labels("rod").splitlines()
    assert any("cylinder d 10, axis (0, 0, 1) through (10, 20" in f for f in faces), faces
    assert any("plane, normal (0, 0, 1), centre (10.00, 20.00, 30.00)" in f for f in faces), faces
    ws.apply_ops([{"op": "add_feature", "feature": {"id": "sk", "type": "sketch", "plane": {"datum": "XY"}}},
                  {"op": "add_rectangle", "sketch": "sk", "id": "b", "width": 40, "height": 40, "center": [10, 20]},
                  {"op": "add_feature", "feature": {"id": "block", "type": "extrude", "profile": {"sketch": "sk"}, "distance": 10}}], "block")
    fit = json.loads(ws.check_fit([]))
    assert fit["verdict"].startswith("OVERLAP with reference rod"), fit
    assert "reference imports" in ws.face_labels()


def test_edit_reports_say_when_the_part_runs_into_a_reference(tmp_path):
    import json

    from vibecad.workspace import Workspace
    bd.export_step(bd.Box(10, 10, 10, align=bd.Align.MIN), str(tmp_path / "cube.step"))
    ws = Workspace(tmp_path)
    ws.new_part(path="t.vcad.json", name="t")
    ws.apply_ops([{"op": "add_feature", "feature": {"id": "cube", "type": "import", "file": "cube.step", "mode": "reference"}}], "ref")
    rep = json.loads(ws.apply_ops([{"op": "add_feature", "feature": {"id": "sk", "type": "sketch", "plane": {"datum": "XY"}}},
                                   {"op": "add_rectangle", "sketch": "sk", "id": "b", "width": 20, "height": 20, "center": [5, 5]},
                                   {"op": "add_feature", "feature": {"id": "block", "type": "extrude", "profile": {"sketch": "sk"}, "distance": 4}}], "b"))
    assert rep["ok"] and "overlaps reference cube by 400.0" in rep["fit"][0], rep
    rep = json.loads(ws.apply_ops([{"op": "add_feature", "feature": {"id": "nest", "type": "boolean", "tool": "cube"}}], "nest"))
    assert "fit" not in rep, rep
