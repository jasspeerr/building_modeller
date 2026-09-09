"""The core per-building data model."""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Optional

from shapely.geometry import Polygon

from .roofshapes import BuildingMesh, RoofParams, RoofType, generate_mesh


class ModellingStatus(str, enum.Enum):
    UNMODELLED = "unmodelled"  # only a footprint, no roof chosen yet
    EDITED = "edited"  # user has picked/adjusted a roof
    EXPORTED = "exported"  # included in the last export


@dataclass
class Building:
    """One building: a BAG footprint plus (optionally) a manually chosen
    LOD2.2 roof shape informed by LiDAR height statistics."""

    bag_id: str
    footprint: Polygon  # EPSG:28992 (RD New)
    ground_height: float = 0.0  # absolute Z, e.g. from AHN6 5th percentile
    roof: Optional[RoofParams] = None
    status: ModellingStatus = ModellingStatus.UNMODELLED
    lidar_stats: Optional[dict] = None  # see data.pointcloud.stats_for_footprint

    def set_roof(self, roof: RoofParams) -> None:
        self.roof = roof
        self.status = ModellingStatus.EDITED

    def mesh(self) -> BuildingMesh:
        """Build the wall/roof/ground surfaces for export or preview.

        Unmodelled buildings fall back to a flat LOD1-style box at the
        LiDAR-derived (or default) top height, so a batch export never
        silently drops a building the user hasn't gotten to yet.
        """
        if self.roof is not None:
            return generate_mesh(self.footprint, self.ground_height, self.roof)
        fallback_height = self._fallback_height()
        fallback_roof = RoofParams(roof_type=RoofType.FLAT, eave_height=fallback_height)
        return generate_mesh(self.footprint, self.ground_height, fallback_roof)

    def effective_lod(self) -> str:
        if self.roof is None:
            return "1.2"
        return "1.2" if self.roof.roof_type == RoofType.FLAT else "2.2"

    def _fallback_height(self) -> float:
        if self.lidar_stats and "top_height" in self.lidar_stats:
            return float(self.lidar_stats["top_height"])
        return self.ground_height + 3.0
