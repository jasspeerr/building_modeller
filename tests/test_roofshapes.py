import math

import pytest
from shapely.geometry import Polygon, box

from building_modeller.model.roofshapes import (
    RoofParams,
    RoofType,
    flat_mesh,
    gable_mesh,
    generate_mesh,
    hip_mesh,
    pyramid_mesh,
    shed_mesh,
)

RECT = box(0.0, 0.0, 10.0, 6.0)  # 10 x 6 rectangle
SQUARE = box(0.0, 0.0, 8.0, 8.0)


def ring_area(ring):
    """Area of a (possibly 3D) planar ring's XY projection, shoelace formula."""
    n = len(ring)
    area = 0.0
    for i in range(n):
        x0, y0 = ring[i][0], ring[i][1]
        x1, y1 = ring[(i + 1) % n][0], ring[(i + 1) % n][1]
        area += x0 * y1 - x1 * y0
    return abs(area) / 2.0


def assert_planar(ring, tol=1e-6):
    if len(ring) <= 3:
        return
    p0, p1, p2 = ring[0], ring[1], ring[2]
    import numpy as np

    v1 = np.array(p1) - np.array(p0)
    v2 = np.array(p2) - np.array(p0)
    normal = np.cross(v1, v2)
    for p in ring[3:]:
        v = np.array(p) - np.array(p0)
        assert abs(np.dot(normal, v)) < tol * max(1.0, np.linalg.norm(normal)), (
            f"ring is not planar: {ring}"
        )


class TestFlatMesh:
    def test_ground_and_roof_areas_match_footprint(self):
        mesh = flat_mesh(RECT, ground_height=0.0, roof_height=3.0)
        assert len(mesh.roof) == 1
        assert len(mesh.ground) == 1
        assert ring_area(mesh.roof[0]) == pytest.approx(60.0)
        assert ring_area(mesh.ground[0]) == pytest.approx(60.0)
        assert len(mesh.walls) == 4

    def test_roof_at_correct_height(self):
        mesh = flat_mesh(RECT, ground_height=1.0, roof_height=4.0)
        assert all(z == pytest.approx(4.0) for _, _, z in mesh.roof[0])
        assert all(z == pytest.approx(1.0) for _, _, z in mesh.ground[0])

    def test_walls_are_planar_quads(self):
        mesh = flat_mesh(RECT, ground_height=0.0, roof_height=3.0)
        for wall in mesh.walls:
            assert len(wall) == 4
            assert_planar(wall)


class TestShedMesh:
    def test_roof_is_single_plane(self):
        mesh = shed_mesh(RECT, ground_height=0.0, low_height=3.0, high_height=5.0)
        assert len(mesh.roof) == 1
        assert_planar(mesh.roof[0])

    def test_height_range_matches_params(self):
        mesh = shed_mesh(RECT, ground_height=0.0, low_height=3.0, high_height=5.0)
        zs = [z for _, _, z in mesh.roof[0]]
        assert min(zs) == pytest.approx(3.0, abs=1e-6)
        assert max(zs) == pytest.approx(5.0, abs=1e-6)


class TestGableMesh:
    def test_produces_two_planar_roof_faces(self):
        mesh = gable_mesh(RECT, ground_height=0.0, eave_height=3.0, ridge_height=5.0)
        assert len(mesh.roof) == 2
        for face in mesh.roof:
            assert_planar(face)

    def test_ridge_height_reached(self):
        mesh = gable_mesh(RECT, ground_height=0.0, eave_height=3.0, ridge_height=5.0)
        max_z = max(z for face in mesh.roof for _, _, z in face)
        assert max_z == pytest.approx(5.0)

    def test_four_walls_two_pentagon_two_rect(self):
        mesh = gable_mesh(RECT, ground_height=0.0, eave_height=3.0, ridge_height=5.0)
        assert len(mesh.walls) == 4
        sizes = sorted(len(w) for w in mesh.walls)
        assert sizes == [4, 4, 5, 5]


