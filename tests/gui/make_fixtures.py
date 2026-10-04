"""CAD files for the browser tests' import checks: a phone-like body (STEP and STL), placed off-origin the way
real downloads are, and a block modelled in inches.  Usage: python make_fixtures.py OUT_DIR"""
import sys
from pathlib import Path

import build123d as bd

out = Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)
with bd.BuildPart() as p:
    with bd.BuildSketch():
        bd.RectangleRounded(71.5, 146.7, 9)
    bd.extrude(amount=7.8)
    with bd.BuildSketch(bd.Plane.XY.offset(7.8)):
        with bd.Locations((20, 55)):
            bd.RectangleRounded(26, 26, 6)
    bd.extrude(amount=1.2)
phone = p.part.translate((200, 100, 30))
bd.export_step(phone, str(out / "phone.step"))
bd.export_stl(phone, str(out / "phone.stl"), tolerance=0.05, angular_tolerance=0.3)
bd.export_step(bd.Box(2, 1.5, 0.5, align=bd.Align.MIN), str(out / "block_inches.step"))
print(f"{phone.volume:.3f}")
# a real part, as someone else's CAD would send it
from vibecad.regen import Regenerator, load  # noqa: E402

ex = Path(__file__).resolve().parents[2] / "examples" / "pillow_block.vcad.json"
bd.export_step(Regenerator(ex.parent).run(load(ex)).part, str(out / "pillow_block.step"))
