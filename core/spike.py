"""Phase 0 spike: PlaneGCS solves a constrained sketch, build123d turns it into a solid.

Sketch: a W x H plate anchored at the origin with a centered hole of diameter D.
Initial guesses are deliberately sloppy so the solver has to do real work.
Run:  uv run python core/spike.py [--width 60] [--show]
"""
from __future__ import annotations

import argparse
from pathlib import Path

import build123d as bd
from planegcs import Sketch, SolveStatus

OUT = Path(__file__).resolve().parent.parent / "out"


def solve_plate(width: float, height: float, hole_d: float) -> dict:
    s = Sketch()
    # Corners: rough guesses, not the answer
    p0 = s.add_point(0.3, -0.2)
    p1 = s.add_point(width * 0.7, 1.0)
    p2 = s.add_point(width * 1.2, height * 0.8)
    p3 = s.add_point(-1.0, height * 1.3)
    l_bottom, l_right = s.add_line(p0, p1), s.add_line(p1, p2)
    l_top, l_left = s.add_line(p2, p3), s.add_line(p3, p0)

    s.fix_point(p0, 0.0, 0.0)          # anchor sketch to origin
    s.horizontal(l_bottom)
    s.horizontal(l_top)
    s.vertical(l_left)
    s.vertical(l_right)
    s.set_p2p_distance(p0, p1, width)   # named dimension: width
    s.set_p2p_distance(p1, p2, height)  # named dimension: height

    # Hole: center at the intersection of the two diagonals (construction lines)
    c = s.add_point(width * 0.4, height * 0.6)
    hole = s.add_circle(c, s.add_param(hole_d / 3))
    s.set_circle_radius(hole, hole_d / 2)  # named dimension: hole_d
    s.point_on_line(c, s.add_line(p0, p2))
    s.point_on_line(c, s.add_line(p1, p3))

    status = s.solve()
    diag_res = s.diagnose()
    pt = lambda p: tuple(s.get_point(p))
    return {
        "status": status,
        "dof": diag_res.dof,
        "conflicting": list(diag_res.conflicting),
        "corners": [pt(p) for p in (p0, p1, p2, p3)],
        "hole_center": pt(c),
        "hole_r": s.get_circle(hole).radius,
    }


def build(sol: dict, thickness: float) -> bd.Part:
    plate = bd.extrude(bd.Polygon(*sol["corners"], align=None), amount=thickness)
    hole = bd.extrude(
        bd.Pos(*sol["hole_center"]) * bd.Circle(sol["hole_r"]), amount=thickness
    )
    return plate - hole


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--width", type=float, default=60.0)
    ap.add_argument("--height", type=float, default=40.0)
    ap.add_argument("--hole-d", type=float, default=8.0)
    ap.add_argument("--thickness", type=float, default=6.0)
    ap.add_argument("--show", action="store_true", help="push to ocp_viewer on :3939")
    a = ap.parse_args()

    sol = solve_plate(a.width, a.height, a.hole_d)
    print(f"solve: {sol['status']}  dof: {sol['dof']}  conflicting: {sol['conflicting']}")
    print("corners:", [tuple(round(v, 6) for v in c) for c in sol["corners"]])
    print("hole center:", tuple(round(v, 6) for v in sol["hole_center"]), "r:", round(sol["hole_r"], 6))
    assert sol["status"] == SolveStatus.Success, "solver failed"

    part = build(sol, a.thickness)
    bb = part.bounding_box()
    print(f"volume: {part.volume:.3f} mm^3  bbox: {bb.size.X:.3f} x {bb.size.Y:.3f} x {bb.size.Z:.3f}")

    OUT.mkdir(exist_ok=True)
    bd.export_step(part, str(OUT / "spike_plate.step"))
    bd.export_stl(part, str(OUT / "spike_plate.stl"))
    print(f"wrote {OUT/'spike_plate.step'} and .stl")

    if a.show:
        from ocp_viewer import show  # viewer must be running: python -m ocp_viewer
        show(part)


if __name__ == "__main__":
    main()
