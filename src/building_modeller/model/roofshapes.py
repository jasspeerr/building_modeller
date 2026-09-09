"""Parametric LOD2 roof-shape generators.

Each function turns a 2D building footprint plus a few height parameters
into an explicit set of wall / roof / ground surface polygons, already
split apart the way CityGML LOD2.2 wants them
(``bldg:WallSurface`` / ``bldg:RoofSurface`` / ``bldg:GroundSurface``).

There is no automatic fitting to LiDAR here -- the caller (the UI, seeded
from ``data.pointcloud`` height statistics) picks the roof type and the
eave/ridge heights; this module is pure geometry.

Known MVP simplifications:

* Footprint holes (courtyards) are not supported -- only the exterior
  ring is used.
* ``gable`` and ``hip`` roofs are built on the footprint's minimum
  rotated bounding rectangle rather than its exact outline, since a
  general straight-skeleton roof for arbitrary (e.g. L-shaped) polygons
  is out of scope for now. ``flat``, ``shed`` and ``pyramid`` are exact
  for any simple polygon.
"""
from __future__ import annotations

import enum
import math
from dataclasses import dataclass
from typing import List, Optional, Tuple

import numpy as np
from shapely.geometry import Polygon
from shapely.geometry.polygon import orient

Point3 = Tuple[float, float, float]
Ring3 = List[Point3]

_EPS = 1e-9


class RoofType(str, enum.Enum):
    FLAT = "flat"
    SHED = "shed"
    GABLE = "gable"
    HIP = "hip"
    PYRAMID = "pyramid"


@dataclass
class RoofParams:
    """Parameters for one building's roof.

    ``eave_height`` and ``ridge_height`` are absolute Z values (same CRS
    vertical datum as the footprint/point cloud, e.g. NAP for AHN6).

    * ``flat``: only ``eave_height`` is used (the flat roof's height).
    * ``shed``: ``eave_height`` is the low edge, ``ridge_height`` the
      high edge.
    * ``gable`` / ``hip`` / ``pyramid``: ``eave_height`` is the wall top
      / roof low edge, ``ridge_height`` the ridge/apex height.
    * ``ridge_along``: for ``gable``/``hip`` only, ``"long"`` (default)
      or ``"short"`` -- which axis of the bounding rectangle the ridge
      runs along.
    """

    roof_type: RoofType
    eave_height: float
    ridge_height: Optional[float] = None
    ridge_along: str = "long"


@dataclass
class BuildingMesh:
    """Explicit, CityGML-ready surface decomposition of one building."""

    ground: List[Ring3]
    walls: List[Ring3]
    roof: List[Ring3]


def _dedupe_ring(ring: List[Point3]) -> List[Point3]:
    out: List[Point3] = []
    for p in ring:
        if not out or math.dist(out[-1], p) > _EPS:
            out.append(p)
    if len(out) > 1 and math.dist(out[0], out[-1]) <= _EPS:
        out.pop()
    return out


def _exterior_xy(polygon: Polygon, ccw: bool) -> List[Tuple[float, float]]:
    oriented = orient(polygon, sign=1.0 if ccw else -1.0)
    return list(oriented.exterior.coords)[:-1]


def _walls_from_ring(
    ring_xy: List[Tuple[float, float]], z_bottom: float, z_top
) -> List[Ring3]:
    """One quad per edge of ``ring_xy`` (CCW as seen from above).

    ``z_top`` is either a constant float or a callable ``(x, y) -> z``.
    """
    top = z_top if callable(z_top) else (lambda x, y: z_top)
    walls = []
    n = len(ring_xy)
    for i in range(n):
        x0, y0 = ring_xy[i]
        x1, y1 = ring_xy[(i + 1) % n]
        quad = _dedupe_ring(
            [
                (x0, y0, z_bottom),
                (x1, y1, z_bottom),
                (x1, y1, top(x1, y1)),
                (x0, y0, top(x0, y0)),
            ]
        )
        if len(quad) >= 3:
            walls.append(quad)
    return walls


def _ground_from_ring(ring_xy_ccw: List[Tuple[float, float]], z: float) -> Ring3:
    # Ground faces down, so wind it opposite (CW) to the CCW roof/exterior ring.
    return [(x, y, z) for x, y in reversed(ring_xy_ccw)]


