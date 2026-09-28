"""The VibeCAD IR: a feature-tree document.

A document holds named parameters and an ordered list of features. Numeric fields
marked `Num` accept a number (mm or degrees) or an expression over params.
Sketch entity coordinates are initial guesses; the solver owns their final values.
"""
from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator

Num = Union[float, str]
Vec2 = tuple[Num, Num]  # sketch coordinates: numbers or expressions (initial guesses for the solver)
Vec3 = tuple[float, float, float]
Axis = Union[Literal["X", "Y", "Z"], Vec3]


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ── Sketch entities ────────────────────────────────────────────────
class Point(_M):
    id: str
    type: Literal["point"] = "point"
    at: Vec2
    construction: bool = True


class Line(_M):
    id: str
    type: Literal["line"] = "line"
    p1: Vec2
    p2: Vec2
    construction: bool = False


class Circle(_M):
    id: str
    type: Literal["circle"] = "circle"
    center: Vec2
    r: Num
    construction: bool = False


class Arc(_M):
    """Arc swept counterclockwise from start_angle to end_angle (degrees, from sketch +x)."""
    id: str
    type: Literal["arc"] = "arc"
    center: Vec2
    r: Num
    start_angle: Num
    end_angle: Num
    construction: bool = False



ConstraintType = Literal[
    "coincident", "horizontal", "vertical", "parallel", "perpendicular", "equal", "tangent",
    "point_on", "midpoint", "symmetric", "concentric", "fix",
    "distance", "distance_x", "distance_y", "radius", "diameter", "angle",
]
DIMENSIONAL = {"distance", "distance_x", "distance_y", "radius", "diameter", "angle"}


class Constraint(_M):
    """`on` holds references: entity ids, sub-points (`l1.p1`, `l1.p2`, `c1.center`, `a1.start`,
    `a1.end`), or the sketch's `origin`, `x_axis`, `y_axis`."""
    type: ConstraintType
    on: list[str]
    id: str | None = None  # optional stable handle for edits
    value: Num | None = None
    at: tuple[Num, Num] | None = None  # for `fix`
    name: str | None = None  # dimension name shown to users, e.g. "slot_width"
    note: str | None = None

    @model_validator(mode="after")
    def _check_value(self):
        if self.type in DIMENSIONAL and self.value is None:
            raise ValueError(f"{self.type} constraint needs a value")
        if self.type == "fix" and self.at is None:
            raise ValueError("fix constraint needs `at: [x, y]`")
        return self


# ── References to topology ─────────────────────────────────────────
class FaceRef(_M):
    """Selects faces by the feature that created them.

    role: extrude/revolve -> start | end | side ; fillet -> fillet ; chamfer -> chamfer ;
          shell -> inner ; any feature -> new (faces with no better label).
    entity: for side faces, the sketch entity that was swept.
    instance: which pattern/mirror copy: omitted = original only, "*" = all, or "<pattern_id>#<n>".
    pick: how to reduce multiple matches when one face is required.
    """
    feature: str
    role: str
    entity: str | None = None
    instance: str | None = None
    pick: Literal["all", "largest", "smallest", "nearest"] = "all"
    near: Vec3 | None = None
    note: str | None = None


class EdgeFilter(_M):
    type: Literal["line", "circle", "any"] = "any"
    parallel_to: Axis | None = None
    perpendicular_to: Axis | None = None


class EdgeRef(_M):
    """Either the edges shared by two face sets (`between`), or the edges of a face set (`of`).
    pick "nearest" with `near: [x, y, z]` keeps only the edge closest to that point (two faces can meet
    along several edges)."""
    between: tuple[FaceRef, FaceRef] | None = None
    of: FaceRef | None = None
    filter: EdgeFilter | None = None
    pick: Literal["all", "nearest"] = "all"
    near: Vec3 | None = None
    note: str | None = None

    @model_validator(mode="after")
    def _one(self):
        if (self.between is None) == (self.of is None):
            raise ValueError("EdgeRef needs exactly one of `between` or `of`")
        if self.pick == "nearest" and self.near is None:
            raise ValueError("EdgeRef pick 'nearest' needs `near: [x, y, z]`")
        return self


