"""GUI server logic (the App object behind the HTTP routes), without a browser."""
import shutil
import threading
from pathlib import Path

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
    assert {d["label"].split(" =")[0] for d in sk["dims"]} >= {"width", "depth"}
    a.rollback = None
    assert len(a.mesh()["faces"]) == full
