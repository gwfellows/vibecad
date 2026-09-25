"""Phase 1 exit criteria.

1. Every reference part regenerates with no errors, every sketch at 0 DOF, and a valid solid whose
   volume matches a hand calculation (so a feature that silently does nothing is caught).
2. Changing any single parameter by +/-10% still regenerates cleanly: no broken references.
"""
from math import pi
from pathlib import Path

import pytest

from vibecad.regen import Regenerator, load

EX = Path(__file__).resolve().parent.parent / "examples"
PARTS = sorted(p.stem.replace(".vcad", "") for p in EX.glob("*.vcad.json"))


def fillet_corner_area(r):
    return r * r * (1 - pi / 4)


def fillet_ring_centroid(r):  # distance of the corner-region centroid from the corner, per axis
    return r * (10 - 3 * pi) / (3 * (4 - pi))


def expected_volume(name, p):
    if name == "l_bracket":
        slot = pi * (p["slot_w"] / 2) ** 2 + p["slot_len"] * p["slot_w"]
        return (p["width"] * p["depth"] * p["base_t"]
                + p["width"] * p["wall_t"] * (p["wall_h"] - p["base_t"])
                - 2 * slot * p["base_t"]
                - 2 * pi * (p["hole_d"] / 2) ** 2 * p["wall_t"]
                + fillet_corner_area(p["corner_r"]) * p["width"])
    if name == "flanged_bushing":
        rb, ro, rf, c = p["bore_d"] / 2, p["od_d"] / 2, p["flange_d"] / 2, p["chamfer"]
        v = pi * ((rf**2 - rb**2) * p["flange_t"] + (ro**2 - rb**2) * (p["length"] - p["flange_t"]))
        return v - 2 * pi * (ro - c / 3) * c * c / 2 - 2 * pi * (rb + c / 3) * c * c / 2
    if name == "spacer_plate":
        R, r = p["plate_d"] / 2, p["edge_r"]
        v = pi * (R**2 - (p["bore_d"] / 2) ** 2) * p["plate_t"]
        v -= round(p["bolt_n"]) * pi * (p["bolt_d"] / 2) ** 2 * p["plate_t"]
        return v - 2 * pi * (R - fillet_ring_centroid(r)) * fillet_corner_area(r)
    if name == "nema17_mount":
        t, rp, c = p["plate_t"], p["pilot_d"] / 2, p["pilot_chamfer"]
        v = (p["plate_w"] ** 2 - 4 * fillet_corner_area(p["corner_r"])) * t
        v -= pi * rp**2 * t + 4 * pi * (p["screw_d"] / 2) ** 2 * t
        return v - 2 * pi * (rp + c / 3) * c * c / 2
    if name == "enclosure_lid":
        W, D, H, t, r = p["width"], p["depth"], p["height"], p["wall_t"], p["corner_r"]
        outer = (W * D - 4 * fillet_corner_area(r)) * H
        inner = ((W - 2 * t) * (D - 2 * t) - 4 * fillet_corner_area(r - t)) * (H - t)
        bosses = 4 * pi * (p["boss_d"] / 2) ** 2 * p["boss_h"] - 4 * pi * (p["pilot_d"] / 2) ** 2 * p["pilot_depth"]
        vents = round(p["vent_n"]) * p["vent_w"] * p["vent_l"] * t
        return outer - inner + bosses - vents
    if name == "pillow_block":
        base = p["base_w"] * p["depth"] * p["base_t"]
        housing = (2 * p["housing_r"] * (p["shaft_h"] - p["base_t"]) + pi * p["housing_r"] ** 2 / 2) * p["depth"]
        pocket = pi * (p["brg_od"] / 2) ** 2 * p["brg_w"]
        shaft = pi * (p["shaft_clear_d"] / 2) ** 2 * (p["depth"] - p["brg_w"])
        bolts = 2 * pi * (p["bolt_d"] / 2) ** 2 * p["base_t"]
        c, rp = 0.6, p["brg_od"] / 2
        return base + housing - pocket - shaft - bolts - 2 * pi * (rp + c / 3) * c * c / 2
    if name == "battery_tray":
        tray_len = p["bat_l"] + 2 * p["sheet_t"]
        tray_wid = p["bat_w"] + 2 * (p["clr"] + p["sheet_t"])
        base = tray_len * tray_wid * p["sheet_t"]
        wall = tray_len * p["wall_h"] * p["sheet_t"] - 2 * pi * (p["bolt_d"] / 2) ** 2 * p["sheet_t"]
        return base + 2 * wall
    if name == "battery_strap":
        strap_len = p["bat_l"] + 2 * p["sheet_t"]
        strap_wid = p["bat_w"] + 2 * p["clr"] + 4 * p["sheet_t"]
        top = strap_len * strap_wid * p["sheet_t"]
        flange = strap_len * p["flange_h"] * p["sheet_t"] - 2 * pi * (p["bolt_d"] / 2) ** 2 * p["sheet_t"]
        return top + 2 * flange
    raise KeyError(name)


def check(res, name):
    errors = [(f.id, f.message) for f in res.features if f.status == "error"]
    assert not errors, errors
    warnings = [(f.id, w) for f in res.features for w in f.warnings]
    assert not warnings, warnings
    for sid, (solved, _) in res.sketches.items():
        assert solved.report.dof == 0, f"{sid} has {solved.report.dof} DOF"
    part = res.part
    assert part.is_valid
    exp = expected_volume(name, res.env)
    assert part.volume == pytest.approx(exp, rel=2e-4), f"volume {part.volume:.3f} != expected {exp:.3f}"


@pytest.mark.parametrize("name", PARTS)
def test_part_builds(name):
    doc = load(EX / f"{name}.vcad.json")
    check(Regenerator().run(doc), name)


def _sweep_cases():
    for name in PARTS:
        doc = load(EX / f"{name}.vcad.json")
        for k in doc.params:
            for s in (0.9, 1.1):
                yield pytest.param(name, k, s, id=f"{name}-{k}-x{s}")


@pytest.mark.parametrize("name,param,scale", list(_sweep_cases()))
def test_param_change(name, param, scale):
    doc = load(EX / f"{name}.vcad.json")
    base = Regenerator().run(doc).env
    if param in ("bolt_n", "vent_n"):  # integer counts: step by one instead
        val = base[param] + (1 if scale > 1 else -1)
    else:
        val = base[param] * scale
    check(Regenerator().run(doc, {param: val}), name)


def test_cache_reuses_upstream():
    doc = load(EX / "l_bracket.vcad.json")
    rg = Regenerator()
    rg.run(doc)
    res = rg.run(doc, {"corner_r": 3})
    cached = {f.id for f in res.features if f.cached}
    assert "base" in cached and "hole_cut" in cached and "corner_fillet" not in cached
