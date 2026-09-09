"""An editable, indexed 3D mesh: the actual persistent geometry of a
building, replacing the old "pick a roof type + two heights" model.

Unlike ``roofshapes.BuildingMesh`` (a list of independent per-face vertex
rings, generated fresh from a few parameters every time), an
``EditableMesh`` has a single shared vertex pool that faces reference by
index -- this is what makes editing coherent: moving vertex 7 moves it
everywhere it's used (a roof eave corner and the wall below it can be the
*same* vertex), instead of tearing the mesh apart at the seam.

Every building starts from a simple extruded box (``seed_flat_box``, built
directly on top of the actual footprint) and is then shaped by moving
vertices -- there is no roof-type picker anymore.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Tuple

from shapely.geometry import Polygon
from shapely.geometry.polygon import orient

Point3 = Tuple[float, float, float]

SURFACE_TYPES = ("wall", "roof", "ground")


@dataclass
class Face:
    """A planar polygon face, referencing vertices by index into the
    owning ``EditableMesh.vertices`` pool. ``vertex_indices`` should be
    wound CCW as seen from outside the solid (matching the convention
    ``roofshapes`` already used for wall/roof rings, ground reversed)."""

    vertex_indices: List[int]
    surface_type: str  # one of SURFACE_TYPES


@dataclass
class EditableMesh:
    vertices: List[Point3] = field(default_factory=list)
    faces: List[Face] = field(default_factory=list)

    def faces_by_type(self, surface_type: str) -> List[Face]:
        return [f for f in self.faces if f.surface_type == surface_type]

    def move_vertex(self, index: int, position: Point3) -> None:
        if not (0 <= index < len(self.vertices)):
            raise IndexError(f"vertex index {index} out of range (0..{len(self.vertices) - 1})")
        self.vertices[index] = (float(position[0]), float(position[1]), float(position[2]))

    def edge_faces(self, index_a: int, index_b: int) -> List[Face]:
        """Faces where ``index_a``/``index_b`` are adjacent vertices (in
        either direction) -- i.e. the faces that share this edge."""
        target = {index_a, index_b}
        result = []
        for face in self.faces:
            idx = face.vertex_indices
            n = len(idx)
            for i in range(n):
                if {idx[i], idx[(i + 1) % n]} == target:
                    result.append(face)
                    break
        return result

    def split_edge(self, index_a: int, index_b: int) -> int:
        """Insert a new vertex at the midpoint of the edge between
        ``index_a`` and ``index_b``, in every face that has this edge (an
        interior edge is shared by two faces; both get the new vertex, so
        the mesh stays watertight). Returns the new vertex's index.

        Raises ``ValueError`` if the two vertices are never adjacent in
        any face (there is no such edge to split)."""
        faces = self.edge_faces(index_a, index_b)
        if not faces:
            raise ValueError(f"vertices {index_a} and {index_b} are not adjacent in any face")

        ax, ay, az = self.vertices[index_a]
        bx, by, bz = self.vertices[index_b]
        midpoint = ((ax + bx) / 2.0, (ay + by) / 2.0, (az + bz) / 2.0)
        new_index = len(self.vertices)
        self.vertices.append(midpoint)

        target = {index_a, index_b}
        for face in faces:
            idx = face.vertex_indices
            n = len(idx)
            for i in range(n):
                if {idx[i], idx[(i + 1) % n]} == target:
                    idx.insert(i + 1, new_index)
                    break
        return new_index

    def delete_vertex(self, index: int) -> None:
        """Remove a vertex, dropping it from every face's ring (a face
        that would collapse below 3 vertices is removed entirely), then
        compact the vertex pool so indices stay contiguous."""
        if not (0 <= index < len(self.vertices)):
            raise IndexError(f"vertex index {index} out of range (0..{len(self.vertices) - 1})")

        new_faces = []
        for face in self.faces:
            remaining = [i for i in face.vertex_indices if i != index]
            if len(remaining) >= 3:
                new_faces.append(Face(vertex_indices=remaining, surface_type=face.surface_type))
        self.faces = new_faces

        del self.vertices[index]
        for face in self.faces:
            face.vertex_indices = [i - 1 if i > index else i for i in face.vertex_indices]


def _exterior_ring_ccw(polygon: Polygon) -> List[Tuple[float, float]]:
    oriented = orient(polygon, sign=1.0)
    return list(oriented.exterior.coords)[:-1]


def seed_flat_box(polygon: Polygon, ground_height: float, roof_height: float) -> EditableMesh:
    """The starting geometry for a newly loaded building: a flat-topped
    box following the exact footprint (any simple polygon), ground_height
    to roof_height. This is the only remaining "shape generator" -- from
    here, the building is shaped by moving individual vertices."""
    ring = _exterior_ring_ccw(polygon)
    n = len(ring)

    vertices: List[Point3] = []
    ground_idx = []
    roof_idx = []
    for x, y in ring:
        ground_idx.append(len(vertices))
        vertices.append((x, y, ground_height))
    for x, y in ring:
        roof_idx.append(len(vertices))
        vertices.append((x, y, roof_height))

    faces: List[Face] = []
    for i in range(n):
        j = (i + 1) % n
        faces.append(
            Face(
                vertex_indices=[ground_idx[i], ground_idx[j], roof_idx[j], roof_idx[i]],
                surface_type="wall",
            )
        )
    faces.append(Face(vertex_indices=list(roof_idx), surface_type="roof"))
    faces.append(Face(vertex_indices=list(reversed(ground_idx)), surface_type="ground"))

    return EditableMesh(vertices=vertices, faces=faces)


def mesh_to_payload(mesh: EditableMesh) -> dict:
    return {
        "vertices": [list(v) for v in mesh.vertices],
        "faces": [{"indices": f.vertex_indices, "surface_type": f.surface_type} for f in mesh.faces],
    }


def mesh_from_payload(payload: dict) -> EditableMesh:
    vertices = [tuple(v) for v in payload.get("vertices", [])]
    faces = [
        Face(vertex_indices=list(f["indices"]), surface_type=f["surface_type"])
        for f in payload.get("faces", [])
    ]
    return EditableMesh(vertices=vertices, faces=faces)
