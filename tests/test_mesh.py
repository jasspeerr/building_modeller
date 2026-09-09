import pytest
from shapely.geometry import Polygon, box

from building_modeller.model.mesh import (
    EditableMesh,
    Face,
    mesh_from_payload,
    mesh_to_payload,
    seed_flat_box,
)


class TestSeedFlatBox:
    def test_rectangle_vertex_and_face_counts(self):
        mesh = seed_flat_box(box(0, 0, 10, 6), ground_height=0.0, roof_height=3.0)
        assert len(mesh.vertices) == 8  # 4 ground + 4 roof
        assert len(mesh.faces_by_type("wall")) == 4
        assert len(mesh.faces_by_type("roof")) == 1
        assert len(mesh.faces_by_type("ground")) == 1

    def test_heights_are_correct(self):
        mesh = seed_flat_box(box(0, 0, 10, 6), ground_height=1.0, roof_height=4.0)
        ground_face = mesh.faces_by_type("ground")[0]
        roof_face = mesh.faces_by_type("roof")[0]
        assert all(mesh.vertices[i][2] == pytest.approx(1.0) for i in ground_face.vertex_indices)
        assert all(mesh.vertices[i][2] == pytest.approx(4.0) for i in roof_face.vertex_indices)

    def test_walls_share_vertices_with_roof_and_ground(self):
        mesh = seed_flat_box(box(0, 0, 10, 6), ground_height=0.0, roof_height=3.0)
        roof_indices = set(mesh.faces_by_type("roof")[0].vertex_indices)
        ground_indices = set(mesh.faces_by_type("ground")[0].vertex_indices)
        wall_indices = set(i for f in mesh.faces_by_type("wall") for i in f.vertex_indices)
        # Every wall vertex is either a roof vertex or a ground vertex -- no
        # separate/duplicated copies.
        assert wall_indices <= (roof_indices | ground_indices)

    def test_works_on_l_shaped_footprint(self):
        l_shape = Polygon([(0, 0), (10, 0), (10, 4), (4, 4), (4, 8), (0, 8)])
        mesh = seed_flat_box(l_shape, ground_height=0.0, roof_height=3.0)
        n_edges = len(list(l_shape.exterior.coords)) - 1
        assert len(mesh.faces_by_type("wall")) == n_edges
        assert len(mesh.vertices) == n_edges * 2


class TestMoveVertex:
    def test_moves_vertex_in_place(self):
        mesh = seed_flat_box(box(0, 0, 10, 6), ground_height=0.0, roof_height=3.0)
        roof_face = mesh.faces_by_type("roof")[0]
        idx = roof_face.vertex_indices[0]
        mesh.move_vertex(idx, (1.0, 2.0, 5.0))
        assert mesh.vertices[idx] == (1.0, 2.0, 5.0)

    def test_moving_shared_vertex_updates_adjacent_faces_together(self):
        mesh = seed_flat_box(box(0, 0, 10, 6), ground_height=0.0, roof_height=3.0)
        roof_face = mesh.faces_by_type("roof")[0]
        idx = roof_face.vertex_indices[0]
        # This vertex is shared by exactly one roof face and (up to) two wall faces.
        sharing_walls = [f for f in mesh.faces_by_type("wall") if idx in f.vertex_indices]
        assert len(sharing_walls) == 2

        mesh.move_vertex(idx, (1.0, 2.0, 9.0))
        for f in [roof_face] + sharing_walls:
            pos = mesh.vertices[f.vertex_indices[f.vertex_indices.index(idx)]]
            assert pos == (1.0, 2.0, 9.0)

    def test_out_of_range_index_raises(self):
        mesh = seed_flat_box(box(0, 0, 10, 6), ground_height=0.0, roof_height=3.0)
        with pytest.raises(IndexError):
            mesh.move_vertex(999, (0.0, 0.0, 0.0))


class TestPayloadRoundTrip:
    def test_round_trips(self):
        mesh = seed_flat_box(box(0, 0, 10, 6), ground_height=0.0, roof_height=3.0)
        restored = mesh_from_payload(mesh_to_payload(mesh))
        assert restored.vertices == mesh.vertices
        assert [(f.vertex_indices, f.surface_type) for f in restored.faces] == [
            (f.vertex_indices, f.surface_type) for f in mesh.faces
        ]

    def test_empty_mesh_round_trips(self):
        restored = mesh_from_payload(mesh_to_payload(EditableMesh()))
        assert restored.vertices == []
        assert restored.faces == []