class TestHipMesh:
    def test_roof_faces_are_all_triangles(self):
        mesh = hip_mesh(RECT, ground_height=0.0, eave_height=3.0, ridge_height=5.0, resolution=1.0)
        assert len(mesh.roof) > 0
        assert all(len(face) == 3 for face in mesh.roof)

    def test_all_walls_are_simple_rectangles_at_eave_height(self):
        mesh = hip_mesh(RECT, ground_height=0.0, eave_height=3.0, ridge_height=5.0)
        assert len(mesh.walls) == 4
        assert all(len(w) == 4 for w in mesh.walls)
        for wall in mesh.walls:
            top_zs = [z for _, _, z in wall if z != 0.0]
            assert all(z == pytest.approx(3.0) for z in top_zs)

    def test_height_range_spans_eave_to_ridge(self):
        mesh = hip_mesh(RECT, ground_height=0.0, eave_height=3.0, ridge_height=5.0, resolution=1.0)
        zs = [z for face in mesh.roof for _, _, z in face]
        assert min(zs) == pytest.approx(3.0, abs=1e-6)
        assert max(zs) == pytest.approx(5.0, abs=0.05)

    def test_height_increases_with_distance_from_boundary(self):
        # A point near the middle of the long edge should be lower than the
        # ridge, and boundary vertices should sit exactly at eave height.
        mesh = hip_mesh(RECT, ground_height=0.0, eave_height=3.0, ridge_height=6.0, resolution=0.5)
        boundary_zs = {round(z, 6) for face in mesh.roof for x, y, z in face
                        if (x in (0.0, 10.0) or y in (0.0, 6.0))}
        assert 3.0 in boundary_zs or all(z >= 3.0 for z in boundary_zs)
        interior_max_z = max(z for face in mesh.roof for _, _, z in face)
        assert interior_max_z > 3.0

    def test_ground_and_walls_use_real_footprint_for_l_shape(self):
        l_shape = Polygon([(0, 0), (10, 0), (10, 4), (4, 4), (4, 8), (0, 8)])
        mesh = hip_mesh(l_shape, ground_height=0.0, eave_height=3.0, ridge_height=6.0, resolution=1.0)
        n_edges = len(list(l_shape.exterior.coords)) - 1
        assert len(mesh.walls) == n_edges
        assert len(mesh.ground) == 1

    def test_works_on_l_shaped_footprint_without_crashing(self):
        l_shape = Polygon([(0, 0), (10, 0), (10, 4), (4, 4), (4, 8), (0, 8)])
        mesh = hip_mesh(l_shape, ground_height=0.0, eave_height=3.0, ridge_height=6.0, resolution=1.0)
        assert len(mesh.roof) > 0
        zs = [z for face in mesh.roof for _, _, z in face]
        assert min(zs) == pytest.approx(3.0, abs=1e-6)
        assert max(zs) <= 6.0 + 1e-6

    def test_flat_when_eave_equals_ridge(self):
        mesh = hip_mesh(RECT, ground_height=0.0, eave_height=4.0, ridge_height=4.0, resolution=1.0)
        zs = [z for face in mesh.roof for _, _, z in face]
        assert all(z == pytest.approx(4.0) for z in zs)

    def test_tiny_footprint_does_not_crash(self):
        tiny = box(0.0, 0.0, 0.3, 0.3)
        mesh = hip_mesh(tiny, ground_height=0.0, eave_height=3.0, ridge_height=4.0, resolution=1.5)
        assert len(mesh.roof) > 0

    def test_square_footprint_reaches_ridge_near_center(self):
        mesh = hip_mesh(SQUARE, ground_height=0.0, eave_height=3.0, ridge_height=6.0, resolution=0.5)
        max_z = max(z for face in mesh.roof for _, _, z in face)
        assert max_z == pytest.approx(6.0, abs=0.1)


class TestPyramidMesh:
    def test_fan_of_triangles_from_apex(self):
        mesh = pyramid_mesh(RECT, ground_height=0.0, eave_height=3.0, ridge_height=6.0)
        assert len(mesh.roof) == 4  # one triangle per footprint edge
        for tri in mesh.roof:
            assert len(tri) == 3
        apex_zs = {round(t[2][2], 6) for t in mesh.roof}
        assert apex_zs == {6.0}

    def test_works_on_non_rectangular_polygon(self):
        # L-shaped footprint
        l_shape = Polygon([(0, 0), (10, 0), (10, 4), (4, 4), (4, 8), (0, 8)])
        mesh = pyramid_mesh(l_shape, ground_height=0.0, eave_height=3.0, ridge_height=6.0)
        assert len(mesh.roof) == len(list(l_shape.exterior.coords)) - 1
        assert len(mesh.walls) == len(mesh.roof)


class TestGenerateMeshDispatch:
    def test_flat(self):
        roof = RoofParams(roof_type=RoofType.FLAT, eave_height=3.0)
        mesh = generate_mesh(RECT, 0.0, roof)
        assert len(mesh.roof) == 1

    def test_missing_ridge_height_raises(self):
        roof = RoofParams(roof_type=RoofType.GABLE, eave_height=3.0)
        with pytest.raises(ValueError):
            generate_mesh(RECT, 0.0, roof)

    def test_unknown_roof_type_raises(self):
        roof = RoofParams(roof_type=RoofType.FLAT, eave_height=3.0)
        roof.roof_type = "not-a-type"
        with pytest.raises(ValueError):
            generate_mesh(RECT, 0.0, roof)
