"""Turn a BuildingMesh's wall/roof/ground polygons into a flat
triangle-soup (vertices + face indices) for the browser's Three.js
viewer, offset into scene-local coordinates.

RD New coordinates are in the hundreds of thousands; WebGL vertex buffers
are float32, which only carries ~7 significant decimal digits, so large
absolute coordinates would swamp sub-meter geometry detail. Everything
sent to the browser is therefore expressed relative to a scene origin
(see ``AppState.origin`` in ``server.py``).
"""
from __future__ import annotations

from typing import Iterable, List, Tuple

from ..model.roofshapes import BuildingMesh, Ring3


def _triangulate_fan(ring: Ring3) -> List[Tuple[int, int, int]]:
    """Fan triangulation from the first vertex. Valid for the convex
    wall/roof/ground polygons ``roofshapes`` produces (quads, the convex
    gable-end pentagons, and triangles)."""
    return [(0, i, i + 1) for i in range(1, len(ring) - 1)]


def mesh_to_triangles(mesh: BuildingMesh, origin: Tuple[float, float, float]):
    """Return (vertices, faces) as flat lists ready for JSON: vertices is
    ``[x0, y0, z0, x1, y1, z1, ...]``, faces is ``[i0, i1, i2, ...]``
    (triples of vertex indices)."""
    ox, oy, oz = origin
    vertices: List[float] = []
    faces: List[int] = []
    rings: Iterable[Ring3] = list(mesh.ground) + list(mesh.walls) + list(mesh.roof)
    for ring in rings:
        if len(ring) < 3:
            continue
        base = len(vertices) // 3
        for x, y, z in ring:
            vertices.extend((x - ox, y - oy, z - oz))
        for a, b, c in _triangulate_fan(ring):
            faces.extend((base + a, base + b, base + c))
    return vertices, faces
