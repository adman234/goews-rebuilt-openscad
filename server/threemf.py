"""
Build a multi-part 3MF from STL meshes.

The result is a single object made of several parts, laid out the way
Bambu Studio / OrcaSlicer save their own projects, so each part keeps its
own filament (extruder) assignment when the file is opened.
"""

import io
import re
import struct
import zipfile
from dataclasses import dataclass
from xml.sax.saxutils import escape, quoteattr

_NUM = r"(-?[\d.]+(?:[eE][-+]?\d+)?)"
_ASCII_FACET = re.compile(
    rb"outer\s+loop\s+"
    + rb"\s+".join([rb"vertex\s+" + rb"\s+".join([_NUM.encode()] * 3)] * 3)
)


@dataclass
class Mesh:
    vertices: list[tuple[float, float, float]]
    triangles: list[tuple[int, int, int]]


@dataclass
class Part:
    name: str
    stl: bytes
    extruder: int


def _is_binary_stl(data: bytes) -> bool:
    if len(data) < 84:
        return False
    (count,) = struct.unpack_from("<I", data, 80)
    return len(data) == 84 + count * 50


def _iter_stl_triangles(data: bytes):
    if _is_binary_stl(data):
        (count,) = struct.unpack_from("<I", data, 80)
        for i in range(count):
            v = struct.unpack_from("<9f", data, 84 + i * 50 + 12)
            yield (v[0:3], v[3:6], v[6:9])
    else:
        for m in _ASCII_FACET.finditer(data):
            v = [float(x) for x in m.groups()]
            yield (tuple(v[0:3]), tuple(v[3:6]), tuple(v[6:9]))


def load_stl(data: bytes) -> Mesh:
    """Load an ASCII or binary STL, merging identical vertices."""
    index: dict[tuple[float, float, float], int] = {}
    vertices: list[tuple[float, float, float]] = []
    triangles: list[tuple[int, int, int]] = []

    for tri in _iter_stl_triangles(data):
        ids = []
        for v in tri:
            i = index.get(v)
            if i is None:
                i = index[v] = len(vertices)
                vertices.append(v)
            ids.append(i)
        if len(set(ids)) == 3:
            triangles.append(tuple(ids))

    return Mesh(vertices, triangles)


_NAMESPACES = (
    'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02" '
    'xmlns:p="http://schemas.microsoft.com/3dmanufacturing/production/2015/06" '
    'xmlns:BambuStudio="http://schemas.bambulab.com/package/2021"'
)

_CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>'
    '<Default Extension="config" ContentType="text/xml"/>'
    "</Types>"
)


def _rels(target: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        f'<Relationship Target="{target}" Id="rel-1" '
        'Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>'
        "</Relationships>"
    )


def _fmt(x: float) -> str:
    return f"{x:.6g}"


def _mesh_xml(mesh: Mesh) -> str:
    out = io.StringIO()
    out.write("<mesh><vertices>")
    for x, y, z in mesh.vertices:
        out.write(f'<vertex x="{_fmt(x)}" y="{_fmt(y)}" z="{_fmt(z)}"/>')
    out.write("</vertices><triangles>")
    for a, b, c in mesh.triangles:
        out.write(f'<triangle v1="{a}" v2="{b}" v3="{c}"/>')
    out.write("</triangles></mesh>")
    return out.getvalue()


def build_3mf(
    name: str,
    parts: list[Part],
    bed_center: tuple[float, float] = (128.0, 128.0),
) -> bytes:
    """Combine the given STL parts into one object and return the 3MF bytes."""
    meshes = [load_stl(p.stl) for p in parts]
    xs = [v[0] for m in meshes for v in m.vertices]
    ys = [v[1] for m in meshes for v in m.vertices]
    zs = [v[2] for m in meshes for v in m.vertices]
    if not xs:
        raise ValueError("No geometry to export")

    # Center the object on the bed and set it on the build plate.
    tx = bed_center[0] - (min(xs) + max(xs)) / 2
    ty = bed_center[1] - (min(ys) + max(ys)) / 2
    tz = -min(zs)

    object_id = len(parts) + 1

    objects = io.StringIO()
    objects.write(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<model unit="millimeter" xml:lang="en-US" {_NAMESPACES} requiredextensions="p">'
        "<resources>"
    )
    for i, mesh in enumerate(meshes, start=1):
        objects.write(
            f'<object id="{i}" p:UUID="{i:08x}-61cb-4c03-9d28-80fed5dfa1dc" type="model">'
            f"{_mesh_xml(mesh)}</object>"
        )
    objects.write("</resources><build/></model>")

    components = "".join(
        f'<component p:path="/3D/Objects/object_1.model" objectid="{i}" '
        f'p:UUID="{0x10000 + i:08x}-b206-40ff-9872-83e8017abed1" '
        'transform="1 0 0 0 1 0 0 0 1 0 0 0"/>'
        for i in range(1, len(parts) + 1)
    )
    main = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<model unit="millimeter" xml:lang="en-US" {_NAMESPACES} requiredextensions="p">'
        f"<metadata name=\"Title\">{escape(name)}</metadata>"
        '<metadata name="BambuStudio:3mfVersion">1</metadata>'
        f'<resources><object id="{object_id}" p:UUID="{object_id:08x}-61cb-4c03-9d28-80fed5dfa1dc" '
        f'type="model"><components>{components}</components></object></resources>'
        '<build p:UUID="2c7c17d8-22b5-4d84-8835-1976022ea369">'
        f'<item objectid="{object_id}" p:UUID="{object_id + 1:08x}-b1ec-4553-aec9-835e5b724bb4" '
        f'transform="1 0 0 0 1 0 0 0 1 {tx:.4f} {ty:.4f} {tz:.4f}" printable="1"/>'
        "</build></model>"
    )

    config = io.StringIO()
    config.write(
        '<?xml version="1.0" encoding="UTF-8"?>\n<config>\n'
        f'  <object id="{object_id}">\n'
        f'    <metadata key="name" value={quoteattr(name)}/>\n'
        '    <metadata key="extruder" value="1"/>\n'
    )
    for i, part in enumerate(parts, start=1):
        config.write(
            f'    <part id="{i}" subtype="normal_part">\n'
            f'      <metadata key="name" value={quoteattr(part.name)}/>\n'
            '      <metadata key="matrix" value="1 0 0 0 0 1 0 0 0 0 1 0 0 0 0 1"/>\n'
            f'      <metadata key="extruder" value="{part.extruder}"/>\n'
            "    </part>\n"
        )
    config.write(
        "  </object>\n"
        "  <plate>\n"
        '    <metadata key="plater_id" value="1"/>\n'
        "    <model_instance>\n"
        f'      <metadata key="object_id" value="{object_id}"/>\n'
        '      <metadata key="instance_id" value="0"/>\n'
        "    </model_instance>\n"
        "  </plate>\n"
        "</config>\n"
    )

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", _CONTENT_TYPES)
        zf.writestr("_rels/.rels", _rels("/3D/3dmodel.model"))
        zf.writestr("3D/3dmodel.model", main)
        zf.writestr("3D/_rels/3dmodel.model.rels", _rels("/3D/Objects/object_1.model"))
        zf.writestr("3D/Objects/object_1.model", objects.getvalue())
        zf.writestr("Metadata/model_settings.config", config.getvalue())
    return buf.getvalue()
