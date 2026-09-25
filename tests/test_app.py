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
