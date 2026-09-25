"""Headless PNG renders: shaded part views with hidden-line edges, and annotated sketch plots.

Painter's algorithm: triangles and edge segments (as thin quads) are sorted far-to-near in one
PolyCollection, so edges behind the part are covered by it. Good enough for verification renders.
"""
from __future__ import annotations

import math
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from matplotlib.collections import LineCollection, PolyCollection  # noqa: E402
from OCP.gp import gp_Dir, gp_Lin, gp_Pnt  # noqa: E402
from OCP.IntCurvesFace import IntCurvesFace_ShapeIntersector  # noqa: E402
from OCP.TopoDS import TopoDS  # noqa: E402

import build123d as bd  # noqa: E402

from .topo import Body, list_edges, list_faces  # noqa: E402

VIEWS = {  # name: (camera direction from target, world up)
    "iso": ((1.0, -1.0, 1.0), (0.0, 0.0, 1.0)),
    "iso_back": ((-1.0, 1.0, 1.0), (0.0, 0.0, 1.0)),
    "iso_below": ((1.0, -1.0, -1.0), (0.0, 0.0, 1.0)),
    "front": ((0.0, -1.0, 0.0), (0.0, 0.0, 1.0)),
    "top": ((0.0, 0.0, 1.0), (0.0, 1.0, 0.0)),
    "right": ((1.0, 0.0, 0.0), (0.0, 0.0, 1.0)),
}
BASE = np.array([0.62, 0.70, 0.80])
HILITE = np.array([0.95, 0.55, 0.15])


def _basis(view):
    b, up = (np.array(v, float) for v in VIEWS[view])
    b /= np.linalg.norm(b)
    right = np.cross(up, b)
    right /= np.linalg.norm(right)
    up2 = np.cross(b, right)
    return b, right, up2


def render_view(body: Body, path: Path, view: str = "iso", title: str = "", highlight: set[str] | None = None,
                size_px: int = 900) -> None:
    b, right, up2 = _basis(view)
    light = b + 0.4 * up2 + 0.25 * right
    light /= np.linalg.norm(light)
    polys, colors, depths = [], [], []
    part = bd.Shape.cast(body.shape)
    diag = part.bounding_box().diagonal or 1.0
    tol = diag / 400

    for f in list_faces(body.shape):
        face = bd.Face(TopoDS.Face(f))
        hl = bool(highlight) and any(l.feature in highlight for l in body.labels_of(f))
        verts, tris = face.tessellate(tol, 0.2)
        if not tris:
            continue
        V = np.array([[v.X, v.Y, v.Z] for v in verts])
        planar = face.geom_type == bd.GeomType.PLANE
        fn = np.array([*face.normal_at()]) if planar else None
        for t in tris:
            p0 = V[list(t)]
            n = np.cross(p0[1] - p0[0], p0[2] - p0[0])
            nn = np.linalg.norm(n)
            if nn < 1e-12:
                continue
            n /= nn
            out = fn if planar else np.array([*face.normal_at(bd.Vector(*p0.mean(0)))])
            if n @ out < 0:
                n = -n
            if n @ b < -1e-6:  # back-facing: hidden on a closed solid
                continue
            shade = 0.45 + 0.55 * max(0.0, float(n @ light))
            col = (*((HILITE if hl else BASE) * shade), 1.0)
            for p in _subdivide(p0, diag / 25):
                polys.append(np.c_[p @ right, p @ up2])
                colors.append(col)
                depths.append(float((p @ b).mean()))

    segs = _visible_edges(body.shape, b, diag)
    order = np.argsort(depths)
    fig, ax = plt.subplots(figsize=(size_px / 100, size_px / 100), dpi=100)
    ax.add_collection(PolyCollection([polys[i] for i in order], facecolors=[colors[i] for i in order],
                                     edgecolors="none", antialiaseds=False))
    if segs:
        ax.add_collection(LineCollection([[(a @ right, a @ up2), (c @ right, c @ up2)] for a, c in segs],
                                         colors="#15171c", linewidths=0.9))
    allp = np.vstack(polys) if polys else np.zeros((1, 2))
    lo, hi = allp.min(0), allp.max(0)
    pad = 0.08 * max(hi - lo)
    ax.set_xlim(lo[0] - pad, hi[0] + pad)
    ax.set_ylim(lo[1] - pad, hi[1] + pad)
    ax.set_aspect("equal")
    ax.axis("off")
    _triad(fig, right, up2)
    ax.set_title(title or view, fontsize=10, loc="left")
    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)