class External(_M):
    """An edge of the part projected into the sketch as fixed construction geometry (a line, circle or arc;
    a point if the edge is perpendicular to the plane). Re-projected on every build, so it follows the part.
    Reference it like any entity: `id`, `id.p1` / `id.p2` (line), `id.center`, `id.start` / `id.end` (arc)."""
    id: str
    type: Literal["external"] = "external"
    edge: EdgeRef
    note: str | None = None
    construction: Literal[True] = True


class Offset(_M):
    """Derived geometry: the chain of lines and arcs `of` (they may be projected edges) copied at `distance`, on
    `side` (closed loops: outside / inside; open chains: left / right of the chain's direction). Sharp corners are
    mitred, arcs grow or shrink. Rebuilt from the solved source on every build, so it follows it; fixed, so it
    adds no DOF. Its pieces are `<id>_1`, `<id>_2`, ... in chain order, referenced like ordinary lines and arcs."""
    id: str
    type: Literal["offset"] = "offset"
    of: list[str]
    distance: Num
    side: Literal["outside", "inside", "left", "right"] = "outside"
    construction: bool = False
    note: str | None = None


Entity = Annotated[Union[Point, Line, Circle, Arc, External, Offset], Field(discriminator="type")]


# ── Planes ──────────────────────────────────────────────────────────
class DatumPlane(_M):
    """XY: x=+X, y=+Y, normal=+Z.  XZ: x=+X, y=+Z, normal=-Y.  YZ: x=+Y, y=+Z, normal=+X."""
    datum: Literal["XY", "XZ", "YZ"]
    offset: Num = 0.0


class FacePlane(_M):
    """Plane of a planar face, normal pointing out of the solid. Origin = world origin projected
    onto the plane; x = world X projected (world Y if X is nearly normal)."""
    face: FaceRef
    offset: Num = 0.0


PlaneSpec = Union[DatumPlane, FacePlane]


# ── Features ────────────────────────────────────────────────────────
class _Feature(_M):
    id: str
    name: str | None = None
    intent: str | None = None
    suppressed: bool = False


class Sketch(_Feature):
    type: Literal["sketch"] = "sketch"
    plane: PlaneSpec
    entities: list[Entity] = []
    constraints: list[Constraint] = []


class Profile(_M):
    sketch: str
    regions: Literal["all"] | list[str] = "all"  # entity ids on a region's outer loop


Mode = Literal["add", "cut", "intersect", "new"]


class Extrude(_Feature):
    type: Literal["extrude"] = "extrude"
    profile: Profile
    distance: Num = 0.0
    direction: Literal["normal", "reverse", "symmetric"] = "normal"
    extent: Literal["blind", "through_all", "up_to_face"] = "blind"
    to_face: FaceRef | None = None  # up_to_face: a planar face parallel to the sketch; `distance` goes past it
    draft: Num = 0.0  # degrees; positive tapers the walls inward along the extrusion (a molded boss, a pocket)
    mode: Mode = "add"

    @model_validator(mode="after")
    def _check(self):
        if self.extent == "up_to_face" and self.to_face is None:
            raise ValueError("extent 'up_to_face' needs to_face")
        return self


class Revolve(_Feature):
    type: Literal["revolve"] = "revolve"
    profile: Profile
    axis: str  # a line entity id in the profile's sketch, or "x_axis" / "y_axis"
    angle: Num = 360.0
    mode: Mode = "add"


class Fillet(_Feature):
    type: Literal["fillet"] = "fillet"
    edges: list[EdgeRef]
    radius: Num


class Chamfer(_Feature):
    type: Literal["chamfer"] = "chamfer"
    edges: list[EdgeRef]
    distance: Num


class Shell(_Feature):
    type: Literal["shell"] = "shell"
    remove_faces: list[FaceRef]
    thickness: Num
    outward: bool = False  # the wall grows outside the surfaces: a skin around an imported body (a case around a phone)


class LinearPattern(_Feature):
    type: Literal["linear_pattern"] = "linear_pattern"
    features: list[str]
    direction: Axis
    spacing: Num
    count: Num


class CircularPattern(_Feature):
    type: Literal["circular_pattern"] = "circular_pattern"
    features: list[str]
    axis: Axis = "Z"
    origin: tuple[Num, Num, Num] = (0.0, 0.0, 0.0)
    count: Num
    angle: Num = 360.0  # total span; 360 spaces copies evenly around the full circle


