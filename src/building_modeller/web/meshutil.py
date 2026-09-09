"""Turn an EditableMesh into rendering data for the browser's Three.js
viewer: a flat vertex array plus a triangle index buffer, offset into
scene-local coordinates.

Because EditableMesh already has a single shared vertex pool (faces
reference vertices by index, see model/mesh.py), this is just a
coordinate offset plus a fan-triangulation of each face -- no vertex
deduplication needed, and critically, the vertex indices returned here are
the *same* indices ``Building.move_vertex`` expects, so the frontend can
select/drag "vertex 7" and mean the same thing on both ends.

RD New coordinates are in the hundreds of thousands; WebGL vertex buffers
are float32, which only carries ~7 significant decimal digits, so large
absolute coordinates would swamp sub-meter geometry detail. Everything
sent to the browser is therefore expressed relative to a scene origin
(see ``AppState.origin`` in ``server.py``).
"""
from __future__ import annotations

from typing import List, Tuple

from ..model.mesh import EditableMesh


def mesh_to_render_data(mesh: EditableMesh, origin: Tuple[float, float, float]) -> dict:
    """Return a dict with:

    * ``vertices``: flat ``[x0, y0, z0, x1, y1, z1, ...]``, relative to
      ``origin``, indexed exactly as in ``mesh.vertices``.
    * ``triangles``: flat ``[i0, i1, i2, ...]`` triangle index buffer for
      rendering (fan-triangulated per face).
    * ``faces``: ``[{indices, surface_type}, ...]`` -- the original
      (non-triangulated) face list, for editing UI that cares about which
      vertices make up a given face rather than just render triangles.
    """
    ox, oy, oz = origin
    vertices: List[float] = []
    for x, y, z in mesh.vertices:
        vertices.extend((x - ox, y - oy, z - oz))

    triangles: List[int] = []
    for face in mesh.faces:
        idx = face.vertex_indices
        for i in range(1, len(idx) - 1):
            triangles.extend((idx[0], idx[i], idx[i + 1]))

    faces = [{"indices": f.vertex_indices, "surface_type": f.surface_type} for f in mesh.faces]
    return {"vertices": vertices, "triangles": triangles, "faces": faces}
