"""Standard fastener sizes for the hole feature: clearance, tap drill, counterbore and countersink diameters.

Metric: clearance holes per ISO 273 (fine / medium / coarse series, here "close" / "normal" / "loose"), tap
drills for coarse threads, counterbores for ISO 4762 socket head cap screws, countersinks (90°) for ISO 10642
flat head screws. Inch (UNC/UNF): clearance per ASME B18.2.8 (close / normal / loose), number and letter tap
drills for 75% thread, counterbores for socket head cap screws, countersinks (82°) for flat heads. All in mm.

These are the common shop values; check them against your supplier's screws for critical fits.
"""
from __future__ import annotations

from dataclasses import dataclass

IN = 25.4


@dataclass(frozen=True)
class Size:
    name: str
    major: float                     # nominal thread diameter
    clearance: tuple[float, float, float]  # close, normal, loose
    tap: float                       # tap drill
    cbore: tuple[float, float]       # counterbore diameter, depth (socket head sits flush)
    csink: float                     # countersink diameter at the surface
    csink_angle: float               # included angle


def _m(name, d, close, normal, loose, tap, cb_d, cb_h, cs_d):
    return Size(name, d, (close, normal, loose), tap, (cb_d, cb_h), cs_d, 90.0)


def _i(name, d, close, normal, loose, tap, cb_d, cb_h, cs_d):
    return Size(name, d * IN, (close * IN, normal * IN, loose * IN), tap * IN, (cb_d * IN, cb_h * IN), cs_d * IN, 82.0)


SIZES: dict[str, Size] = {s.name.lower(): s for s in [
    #   name   major close normal loose  tap    cbore d, depth  csink d
    _m("M1.6", 1.6, 1.7, 1.8, 2.0, 1.25, 3.3, 1.8, 3.2),
    _m("M2", 2.0, 2.2, 2.4, 2.6, 1.6, 4.4, 2.3, 4.4),
    _m("M2.5", 2.5, 2.7, 2.9, 3.1, 2.05, 5.5, 2.8, 5.5),
    _m("M3", 3.0, 3.2, 3.4, 3.6, 2.5, 6.5, 3.3, 6.7),
    _m("M4", 4.0, 4.3, 4.5, 4.8, 3.3, 8.0, 4.4, 9.0),
    _m("M5", 5.0, 5.3, 5.5, 5.8, 4.2, 10.0, 5.4, 11.2),
    _m("M6", 6.0, 6.4, 6.6, 7.0, 5.0, 11.0, 6.5, 13.4),
    _m("M8", 8.0, 8.4, 9.0, 10.0, 6.8, 15.0, 8.6, 17.9),
    _m("M10", 10.0, 10.5, 11.0, 12.0, 8.5, 18.0, 10.8, 22.4),
    _m("M12", 12.0, 13.0, 13.5, 14.5, 10.2, 20.0, 13.0, 26.9),
    _m("M16", 16.0, 17.0, 17.5, 18.5, 14.0, 26.0, 17.5, 33.6),
    _m("M20", 20.0, 21.0, 22.0, 24.0, 17.5, 33.0, 21.5, 40.3),
    #   name      major  close  normal loose  tap     cbore d, depth  csink d  (inches)
    _i("#2-56", 0.086, 0.096, 0.102, 0.116, 0.0700, 0.188, 0.086, 0.203),
    _i("#4-40", 0.112, 0.116, 0.128, 0.140, 0.0890, 0.219, 0.112, 0.255),
    _i("#6-32", 0.138, 0.144, 0.149, 0.166, 0.1065, 0.250, 0.138, 0.307),
    _i("#8-32", 0.164, 0.170, 0.177, 0.194, 0.1360, 0.312, 0.164, 0.359),
    _i("#10-24", 0.190, 0.196, 0.201, 0.228, 0.1495, 0.344, 0.190, 0.411),
    _i("#10-32", 0.190, 0.196, 0.201, 0.228, 0.1590, 0.344, 0.190, 0.411),
    _i("1/4-20", 0.250, 0.257, 0.266, 0.281, 0.2010, 0.438, 0.250, 0.531),
    _i("1/4-28", 0.250, 0.257, 0.266, 0.281, 0.2130, 0.438, 0.250, 0.531),
    _i("5/16-18", 0.3125, 0.323, 0.332, 0.344, 0.2570, 0.531, 0.312, 0.656),
    _i("3/8-16", 0.375, 0.386, 0.397, 0.413, 0.3125, 0.625, 0.375, 0.781),
    _i("1/2-13", 0.500, 0.516, 0.531, 0.562, 0.4219, 0.812, 0.500, 1.031),
]}
ALIASES = {"#2": "#2-56", "#4": "#4-40", "#6": "#6-32", "#8": "#8-32", "#10": "#10-32", "1/4": "1/4-20",
           "5/16": "5/16-18", "3/8": "3/8-16", "1/2": "1/2-13"}
FITS = ("close", "normal", "loose")


def lookup(name: str) -> Size:
    key = name.strip().lower().replace(" ", "")
    key = ALIASES.get(key, key)
    if key not in SIZES:
        raise KeyError(f"unknown fastener size {name!r}; known: {', '.join(s.name for s in SIZES.values())}")
    return SIZES[key]


def table_markdown() -> str:
    rows = ["| size | close | normal | loose | tap drill | c'bore ⌀ × depth | c'sink ⌀ (angle) |", "|---|---|---|---|---|---|---|"]
    for s in SIZES.values():
        c = s.clearance
        rows.append(f"| {s.name} | {c[0]:.2f} | {c[1]:.2f} | {c[2]:.2f} | {s.tap:.2f} | {s.cbore[0]:.1f} × {s.cbore[1]:.1f} "
                    f"| {s.csink:.1f} ({s.csink_angle:g}°) |")
    return "\n".join(rows)
