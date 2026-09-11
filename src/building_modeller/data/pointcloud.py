"""AHN6 LiDAR point cloud loading and per-footprint height statistics.

LiDAR is used only to *inform* manual modelling here -- computing rough
ground/eave/ridge height estimates for a footprint -- not to fit roof
planes automatically (that is a deliberately postponed future milestone).

Points carry their ASPRS classification when the source file has one,
which is what lets the height estimates separate a roof from the tree
overhanging it, and what feeds the ground-only terrain model in
``model/terrain.py``. Files without usable classification still work --
every consumer falls back to the older percentile-over-everything
behaviour and says so.
"""
from __future__ import annotations

import glob
import os
from dataclasses import dataclass
from typing import Callable, Iterable, List, Optional, Sequence

import numpy as np
from shapely.geometry import Polygon

try:
    import laspy
except ImportError:  # pragma: no cover - exercised only when laspy is missing
    laspy = None

#: ASPRS standard classification codes (AHN follows these).
CLASS_NEVER_CLASSIFIED = 0
CLASS_UNASSIGNED = 1
CLASS_GROUND = 2
CLASS_BUILDING = 6
CLASS_WATER = 9

VEGETATION_CLASSES = (3, 4, 5)  # low / medium / high vegetation
NOISE_CLASSES = (7, 18)  # low point (noise) / high noise

#: Classes that mean "nothing was decided about this point".
UNCLASSIFIED_CLASSES = (CLASS_NEVER_CLASSIFIED, CLASS_UNASSIGNED)


@dataclass(eq=False, repr=False)
class LidarPointCloud:
    """A flat, in-memory point cloud (x, y, z in the source CRS, typically
    EPSG:28992 + NAP for AHN6), with optional per-point ASPRS class.

    ``eq``/``repr`` are off deliberately: the generated ``__eq__`` would
    compare numpy arrays (ambiguous-truth-value errors, or a quietly wrong
    ``False`` once a field can be ``None``), and the generated ``__repr__``
    would dump every coordinate of a multi-million-point cloud into any
    traceback or log line that happens to mention one.
    """

    x: np.ndarray
    y: np.ndarray
    z: np.ndarray
    classification: Optional[np.ndarray] = None

    def __len__(self) -> int:
        return len(self.x)

    @property
    def has_classification(self) -> bool:
        """True when the cloud carries classes that actually say something --
        an all-unassigned cloud is treated as unclassified."""
        if self.classification is None or len(self) == 0:
            return False
        return bool(np.isin(self.classification, UNCLASSIFIED_CLASSES, invert=True).any())

    @property
    def classes(self) -> np.ndarray:
        """Per-point classification, falling back to all-zeros ("never
        classified") so callers can index without a None check."""
        if self.classification is None:
            return np.zeros(len(self), dtype=np.uint8)
        return self.classification

    def _subset(self, mask: np.ndarray) -> "LidarPointCloud":
        """Derive a cloud from a boolean mask, carrying classification."""
        return LidarPointCloud(
            self.x[mask],
            self.y[mask],
            self.z[mask],
            None if self.classification is None else self.classification[mask],
        )

    @classmethod
    def empty(cls) -> "LidarPointCloud":
        return cls(np.empty(0), np.empty(0), np.empty(0), np.empty(0, dtype=np.uint8))

    @classmethod
    def from_files(
        cls,
        paths: Iterable[str],
        bbox: Optional[tuple] = None,
        on_progress: Optional[Callable[[int, int], None]] = None,
        warnings: Optional[List[str]] = None,
    ) -> "LidarPointCloud":
        """Load one or more LAS/LAZ files, optionally cropped to a bbox
        (minx, miny, maxx, maxy) while reading to keep memory bounded.

        ``on_progress(points_read, total_points)`` is called once per chunk
        so a caller can drive a progress bar through the decode, which is
        the slowest part of loading an area.
        """
        if laspy is None:
            raise ImportError(
                "laspy is required to load point clouds (pip install 'laspy[lazrs]')"
            )
        xs, ys, zs, cs = [], [], [], []
        for path in paths:
            with laspy.open(path) as reader:
                if bbox is not None and _header_misses_bbox(reader.header, bbox):
                    # Skipping here avoids decompressing a whole tile that
                    # cannot contribute a single point to this area.
                    continue
                total = int(getattr(reader.header, "point_count", 0) or 0)
                read = 0
                for chunk in reader.chunk_iterator(2_000_000):
                    x = np.asarray(chunk.x)
                    y = np.asarray(chunk.y)
                    z = np.asarray(chunk.z)
                    # np.asarray matters: for point formats 0-5 laspy hands
                    # back a SubFieldView, which does not mask like an ndarray.
                    c = np.asarray(chunk.classification, dtype=np.uint8)
                    read += len(x)
                    if bbox is not None:
                        minx, miny, maxx, maxy = bbox
                        mask = (x >= minx) & (x <= maxx) & (y >= miny) & (y <= maxy)
                        # All four must be masked together -- masking the
                        # coordinates separately from the classes would
                        # silently misalign them rather than raise.
                        x, y, z, c = x[mask], y[mask], z[mask], c[mask]
                    xs.append(x)
                    ys.append(y)
                    zs.append(z)
                    cs.append(c)
                    if on_progress is not None:
                        on_progress(read, total)
        if not xs:
            return cls.empty()
        cloud = cls(
            np.concatenate(xs),
            np.concatenate(ys),
            np.concatenate(zs),
            np.concatenate(cs),
        )
        if warnings is not None and len(cloud) > 0 and not cloud.has_classification:
            warnings.append(
                "LiDAR points carry no usable classification (all unassigned); "
                "height estimates fall back to percentiles and no terrain is built."
            )
        return cloud

    def crop_to_bbox(self, minx: float, miny: float, maxx: float, maxy: float) -> "LidarPointCloud":
        mask = (self.x >= minx) & (self.x <= maxx) & (self.y >= miny) & (self.y <= maxy)
        return self._subset(mask)

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
        return self._subset(self.mask_in_polygon(polygon))

    def filter_classes(
        self,
        include: Optional[Sequence[int]] = None,
        exclude: Optional[Sequence[int]] = None,
    ) -> "LidarPointCloud":
        """Keep only ``include`` classes and/or drop ``exclude`` ones.

        An unclassified cloud is returned untouched -- filtering it would
        silently throw everything away.
        """
        if len(self) == 0 or self.classification is None:
            return self
        mask = np.ones(len(self), dtype=bool)
        if include is not None:
            mask &= np.isin(self.classification, list(include))
        if exclude is not None:
            mask &= np.isin(self.classification, list(exclude), invert=True)
        return self._subset(mask)

    def height_near(
        self,
        x: float,
        y: float,
        radius: float = 1.0,
        classes: Optional[Sequence[int]] = None,
    ) -> Optional[dict]:
        """The representative height right at (x, y), for snapping a single
        mesh vertex to the point cloud -- the median Z of points within
        ``radius`` meters, or ``None`` if none are found.

        With ``classes``, points of those classes are preferred (so a roof
        vertex snaps to the roof rather than the tree overhanging it), but
        if none are in range the search falls back to every nearby point
        rather than refusing to snap at all.
        """
        if len(self) == 0:
            return None
        dist2 = (self.x - x) ** 2 + (self.y - y) ** 2
        in_range = dist2 <= radius * radius
        if not in_range.any():
            return None

        mask = in_range
        matched_class = False
        if classes is not None and self.classification is not None:
            preferred = in_range & np.isin(self.classification, list(classes))
            if preferred.any():
                mask = preferred
                matched_class = True
        return {
            "z": float(np.median(self.z[mask])),
            "point_count": int(mask.sum()),
            "matched_class": matched_class,
        }