def _rectangle_axes(polygon: Polygon):
    """Return (center, long_dir, short_dir, long_len, short_len) of the
    footprint's minimum rotated bounding rectangle."""
    rect = polygon.minimum_rotated_rectangle
    coords = np.array(list(rect.exterior.coords)[:-1])
    if len(coords) < 4:
        # Degenerate (near-collinear) footprint: fall back to the axis-aligned bbox.
        minx, miny, maxx, maxy = polygon.bounds
        coords = np.array([[minx, miny], [maxx, miny], [maxx, maxy], [minx, maxy]])
    edge_lengths = [np.linalg.norm(coords[(i + 1) % 4] - coords[i]) for i in range(4)]
    li = int(np.argmax(edge_lengths))
    long_vec = coords[(li + 1) % 4] - coords[li]
    long_len = float(np.linalg.norm(long_vec))
    long_dir = long_vec / max(long_len, _EPS)
    short_dir = np.array([-long_dir[1], long_dir[0]])
    short_len = float(edge_lengths[(li + 1) % 4])
    center = coords.mean(axis=0)
    return center, long_dir, short_dir, long_len, short_len


def flat_mesh(polygon: Polygon, ground_height: float, roof_height: float) -> BuildingMesh:
    ext_up = _exterior_xy(polygon, ccw=True)
    walls = _walls_from_ring(ext_up, ground_height, roof_height)
    roof = [_dedupe_ring([(x, y, roof_height) for x, y in ext_up])]
    ground = [_dedupe_ring(_ground_from_ring(ext_up, ground_height))]
    return BuildingMesh(ground=ground, walls=walls, roof=roof)


def shed_mesh(
    polygon: Polygon, ground_height: float, low_height: float, high_height: float
) -> BuildingMesh:
    center, _long_dir, short_dir, _long_len, short_len = _rectangle_axes(polygon)
    short_len = max(short_len, _EPS)
    slope = (high_height - low_height) / short_len

    def z_top(x: float, y: float) -> float:
        v = np.dot(np.array([x, y]) - center, short_dir)
        return low_height + slope * (v + short_len / 2.0)

    ext_up = _exterior_xy(polygon, ccw=True)
    walls = _walls_from_ring(ext_up, ground_height, z_top)
    roof = [_dedupe_ring([(x, y, z_top(x, y)) for x, y in ext_up])]
    ground = [_dedupe_ring(_ground_from_ring(ext_up, ground_height))]
    return BuildingMesh(ground=ground, walls=walls, roof=roof)


def _uv(point, center, long_dir, short_dir):
    d = np.array(point) - center
    return float(np.dot(d, long_dir)), float(np.dot(d, short_dir))


def _xy(u, v, center, long_dir, short_dir):
    p = center + u * long_dir + v * short_dir
    return float(p[0]), float(p[1])


def gable_mesh(
    polygon: Polygon,
    ground_height: float,
    eave_height: float,
    ridge_height: float,
    ridge_along: str = "long",
) -> BuildingMesh:
    center, long_dir, short_dir, L, W = _rectangle_axes(polygon)
    if ridge_along == "short":
        long_dir, short_dir = short_dir, long_dir
        L, W = W, L
    half_l, half_w = L / 2.0, W / 2.0

    def xy(u, v):
        return _xy(u, v, center, long_dir, short_dir)

    c_nn = xy(-half_l, -half_w)  # near-near
    c_pn = xy(half_l, -half_w)
    c_pp = xy(half_l, half_w)
    c_np = xy(-half_l, half_w)
    ridge0 = xy(-half_l, 0.0)
    ridge1 = xy(half_l, 0.0)

    walls = [
        # side walls (parallel to ridge) -- simple rectangles up to eave
        _dedupe_ring(
            [
                (*c_nn, ground_height),
                (*c_pn, ground_height),
                (*c_pn, eave_height),
                (*c_nn, eave_height),
            ]
        ),
        _dedupe_ring(
            [
                (*c_pp, ground_height),
                (*c_np, ground_height),
                (*c_np, eave_height),
                (*c_pp, eave_height),
            ]
        ),
        # gable-end walls (pentagons up to the ridge apex)
        _dedupe_ring(
            [
                (*c_nn, ground_height),
                (*c_nn, eave_height),
                (*ridge0, ridge_height),
                (*c_np, eave_height),
                (*c_np, ground_height),
            ]
        ),
        _dedupe_ring(
            [
                (*c_pn, ground_height),
                (*c_pp, ground_height),
                (*c_pp, eave_height),
                (*ridge1, ridge_height),
                (*c_pn, eave_height),
            ]
        ),
    ]
    walls = [w for w in walls if len(w) >= 3]

    roof = [
        _dedupe_ring(
            [
                (*c_nn, eave_height),
                (*c_pn, eave_height),
                (*ridge1, ridge_height),
                (*ridge0, ridge_height),
            ]
        ),
        _dedupe_ring(
            [
                (*c_pp, eave_height),
                (*c_np, eave_height),
                (*ridge0, ridge_height),
                (*ridge1, ridge_height),
            ]
        ),
    ]
    roof = [r for r in roof if len(r) >= 3]

    ground = [_dedupe_ring([(*c_np, ground_height), (*c_pp, ground_height),
                             (*c_pn, ground_height), (*c_nn, ground_height)])]

    return BuildingMesh(ground=ground, walls=walls, roof=roof)


