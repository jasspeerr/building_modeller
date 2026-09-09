"""The core per-building data model."""
from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Optional

from shapely.geometry import Polygon

from .mesh import EditableMesh, Point3, seed_flat_box


class ModellingStatus(str, enum.Enum):
    UNMODELLED = "unmodelled"  # still the seeded flat box, never edited
    EDITED = "edited"  # user has moved at least one vertex
    EXPORTED = "exported"  # included in the last export


@dataclass
class Building:
    """One building: a BAG footprint plus an editable 3D mesh, seeded as
    a flat box and shaped by moving vertices (no roof-type picker --
    see model.mesh)."""

    bag_id: str
    footprint: Polygon  # EPSG:28992 (RD New) -- the original BAG outline,
    # kept for area queries/LiDAR stats even after the mesh is edited.
    ground_height: float = 0.0  # absolute Z, e.g. from AHN6 5th percentile
    geometry: Optional[EditableMesh] = None
    status: ModellingStatus = ModellingStatus.UNMODELLED
    lidar_stats: Optional[dict] = None  # see data.pointcloud.stats_for_footprint

    def mesh(self) -> EditableMesh:
        """The building's actual, persistent 3D geometry, seeding it from
        the footprint on first access."""
        if self.geometry is None:
            self.geometry = seed_flat_box(self.footprint, self.ground_height, self._fallback_height())
        return self.geometry

    def move_vertex(self, index: int, position: Point3) -> None:
        self.mesh().move_vertex(index, position)
        self.status = ModellingStatus.EDITED

    def split_edge(self, index_a: int, index_b: int) -> int:
        new_index = self.mesh().split_edge(index_a, index_b)
        self.status = ModellingStatus.EDITED
        return new_index

    def delete_vertex(self, index: int) -> None:
        self.mesh().delete_vertex(index)
        self.status = ModellingStatus.EDITED

    def split_face(self, index_a: int, index_b: int) -> None:
        self.mesh().split_face(index_a, index_b)
        self.status = ModellingStatus.EDITED

    def extrude_face(self, index_a: int, index_b: int, distance: float) -> None:
        self.mesh().extrude_face(index_a, index_b, distance)
        self.status = ModellingStatus.EDITED

    def _fallback_height(self) -> float:
        if self.lidar_stats and "top_height" in self.lidar_stats:
            return float(self.lidar_stats["top_height"])
        return self.ground_height + 3.0