class Hole(_Feature):
    """Drilled holes at a sketch's points (point entities, and circle/arc centres), along the sketch normal.
    `direction: reverse` (default) drills into the solid from a face sketch. `depth` is to the end of the
    full diameter; a blind hole also gets a drill-point cone (`tip_angle`, 0 for a flat bottom)."""
    type: Literal["hole"] = "hole"
    sketch: str
    points: Literal["all"] | list[str] = "all"  # entity ids: points, or circles/arcs (their centre)
    kind: Literal["simple", "counterbore", "countersink"] = "simple"
    diameter: Num
    extent: Literal["blind", "through_all"] = "through_all"
    depth: Num | None = None  # blind holes
    direction: Literal["reverse", "normal"] = "reverse"
    tip_angle: Num = 118.0
    cbore_diameter: Num | None = None
    cbore_depth: Num | None = None
    csk_diameter: Num | None = None
    csk_angle: Num = 90.0
    thread: str | None = None  # e.g. "M3x0.5": the hole is tapped; `diameter` is the tap drill

    @model_validator(mode="after")
    def _check(self):
        if self.extent == "blind" and self.depth is None:
            raise ValueError("a blind hole needs `depth`")
        if self.kind == "counterbore" and (self.cbore_diameter is None or self.cbore_depth is None):
            raise ValueError("a counterbore hole needs `cbore_diameter` and `cbore_depth`")
        if self.kind == "countersink" and self.csk_diameter is None:
            raise ValueError("a countersink hole needs `csk_diameter`")
        return self


class Text(_Feature):
    """Text engraved into or raised from a face: placed on a sketch's plane at one of its points (the text's
    centre, or its left end with `align: left`), `angle` degrees from sketch +x."""
    type: Literal["text"] = "text"
    sketch: str
    at: str  # a point entity (or origin) of that sketch
    text: str
    size: Num = 5.0  # cap height-ish: the font size in mm
    depth: Num = 0.5
    mode: Literal["cut", "add"] = "cut"
    angle: Num = 0.0
    align: Literal["center", "left"] = "center"
    font: str | None = None  # a font name or a .ttf/.otf path; default: the system's sans


class Import(_Feature):
    """A solid from a STEP / IGES / BREP / STL file. `mode: reference` keeps it out of the part: shown ghosted,
    usable for sketch planes, projected edges and fit checks (a mating part, a phone, a motor)."""
    type: Literal["import"] = "import"
    file: str  # relative to the part file's folder
    mode: Union[Mode, Literal["reference"]] = "new"
    scale: Num = 1.0  # e.g. 25.4 for a file in inches
    rotate: tuple[Num, Num, Num] = (0.0, 0.0, 0.0)  # degrees about world X, then Y, then Z (about the origin)
    translate: tuple[Num, Num, Num] = (0.0, 0.0, 0.0)  # applied after rotate


class Mirror(_Feature):
    type: Literal["mirror"] = "mirror"
    features: list[str]
    plane: DatumPlane


Feature = Annotated[
    Union[Sketch, Extrude, Revolve, Fillet, Chamfer, Shell, LinearPattern, CircularPattern, Mirror, Import, Hole, Text],
    Field(discriminator="type"),
]


class Document(_M):
    vibecad: str = "0.1"
    name: str
    units: Literal["mm"] = "mm"
    material: str | None = None
    process: str | None = None
    design_notes: str | None = None
    params: dict[str, Num] = {}
    features: list[Feature] = []

    @model_validator(mode="after")
    def _unique_ids(self):
        seen: set[str] = set()
        for f in self.features:
            if f.id in seen:
                raise ValueError(f"duplicate feature id {f.id!r}")
            seen.add(f.id)
            if isinstance(f, Sketch):
                eids = [e.id for e in f.entities]
                dup = {i for i in eids if eids.count(i) > 1}
                if dup:
                    raise ValueError(f"sketch {f.id!r}: duplicate entity ids {sorted(dup)}")
                bad = {i for i in eids if i in ("origin", "x_axis", "y_axis") or "." in i}
                if bad:
                    raise ValueError(f"sketch {f.id!r}: reserved entity ids {sorted(bad)}")
        return self

    def feature(self, fid: str):
        for f in self.features:
            if f.id == fid:
                return f
        raise KeyError(fid)
