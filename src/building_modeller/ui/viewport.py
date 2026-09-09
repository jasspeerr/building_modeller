"""3D viewport built on pyqtgraph.opengl -- deliberately lighter-weight
than a VTK/PyVista or Open3D based viewer (no extra compiled geometry
kernel beyond what pyqtgraph/PyOpenGL already need)."""
from __future__ import annotations

from typing import Iterable, List, Sequence, Tuple

import numpy as np
import pyqtgraph.opengl as gl

from ..data.pointcloud import LidarPointCloud
from ..model.roofshapes import BuildingMesh, Ring3


def _triangulate_fan(ring: Ring3) -> List[Tuple[int, int, int]]:
    """Fan triangulation from the first vertex. Valid for the convex
    wall/roof/ground polygons ``roofshapes`` produces (quads, the convex
    gable-end pentagons, and triangles)."""
    return [(0, i, i + 1) for i in range(1, len(ring) - 1)]


def _mesh_to_arrays(rings: Iterable[Ring3]):
    verts: List[Tuple[float, float, float]] = []
    faces: List[Tuple[int, int, int]] = []
    for ring in rings:
        if len(ring) < 3:
            continue
        base = len(verts)
        verts.extend(ring)
        faces.extend((base + a, base + b, base + c) for a, b, c in _triangulate_fan(ring))
    if not verts:
        return np.empty((0, 3)), np.empty((0, 3), dtype=int)
    return np.array(verts, dtype=float), np.array(faces, dtype=int)


def _elevation_colormap(z: np.ndarray) -> np.ndarray:
    if len(z) == 0:
        return np.empty((0, 4))
    zmin, zmax = float(z.min()), float(z.max())
    span = max(zmax - zmin, 1e-6)
    t = (z - zmin) / span
    colors = np.zeros((len(z), 4))
    colors[:, 0] = t
    colors[:, 1] = 0.3
    colors[:, 2] = 1.0 - t
    colors[:, 3] = 1.0
    return colors


class Viewport(gl.GLViewWidget):
    """The 3D scene: footprint outlines, the LiDAR reference point cloud,
    and the live building mesh preview.

    RD New coordinates are in the hundreds of thousands, which is bad for
    OpenGL numerical precision, so everything drawn here is offset by
    :meth:`set_origin` (typically the loaded area's center).
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setCameraPosition(distance=150)
        self._origin = np.array([0.0, 0.0, 0.0])
        self._footprint_items: List[gl.GLLinePlotItem] = []
        self._point_cloud_item = None
        self._mesh_item = None

        grid = gl.GLGridItem()
        grid.setSize(400, 400)
        grid.setSpacing(20, 20)
        self.addItem(grid)

    def set_origin(self, x: float, y: float, z: float = 0.0) -> None:
        self._origin = np.array([x, y, z])

    def clear_footprints(self) -> None:
        for item in self._footprint_items:
            self.removeItem(item)
        self._footprint_items = []

    def add_footprint_outline(
        self,
        ring_xy: Sequence[Tuple[float, float]],
        z: float = 0.0,
        color: Tuple[float, float, float, float] = (0.2, 0.6, 1.0, 1.0),
    ) -> None:
        ring = list(ring_xy)
        if not ring:
            return
        closed = ring + [ring[0]]
        pts = np.array([(x, y, z) for x, y in closed]) - self._origin
        item = gl.GLLinePlotItem(pos=pts, color=color, width=2, mode="line_strip")
        self.addItem(item)
        self._footprint_items.append(item)

    def set_point_cloud(self, cloud: LidarPointCloud, point_size: float = 2.0) -> None:
        if self._point_cloud_item is not None:
            self.removeItem(self._point_cloud_item)
            self._point_cloud_item = None
        if len(cloud) == 0:
            return
        pos = np.column_stack([cloud.x, cloud.y, cloud.z]) - self._origin
        colors = _elevation_colormap(cloud.z)
        self._point_cloud_item = gl.GLScatterPlotItem(pos=pos, color=colors, size=point_size)
        self.addItem(self._point_cloud_item)

    def set_building_mesh(
        self, mesh: BuildingMesh, color: Tuple[float, float, float, float] = (0.85, 0.3, 0.2, 0.9)
    ) -> None:
        if self._mesh_item is not None:
            self.removeItem(self._mesh_item)
            self._mesh_item = None
        rings = list(mesh.ground) + list(mesh.walls) + list(mesh.roof)
        verts, faces = _mesh_to_arrays(rings)
        if len(verts) == 0:
            return
        verts = verts - self._origin
        mesh_data = gl.MeshData(vertexes=verts, faces=faces)
        self._mesh_item = gl.GLMeshItem(
            meshdata=mesh_data, smooth=False, color=color, shader="shaded", drawEdges=True
        )
        self.addItem(self._mesh_item)

    def clear_building_mesh(self) -> None:
        if self._mesh_item is not None:
            self.removeItem(self._mesh_item)
            self._mesh_item = None
