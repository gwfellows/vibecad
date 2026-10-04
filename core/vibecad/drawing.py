"""A 2D manufacturing drawing of a part, as SVG: front, top and right views in third-angle projection plus an
isometric, visible and hidden lines, overall dimensions, hole callouts from the part's hole features, and a
title block. Sized in millimetres on an A4 or A3 sheet at a standard scale, so it prints true to scale.
"""
from __future__ import annotations

import datetime
import html
import math

import build123d as bd

from . import schema as S

SHEETS = {"A4": (297.0, 210.0), "A3": (420.0, 297.0)}
SCALES = [10, 5, 4, 2, 1, 1 / 2, 1 / 2.5, 1 / 4, 1 / 5, 1 / 10, 1 / 20, 1 / 50, 1 / 100]
MARGIN, TITLE_H, GAP = 10.0, 30.0, 16.0

# (name, viewing direction from the part toward the eye, up)
VIEWS = {
    "front": ((0, -1, 0), (0, 0, 1)),
    "top": ((0, 0, 1), (0, 1, 0)),
    "right": ((1, 0, 0), (0, 0, 1)),
    "iso": ((1, -1, 1), (0, 0, 1)),
}


def _polyline(e: bd.Edge) -> list[tuple[float, float]]:
    n = 2 if e.geom_type == bd.GeomType.LINE else max(8, min(64, int(e.length / 0.5) + 2))
    return [(p.X, p.Y) for p in e.positions([i / (n - 1) for i in range(n)])]


def _project(part: bd.Shape, name: str):
    d, up = VIEWS[name]
    bb = part.bounding_box()
    c = bb.center()
    dist = bb.diagonal * 4 + 10
    dv = bd.Vector(*d).normalized()
    vis, hid = part.project_to_viewport(c + dv * dist, up, c)
    # the isometric is a picture, not a view to measure: visible lines only
    lines = [(_polyline(e), False) for e in vis] + ([] if name == "iso" else [(_polyline(e), True) for e in hid])
    xs = [p[0] for pl, _ in lines for p in pl] or [0.0]
    ys = [p[1] for pl, _ in lines for p in pl] or [0.0]
    return {"lines": lines, "box": (min(xs), min(ys), max(xs), max(ys))}


def _fmt(v: float) -> str:
    s = f"{v:.2f}".rstrip("0").rstrip(".")
    return s if s != "-0" else "0"


def _scale_label(s: float) -> str:
    return f"{_fmt(s)}:1" if s >= 1 else f"1:{_fmt(1 / s)}"


def hole_callouts(doc: S.Document, env: dict, info: dict[str, dict]) -> list[str]:
    """One line per hole feature, in drawing notation: `4× ⌀4.5 THRU, CBORE ⌀8 ↧4.4`."""
    from .expr import evaluate
    ev = lambda v: evaluate(v, env)
    out = []
    for f in doc.features:
        if not isinstance(f, S.Hole) or f.suppressed:
            continue
        n = info.get(f.id, {}).get("holes")
        if not n:
            continue
        txt = f"{n}× " if n > 1 else ""
        txt += f"{f.thread} TAP, ⌀{_fmt(ev(f.diameter))} DRILL" if f.thread else f"⌀{_fmt(ev(f.diameter))}"
        txt += " THRU" if f.extent == "through_all" else f" ↧{_fmt(ev(f.depth))}"
        if f.kind == "counterbore":
            txt += f", CBORE ⌀{_fmt(ev(f.cbore_diameter))} ↧{_fmt(ev(f.cbore_depth))}"
        elif f.kind == "countersink":
            txt += f", CSK ⌀{_fmt(ev(f.csk_diameter))} × {_fmt(ev(f.csk_angle))}°"
        out.append(f"{f.id}: {txt}")
    return out


