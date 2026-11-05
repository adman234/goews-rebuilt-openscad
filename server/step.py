"""
Build a STEP file of the tile stack's PETG separators.

The separators are flat sheets, so they are built as exact solids from the
2D outline OpenSCAD exports as SVG: one face (outline plus holes) extruded
to the separator thickness, then copied to each layer of the stack.

STEP export needs OpenCascade (the ``cadquery-ocp`` package). It is
optional: when it is not installed, ``step_available()`` returns False and
the bundle is built without a STEP file.
"""

import re
import tempfile
from pathlib import Path

try:
    from OCP.BRep import BRep_Builder
    from OCP.BRepBuilderAPI import (
        BRepBuilderAPI_MakeFace,
        BRepBuilderAPI_MakePolygon,
        BRepBuilderAPI_Transform,
    )
    from OCP.BRepPrimAPI import BRepPrimAPI_MakePrism
    from OCP.gp import gp_Pnt, gp_Trsf, gp_Vec
    from OCP.IFSelect import IFSelect_ReturnStatus
    from OCP.Interface import Interface_Static
    from OCP.Message import Message, Message_Gravity
    from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer
    from OCP.TopoDS import TopoDS_Compound

    _HAVE_OCP = True
except ImportError:  # pragma: no cover - depends on the environment
    _HAVE_OCP = False


def step_available() -> bool:
    return _HAVE_OCP


_PATH = re.compile(r'<path\b[^>]*\bd="([^"]+)"', re.S)
_POINT = re.compile(r"(-?[\d.]+(?:[eE][-+]?\d+)?)[ ,]+(-?[\d.]+(?:[eE][-+]?\d+)?)")


def parse_svg_contours(svg: str) -> list[list[tuple[float, float]]]:
    """Return the closed polygons in an OpenSCAD SVG export, in model coordinates."""
    contours = []
    for d in _PATH.findall(svg):
        for chunk in re.split(r"[Mm]", d)[1:]:
            # OpenSCAD writes SVG with Y pointing down; flip back to model space.
            pts = [(float(x), -float(y)) for x, y in _POINT.findall(chunk)]
            if len(pts) > 1 and pts[0] == pts[-1]:
                pts = pts[:-1]
            if len(pts) >= 3:
                contours.append(pts)
    return contours


def _area(pts):
    n = len(pts)
    return sum(
        pts[i][0] * pts[(i + 1) % n][1] - pts[(i + 1) % n][0] * pts[i][1]
        for i in range(n)
    ) / 2


def _inside(pt, poly):
    x, y = pt
    result = False
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
            result = not result
    return result


def _wire(pts, ccw):
    if (_area(pts) > 0) != ccw:
        pts = pts[::-1]
    poly = BRepBuilderAPI_MakePolygon()
    for x, y in pts:
        poly.Add(gp_Pnt(x, y, 0))
    poly.Close()
    return poly.Wire()


def build_separator_step(
    outline_svg: str,
    thickness: float,
    first_z: float,
    pitch: float,
    count: int,
) -> bytes:
    """Extrude the separator outline and place ``count`` copies ``pitch`` apart."""
    if not _HAVE_OCP:
        raise RuntimeError("STEP export needs the cadquery-ocp package")

    contours = parse_svg_contours(outline_svg)
    if not contours:
        raise ValueError("Separator outline is empty")

    # Even nesting depth = outer boundary, odd = hole.
    depth = [
        sum(_inside(c[0], other) for other in contours if other is not c)
        for c in contours
    ]

    faces = []
    for i, outer in enumerate(contours):
        if depth[i] % 2:
            continue
        face = BRepBuilderAPI_MakeFace(_wire(outer, True), True)
        for j, hole in enumerate(contours):
            if depth[j] == depth[i] + 1 and _inside(hole[0], outer):
                face.Add(_wire(hole, False))
        faces.append(face.Face())

    builder = BRep_Builder()
    compound = TopoDS_Compound()
    builder.MakeCompound(compound)
    for face in faces:
        solid = BRepPrimAPI_MakePrism(face, gp_Vec(0, 0, thickness)).Shape()
        for k in range(count):
            move = gp_Trsf()
            move.SetTranslation(gp_Vec(0, 0, first_z + k * pitch))
            builder.Add(compound, BRepBuilderAPI_Transform(solid, move, True).Shape())

    # Keep OpenCascade's transfer statistics out of the server log.
    Message.DefaultMessenger_s().Printers().First().SetTraceLevel(Message_Gravity.Message_Fail)

    writer = STEPControl_Writer()
    Interface_Static.SetCVal_s("write.step.unit", "MM")
    writer.Transfer(compound, STEPControl_AsIs)

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "separators.step"
        if writer.Write(str(path)) != IFSelect_ReturnStatus.IFSelect_RetDone:
            raise RuntimeError("STEP export failed")
        return path.read_bytes()
