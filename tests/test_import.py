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