def _triad(fig, right, up2):
    t = fig.add_axes([0.02, 0.02, 0.12, 0.12])
    t.set_xlim(-1.3, 1.3)
    t.set_ylim(-1.3, 1.3)
    t.set_aspect("equal")
    t.axis("off")
    for name, v, col in (("X", (1, 0, 0), "#c0392b"), ("Y", (0, 1, 0), "#27ae60"), ("Z", (0, 0, 1), "#2e6fd8")):
        v = np.array(v, float)
        x, y = v @ right, v @ up2
        if math.hypot(x, y) < 0.05:
            t.plot(0, 0, "o", color=col, ms=4)
            t.text(0.12, 0.12, name, color=col, fontsize=8)
            continue
        t.annotate("", xy=(x, y), xytext=(0, 0), arrowprops=dict(arrowstyle="->", color=col, lw=1.4))
        t.text(x * 1.18, y * 1.18, name, color=col, fontsize=8, ha="center", va="center")


def render_sketch(solved, frame, path: Path, constraints=(), env=None) -> None:
    fig, ax = plt.subplots(figsize=(7, 7), dpi=100)
    for e in solved.entities.values():
        style = dict(color="#888" if e.construction else "#1f3b73", lw=1.0 if e.construction else 1.8,
                     ls="--" if e.construction else "-")
        if e.type == "line":
            ax.plot([e.p1[0], e.p2[0]], [e.p1[1], e.p2[1]], **style)
            m = ((e.p1[0] + e.p2[0]) / 2, (e.p1[1] + e.p2[1]) / 2)
        elif e.type in ("circle", "arc"):
            a0, a1 = (0, 360) if e.type == "circle" else (e.start_angle, e.end_angle)
            ts = np.radians(np.linspace(a0, a1, 90))
            ax.plot(e.center[0] + e.r * np.cos(ts), e.center[1] + e.r * np.sin(ts), **style)
            ax.plot(*e.center, "+", color="#555", ms=6)
            mid = math.radians((a0 + a1) / 2)
            m = (e.center[0] + e.r * math.cos(mid), e.center[1] + e.r * math.sin(mid))
        else:
            ax.plot(*e.p1, "o", color="#555", ms=3)
            m = e.p1
        ax.annotate(e.id, m, fontsize=7, color="#a33", xytext=(3, 3), textcoords="offset points")
    ax.plot(0, 0, "o", color="k", ms=4)
    ax.annotate("origin", (0, 0), fontsize=7, xytext=(3, -9), textcoords="offset points")
    ax.axhline(0, color="#ccc", lw=0.6, zorder=0)
    ax.axvline(0, color="#ccc", lw=0.6, zorder=0)
    ax.set_aspect("equal")
    ax.margins(0.12)
    ax.tick_params(labelsize=7)
    dims = [f"{c.name or c.type}: {c.type}({', '.join(c.on)}) = {c.value}" for c in constraints if c.value is not None]
    if dims:
        ax.text(1.02, 1.0, "\n".join(dims), transform=ax.transAxes, fontsize=7, va="top", family="monospace")
    r = solved.report
    fd = frame.describe()
    ax.set_title(f"sketch {solved.id}  |  DOF {r.dof}  |  {r.status}\n"
                 f"x_dir {fd['x_dir']}  y_dir {fd['y_dir']}  normal {fd['normal']}", fontsize=8, loc="left")
    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)


def _subdivide(tri: np.ndarray, max_len: float, depth: int = 0):
    """Split a triangle at its longest edge until all edges are shorter than max_len."""
    lens = [np.linalg.norm(tri[(i + 1) % 3] - tri[i]) for i in range(3)]
    i = int(np.argmax(lens))
    if lens[i] <= max_len or depth > 8:
        yield tri
        return
    a, c, o = tri[i], tri[(i + 1) % 3], tri[(i + 2) % 3]
    m = (a + c) / 2
    yield from _subdivide(np.array([a, m, o]), max_len, depth + 1)
    yield from _subdivide(np.array([m, c, o]), max_len, depth + 1)


def _visible_edges(shape, b: np.ndarray, diag: float):
    """Edge segments not hidden by the solid, found by casting a ray from each sample toward the camera."""
    inter = IntCurvesFace_ShapeIntersector()
    inter.Load(shape, diag * 1e-6)
    eps = diag * 2e-4
    step = diag / 150
    d = gp_Dir(*b)

    def visible(p):
        q = p + b * eps
        inter.Perform(gp_Lin(gp_Pnt(*q), d), 0.0, 1e9)
        for i in range(1, inter.NbPnt() + 1):
            f = bd.Face(inter.Face(i))
            hit = inter.Pnt(i)
            n = f.normal_at(bd.Vector(hit.X(), hit.Y(), hit.Z()))
            if abs(n.X * b[0] + n.Y * b[1] + n.Z * b[2]) > 1e-3:  # ignore faces seen exactly edge-on
                return False
        return True

    segs = []
    for e in list_edges(shape):
        edge = bd.Edge(TopoDS.Edge(e))
        k = max(2, int(edge.length / step) + 1)
        pts = [np.array([q.X, q.Y, q.Z]) for q in edge.positions([i / (k - 1) for i in range(k)])]
        vis = [visible(p) for p in pts]
        for i in range(k - 1):
            if vis[i] and vis[i + 1]:
                segs.append((pts[i], pts[i + 1]))
    return segs