def _header_misses_bbox(header, bbox: tuple) -> bool:
    """True when a tile's own header bounds prove it cannot overlap the bbox.

    Guarded on the header bounds being usable: an unset header reports all
    zeros, and a tile with a bogus bbox must be read rather than dropped.
    """
    mins = getattr(header, "mins", None)
    maxs = getattr(header, "maxs", None)
    if mins is None or maxs is None or len(mins) < 2 or len(maxs) < 2:
        return False
    if not (maxs[0] > mins[0] and maxs[1] > mins[1]):
        return False
    minx, miny, maxx, maxy = bbox
    return bool(maxs[0] < minx or mins[0] > maxx or maxs[1] < miny or mins[1] > maxy)


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
    ground_height: Optional[float] = None,
) -> Optional[dict]:
    """Rough ground/eave/ridge height suggestions for one footprint.

    Returns ``None`` if there are no LiDAR points over the footprint.
    Heights are meant as a *starting point* the user adjusts in the UI,
    not a reconstruction result.

    When the cloud is classified, the roof height comes from building-class
    points only, so vegetation overhanging the footprint no longer inflates
    it. ``ground_height`` should be supplied from the terrain model
    (``model/terrain.py``): a building occludes the ground beneath it, so
    there are usually no ground returns *inside* a footprint at all and
    points inside the outline are the wrong source for it.
    """
    inside = cloud.points_in_polygon(polygon)
    if len(inside) == 0:
        return None

    roof_points = inside
    classified = inside.has_classification
    if classified:
        building = inside.filter_classes(include=[CLASS_BUILDING])
        if len(building) > 0:
            roof_points = building
        else:
            # Classified, but nothing here was called a building (a shed
            # below the classifier's threshold, a demolished pand). Exclude
            # the classes we know are not roof rather than trusting all.
            roof_points = inside.filter_classes(
                exclude=list(VEGETATION_CLASSES) + list(NOISE_CLASSES)
            )
            if len(roof_points) == 0:
                roof_points = inside

    top = float(np.percentile(roof_points.z, top_percentile))
    if ground_height is None:
        ground_height = float(np.percentile(inside.z, ground_percentile))
    ground_height = float(ground_height)
    if top < ground_height:
        # Degenerate input (e.g. a footprint over water): keep the box valid.
        top = ground_height

    return {
        "point_count": len(inside),
        "ground_height": ground_height,
        "top_height": top,
        "eave_estimate": ground_height + 0.7 * (top - ground_height),
        "ridge_estimate": top,
        "classified": classified,
        "roof_point_count": len(roof_points),
    }
