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


class TestSplitEdge:
    def test_splits_interior_eave_edge_shared_by_two_faces(self):
        mesh = seed_flat_box(box(0, 0, 10, 6), ground_height=0.0, roof_height=3.0)
        roof_face = mesh.faces_by_type("roof")[0]
        # First two roof vertices are the eave edge shared with a wall face.
        a, b = roof_face.vertex_indices[0], roof_face.vertex_indices[1]
        sharing = mesh.edge_faces(a, b)
        assert len(sharing) == 2

        n_before = len(mesh.vertices)
        new_index = mesh.split_edge(a, b)

        assert new_index == n_before
        assert len(mesh.vertices) == n_before + 1
        expected_midpoint = tuple((x + y) / 2.0 for x, y in zip(mesh.vertices[a], mesh.vertices[b]))
        assert mesh.vertices[new_index] == pytest.approx(expected_midpoint)
        for face in sharing:
            assert new_index in face.vertex_indices

    def test_new_vertex_sits_between_the_two_endpoints_in_the_ring(self):
        mesh = seed_flat_box(box(0, 0, 10, 6), ground_height=0.0, roof_height=3.0)
        roof_face = mesh.faces_by_type("roof")[0]
        a, b = roof_face.vertex_indices[0], roof_face.vertex_indices[1]
        new_index = mesh.split_edge(a, b)
        idx = roof_face.vertex_indices
        pos_a, pos_new = idx.index(a), idx.index(new_index)
        assert pos_new == (pos_a + 1) % len(idx)

    def test_raises_for_non_adjacent_vertices(self):
        mesh = seed_flat_box(box(0, 0, 10, 6), ground_height=0.0, roof_height=3.0)
        # Diagonal roof corners are never adjacent in the same face.
        roof_face = mesh.faces_by_type("roof")[0]
        a, c = roof_face.vertex_indices[0], roof_face.vertex_indices[2]
        with pytest.raises(ValueError):
            mesh.split_edge(a, c)

    def test_edge_faces_for_disconnected_vertices_is_empty(self):
        mesh = seed_flat_box(box(0, 0, 10, 6), ground_height=0.0, roof_height=3.0)
        assert mesh.edge_faces(0, 999) == []


class TestDeleteVertex:
    def test_removes_vertex_from_every_referencing_face(self):
        mesh = seed_flat_box(box(0, 0, 10, 6), ground_height=0.0, roof_height=3.0)
        ground_face = mesh.faces_by_type("ground")[0]
        idx = ground_face.vertex_indices[0]
        faces_before = [f for f in mesh.faces if idx in f.vertex_indices]
        assert len(faces_before) >= 2

        mesh.delete_vertex(idx)

        assert len(mesh.vertices) == 7
        assert all(len(f.vertex_indices) >= 3 for f in mesh.faces)
        assert all(0 <= i < len(mesh.vertices) for f in mesh.faces for i in f.vertex_indices)

    def test_reindexes_higher_vertex_indices_down_by_one(self):
        mesh = seed_flat_box(box(0, 0, 10, 6), ground_height=0.0, roof_height=3.0)
        removed = 3
        # Snapshot which faces referenced vertex removed+1 before deletion.
        had_four = {id(f) for f in mesh.faces if (removed + 1) in f.vertex_indices}

        mesh.delete_vertex(removed)

        for face in mesh.faces:
            assert max(face.vertex_indices) < len(mesh.vertices)
        # Every face that used to reference `removed + 1` must now reference
        # `removed` instead (shifted down by one), assuming it survived.
        for face in mesh.faces:
            if id(face) in had_four:
                assert removed in face.vertex_indices

    def test_drops_faces_that_collapse_below_three_vertices(self):
        # A standalone triangle plus one unrelated vertex.
        mesh = EditableMesh(
            vertices=[(0, 0, 0), (1, 0, 0), (0, 1, 0), (5, 5, 5)],
            faces=[Face(vertex_indices=[0, 1, 2], surface_type="roof")],
        )
        mesh.delete_vertex(0)
        assert mesh.faces == []
        assert len(mesh.vertices) == 3

    def test_out_of_range_index_raises(self):
        mesh = seed_flat_box(box(0, 0, 10, 6), ground_height=0.0, roof_height=3.0)
        with pytest.raises(IndexError):
            mesh.delete_vertex(999)


