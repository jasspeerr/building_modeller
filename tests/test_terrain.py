"""Terrain/DEM tests.

The building-hole case (``TestBuildingHoles``) is the one that matters
most: a building occludes the ground beneath it, so every footprint leaves
a gap in the ground returns, and a naive long-edge cull deletes exactly the
triangles that bridge those gaps -- leaving the terrain full of
building-shaped holes with the buildings floating over them.
"""
import numpy as np
import pytest
from shapely.geometry import box

from building_modeller.data.pointcloud import CLASS_BUILDING, CLASS_GROUND, LidarPointCloud
from building_modeller.model.terrain import (
    MAX_TERRAIN_CELLS,
    GroundGrid,
    build_ground_grid,
    cell_size_for_bbox,
    triangulate,
)

BBOX = (0.0, 0.0, 60.0, 60.0)


def make_ground_cloud(hole=None, slope=0.05, n=40_000, seed=1):
    """A sloped ground plane, optionally with a rectangular hole in the
    ground returns (i.e. a building standing there) filled with
    building-class points instead."""
    rng = np.random.default_rng(seed)
    x = rng.uniform(BBOX[0], BBOX[2], n)
    y = rng.uniform(BBOX[1], BBOX[3], n)
    z = 1.0 + slope * x + rng.normal(0.0, 0.01, n)
    cls = np.full(n, CLASS_GROUND, dtype=np.uint8)

    if hole is not None:
        minx, miny, maxx, maxy = hole
        inside = (x >= minx) & (x <= maxx) & (y >= miny) & (y <= maxy)
        # Ground returns do not exist under a roof -- they come back as
        # building points several meters up instead.
        cls[inside] = CLASS_BUILDING
        z[inside] += 8.0
    return LidarPointCloud(x, y, z, cls)


class TestCellSize:
    def test_uses_preferred_size_for_a_small_area(self):
        assert cell_size_for_bbox((0, 0, 100, 100)) == pytest.approx(2.0)

    def test_coarsens_rather_than_blowing_the_cell_budget(self):
        bbox = (0, 0, 5000, 5000)
        cell = cell_size_for_bbox(bbox)
        assert cell > 2.0
        cells = (5000 / cell) ** 2
        assert cells <= MAX_TERRAIN_CELLS * 1.01


class TestBuildGroundGrid:
    def test_recovers_a_sloped_plane(self):
        grid = build_ground_grid(make_ground_cloud(), BBOX)
        assert grid is not None
        for x in (10.0, 30.0, 50.0):
            assert grid.sample(x, 30.0) == pytest.approx(1.0 + 0.05 * x, abs=0.2)

    def test_ignores_non_ground_classes(self):
        cloud = make_ground_cloud(hole=(20.0, 20.0, 40.0, 40.0))
        grid = build_ground_grid(cloud, BBOX)
        assert grid is not None
        # The building points are ~8m up; the grid must not have taken them.
        assert np.nanmax(grid.z) < 5.0

    def test_returns_none_without_ground_points(self):
        cloud = make_ground_cloud()
        cloud.classification[:] = CLASS_BUILDING
        assert build_ground_grid(cloud, BBOX) is None

    def test_returns_none_for_an_unclassified_cloud(self):
        cloud = make_ground_cloud()
        assert build_ground_grid(LidarPointCloud(cloud.x, cloud.y, cloud.z), BBOX) is None

    def test_returns_none_for_an_empty_cloud(self):
        assert build_ground_grid(LidarPointCloud.empty(), BBOX) is None


class TestSample:
    def test_hole_cells_are_nan_but_sample_still_answers(self):
        hole = (20.0, 20.0, 40.0, 40.0)
        grid = build_ground_grid(make_ground_cloud(hole=hole), BBOX)
        ix, iy = grid.cell_of(30.0, 30.0)
        assert np.isnan(grid.z[ix, iy])  # no ground returns under the building
        # ...but the ring search interpolates across it.
        assert grid.sample(30.0, 30.0) == pytest.approx(1.0 + 0.05 * 30.0, abs=1.0)

    def test_returns_none_when_the_grid_is_entirely_empty(self):
        grid = GroundGrid(cell_size=2.0, minx=0.0, miny=0.0, z=np.full((5, 5), np.nan))
        assert grid.sample(4.0, 4.0) is None


