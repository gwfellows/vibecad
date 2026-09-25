"""Coordinate frames for sketch planes."""
from __future__ import annotations

from dataclasses import dataclass

import build123d as bd

DATUMS = {  # (x_dir, y_dir, normal) — right-handed: normal = x × y
    "XY": ((1, 0, 0), (0, 1, 0), (0, 0, 1)),
    "XZ": ((1, 0, 0), (0, 0, 1), (0, -1, 0)),
    "YZ": ((0, 1, 0), (0, 0, 1), (1, 0, 0)),
}
AXES = {"X": (1.0, 0.0, 0.0), "Y": (0.0, 1.0, 0.0), "Z": (0.0, 0.0, 1.0)}


@dataclass(frozen=True)
class Frame:
    origin: bd.Vector
    x: bd.Vector
    y: bd.Vector
    n: bd.Vector

    @property
    def plane(self) -> bd.Plane:
        return bd.Plane(origin=self.origin, x_dir=self.x, z_dir=self.n)

    @property
    def location(self) -> bd.Location:
        return bd.Location(self.plane)

    def to_world(self, u: float, v: float, w: float = 0.0) -> bd.Vector:
        return self.origin + self.x * u + self.y * v + self.n * w

    def to_local(self, p) -> tuple[float, float, float]:
        d = bd.Vector(p) - self.origin
        return (d.dot(self.x), d.dot(self.y), d.dot(self.n))

    def describe(self) -> dict:
        r = lambda v: [round(c, 6) + 0.0 for c in (v.X, v.Y, v.Z)]
        return {"origin": r(self.origin), "x_dir": r(self.x), "y_dir": r(self.y), "normal": r(self.n)}


def datum_frame(name: str, offset: float = 0.0) -> Frame:
    x, y, n = (bd.Vector(v) for v in DATUMS[name])
    return Frame(n * offset, x, y, n)


def face_frame(face: bd.Face, offset: float = 0.0) -> Frame:
    if face.geom_type != bd.GeomType.PLANE:
        raise ValueError(f"sketch plane face must be planar, got {face.geom_type.name.lower()}")
    n = face.normal_at().normalized()  # build123d returns the outward normal for faces of a solid
    p0 = face.center()
    origin = bd.Vector(0, 0, 0) - n * (bd.Vector(0, 0, 0) - p0).dot(n)  # world origin projected onto plane
    x, y = face_axes(n)
    return Frame(origin + n * offset, x, y, n)


def face_axes(n: bd.Vector) -> tuple[bd.Vector, bd.Vector]:
    """Sketch axes on a face, as seen looking at the face from outside the part.

    Faces that are mostly horizontal (|n.z| > 0.7): sketch x = world +X (projected); y = n x x
    (so on a top face y = +Y). Other faces: sketch y = world +Z projected (up is up); x = y x n,
    which is "right" for someone looking at the face from outside."""
    z = bd.Vector(0, 0, 1)
    if abs(n.dot(z)) > 0.7:
        wx = bd.Vector(1, 0, 0)
        x = (wx - n * wx.dot(n)).normalized()
        return x, n.cross(x)
    y = (z - n * z.dot(n)).normalized()
    return y.cross(n), y


def axis_vec(a) -> bd.Vector:
    return bd.Vector(AXES[a]) if isinstance(a, str) else bd.Vector(*a).normalized()
