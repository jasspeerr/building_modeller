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

import numpy as np
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

    def _face_for_diagonal(self, index_a: int, index_b: int) -> Face:
        """The single face that has both vertices as *non-adjacent*
        members (a diagonal, not an edge) -- this is how ``split_face``
        and ``extrude_face`` identify "the face" from just two vertex
        indices, reusing the same selection the UI already has for
        ``split_edge``. Non-adjacency matters: an actual edge is normally
        shared by two faces, which would make "the face" ambiguous; a
        diagonal only ever belongs to at most one."""
        candidates = []
        for face in self.faces:
            idx = face.vertex_indices
            if index_a not in idx or index_b not in idx:
                continue
            n = len(idx)
            adjacent = any(
                {idx[i], idx[(i + 1) % n]} == {index_a, index_b} for i in range(n)
            )
            if not adjacent:
                candidates.append(face)
        if not candidates:
            raise ValueError(
                f"vertices {index_a} and {index_b} are not a non-adjacent diagonal of any single face"
            )
        if len(candidates) > 1:
            raise ValueError(f"vertices {index_a} and {index_b} are ambiguous -- they span multiple faces")
        return candidates[0]

    def split_face(self, index_a: int, index_b: int) -> Tuple[Face, Face]:
        """Split the face that has ``index_a``/``index_b`` as a diagonal
        into two faces along that diagonal (each keeping the original's
        surface_type). E.g. split a roof face's two newly-added edge
        midpoints to turn one flat plane into two separate pitches."""
        face = self._face_for_diagonal(index_a, index_b)
        idx = face.vertex_indices
        pos_a, pos_b = idx.index(index_a), idx.index(index_b)

        if pos_a < pos_b:
            ring1 = idx[pos_a : pos_b + 1]
            ring2 = idx[pos_b:] + idx[: pos_a + 1]
        else:
            ring1 = idx[pos_a:] + idx[: pos_b + 1]
            ring2 = idx[pos_b : pos_a + 1]

        new_faces = (
            Face(vertex_indices=ring1, surface_type=face.surface_type),
            Face(vertex_indices=ring2, surface_type=face.surface_type),
        )
        face_pos = self.faces.index(face)
        self.faces[face_pos : face_pos + 1] = list(new_faces)
        return new_faces

    def extrude_face(self, index_a: int, index_b: int, distance: float) -> Face:
        """Push the face identified by the ``index_a``/``index_b``
        diagonal outward along its own normal by ``distance``, creating a
        protrusion (e.g. a dormer or bay): the face's vertices are
        duplicated and offset, new wall-type "skirt" faces connect the
        original (unmoved, still shared with the surrounding mesh)
        boundary to the offset copy, and a new cap face -- same
        surface_type as the original -- sits at the extruded position.
        Returns the new cap face."""
        face = self._face_for_diagonal(index_a, index_b)
        idx = face.vertex_indices
        if len(idx) < 3:
            raise ValueError("face has fewer than 3 vertices")

        p0 = np.array(self.vertices[idx[0]])
        p1 = np.array(self.vertices[idx[1]])
        p2 = np.array(self.vertices[idx[2]])
        normal = np.cross(p1 - p0, p2 - p0)
        norm_len = np.linalg.norm(normal)
        if norm_len < 1e-9:
            raise ValueError("face is degenerate (near-collinear points); can't compute a normal")
        normal = normal / norm_len
        offset = normal * distance

        new_indices = []
        for i in idx:
            new_pos = tuple((np.array(self.vertices[i]) + offset).tolist())
            new_indices.append(len(self.vertices))
            self.vertices.append(new_pos)

        n = len(idx)
        skirt = [
            Face(
                vertex_indices=[idx[i], idx[(i + 1) % n], new_indices[(i + 1) % n], new_indices[i]],
                surface_type="wall",
            )
            for i in range(n)
        ]
        cap = Face(vertex_indices=new_indices, surface_type=face.surface_type)

        face_pos = self.faces.index(face)
        self.faces[face_pos : face_pos + 1] = skirt + [cap]
        return cap


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