def drawing_svg(result) -> str:
    """The drawing of a regenerated part (a RegenResult with a solid)."""
    part = result.part
    if part is None:
        raise ValueError("there is no solid to draw")
    views = {k: _project(part, k) for k in VIEWS}
    size = lambda k: (views[k]["box"][2] - views[k]["box"][0], views[k]["box"][3] - views[k]["box"][1])
    (wf, hf), (wt, ht), (wr, hr), (wi, hi) = (size(k) for k in ("front", "top", "right", "iso"))
    # third-angle layout: top above front, right to the right of front, the isometric above the right view
    col_w, row_h = [wf, max(wr, wi)], [max(ht, hi), hf]
    sheet, scale = None, None
    for name, (W, H) in SHEETS.items():
        aw, ah = W - 2 * MARGIN - 2 * GAP, H - 2 * MARGIN - TITLE_H - 2 * GAP
        fit = min(aw / max(sum(col_w), 1e-9), ah / max(sum(row_h), 1e-9))
        s = next((x for x in SCALES if x <= fit), SCALES[-1])
        sheet, scale = (name, (W, H)), s
        if s >= 1 or name == "A3":
            break
    (sname, (W, H)) = sheet
    k = scale
    x0 = MARGIN + GAP
    y_top = MARGIN + GAP  # svg y grows downward
    cells = {
        "top": (x0, y_top, col_w[0], row_h[0]),
        "iso": (x0 + col_w[0] * k + GAP, y_top, col_w[1], row_h[0]),
        "front": (x0, y_top + row_h[0] * k + GAP, col_w[0], row_h[1]),
        "right": (x0 + col_w[0] * k + GAP, y_top + row_h[0] * k + GAP, col_w[1], row_h[1]),
    }
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}mm" height="{H}mm" viewBox="0 0 {W} {H}" '
           f'font-family="Helvetica, Arial, sans-serif">',
           '<style>.v{fill:none;stroke:#000;stroke-width:.35;stroke-linecap:round;stroke-linejoin:round}'
           '.h{fill:none;stroke:#777;stroke-width:.18;stroke-dasharray:1.6 .9}'
           '.d{fill:none;stroke:#1d4ed8;stroke-width:.18}.dt{fill:#1d4ed8;font-size:3px;text-anchor:middle}'
           '.t{font-size:3px;fill:#111}.lbl{font-size:3.2px;fill:#444;text-anchor:middle}</style>',
           f'<rect x="0" y="0" width="{W}" height="{H}" fill="#fff"/>',
           f'<rect x="{MARGIN / 2}" y="{MARGIN / 2}" width="{W - MARGIN}" height="{H - MARGIN}" fill="none" stroke="#000" stroke-width=".5"/>']
    placed = {}
    for name, (cx, cy, cw, ch) in cells.items():
        v = views[name]
        bx0, by0, bx1, by1 = v["box"]
        # centre the view in its cell; model y up -> svg y down
        ox = cx + (cw - (bx1 - bx0)) * k / 2 - bx0 * k
        oy = cy + (ch - (by1 - by0)) * k / 2 + by1 * k
        tx = lambda p, ox=ox, oy=oy: (ox + p[0] * k, oy - p[1] * k)
        placed[name] = (tx, v["box"])
        for hidden in (True, False):  # hidden first, so visible lines draw over them
            for pl, h in v["lines"]:
                if h != hidden:
                    continue
                pts = " ".join(f"{a:.3f},{b:.3f}" for a, b in map(tx, pl))
                out.append(f'<polyline class="{"h" if h else "v"}" points="{pts}"/>')
        lx, ly = tx(((bx0 + bx1) / 2, by0))
        out.append(f'<text class="lbl" x="{lx:.2f}" y="{ly + 5:.2f}">{name.upper()}</text>')

    def dim(p, q, off, horizontal):
        """An overall dimension from p to q (svg coords), offset away from the view."""
        (x1, y1), (x2, y2) = p, q
        if horizontal:
            y = max(y1, y2) + off
            out.append(f'<path class="d" d="M{x1:.2f},{y1 + 1:.2f}V{y + 1.5:.2f}M{x2:.2f},{y2 + 1:.2f}V{y + 1.5:.2f}M{x1:.2f},{y:.2f}H{x2:.2f}"/>')
            for x, s_ in ((x1, 1), (x2, -1)):
                out.append(f'<path class="d" d="M{x:.2f},{y:.2f}l{2 * s_:.2f},-.7v1.4z" fill="#1d4ed8"/>')
            return (x1 + x2) / 2, y - 1.2
        x = min(x1, x2) - off
        out.append(f'<path class="d" d="M{x1 - 1:.2f},{y1:.2f}H{x - 1.5:.2f}M{x2 - 1:.2f},{y2:.2f}H{x - 1.5:.2f}M{x:.2f},{y1:.2f}V{y2:.2f}"/>')
        for y, s_ in ((y1, 1), (y2, -1)):
            out.append(f'<path class="d" d="M{x:.2f},{y:.2f}l-.7,{2 * s_:.2f}h1.4z" fill="#1d4ed8"/>')
        return x - 1.2, (y1 + y2) / 2

    bb = part.bounding_box()
    tx, (bx0, by0, bx1, by1) = placed["front"]
    x, y = dim(tx((bx0, by0)), tx((bx1, by0)), 12, True)
    out.append(f'<text class="dt" x="{x:.2f}" y="{y:.2f}">{_fmt(bb.size.X)}</text>')
    x, y = dim(tx((bx0, by1)), tx((bx0, by0)), 8, False)
    out.append(f'<text class="dt" x="{x:.2f}" y="{y:.2f}" transform="rotate(-90 {x:.2f} {y:.2f})">{_fmt(bb.size.Z)}</text>')
    tx, (bx0, by0, bx1, by1) = placed["top"]
    x, y = dim(tx((bx0, by1)), tx((bx0, by0)), 8, False)
    out.append(f'<text class="dt" x="{x:.2f}" y="{y:.2f}" transform="rotate(-90 {x:.2f} {y:.2f})">{_fmt(bb.size.Y)}</text>')

    # notes: hole callouts
    info = {fr.id: fr.info for fr in result.features}
    notes = hole_callouts(result.doc, result.env, info)
    ny = H - MARGIN / 2 - TITLE_H - 2 - 4.2 * len(notes)
    if notes:
        out.append(f'<text class="t" x="{MARGIN + 2}" y="{ny - 1:.2f}" font-weight="700">HOLES</text>')
        for i, n in enumerate(notes):
            out.append(f'<text class="t" x="{MARGIN + 2}" y="{ny + 4.2 * (i + 1):.2f}">{html.escape(n)}</text>')
    # title block
    tw, th = 170.0, TITLE_H
    tx0, ty0 = W - MARGIN / 2 - tw, H - MARGIN / 2 - th
    out.append(f'<rect x="{tx0}" y="{ty0}" width="{tw}" height="{th}" fill="none" stroke="#000" stroke-width=".5"/>')
    out.append(f'<path d="M{tx0},{ty0 + 10}h{tw}M{tx0},{ty0 + 20}h{tw}M{tx0 + 110},{ty0 + 10}v{th - 10}M{tx0 + 60},{ty0 + 10}v{th - 10}" stroke="#000" stroke-width=".25" fill="none"/>')
    doc = result.doc

    def cell(x, y, label, value, width=54.0):
        """A labelled title-block cell; long values shrink, then get cut with an ellipsis, to stay inside."""
        fs = max(2.4, min(3.4, width / (0.56 * max(len(value), 1))))
        fit = int(width / (0.56 * fs))
        if len(value) > fit:
            value = value[: fit - 1].rstrip(" ,;") + "…"
        out.append(f'<text x="{x:.1f}" y="{y:.1f}" font-size="2.2" fill="#666">{html.escape(label)}</text>'
                   f'<text x="{x:.1f}" y="{y + 4.2:.1f}" font-size="{fs:g}" fill="#000">{html.escape(value)}</text>')
    out.append(f'<text x="{tx0 + 3}" y="{ty0 + 7}" font-size="5" font-weight="700">{html.escape(doc.name)}</text>')
    cell(tx0 + 3, ty0 + 13.5, "MATERIAL", doc.material or "—")
    cell(tx0 + 63, ty0 + 13.5, "PROCESS", doc.process or "—", 45)
    cell(tx0 + 113, ty0 + 13.5, "SCALE / SHEET", f"{_scale_label(scale)}  {sname}")
    cell(tx0 + 3, ty0 + 23.5, "UNITS", "mm   third-angle projection")
    cell(tx0 + 63, ty0 + 23.5, "SIZE", f"{_fmt(bb.size.X)} × {_fmt(bb.size.Y)} × {_fmt(bb.size.Z)}")
    cell(tx0 + 113, ty0 + 23.5, "DATE", datetime.date.today().isoformat())
    out.append(f'<text x="{tx0 + tw - 2}" y="{ty0 + 7}" font-size="2.2" fill="#888" text-anchor="end">VibeCAD</text>')
    out.append("</svg>")
    return "\n".join(out)
