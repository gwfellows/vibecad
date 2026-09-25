"""GUI server logic (the App object behind the HTTP routes), without a browser."""
import shutil
import threading
from pathlib import Path

import pytest

from vibecad.app import App

EX = Path(__file__).resolve().parent.parent / "examples"


def _app(tmp_path, part="l_bracket.vcad.json"):
    shutil.copy(EX / part, tmp_path / part)
    a = App(tmp_path, "sonnet")
    a.ws.open_part(part)
    return a


def test_concurrent_mesh_and_render(tmp_path):
    # the browser asks for the mesh twice per edit and the agent may render at the same time; OCCT meshes
    # shapes in place, so unsynchronized calls returned faces without triangulation (HTTP 500)
    a = _app(tmp_path, "pillow_block.vcad.json")
    errors = []

    def run(fn):
        try:
            fn()
        except Exception as e:
            errors.append(repr(e))

    threads = [threading.Thread(target=run, args=(a.mesh,)) for _ in range(3)]
    threads.append(threading.Thread(target=run, args=(lambda: a.ws.render(["iso"]),)))
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors
    assert a.mesh()["faces"]


def test_rejected_param_leaves_rollback_working(tmp_path):
    a = _app(tmp_path)
    rep = a.ws.apply_ops([{"op": "set_param", "name": "hole_d", "value": "2 *"}], "typo", "user")
    assert '"applied": 0' in rep
    a.rollback = 2
    st = a.state()
    assert st["rollback"] == 2 and st["volume"] < a.ws.session().result.summary()["volume_mm3"]
    assert a.mesh()["faces"]


def test_rollback_mesh_and_sketch_geometry(tmp_path):
    a = _app(tmp_path)
    full = len(a.mesh()["faces"])
    a.rollback = 2  # base only
    assert len(a.mesh()["faces"]) == 6 < full
    sk = a.sketch_geometry("base_sketch")
    assert sk["dof"] == 0
    assert {e["id"] for e in sk["entities"]} == {"base_front", "base_right", "base_back", "base_left"}
    a.rollback = None
    assert len(a.mesh()["faces"]) == full


def test_sketch_geometry_for_the_editor(tmp_path):
    a = _app(tmp_path)
    sk = a.sketch_geometry("base_sketch")
    assert all(e["fixed"] for e in sk["entities"])  # 0 DOF: everything is fully constrained
    front = next(e for e in sk["entities"] if e["id"] == "base_front")
    assert front["p1"] == [0, 0] and front["p2"] == [60, 0]
    dims = {c["name"]: c for c in sk["constraints"] if c.get("name")}
    assert dims["width"]["value"] == 60 and dims["width"]["param"] == "width"
    assert {"origin", "base_front.p1", "base_front.p2"} <= {p["ref"] for p in sk["points"]}
    assert "width" in sk["params"]


def test_sketch_drag_preview_changes_nothing_until_committed(tmp_path):
    a = _app(tmp_path)
    a.ws.apply_ops([
        {"op": "add_feature", "feature": {"id": "free_sk", "type": "sketch", "plane": {"datum": "XY"}}},
        {"op": "add_entity", "sketch": "free_sk", "entity": {"id": "l", "type": "line", "p1": [0, 0], "p2": [10, 0]}},
        {"op": "add_constraint", "sketch": "free_sk", "constraint": {"type": "horizontal", "on": ["l"]}},
    ], "free line", "user")
    before = a.ws.session().path.read_text()
    pv = a.sketch_drag("free_sk", "l.p2", [20, 5])
    line = next(e for e in pv["entities"] if e["id"] == "l")
    assert pv["moved"] and line["p2"] == [20, 5] and line["p1"][1] == 5  # p1 is free: the line rises, staying level
    assert a.ws.session().path.read_text() == before
    free = {e["id"]: e["fixed"] for e in a.sketch_geometry("free_sk")["entities"]}
    assert free == {"l": False}
    ir = next(e for e in pv["ir"] if e["id"] == "l")
    rep = a.ws.apply_ops([{"op": "update_entity", "sketch": "free_sk", "id": "l",
                           "set": {k: v for k, v in ir.items() if k != "id"}}], "drag", "user")
    assert '"applied": 1' in rep
    assert a.ws.session().result.sketches["free_sk"][0].entities["l"].p2 == pytest.approx((20, 5))
    fixed = a.sketch_drag("base_sketch", "base_front.p2", [80, 5])
    assert not fixed["moved"]


