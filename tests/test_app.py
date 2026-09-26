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


def test_refs_context_maps_each_token_to_a_ref(tmp_path):
    from vibecad.app import refs_context

    a = _app(tmp_path)
    s = refs_context([
        {"token": "@face:base.end", "kind": "face", "label": "base.end", "ref": {"feature": "base", "role": "end"}, "point": [1, 2, 3]},
        {"token": "@edge:base.end|wall.start", "kind": "edge", "label": "base.end | wall.start",
         "ref": {"between": [{"feature": "base", "role": "end"}, {"feature": "wall", "role": "start"}]}},
        {"token": "@sketch:slot_sketch/slot_left", "kind": "sketch", "sketch": "slot_sketch", "key": "slot_left"},
    ], a.ws.session().doc)
    assert '@face:base.end = the face base.end at (1.00, 2.00, 3.00); FaceRef {"feature": "base", "role": "end"}' in s
    assert "@edge:base.end|wall.start = the edge between base.end and wall.start; EdgeRef {\"between\"" in s
    assert "@sketch:slot_sketch/slot_left = slot_left in sketch `slot_sketch`" in s
    assert "[In sketch `slot_sketch` the user selected: slot_left." in s
    assert refs_context([], None) == ""


def test_param_usage_puts_single_feature_params_under_that_feature(tmp_path):
    a = _app(tmp_path)
    st = a.state()
    users = {p["name"]: p["users"] for p in st["params"]}
    feats = {f["id"]: f for f in st["features"]}
    for p, us in users.items():
        for fid, f in feats.items():
            assert (p in f["params"]) == (us == [fid]), (p, us, fid)
    # a param used by several features stays global; a sketch lists its named dimensions
    assert any(len(us) > 1 for us in users.values())
    dims = {d["name"]: d for d in feats["base_sketch"]["dims"]}
    assert dims["width"]["expr"] == "width" and dims["width"]["value"] == 60
    assert feats["base"]["fields"][0]["key"] == "distance"


# ── per-part conversations, attachments, fillet edge lookup, regen progress ──
def test_feature_edges_resolve_to_edges_before_the_fillet(tmp_path):
    a = _app(tmp_path)
    ids = [f.id for f in a.ws.session().doc.features]
    k = ids.index("corner_fillet")
    a.rollback = k
    r = a.feature_edges("corner_fillet")
    assert r["index"] == k and r["type"] == "fillet"
    assert len(r["items"]) == 1 and len(r["items"][0]["idx"]) == 1 and r["items"][0]["error"] is None
    # the index is the mesh's edge index, and naming that edge again resolves to the same edge
    i = r["items"][0]["idx"][0]
    assert a.edge_ref(i)["ref"]["between"]
    with pytest.raises(Exception):
        a.feature_edges("base")  # an extrude has no edges to pick


def test_conversations_are_per_part_and_saved(tmp_path):
    import asyncio
    import json

    shutil.copy(EX / "spacer_plate.vcad.json", tmp_path / "spacer_plate.vcad.json")
    a = _app(tmp_path)
    a.loop = asyncio.new_event_loop()
    a.queue = asyncio.Queue()
    pa = a.ws.active

    async def go():
        await a.switch_conversation(pa)
        a.transcript.append({"type": "user_prompt", "text": "on the bracket"})
        a.session_id = "sess-a"
        a.ws.open_part("spacer_plate.vcad.json")
        await a.switch_conversation(a.ws.active)
        assert a.transcript == [] and a.session_id is None  # the other part: its own, empty conversation
        a.transcript.append({"type": "user_prompt", "text": "on the spacer"})
        a.transcript.append({"type": "tool_image", "png_b64": "x" * 100})
        a.ws.open_part("l_bracket.vcad.json")
        await a.switch_conversation(a.ws.active)
        assert [e["text"] for e in a.transcript] == ["on the bracket"] and a.session_id == "sess-a"

    a.loop.run_until_complete(go())
    saved = json.loads((tmp_path / "spacer_plate.chat.json").read_text())
    assert [e["type"] for e in saved["transcript"]] == ["user_prompt"]  # renders are not written to disk
    # a fresh app (server restart) picks the saved conversation up again
    b = App(tmp_path, "sonnet")
    assert b.load_conv(str(tmp_path / "spacer_plate.vcad.json"))["transcript"][0]["text"] == "on the spacer"


def test_a_part_the_agent_creates_keeps_its_conversation(tmp_path):
    a = _app(tmp_path)
    a.conv_part = a.ws.active
    a.convs[a.ws.active] = a.conv
    a.transcript.append({"type": "user_prompt", "text": "make a second part"})
    a.ws.new_part("parts/second.vcad.json", "second")  # what the agent's new_part tool does
    a.adopt_parts()
    assert a.conv_part == a.ws.active
    assert a.convs[a.ws.active] is a.convs[str(tmp_path / "l_bracket.vcad.json")]
    assert (tmp_path / "parts" / "second.chat.json").exists()


def test_attachments_become_content_blocks(tmp_path):
    import base64
    import io

    from PIL import Image

    from vibecad import uploads

    buf = io.BytesIO()
    Image.new("RGB", (3000, 1000), "red").save(buf, "PNG")
    img = uploads.save(tmp_path, "big photo.png", buf.getvalue())
    txt = uploads.save(tmp_path, "dims.csv", b"a,b\n1,2\n")
    pdf = uploads.save(tmp_path, "spec.pdf", b"%PDF-1.4 fake")
    binf = uploads.save(tmp_path, "part.3mf", bytes(range(256)))
    assert img["id"].startswith("uploads/") and img["kind"] == "image" and img["name"] == "big photo.png"
    ctx, blocks = uploads.blocks(tmp_path, [img["id"], txt["id"], pdf["id"], binf["id"]])
    assert [b["type"] for b in blocks] == ["image", "text", "document"]
    shrunk = Image.open(io.BytesIO(base64.b64decode(blocks[0]["source"]["data"])))
    assert max(shrunk.size) <= uploads.IMAGE_MAX_SIDE  # big images are scaled down before sending
    assert "a,b" in blocks[1]["text"]
    assert "attached 4 file(s)" in ctx and "part.3mf" in ctx and "can't read" in ctx
    with pytest.raises(ValueError):
        uploads.resolve(tmp_path, "../l_bracket.vcad.json")  # only files in uploads/
    with pytest.raises(ValueError):
        uploads.save(tmp_path, "empty.txt", b"")


def test_regen_reports_progress_only_for_rebuilt_features(tmp_path):
    from vibecad import regen

    a = _app(tmp_path)
    seen = []
    regen.PROGRESS.append(lambda *x: seen.append(x))
    try:
        a.ws.apply_ops([{"op": "set_param", "name": "corner_r", "value": "3 mm"}], "t", "user")
    finally:
        regen.PROGRESS.clear()
    rebuilt = [x[3] for x in seen if x[3]]
    assert rebuilt == ["corner_fillet"]  # only the feature that uses corner_r; the rest come from the cache
    assert seen[-1][3] is None and seen[-1][1] == seen[-1][2]