class TestSplitFace:
    def test_splits_roof_into_two_pitches_via_two_edge_splits(self):
        mesh = seed_flat_box(box(0, 0, 10, 6), ground_height=0.0, roof_height=3.0)
        roof = list(mesh.faces_by_type("roof")[0].vertex_indices)  # [4,5,6,7]

        a = mesh.split_edge(roof[0], roof[1])  # midpoint of one long eave edge
        b = mesh.split_edge(roof[3], roof[2])  # midpoint of the opposite long eave edge

        f1, f2 = mesh.split_face(a, b)
        assert f1.surface_type == "roof"
        assert f2.surface_type == "roof"
        assert a in f1.vertex_indices and b in f1.vertex_indices
        assert a in f2.vertex_indices and b in f2.vertex_indices
        assert len(mesh.faces_by_type("roof")) == 2
        # Original single roof face is gone, replaced by the two new ones.
        assert roof not in [f.vertex_indices for f in mesh.faces]

    def test_new_faces_together_cover_the_same_vertices_as_the_original(self):
        mesh = seed_flat_box(box(0, 0, 10, 6), ground_height=0.0, roof_height=3.0)
        roof = list(mesh.faces_by_type("roof")[0].vertex_indices)
        a = mesh.split_edge(roof[0], roof[1])
        b = mesh.split_edge(roof[3], roof[2])
        f1, f2 = mesh.split_face(a, b)
        combined = set(f1.vertex_indices) | set(f2.vertex_indices)
        assert combined == set(roof) | {a, b}

    def test_raises_for_adjacent_vertices(self):
        mesh = seed_flat_box(box(0, 0, 10, 6), ground_height=0.0, roof_height=3.0)
        roof = mesh.faces_by_type("roof")[0].vertex_indices
        with pytest.raises(ValueError):
            mesh.split_face(roof[0], roof[1])  # adjacent -- not a diagonal

    def test_raises_when_no_face_has_both_vertices(self):
        mesh = seed_flat_box(box(0, 0, 10, 6), ground_height=0.0, roof_height=3.0)
        with pytest.raises(ValueError):
            mesh.split_face(0, 999)

    def test_raises_when_vertices_never_share_a_face(self):
        mesh = seed_flat_box(box(0, 0, 10, 6), ground_height=0.0, roof_height=3.0)
        # Vertex 0 (a ground corner) and vertex 6 (the diagonally opposite
        # roof corner) never appear together in any single face at all.
        with pytest.raises(ValueError):
            mesh.split_face(0, 6)


class TestExtrudeFace:
    def test_extrudes_along_normal_creating_skirt_and_cap(self):
        mesh = seed_flat_box(box(0, 0, 10, 6), ground_height=0.0, roof_height=3.0)
        roof = mesh.faces_by_type("roof")[0]
        n_faces_before = len(mesh.faces)

        cap = mesh.extrude_face(roof.vertex_indices[0], roof.vertex_indices[2], distance=1.5)

        assert cap.surface_type == "roof"
        assert all(v[2] == pytest.approx(4.5) for v in [mesh.vertices[i] for i in cap.vertex_indices])
        # 4 new skirt (wall) faces + 1 cap replacing the original roof face.
        assert len(mesh.faces) == n_faces_before + 4
        assert len(mesh.faces_by_type("wall")) == 8  # original 4 + 4 new skirt walls

    def test_original_boundary_vertices_are_unmoved(self):
        mesh = seed_flat_box(box(0, 0, 10, 6), ground_height=0.0, roof_height=3.0)
        roof = mesh.faces_by_type("roof")[0]
        original_positions = [mesh.vertices[i] for i in roof.vertex_indices]
        mesh.extrude_face(roof.vertex_indices[0], roof.vertex_indices[2], distance=1.5)
        # The *original* vertex indices (now only part of the skirt walls)
        # must still be exactly where they started.
        for i, pos in zip(roof.vertex_indices, original_positions):
            assert mesh.vertices[i] == pos

    def test_extrude_only_a_split_off_sub_face_leaves_rest_of_roof_alone(self):
        mesh = seed_flat_box(box(0, 0, 10, 6), ground_height=0.0, roof_height=3.0)
        roof = list(mesh.faces_by_type("roof")[0].vertex_indices)
        a = mesh.split_edge(roof[0], roof[1])
        b = mesh.split_edge(roof[3], roof[0])
        big, small = mesh.split_face(a, b)
        if len(big.vertex_indices) < len(small.vertex_indices):
            big, small = small, big

        cap = mesh.extrude_face(big.vertex_indices[0], big.vertex_indices[2], distance=1.0)

        assert cap.surface_type == "roof"
        # The untouched small triangular face must stay at the original height.
        assert all(mesh.vertices[i][2] == pytest.approx(3.0) for i in small.vertex_indices)

    def test_raises_for_adjacent_vertices(self):
        mesh = seed_flat_box(box(0, 0, 10, 6), ground_height=0.0, roof_height=3.0)
        roof = mesh.faces_by_type("roof")[0].vertex_indices
        with pytest.raises(ValueError):
            mesh.extrude_face(roof[0], roof[1], distance=1.0)


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
