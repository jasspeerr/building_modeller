"""CityGML 2.0 LOD2.2 writer.

Builds a static ``CityModel`` document with one ``bldg:Building`` per
input building. Each building gets semantically tagged
``bldg:WallSurface`` / ``bldg:RoofSurface`` / ``bldg:GroundSurface``
boundary surfaces, and a ``bldg:lod2Solid`` whose shell reuses the very
same polygon geometries by ``xlink:href`` (the standard CityGML way to
avoid duplicating coordinates between the semantic surfaces and the
solid shell).

Buildings without a manually chosen roof are exported as a flat-topped
box (see ``Building.mesh``) rather than being dropped, so a batch export
never silently loses a building -- their ``lod`` metadata still records
that they were not manually modelled.

No CityGML XML Schema is bundled with this repo (schema files are not
needed to *produce* well-formed, structurally correct CityGML, and
fetching them requires network access this environment may not have);
validate the output against the official schemas with an external tool
(e.g. QGIS, FZK Viewer, or ``xmllint --schema``) if strict validation is
needed.
"""
from __future__ import annotations

import re
from typing import Iterable, List

from lxml import etree

from ..model.building import Building, ModellingStatus
from ..model.roofshapes import Ring3

CITYGML_NS = "http://www.opengis.net/citygml/2.0"
GML_NS = "http://www.opengis.net/gml"
BLDG_NS = "http://www.opengis.net/citygml/building/2.0"
XLINK_NS = "http://www.w3.org/1999/xlink"
XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"

NSMAP = {
    None: CITYGML_NS,
    "gml": GML_NS,
    "bldg": BLDG_NS,
    "xlink": XLINK_NS,
    "xsi": XSI_NS,
}

SCHEMA_LOCATION = (
    "http://www.opengis.net/citygml/2.0 "
    "http://schemas.opengis.net/citygml/2.0/cityGMLBase.xsd "
    "http://www.opengis.net/citygml/building/2.0 "
    "http://schemas.opengis.net/citygml/building/2.0/building.xsd"
)

DEFAULT_SRS = "EPSG:28992"


def _q(ns: str, tag: str) -> str:
    return f"{{{ns}}}{tag}"


def _safe_id(prefix: str, raw: str) -> str:
    """Turn an arbitrary BAG id into a valid GML/XML NCName."""
    cleaned = re.sub(r"[^A-Za-z0-9_.-]", "_", str(raw))
    return f"{prefix}_{cleaned}"


def _poslist_text(ring: Ring3) -> str:
    closed = list(ring)
    if closed[0] != closed[-1]:
        closed = closed + [closed[0]]
    return " ".join(f"{x:.3f} {y:.3f} {z:.3f}" for x, y, z in closed)


def _polygon_element(ring: Ring3, gml_id: str, srs_name: str) -> etree._Element:
    poly = etree.Element(_q(GML_NS, "Polygon"), attrib={_q(GML_NS, "id"): gml_id})
    poly.set("srsName", srs_name)
    exterior = etree.SubElement(poly, _q(GML_NS, "exterior"))
    ring_el = etree.SubElement(exterior, _q(GML_NS, "LinearRing"))
    pos_list = etree.SubElement(ring_el, _q(GML_NS, "posList"))
    pos_list.set("srsDimension", "3")
    pos_list.text = _poslist_text(ring)
    return poly


def _boundary_surface(
    tag: str, rings: List[Ring3], id_prefix: str, srs_name: str
) -> tuple:
    """Return (bldg:*Surface element, [gml:id of each member polygon])."""
    surface_el = etree.Element(_q(BLDG_NS, tag))
    lod_el = etree.SubElement(surface_el, _q(BLDG_NS, "lod2MultiSurface"))
    multi_surface = etree.SubElement(lod_el, _q(GML_NS, "MultiSurface"))
    poly_ids = []
    for i, ring in enumerate(rings):
        poly_id = f"{id_prefix}_{i}"
        member = etree.SubElement(multi_surface, _q(GML_NS, "surfaceMember"))
        member.append(_polygon_element(ring, poly_id, srs_name))
        poly_ids.append(poly_id)
    return surface_el, poly_ids


