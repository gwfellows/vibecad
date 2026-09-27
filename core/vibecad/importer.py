"""Reading STEP / IGES / BREP / STL files into OCCT shapes for the `import` feature.

Loaded shapes are cached by (path, mtime, size), so regenerating a part doesn't re-read a big STEP file, and
editing the file on disk is picked up on the next build.
"""
from __future__ import annotations

from pathlib import Path

from OCP.BRep import BRep_Builder
from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeSolid, BRepBuilderAPI_Sewing
from OCP.ShapeUpgrade import ShapeUpgrade_UnifySameDomain
from OCP.TopAbs import TopAbs_SHELL
from OCP.TopoDS import TopoDS, TopoDS_Face, TopoDS_Shape

from .topo import explore, list_faces

SUFFIXES = {".step": "step", ".stp": "step", ".iges": "iges", ".igs": "iges", ".brep": "brep", ".brp": "brep", ".stl": "stl"}
MAX_STL_SOLID_TRIANGLES = 20000  # sewing a mesh into a solid is slow past this; import it as a reference instead

_cache: dict[tuple, tuple[TopoDS_Shape, dict]] = {}


class ImportError_(ValueError):
    pass


def file_stamp(path: Path) -> str:
    try:
        st = path.stat()
    except OSError:
        return "missing"
    return f"{st.st_mtime_ns}:{st.st_size}"


def load(path: Path, as_solid: bool) -> tuple[TopoDS_Shape, dict]:
    """The file's shape and facts about it ({"format", "triangles"?, "mesh": bool}). `as_solid`: an STL is sewn
    into a solid (needed for booleans); otherwise it stays one triangulated face (fast; display and fit only)."""
    if not path.exists():
        raise ImportError_(f"import file {path} not found")
    kind = SUFFIXES.get(path.suffix.lower())
    if kind is None:
        raise ImportError_(f"can't import {path.suffix!r} files; use STEP (.step/.stp), IGES, BREP or STL")
    key = (str(path.resolve()), file_stamp(path), as_solid if kind == "stl" else None)
    if key in _cache:
        return _cache[key]
    info: dict = {"format": kind, "mesh": False}
    if kind == "step":
        import build123d as bd
        shape = bd.import_step(str(path)).wrapped
    elif kind == "iges":
        from OCP.IGESControl import IGESControl_Reader
        from OCP.IFSelect import IFSelect_RetDone
        r = IGESControl_Reader()
        if r.ReadFile(str(path)) != IFSelect_RetDone:
            raise ImportError_(f"could not read IGES file {path.name}")
        r.TransferRoots()
        shape = r.OneShape()
    elif kind == "brep":
        from OCP.BRepTools import BRepTools
        shape = TopoDS_Shape()
        if not BRepTools.Read_s(shape, str(path), BRep_Builder()):
            raise ImportError_(f"could not read BREP file {path.name}")
    else:
        shape, info = _load_stl(path, as_solid)
    if shape is None or shape.IsNull() or not list_faces(shape):
        raise ImportError_(f"{path.name} contains no surfaces")
    _cache[key] = (shape, info)
    return shape, info


def _load_stl(path: Path, as_solid: bool) -> tuple[TopoDS_Shape, dict]:
    from OCP.RWStl import RWStl

    tri = RWStl.ReadFile_s(str(path))
    if tri is None:
        raise ImportError_(f"could not read STL file {path.name}")
    n = tri.NbTriangles()
    info = {"format": "stl", "triangles": n, "mesh": True}
    if not as_solid:
        face = TopoDS_Face()
        BRep_Builder().MakeFace(face, tri)
        return face, info
    if n > MAX_STL_SOLID_TRIANGLES:
        raise ImportError_(f"{path.name} has {n} triangles; a mesh this big can only be imported with mode "
                           f"'reference' (booleans need a solid, and sewing more than {MAX_STL_SOLID_TRIANGLES} "
                           "triangles is too slow). Use a STEP file for a solid.")
    from OCP.StlAPI import StlAPI_Reader

    raw = TopoDS_Shape()
    if not StlAPI_Reader().Read(raw, str(path)):
        raise ImportError_(f"could not read STL file {path.name}")
    sew = BRepBuilderAPI_Sewing(1e-4)
    sew.Add(raw)
    sew.Perform()
    shells = explore(sew.SewedShape(), TopAbs_SHELL)
    if not shells:
        raise ImportError_(f"{path.name}: the mesh doesn't form a closed surface, so it can't become a solid; import it as a reference")
    mk = BRepBuilderAPI_MakeSolid()
    for sh in shells:
        mk.Add(TopoDS.Shell(sh))
    solid = mk.Solid()
    from OCP.BRepLib import BRepLib
    BRepLib.OrientClosedSolid_s(solid)  # sewn shells can come out inside-out: negative volume
    u = ShapeUpgrade_UnifySameDomain(solid, True, True, False)  # a box's 12 triangles become its 6 faces
    u.Build()
    info["mesh"] = False
    return u.Shape(), info
