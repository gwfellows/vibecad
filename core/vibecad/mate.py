"""Placing an imported body by its faces: move it so a face of it lies against a face of the part (or of another
import), opposed, touching or at a gap, centred on it or slid straight across. The result is new `rotate` (degrees
about X, then Y, then Z) and `translate` values for the import, which applies scale, then those rotations, then
the translation."""
from __future__ import annotations

import math

import numpy as np


def rot_xyz(deg) -> np.ndarray:
    """The import's rotation: about X, then Y, then Z (R = Rz Ry Rx)."""
    a, b, c = (math.radians(float(v)) for v in deg)
    rx = np.array([[1, 0, 0], [0, math.cos(a), -math.sin(a)], [0, math.sin(a), math.cos(a)]])
    ry = np.array([[math.cos(b), 0, math.sin(b)], [0, 1, 0], [-math.sin(b), 0, math.cos(b)]])
    rz = np.array([[math.cos(c), -math.sin(c), 0], [math.sin(c), math.cos(c), 0], [0, 0, 1]])
    return rz @ ry @ rx


def xyz_of(r: np.ndarray) -> list[float]:
    """Angles (degrees) about X, Y, Z with rot_xyz(angles) == r."""
    sb = -r[2, 0]
    if abs(sb) < 1 - 1e-9:
        b = math.asin(max(-1.0, min(1.0, sb)))
        a = math.atan2(r[2, 1], r[2, 2])
        c = math.atan2(r[1, 0], r[0, 0])
    else:  # gimbal lock: Y at ±90°, only a ∓ c is defined; put it all in a
        b = math.copysign(math.pi / 2, sb)
        a = math.atan2(-r[1, 2], r[1, 1]) if sb > 0 else math.atan2(-r[1, 2], r[1, 1])
        c = 0.0
    return [_clean(math.degrees(v)) for v in (a, b, c)]


def _clean(v: float, nd: int = 6) -> float:
    v = round(v, nd)
    return 0.0 if v == 0 else v


def _turn(u: np.ndarray, v: np.ndarray) -> np.ndarray:
    """The smallest rotation taking unit vector u to unit vector v."""
    c = float(np.dot(u, v))
    if c > 1 - 1e-12:
        return np.eye(3)
    if c < -1 + 1e-12:  # opposite: half a turn about an axis across u (prefer one in the XY plane, so "up" stays up)
        ax = np.cross(u, [0, 0, 1])
        if np.linalg.norm(ax) < 1e-9:
            ax = np.cross(u, [1, 0, 0])
        ax /= np.linalg.norm(ax)
        return 2 * np.outer(ax, ax) - np.eye(3)
    k = np.cross(u, v)
    s = np.linalg.norm(k)
    k /= s
    K = np.array([[0, -k[2], k[1]], [k[2], 0, -k[0]], [-k[1], k[0], 0]])
    return np.eye(3) + s * K + (1 - c) * (K @ K)


def mate(rotate, translate, n_a, c_a, n_b, c_b, gap: float = 0.0, align: str = "center") -> tuple[list[float], list[float]]:
    """New (rotate, translate) for an import so its face (outward normal n_a, centre c_a, both in world space as
    placed now) lies against the target face (n_b, c_b): normals opposed, `gap` apart along n_b, and either centred
    on it (align "center") or moved only along the target's normal (align "touch")."""
    n_a, n_b = (np.asarray(v, float) / np.linalg.norm(v) for v in (n_a, n_b))
    c_a, c_b = np.asarray(c_a, float), np.asarray(c_b, float)
    m = _turn(n_a, -n_b)
    r1 = m @ rot_xyz(rotate)
    t0 = np.asarray(translate, float)
    moved = m @ c_a  # where the face centre goes by the turn alone (about the origin, like the import's rotation)
    if align == "center":
        shift = c_b - moved
    else:  # keep it where it is across the face; only close the distance along the normal
        shift = n_b * float(np.dot(c_b - moved, n_b))
    t1 = m @ t0 + shift + gap * n_b
    return xyz_of(r1), [_clean(v) for v in t1]


def slide(rotate, translate, n_b, n_a2, c_a2, n_b2, c_b2, gap2: float = 0.0) -> list[float]:
    """After a mate onto a plane with normal n_b: the translate that slides the import within that plane until its
    second face (normal n_a2, centre c_a2) meets a second target face (n_b2, c_b2), `gap2` apart; a phone leaning
    on a backrest slid down onto the lip. Rotation is unchanged."""
    n_b, n_b2 = (np.asarray(v, float) / np.linalg.norm(v) for v in (n_b, n_b2))
    u = n_b2 - np.dot(n_b2, n_b) * n_b  # the second target's normal, within the first contact plane
    if np.linalg.norm(u) < 1e-6:
        raise ValueError("the second target face is parallel to the first: sliding along the first can't reach it")
    u /= np.linalg.norm(u)
    s = float(np.dot(np.asarray(c_a2, float) - np.asarray(c_b2, float), n_b2))  # how far the second faces are apart now
    t = (gap2 - s) / float(np.dot(u, n_b2))
    return [_clean(v) for v in np.asarray(translate, float) + t * u]


def spin_to(rotate, n_b, n_a2, n_b2) -> list[float]:
    """After a mate onto a plane with normal n_b: turn the import about n_b so its second face's normal n_a2 points
    against the second target's normal n_b2 (as seen within the contact plane). Two face pairs fix the orientation
    fully: a phone's back on the backrest and its long edge on the lip is landscape, whatever the file's axes."""
    n_b = np.asarray(n_b, float) / np.linalg.norm(n_b)
    proj = lambda v: np.asarray(v, float) - np.dot(v, n_b) * n_b
    a, b = proj(n_a2), -proj(n_b2)
    if np.linalg.norm(a) < 1e-6 or np.linalg.norm(b) < 1e-6:
        return list(rotate)  # one of them is along the contact normal: nothing to turn
    a, b = a / np.linalg.norm(a), b / np.linalg.norm(b)
    ang = math.atan2(float(np.dot(np.cross(a, b), n_b)), float(np.dot(a, b)))
    K = np.array([[0, -n_b[2], n_b[1]], [n_b[2], 0, -n_b[0]], [-n_b[1], n_b[0], 0]])
    turn = np.eye(3) + math.sin(ang) * K + (1 - math.cos(ang)) * (K @ K)
    return xyz_of(turn @ rot_xyz(rotate))
