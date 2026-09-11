"""A triangulated terrain model (DEM) built from ground-classified LiDAR.

Two pieces, in the order they are used:

``GroundGrid`` bins ground-class points into square cells and keeps the
median elevation of each -- a small raster with NaN holes wherever no
ground point landed. ``triangulate`` then turns the occupied cells into a
TIN for the viewer.

The holes matter more than they look. A building *occludes the ground
beneath it*, so every footprint leaves a building-shaped gap in the ground
returns. Delaunay would happily bridge those gaps, but it would equally
happily sheet a single triangle across a river, so the long-edge cull that
removes the river also removes the bridge under every building -- leaving
the terrain full of building-shaped holes with the buildings floating over
them. The fix is ``fill_polygon``: stamp each footprint's own ground height
into its cells *before* triangulating, so the cull only ever sees genuine
no-data spans. See ``web/server.py``'s area job for that ordering.

This is a viewport aid only -- it is not part of the exported CityGML.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Optional, Tuple

import numpy as np
import shapely
from shapely.geometry import MultiPoint, Polygon

from ..data.pointcloud import CLASS_GROUND, LidarPointCloud

Point3 = Tuple[float, float, float]

#: Upper bound on terrain cells. This is really a *browser triangle
#: budget* -- Delaunay yields roughly two triangles per occupied cell, so
#: 40k cells is about 80k triangles. When an area is too large for the
#: preferred cell size, it is the DEM's resolution that gives way (the
#: cells get bigger), never the triangle count.
MAX_TERRAIN_CELLS = 40_000

#: Preferred cell size in meters; only coarsened when the budget demands it.
PREFERRED_CELL_SIZE = 2.0

#: Triangles with an edge longer than this many cells are dropped, so the
#: TIN does not sheet across water or off the edge of the data.
MAX_EDGE_CELLS = 4.0


def cell_size_for_bbox(bbox: tuple, preferred: float = PREFERRED_CELL_SIZE) -> float:
    minx, miny, maxx, maxy = bbox
    area = max((maxx - minx) * (maxy - miny), 0.0)
    if area <= 0:
        return preferred
    return max(preferred, math.sqrt(area / MAX_TERRAIN_CELLS))


@dataclass
class GroundGrid:
    """Median ground elevation per cell; NaN where no ground point landed."""

    cell_size: float
    minx: float
    miny: float
    z: np.ndarray  # shape (nx, ny), NaN = no data

    @property
    def shape(self) -> Tuple[int, int]:
        return self.z.shape

    def cell_of(self, x: float, y: float) -> Tuple[int, int]:
        return (
            int((x - self.minx) // self.cell_size),
            int((y - self.miny) // self.cell_size),
        )

    def cell_center(self, ix: int, iy: int) -> Tuple[float, float]:
        return (
            self.minx + (ix + 0.5) * self.cell_size,
            self.miny + (iy + 0.5) * self.cell_size,
        )

    def sample(self, x: float, y: float, max_rings: int = 32) -> Optional[float]:
        """Elevation at (x, y): this cell's value, else the nearest occupied
        cell found by expanding ring search.

        The ring search is what interpolates across the hole a building
        punches in the ground returns, which is exactly the case this is
        needed for.
        """
        nx, ny = self.z.shape
        if nx == 0 or ny == 0:
            return None
        ix, iy = self.cell_of(x, y)
        ix = min(max(ix, 0), nx - 1)
        iy = min(max(iy, 0), ny - 1)
        if not math.isnan(self.z[ix, iy]):
            return float(self.z[ix, iy])

        for ring in range(1, max_rings + 1):
            lo_x, hi_x = max(ix - ring, 0), min(ix + ring, nx - 1)
            lo_y, hi_y = max(iy - ring, 0), min(iy + ring, ny - 1)
            if lo_x == 0 and lo_y == 0 and hi_x == nx - 1 and hi_y == ny - 1 and ring > max(nx, ny):
                break
            window = self.z[lo_x : hi_x + 1, lo_y : hi_y + 1]
            if np.isnan(window).all():
                continue
            return float(np.nanmedian(window))
        return None

    def fill_polygon(self, polygon: Polygon, value: float) -> None:
        """Stamp ``value`` into every cell whose center falls in ``polygon``.

        Used to close the ground-return hole under a building before
        triangulating -- see the module docstring.
        """
        nx, ny = self.z.shape
        if nx == 0 or ny == 0:
            return
        minx, miny, maxx, maxy = polygon.bounds
        ix0 = max(int((minx - self.minx) // self.cell_size), 0)
        iy0 = max(int((miny - self.miny) // self.cell_size), 0)
        ix1 = min(int((maxx - self.minx) // self.cell_size), nx - 1)
        iy1 = min(int((maxy - self.miny) // self.cell_size), ny - 1)
        if ix1 < ix0 or iy1 < iy0:
            return

        ixs = np.arange(ix0, ix1 + 1)
        iys = np.arange(iy0, iy1 + 1)
        cx = self.minx + (ixs + 0.5) * self.cell_size
        cy = self.miny + (iys + 0.5) * self.cell_size
        gx, gy = np.meshgrid(cx, cy, indexing="ij")
        inside = shapely.contains_xy(polygon, gx.ravel(), gy.ravel()).reshape(gx.shape)
        if not inside.any():
            # A footprint smaller than one cell can miss every center; still
            # anchor its own cell so it does not stay a hole.
            ix, iy = self.cell_of(*polygon.centroid.coords[0])
            if 0 <= ix < nx and 0 <= iy < ny:
                self.z[ix, iy] = value
            return
        block = self.z[ix0 : ix1 + 1, iy0 : iy1 + 1]
        block[inside] = value


@dataclass
class TerrainMesh:
    """A TIN: shared vertex pool plus triangles indexing into it."""

    vertices: List[Point3]
    triangles: List[Tuple[int, int, int]]

    def __len__(self) -> int:
        return len(self.triangles)


def build_ground_grid(cloud: LidarPointCloud, bbox: tuple) -> Optional[GroundGrid]:
    """Bin ground-class points into cells, keeping each cell's median Z.

    Returns ``None`` when the cloud has no usable classification or no
    ground points -- "no terrain" is a normal state, not an error.
    """
    if len(cloud) == 0 or not cloud.has_classification:
        return None
    ground = cloud.filter_classes(include=[CLASS_GROUND])
    if len(ground) == 0:
        return None

    minx, miny, maxx, maxy = bbox
    cell = cell_size_for_bbox(bbox)
    nx = max(int(math.ceil((maxx - minx) / cell)), 1)
    ny = max(int(math.ceil((maxy - miny) / cell)), 1)

    ix = np.clip(((ground.x - minx) / cell).astype(np.int64), 0, nx - 1)
    iy = np.clip(((ground.y - miny) / cell).astype(np.int64), 0, ny - 1)
    cid = ix * ny + iy

    # Counting sort into per-cell runs, then a median per run.
    counts = np.bincount(cid, minlength=nx * ny)
    order = np.argsort(cid, kind="stable")
    sorted_z = ground.z[order]
    starts = np.concatenate([[0], np.cumsum(counts)])

    z = np.full(nx * ny, np.nan)
    occupied = np.nonzero(counts)[0]
    for key in occupied:
        z[key] = np.median(sorted_z[starts[key] : starts[key + 1]])

    return GroundGrid(cell_size=cell, minx=minx, miny=miny, z=z.reshape(nx, ny))


def triangulate(grid: GroundGrid, max_edge_cells: float = MAX_EDGE_CELLS) -> Optional[TerrainMesh]:
    """Delaunay-triangulate a ground grid's occupied cells into a TIN.

    The triangulation runs in *integer cell-index space*, not world space.
    Uniform scaling and translation preserve a Delaunay triangulation
    (circumcircles map to circles), so the result is identical -- but
    integer coordinates make mapping an output vertex back to its source
    cell an exact lookup instead of a float-equality gamble.
    """
    occupied = np.nonzero(~np.isnan(grid.z))
    ixs, iys = occupied[0], occupied[1]
    if len(ixs) < 3:
        return None

    index_of: dict = {}
    vertices: List[Point3] = []
    for ix, iy in zip(ixs.tolist(), iys.tolist()):
        cx, cy = grid.cell_center(ix, iy)
        index_of[(ix, iy)] = len(vertices)
        vertices.append((cx, cy, float(grid.z[ix, iy])))

    collection = shapely.delaunay_triangles(
        MultiPoint([(float(ix), float(iy)) for ix, iy in zip(ixs.tolist(), iys.tolist())])
    )
    if collection.is_empty:  # all points collinear
        return None

    max_edge_sq = max_edge_cells * max_edge_cells
    triangles: List[Tuple[int, int, int]] = []
    for tri in collection.geoms:
        ring = list(tri.exterior.coords)[:-1]
        if len(ring) != 3:
            continue
        cells = [(int(round(px)), int(round(py))) for px, py in ring]
        if any(c not in index_of for c in cells):
            continue
        if _longest_edge_sq(cells) > max_edge_sq:
            continue  # spans a genuine no-data gap (water, edge of data)
        if _signed_area(cells) < 0:
            # GEOS does not guarantee consistent winding; normalise to CCW
            # so the viewer's computed normals all face up.
            cells.reverse()
        triangles.append(tuple(index_of[c] for c in cells))  # type: ignore[arg-type]

    if not triangles:
        return None
    return TerrainMesh(vertices=vertices, triangles=triangles)


def _longest_edge_sq(cells: List[Tuple[int, int]]) -> float:
    longest = 0.0
    for i in range(3):
        ax, ay = cells[i]
        bx, by = cells[(i + 1) % 3]
        longest = max(longest, float((ax - bx) ** 2 + (ay - by) ** 2))
    return longest


def _signed_area(cells: List[Tuple[int, int]]) -> float:
    (ax, ay), (bx, by), (cx, cy) = cells
    return 0.5 * ((bx - ax) * (cy - ay) - (cx - ax) * (by - ay))
