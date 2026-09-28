"""STEP files for the bench's design-around-an-import tasks. Run: uv run python bench/fixtures/make_fixtures.py"""
from pathlib import Path

import build123d as bd

here = Path(__file__).parent
# an 18650 cell standing on XY at the origin
bd.export_step(bd.Cylinder(9.2, 65, align=(bd.Align.CENTER, bd.Align.CENTER, bd.Align.MIN)), str(here / "cell_18650.step"))
# a 60 x 40 x 1.6 board with four 3.2 mm holes 3.5 mm in from the corners and a connector block on top; exported
# standing on its long edge, away from the origin, the way a downloaded board model often arrives
with bd.BuildPart() as pcb:
    bd.Box(60, 40, 1.6, align=(bd.Align.CENTER, bd.Align.CENTER, bd.Align.MIN))
    with bd.Locations(*[(x, y, 0) for x in (-26.5, 26.5) for y in (-16.5, 16.5)]):
        bd.Hole(1.6)
    with bd.Locations((15, 0, 1.6)):
        bd.Box(12, 20, 8, align=(bd.Align.CENTER, bd.Align.CENTER, bd.Align.MIN))
board = bd.Pos(40, 30, 25) * bd.Rot(90, 0, 0) * pcb.part
bd.export_step(board, str(here / "pcb.step"))
# a phone: a rounded slab with a camera bump, off the origin like a downloaded model
with bd.BuildPart() as ph:
    with bd.BuildSketch():
        bd.RectangleRounded(71.5, 146.7, 9)
    bd.extrude(amount=7.8)
    with bd.BuildSketch(bd.Plane.XY.offset(7.8)):
        with bd.Locations((20, 55)):
            bd.RectangleRounded(26, 26, 6)
    bd.extrude(amount=1.2)
bd.export_step(bd.Pos(200, 100, 30) * ph.part, str(here / "phone.step"))
# a NEMA 17 stepper: 42.3 mm square body 40 long, 22 mm pilot boss 2 high, 5 mm shaft 24 long, four M3 holes on a
# 31 mm square in the front face; exported lying down with the shaft along +X, as vendor models often are
with bd.BuildPart() as mot:
    bd.Box(42.3, 42.3, 40, align=(bd.Align.CENTER, bd.Align.CENTER, bd.Align.MAX))
    bd.Cylinder(11, 2, align=(bd.Align.CENTER, bd.Align.CENTER, bd.Align.MIN))
    bd.Cylinder(2.5, 26, align=(bd.Align.CENTER, bd.Align.CENTER, bd.Align.MIN))
    with bd.Locations(*[(x, y, 0) for x in (-15.5, 15.5) for y in (-15.5, 15.5)]):
        bd.Hole(1.5, 4.5)
bd.export_step(bd.Pos(60, -20, 21.15) * bd.Rot(0, 90, 0) * mot.part, str(here / "nema17.step"))
# a battery pack: a plain box with a cable exit block on one end
with bd.BuildPart() as bat:
    bd.Box(70, 35, 18, align=(bd.Align.CENTER, bd.Align.CENTER, bd.Align.MIN))
    with bd.Locations((36, 0, 9)):
        bd.Box(4, 8, 6)
bd.export_step(bat.part, str(here / "battery_pack.step"))
print("ok")
