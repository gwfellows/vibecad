"""The 2D drawing export: four views, overall dimensions, hole callouts, a fitting sheet and scale."""
import re
import xml.etree.ElementTree as ET
from pathlib import Path

from vibecad.drawing import drawing_svg
from vibecad.regen import Regenerator, load

EX = Path(__file__).parent.parent / "examples"
NS = "{http://www.w3.org/2000/svg}"


def _draw(name, **overrides):
    r = Regenerator(EX).run(load(EX / f"{name}.vcad.json"), overrides or None)
    svg = drawing_svg(r)
    return svg, ET.fromstring(svg)


def _texts(root):
    return ["".join(t.itertext()) for t in root.iter(f"{NS}text")]


def test_mounting_plate_drawing():
    svg, root = _draw("mounting_plate")
    texts = _texts(root)
    assert {"TOP", "FRONT", "RIGHT", "ISO"} <= set(texts)
    # overall dimensions (80 x 60 x 8 plate), sheet and scale, title
    assert {"80", "60", "8"} <= set(texts)
    assert "1:1  A4" in texts and "mounting_plate" in texts and "80 × 60 × 8" in texts
    assert "hold_down: 4× ⌀4.5 THRU, CBORE ⌀8 ↧4.4" in texts
    assert "sensor_screws: 2× ⌀3.4 THRU, CSK ⌀6.5 × 90°" in texts
    assert any(t.startswith("stud_hole: M6x1 TAP") for t in texts)
    # hidden lines in the orthographic views only, and everything on the sheet
    assert root.findall(f".//{NS}polyline[@class='h']")
    W, H = (float(v) for v in root.get("viewBox").split()[2:])
    for pl in root.iter(f"{NS}polyline"):
        for x, y in (map(float, p.split(",")) for p in pl.get("points").split()):
            assert 0 < x < W and 0 < y < H


def test_sheet_and_scale_fit_the_part():
    svg, _ = _draw("mounting_plate", plate_w=400, plate_d=300)
    assert "1:4  A3" in svg or "1:5  A3" in svg or "1:2.5  A3" in svg, re.findall(r"1:[\d.]+  A\d", svg)
    svg, _ = _draw("mounting_plate", plate_w=20, plate_d=15, screw_inset=3, sensor_spacing=6)
    assert re.search(r">(2|4|5):1  A4<", svg)
    svg, root = _draw("l_bracket")
    assert "HOLES" not in _texts(root)  # no hole features: no callout table