class TestFillPolygon:
    def test_fills_the_cells_a_footprint_covers(self):
        grid = build_ground_grid(make_ground_cloud(hole=(20.0, 20.0, 40.0, 40.0)), BBOX)
        footprint = box(20.0, 20.0, 40.0, 40.0)
        assert np.isnan(grid.z[grid.cell_of(30.0, 30.0)])

        grid.fill_polygon(footprint, 2.5)

        assert grid.z[grid.cell_of(30.0, 30.0)] == pytest.approx(2.5)
        # Cells outside the footprint are untouched.
        assert grid.z[grid.cell_of(5.0, 5.0)] != pytest.approx(2.5)

    def test_a_footprint_smaller_than_a_cell_still_anchors_one(self):
        grid = GroundGrid(cell_size=2.0, minx=0.0, miny=0.0, z=np.full((5, 5), np.nan))
        grid.fill_polygon(box(3.1, 3.1, 3.3, 3.3), 7.0)
        assert grid.z[grid.cell_of(3.2, 3.2)] == pytest.approx(7.0)


class TestTriangulate:
    def test_produces_a_tin_over_a_plain_grid(self):
        grid = build_ground_grid(make_ground_cloud(), BBOX)
        mesh = triangulate(grid)
        assert mesh is not None
        assert len(mesh.triangles) > 100
        assert all(0 <= i < len(mesh.vertices) for t in mesh.triangles for i in t)

    def test_vertices_sit_exactly_on_cell_centers(self):
        # The index-space lookup depends on this being exact, not approximate.
        grid = build_ground_grid(make_ground_cloud(), BBOX)
        mesh = triangulate(grid)
        centers = {
            grid.cell_center(ix, iy)
            for ix, iy in zip(*np.nonzero(~np.isnan(grid.z)))
        }
        assert {(vx, vy) for vx, vy, _ in mesh.vertices} == centers

    def test_all_triangles_wind_counter_clockwise(self):
        # GEOS does not guarantee winding; if this regresses, half the
        # terrain renders black in the viewer.
        grid = build_ground_grid(make_ground_cloud(), BBOX)
        mesh = triangulate(grid)
        for a, b, c in mesh.triangles:
            (ax, ay, _), (bx, by, _), (cx, cy, _) = (
                mesh.vertices[a],
                mesh.vertices[b],
                mesh.vertices[c],
            )
            assert (bx - ax) * (cy - ay) - (cx - ax) * (by - ay) > 0

    def test_returns_none_for_fewer_than_three_cells(self):
        z = np.full((5, 5), np.nan)
        z[0, 0] = 1.0
        z[1, 1] = 1.0
        assert triangulate(GroundGrid(2.0, 0.0, 0.0, z)) is None

    def test_returns_none_for_collinear_cells(self):
        z = np.full((6, 6), np.nan)
        z[:, 0] = 1.0  # a single row of cells
        assert triangulate(GroundGrid(2.0, 0.0, 0.0, z)) is None


class TestBuildingHoles:
    """The cull must remove genuine no-data spans without punching a hole
    under every building."""

    def test_unfilled_building_hole_loses_its_bridging_triangles(self):
        hole = (20.0, 20.0, 40.0, 40.0)  # a 20m building, wider than the cull
        grid = build_ground_grid(make_ground_cloud(hole=hole), BBOX)
        mesh = triangulate(grid)
        assert _covers(mesh, 30.0, 30.0) is False

    def test_filling_the_footprint_first_keeps_the_terrain_closed(self):
        hole = (20.0, 20.0, 40.0, 40.0)
        grid = build_ground_grid(make_ground_cloud(hole=hole), BBOX)
        footprint = box(*hole)
        grid.fill_polygon(footprint, grid.sample(30.0, 30.0))

        mesh = triangulate(grid)
        assert _covers(mesh, 30.0, 30.0) is True

    def test_long_edges_are_still_culled_over_a_genuine_gap(self):
        # A wide strip of no-data (a river) with no footprint to fill it.
        grid = build_ground_grid(make_ground_cloud(), BBOX)
        grid.z[:, 10:22] = np.nan
        mesh = triangulate(grid)
        assert _covers(mesh, 30.0, 32.0) is False


def _covers(mesh, x, y) -> bool:
    """Is (x, y) inside any triangle of the TIN?"""
    if mesh is None:
        return False
    for a, b, c in mesh.triangles:
        (ax, ay, _), (bx, by, _), (cx, cy, _) = (
            mesh.vertices[a],
            mesh.vertices[b],
            mesh.vertices[c],
        )
        d = (by - cy) * (ax - cx) + (cx - bx) * (ay - cy)
        if abs(d) < 1e-12:
            continue
        u = ((by - cy) * (x - cx) + (cx - bx) * (y - cy)) / d
        v = ((cy - ay) * (x - cx) + (ax - cx) * (y - cy)) / d
        if u >= -1e-9 and v >= -1e-9 and u + v <= 1 + 1e-9:
            return True
    return False