def _building_element(building: Building, srs_name: str) -> etree._Element:
    mesh = building.mesh()
    gml_id = _safe_id("BLDG", building.bag_id)

    bldg_el = etree.Element(_q(BLDG_NS, "Building"), attrib={_q(GML_NS, "id"): gml_id})
    name_el = etree.SubElement(bldg_el, _q(GML_NS, "name"))
    name_el.text = str(building.bag_id)

    top = max((z for ring in mesh.walls + mesh.roof for _, _, z in ring), default=building.ground_height)
    height_el = etree.SubElement(bldg_el, _q(BLDG_NS, "measuredHeight"))
    height_el.set("uom", "m")
    height_el.text = f"{max(top - building.ground_height, 0.0):.3f}"

    all_poly_ids: List[str] = []
    surfaces = [
        ("GroundSurface", mesh.ground, f"{gml_id}_gnd"),
        ("WallSurface", mesh.walls, f"{gml_id}_wall"),
        ("RoofSurface", mesh.roof, f"{gml_id}_roof"),
    ]
    for tag, rings, prefix in surfaces:
        if not rings:
            continue
        surface_el, poly_ids = _boundary_surface(tag, rings, prefix, srs_name)
        bounded_by = etree.SubElement(bldg_el, _q(BLDG_NS, "boundedBy"))
        bounded_by.append(surface_el)
        all_poly_ids.extend(poly_ids)

    solid_wrapper = etree.SubElement(bldg_el, _q(BLDG_NS, "lod2Solid"))
    solid = etree.SubElement(solid_wrapper, _q(GML_NS, "Solid"))
    solid.set("srsName", srs_name)
    exterior = etree.SubElement(solid, _q(GML_NS, "exterior"))
    composite = etree.SubElement(exterior, _q(GML_NS, "CompositeSurface"))
    for poly_id in all_poly_ids:
        member = etree.SubElement(composite, _q(GML_NS, "surfaceMember"))
        member.set(_q(XLINK_NS, "href"), f"#{poly_id}")

    return bldg_el


def _envelope(buildings: Iterable[Building], srs_name: str) -> etree._Element:
    xs, ys, zs = [], [], []
    for b in buildings:
        mesh = b.mesh()
        for ring in mesh.ground + mesh.walls + mesh.roof:
            for x, y, z in ring:
                xs.append(x)
                ys.append(y)
                zs.append(z)
    envelope = etree.Element(_q(GML_NS, "Envelope"))
    envelope.set("srsName", srs_name)
    envelope.set("srsDimension", "3")
    lower = etree.SubElement(envelope, _q(GML_NS, "lowerCorner"))
    upper = etree.SubElement(envelope, _q(GML_NS, "upperCorner"))
    if xs:
        lower.text = f"{min(xs):.3f} {min(ys):.3f} {min(zs):.3f}"
        upper.text = f"{max(xs):.3f} {max(ys):.3f} {max(zs):.3f}"
    else:
        lower.text = "0 0 0"
        upper.text = "0 0 0"
    return envelope


def write_citygml(buildings: List[Building], srs_name: str = DEFAULT_SRS) -> bytes:
    """Serialize ``buildings`` to a CityGML 2.0 document, returned as bytes."""
    root = etree.Element(_q(CITYGML_NS, "CityModel"), nsmap=NSMAP)
    root.set(_q(XSI_NS, "schemaLocation"), SCHEMA_LOCATION)

    bounded_by = etree.SubElement(root, _q(GML_NS, "boundedBy"))
    bounded_by.append(_envelope(buildings, srs_name))

    for building in buildings:
        member = etree.SubElement(root, _q(CITYGML_NS, "cityObjectMember"))
        member.append(_building_element(building, srs_name))

    return etree.tostring(root, pretty_print=True, xml_declaration=True, encoding="UTF-8")


def export_citygml(buildings: List[Building], path: str, srs_name: str = DEFAULT_SRS) -> None:
    data = write_citygml(buildings, srs_name)
    with open(path, "wb") as f:
        f.write(data)
    for b in buildings:
        b.status = ModellingStatus.EXPORTED
