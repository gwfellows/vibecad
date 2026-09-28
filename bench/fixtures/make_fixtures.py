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
print("ok")