def hip_mesh(
    polygon: Polygon,
    ground_height: float,
    eave_height: float,
    ridge_height: float,
    ridge_along: str = "long",
) -> BuildingMesh:
    center, long_dir, short_dir, L, W = _rectangle_axes(polygon)
    if ridge_along == "short":
        long_dir, short_dir = short_dir, long_dir
        L, W = W, L
    if L < W:
        L, W = W, L
        long_dir, short_dir = short_dir, long_dir
    half_l, half_w = L / 2.0, W / 2.0
    ridge_half = max(0.0, (L - W) / 2.0)

    def xy(u, v):
        return _xy(u, v, center, long_dir, short_dir)

    c_nn = xy(-half_l, -half_w)
    c_pn = xy(half_l, -half_w)
    c_pp = xy(half_l, half_w)
    c_np = xy(-half_l, half_w)
    r0 = xy(-ridge_half, 0.0)
    r1 = xy(ridge_half, 0.0)

    walls = [
        _dedupe_ring([(*c_nn, ground_height), (*c_pn, ground_height),
                      (*c_pn, eave_height), (*c_nn, eave_height)]),
        _dedupe_ring([(*c_pn, ground_height), (*c_pp, ground_height),
                      (*c_pp, eave_height), (*c_pn, eave_height)]),
        _dedupe_ring([(*c_pp, ground_height), (*c_np, ground_height),
                      (*c_np, eave_height), (*c_pp, eave_height)]),
        _dedupe_ring([(*c_np, ground_height), (*c_nn, ground_height),
                      (*c_nn, eave_height), (*c_np, eave_height)]),
    ]
    walls = [w for w in walls if len(w) >= 3]

    roof = [
        _dedupe_ring([(*c_nn, eave_height), (*c_pn, eave_height), (*r1, ridge_height), (*r0, ridge_height)]),
        _dedupe_ring([(*c_pp, eave_height), (*c_np, eave_height), (*r0, ridge_height), (*r1, ridge_height)]),
        _dedupe_ring([(*c_nn, eave_height), (*r0, ridge_height), (*c_np, eave_height)]),
        _dedupe_ring([(*c_pp, eave_height), (*r1, ridge_height), (*c_pn, eave_height)]),
    ]
    roof = [r for r in roof if len(r) >= 3]

    ground = [_dedupe_ring([(*c_np, ground_height), (*c_pp, ground_height),
                             (*c_pn, ground_height), (*c_nn, ground_height)])]

    return BuildingMesh(ground=ground, walls=walls, roof=roof)


def pyramid_mesh(
    polygon: Polygon, ground_height: float, eave_height: float, ridge_height: float
) -> BuildingMesh:
    ext_up = _exterior_xy(polygon, ccw=True)
    cx, cy = polygon.centroid.x, polygon.centroid.y
    apex = (cx, cy, ridge_height)

    walls = _walls_from_ring(ext_up, ground_height, eave_height)
    ground = [_dedupe_ring(_ground_from_ring(ext_up, ground_height))]

    roof = []
    n = len(ext_up)
    for i in range(n):
        x0, y0 = ext_up[i]
        x1, y1 = ext_up[(i + 1) % n]
        tri = _dedupe_ring([(x0, y0, eave_height), (x1, y1, eave_height), apex])
        if len(tri) >= 3:
            roof.append(tri)

    return BuildingMesh(ground=ground, walls=walls, roof=roof)


def generate_mesh(footprint: Polygon, ground_height: float, roof: RoofParams) -> BuildingMesh:
    """Dispatch to the roof-specific mesh generator."""
    if roof.roof_type == RoofType.FLAT:
        return flat_mesh(footprint, ground_height, roof.eave_height)
    if roof.roof_type == RoofType.SHED:
        if roof.ridge_height is None:
            raise ValueError("shed roof requires ridge_height (the high edge)")
        return shed_mesh(footprint, ground_height, roof.eave_height, roof.ridge_height)
    if roof.roof_type == RoofType.GABLE:
        if roof.ridge_height is None:
            raise ValueError("gable roof requires ridge_height")
        return gable_mesh(footprint, ground_height, roof.eave_height, roof.ridge_height, roof.ridge_along)
    if roof.roof_type == RoofType.HIP:
        if roof.ridge_height is None:
            raise ValueError("hip roof requires ridge_height")
        return hip_mesh(footprint, ground_height, roof.eave_height, roof.ridge_height, roof.ridge_along)
    if roof.roof_type == RoofType.PYRAMID:
        if roof.ridge_height is None:
            raise ValueError("pyramid roof requires ridge_height")
        return pyramid_mesh(footprint, ground_height, roof.eave_height, roof.ridge_height)
    raise ValueError(f"unknown roof type: {roof.roof_type}")
