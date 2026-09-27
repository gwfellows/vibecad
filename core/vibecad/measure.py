"""Measurements for the GUI's Measure tool: sizes of a picked face or edge, the distance and angle between two
picks, and the part's mass properties."""
from __future__ import annotations

import math
import re

import build123d as bd
from OCP.BRepAdaptor import BRepAdaptor_Curve, BRepAdaptor_Surface
from OCP.BRepExtrema import BRepExtrema_DistShapeShape
from OCP.BRepGProp import BRepGProp
from OCP.GeomAbs import GeomAbs_Circle, GeomAbs_Cone, GeomAbs_Cylinder, GeomAbs_Line, GeomAbs_Plane, GeomAbs_Sphere
from OCP.GProp import GProp_GProps
from OCP.TopoDS import TopoDS

# g/cm³, matched against the part's `material` text (first match wins)
DENSITIES = [
    (r"titanium|ti-?6al", 4.43), (r"stainless|304|316", 8.0), (r"steel", 7.85), (r"alumin", 2.70), (r"brass", 8.5),
    (r"bronze", 8.8), (r"copper", 8.96), (r"magnesium", 1.8), (r"zinc", 7.13), (r"\bpla\b", 1.24), (r"petg|\bpet\b", 1.27),
    (r"\babs\b", 1.04), (r"\basa\b", 1.07), (r"nylon|\bpa\d*\b|polyamide", 1.14), (r"\btpu\b", 1.21), (r"polycarbonate|\bpc\b", 1.20),
    (r"acetal|delrin|\bpom\b", 1.41), (r"hdpe|polyethylene", 0.95), (r"polypropylene|\bpp\b", 0.90), (r"acrylic|pmma", 1.18),
    (r"resin", 1.15), (r"plywood|wood|mdf", 0.65),
]


def density_of(material: str | None) -> tuple[float | None, str | None]:
    if not material:
        return None, None
    m = material.lower()
    for pat, rho in DENSITIES:
        hit = re.search(pat, m)
        if hit:
            return rho, hit.group(0)
    return None, None


def _v(p) -> list[float]:
    return [round(p.X(), 4), round(p.Y(), 4), round(p.Z(), 4)] if hasattr(p, "X") and callable(p.X) else [round(p.X, 4), round(p.Y, 4), round(p.Z, 4)]


def describe(shape) -> dict:
    """What one picked face or edge is, with its sizes."""
    if shape.ShapeType().name == "TopAbs_FACE":
        f = TopoDS.Face(shape)
        s = BRepAdaptor_Surface(f)
        g = GProp_GProps()
        BRepGProp.SurfaceProperties_s(f, g)
        out = {"kind": "face", "area": round(g.Mass(), 4)}
        t = s.GetType()
        if t == GeomAbs_Plane:
            n = bd.Face(f).normal_at()
            out.update(surface="plane", normal=_v(n))
        elif t == GeomAbs_Cylinder:
            c = s.Cylinder()
            out.update(surface="cylinder", radius=round(c.Radius(), 4), diameter=round(2 * c.Radius(), 4),
                       axis=_v(c.Axis().Direction()), axis_point=_v(c.Axis().Location()))
        elif t == GeomAbs_Cone:
            c = s.Cone()
            out.update(surface="cone", half_angle=round(math.degrees(c.SemiAngle()), 4), axis=_v(c.Axis().Direction()))
        elif t == GeomAbs_Sphere:
            out.update(surface="sphere", radius=round(s.Sphere().Radius(), 4))
        else:
            out.update(surface="freeform")
        return out
    e = TopoDS.Edge(shape)
    c = BRepAdaptor_Curve(e)
    g = GProp_GProps()
    BRepGProp.LinearProperties_s(e, g)
    out = {"kind": "edge", "length": round(g.Mass(), 4)}
    t = c.GetType()
    if t == GeomAbs_Line:
        out.update(curve="line", direction=_v(c.Line().Direction()))
    elif t == GeomAbs_Circle:
        ci = c.Circle()
        out.update(curve="circle", radius=round(ci.Radius(), 4), diameter=round(2 * ci.Radius(), 4), center=_v(ci.Location()),
                   axis=_v(ci.Axis().Direction()))
    else:
        out.update(curve="curve")
    return out


def _dir(d: dict) -> tuple[float, ...] | None:
    v = d.get("normal") or d.get("direction") or d.get("axis")
    return tuple(v) if v else None


def between(a, b, da: dict, db: dict) -> dict:
    """Minimum distance between two picks (with the closest points), and the angle when both have a direction."""
    ext = BRepExtrema_DistShapeShape(a, b)
    ext.Perform()
    out = {}
    if ext.IsDone() and ext.NbSolution():
        p, q = ext.PointOnShape1(1), ext.PointOnShape2(1)
        out.update(distance=round(ext.Value(), 4), p=_v(p), q=_v(q),
                   dx=round(q.X() - p.X(), 4), dy=round(q.Y() - p.Y(), 4), dz=round(q.Z() - p.Z(), 4))
    u, w = _dir(da), _dir(db)
    if u and w:
        dot = max(-1.0, min(1.0, sum(x * y for x, y in zip(u, w))))
        out["angle"] = round(math.degrees(math.acos(abs(dot))), 4)  # between the two directions, 0..90
        if abs(abs(dot) - 1) < 1e-9:
            out["parallel"] = True
            # two parallel planes: their separation; two coaxial-ish cylinders or circles: the distance between axes
            if da.get("surface") == "plane" and db.get("surface") == "plane":
                pa, pb = bd.Face(TopoDS.Face(a)).center(), bd.Face(TopoDS.Face(b)).center()
                n = bd.Vector(*u)
                out["plane_distance"] = round(abs((pb - pa).dot(n)), 4)
            ca = da.get("axis_point") or da.get("center")
            cb = db.get("axis_point") or db.get("center")
            if ca and cb and (da.get("surface") == "cylinder" or da.get("curve") == "circle") and (
                    db.get("surface") == "cylinder" or db.get("curve") == "circle"):
                d = bd.Vector(*cb) - bd.Vector(*ca)
                ax = bd.Vector(*u)
                out["axis_distance"] = round((d - ax * d.dot(ax)).length, 4)
    return out


def mass_properties(shape, material: str | None) -> dict:
    s = bd.Shape.cast(shape)
    g = GProp_GProps()
    BRepGProp.VolumeProperties_s(shape, g)
    a = GProp_GProps()
    BRepGProp.SurfaceProperties_s(shape, a)
    c = g.CentreOfMass()
    bb = s.bounding_box()
    rho, matched = density_of(material)
    out = {"volume": round(g.Mass(), 3), "area": round(a.Mass(), 3), "center_of_mass": _v(c),
           "bbox": [round(v, 3) for v in (bb.size.X, bb.size.Y, bb.size.Z)], "material": material}
    if rho:
        out.update(density=rho, density_from=matched, mass_g=round(g.Mass() / 1000 * rho, 3))
    return out
