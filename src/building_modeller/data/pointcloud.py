"""AHN6 LiDAR point cloud loading and per-footprint height statistics.

LiDAR is used only to *inform* manual modelling here -- computing rough
ground/eave/ridge height estimates for a footprint -- not to fit roof
planes automatically (that is a deliberately postponed future milestone).
"""
from __future__ import annotations

import glob
import os
from dataclasses import dataclass
from typing import Iterable, List, Optional

import numpy as np
from shapely.geometry import Polygon

try:
    import laspy
except ImportError:  # pragma: no cover - exercised only when laspy is missing
    laspy = None


@dataclass
class LidarPointCloud:
    """A flat, in-memory point cloud (x, y, z in the source CRS, typically
    EPSG:28992 + NAP for AHN6)."""

    x: np.ndarray
    y: np.ndarray
    z: np.ndarray

    def __len__(self) -> int:
        return len(self.x)

    @classmethod
    def empty(cls) -> "LidarPointCloud":
        return cls(np.empty(0), np.empty(0), np.empty(0))

    @classmethod
    def from_files(
        cls, paths: Iterable[str], bbox: Optional[tuple] = None
    ) -> "LidarPointCloud":
        """Load one or more LAS/LAZ files, optionally cropped to a bbox
        (minx, miny, maxx, maxy) while reading to keep memory bounded."""
        if laspy is None:
            raise ImportError(
                "laspy is required to load point clouds (pip install 'laspy[lazrs]')"
            )
        xs, ys, zs = [], [], []
        for path in paths:
            with laspy.open(path) as reader:
                for chunk in reader.chunk_iterator(2_000_000):
                    x = np.asarray(chunk.x)
                    y = np.asarray(chunk.y)
                    z = np.asarray(chunk.z)
                    if bbox is not None:
                        minx, miny, maxx, maxy = bbox
                        mask = (x >= minx) & (x <= maxx) & (y >= miny) & (y <= maxy)
                        x, y, z = x[mask], y[mask], z[mask]
                    xs.append(x)
                    ys.append(y)
                    zs.append(z)
        if not xs:
            return cls.empty()
        return cls(np.concatenate(xs), np.concatenate(ys), np.concatenate(zs))

    def crop_to_bbox(self, minx: float, miny: float, maxx: float, maxy: float) -> "LidarPointCloud":
        mask = (self.x >= minx) & (self.x <= maxx) & (self.y >= miny) & (self.y <= maxy)
        return LidarPointCloud(self.x[mask], self.y[mask], self.z[mask])

    def mask_in_polygon(self, polygon: Polygon) -> np.ndarray:
        if len(self) == 0:
            return np.empty(0, dtype=bool)
        minx, miny, maxx, maxy = polygon.bounds
        bbox_mask = (self.x >= minx) & (self.x <= maxx) & (self.y >= miny) & (self.y <= maxy)
        if not bbox_mask.any():
            return bbox_mask
        import shapely

        fine = np.zeros_like(bbox_mask)
        fine[bbox_mask] = shapely.contains_xy(polygon, self.x[bbox_mask], self.y[bbox_mask])
        return fine

    def points_in_polygon(self, polygon: Polygon) -> "LidarPointCloud":
        mask = self.mask_in_polygon(polygon)
        return LidarPointCloud(self.x[mask], self.y[mask], self.z[mask])

    def height_near(self, x: float, y: float, radius: float = 1.0) -> Optional[dict]:
        """The representative ground/roof height right at (x, y), for
        snapping a single mesh vertex to the point cloud -- the median Z
        of points within ``radius`` meters, or ``None`` if none are
        found."""
        if len(self) == 0:
            return None
        dist2 = (self.x - x) ** 2 + (self.y - y) ** 2
        mask = dist2 <= radius * radius
        if not mask.any():
            return None
        nearby_z = self.z[mask]
        return {"z": float(np.median(nearby_z)), "point_count": int(mask.sum())}


def find_tiles(folder: str, pattern: str = "*.laz") -> List[str]:
    """List candidate AHN6 tile files in a local folder (also matches *.las)."""
    paths = sorted(glob.glob(os.path.join(folder, pattern)))
    if pattern == "*.laz":
        paths += sorted(glob.glob(os.path.join(folder, "*.las")))
    return paths


def stats_for_footprint(
    cloud: LidarPointCloud,
    polygon: Polygon,
    ground_percentile: float = 5.0,
    top_percentile: float = 95.0,
) -> Optional[dict]:
    """Rough ground/eave/ridge height suggestions for one footprint.

    Returns ``None`` if there are no LiDAR points over the footprint.
    Heights are meant as a *starting point* the user adjusts in the UI,
    not a reconstruction result.
    """
    inside = cloud.points_in_polygon(polygon)
    if len(inside) == 0:
        return None
    ground_height = float(np.percentile(inside.z, ground_percentile))
    top_height = float(np.percentile(inside.z, top_percentile))
    return {
        "point_count": len(inside),
        "ground_height": ground_height,
        "top_height": top_height,
        "eave_estimate": ground_height + 0.7 * (top_height - ground_height),
        "ridge_estimate": top_height,
    }