def test_sketch_selection_context_lists_touching_constraints(tmp_path):
    from vibecad.app import sketch_selection_context

    a = _app(tmp_path)
    s = sketch_selection_context(a.ws.session().doc.feature("slot_sketch"), ["slot_left"])
    assert s.startswith("[In sketch `slot_sketch` the user selected: slot_left.")
    assert "slot_w" in s or "slot_left" in s.split("Constraints on them:")[1]


def test_face_context_gives_ready_face_refs():
    from vibecad.app import face_context

    s = face_context({"labels": ["wall.side[wall_top]@slot_mirror#1", "base.end"], "point": [1, 2, 3.456]})
    assert "at (1.00, 2.00, 3.46)" in s
    assert '{"feature": "wall", "role": "side", "entity": "wall_top", "instance": "slot_mirror#1"}' in s
    assert 'base.end = {"feature": "base", "role": "end"}' in s


def test_marks_context_names_nearby_geometry(tmp_path):
    a = _app(tmp_path)
    # a stroke drawn along the base's front edge (y = 0, x 10..50) and a small closed loop near the far corner
    stroke = [[10 + i, 0.3] for i in range(41)]
    loop = [[58 + 1.5 * __import__("math").cos(t / 4), 38 + 1.5 * __import__("math").sin(t / 4)] for t in range(26)]
    s = a.marks_context("base_sketch", [stroke, loop])
    assert s.startswith("[The user drew 2 freehand mark(s) on sketch `base_sketch`")
    first, second = s.split(" | ")
    assert "stroke" in first and "passes near base_front" in first
    assert "closed loop" in second and ("base_right" in second or "base_back" in second)


def test_face_outline_projects_every_edge_of_a_face_sketch(tmp_path):
    a = _app(tmp_path)
    out = a.face_outline("slot_sketch")  # on the base's top face: 4 outer edges + the wall's foot... all named
    assert out["entities"] and all(e["type"] == "external" for e in out["entities"])
    rep = a.ws.apply_ops([{"op": "add_entity", "sketch": "slot_sketch", "entity": e} for e in out["entities"]], "outline", "user")
    assert '"ok": true' in rep, rep
    solved = a.ws.session().result.sketches["slot_sketch"][0]
    ext = [solved.entities[e["id"]] for e in out["entities"]]
    assert all(e.construction for e in ext)
    xs = sorted({round(p[0], 6) for e in ext for p in (e.p1, e.p2)})
    assert xs[0] == 0 and xs[-1] == 60  # the base is 60 wide
    assert solved.report.dof == 0
    with pytest.raises(Exception, match="only a sketch on a face"):
        a.face_outline("base_sketch")


def test_every_edge_of_the_examples_gets_a_verified_ref(tmp_path):
    from vibecad import schema as S
    from vibecad.topo import list_edges, resolve_edges

    for part in ("l_bracket.vcad.json", "pillow_block.vcad.json", "edge_holes_plate.vcad.json", "enclosure_lid.vcad.json"):
        a = _app(tmp_path, part)
        body = a.view_result().body
        edges = list_edges(body.shape)
        seams = a.mesh()["edge_seam"]
        assert len(seams) == len(edges)
        for i, e in enumerate(edges):
            if seams[i]:  # a cylinder's seam lies inside one face: not an edge you can pick
                continue
            r = a.edge_ref(i)
            hits = resolve_edges(body, S.EdgeRef.model_validate(r["ref"]))
            assert len(hits) == 1 and hits[0].IsSame(e), (part, i, r)
