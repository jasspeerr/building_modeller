from lxml import etree
from shapely.geometry import box

from building_modeller.export.citygml_writer import (
    BLDG_NS,
    CITYGML_NS,
    GML_NS,
    write_citygml,
)
from building_modeller.model.building import Building

NS = {"gml": GML_NS, "bldg": BLDG_NS, "citygml": CITYGML_NS}


def make_building(bag_id="0123456789012345", shaped=True):
    footprint = box(0.0, 0.0, 10.0, 6.0)
    b = Building(bag_id=bag_id, footprint=footprint, ground_height=0.0)
    b.mesh()  # seed the flat-box geometry
    if shaped:
        # Move one roof vertex up, so this isn't just the flat seed box.
        roof_face = b.geometry.faces_by_type("roof")[0]
        idx = roof_face.vertex_indices[0]
        x, y, z = b.geometry.vertices[idx]
        b.move_vertex(idx, (x, y, z + 2.0))
    return b


class TestWellFormedness:
    def test_output_parses_as_xml(self):
        data = write_citygml([make_building()])
        root = etree.fromstring(data)
        assert root.tag == f"{{{CITYGML_NS}}}CityModel"

    def test_empty_building_list_still_produces_valid_document(self):
        data = write_citygml([])
        root = etree.fromstring(data)
        assert root.tag == f"{{{CITYGML_NS}}}CityModel"
        assert root.find("gml:boundedBy/gml:Envelope", NS) is not None


class TestBuildingContent:
    def test_one_cityobjectmember_per_building(self):
        buildings = [make_building(bag_id=f"id{i}") for i in range(3)]
        root = etree.fromstring(write_citygml(buildings))
        members = root.findall("citygml:cityObjectMember", NS)
        assert len(members) == 3

    def test_building_has_semantic_surfaces_and_solid(self):
        root = etree.fromstring(write_citygml([make_building()]))
        bldg = root.find(".//bldg:Building", NS)
        assert bldg is not None

        walls = bldg.findall("bldg:boundedBy/bldg:WallSurface", NS)
        roofs = bldg.findall("bldg:boundedBy/bldg:RoofSurface", NS)
        grounds = bldg.findall("bldg:boundedBy/bldg:GroundSurface", NS)
        assert len(walls) == 1  # one WallSurface containing 4 wall polygons
        assert len(roofs) == 1  # one RoofSurface containing 1 roof polygon
        assert len(grounds) == 1

        wall_polys = walls[0].findall(".//gml:Polygon", NS)
        roof_polys = roofs[0].findall(".//gml:Polygon", NS)
        assert len(wall_polys) == 4
        assert len(roof_polys) == 1

        solid_refs = bldg.findall(".//bldg:lod2Solid//gml:surfaceMember", NS)
        assert len(solid_refs) == 4 + 1 + 1  # walls + roof + ground

    def test_solid_references_reuse_boundary_surface_ids(self):
        root = etree.fromstring(write_citygml([make_building()]))
        bldg = root.find(".//bldg:Building", NS)
        all_poly_ids = {
            p.get(f"{{{GML_NS}}}id") for p in bldg.findall(".//bldg:boundedBy//gml:Polygon", NS)
        }
        href_targets = {
            ref.get(f"{{{'http://www.w3.org/1999/xlink'}}}href").lstrip("#")
            for ref in bldg.findall(".//bldg:lod2Solid//gml:surfaceMember", NS)
        }
        assert href_targets == all_poly_ids

    def test_unmodelled_building_falls_back_to_flat_box(self):
        footprint = box(0.0, 0.0, 4.0, 4.0)
        b = Building(bag_id="unmodelled1", footprint=footprint, ground_height=0.0)
        b.lidar_stats = {"top_height": 7.5}
        root = etree.fromstring(write_citygml([b]))
        bldg = root.find(".//bldg:Building", NS)
        roofs = bldg.findall("bldg:boundedBy/bldg:RoofSurface", NS)
        assert len(roofs) == 1
        height_el = bldg.find("bldg:measuredHeight", NS)
        assert float(height_el.text) == 7.5

    def test_bag_id_with_special_characters_becomes_valid_ncname(self):
        b = make_building(bag_id="NL.IMBAG.Pand.0123")
        root = etree.fromstring(write_citygml([b]))
        bldg = root.find(".//bldg:Building", NS)
        gml_id = bldg.get(f"{{{GML_NS}}}id")
        # Must be a valid XML NCName: no dots-as-first-char issues, no invalid chars.
        etree.fromstring(f'<x xmlns:gml="{GML_NS}" gml:id="{gml_id}"/>')
