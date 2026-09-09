from shapely.geometry import box

from building_modeller.model.mesh import seed_flat_box
from building_modeller.web.meshutil import mesh_to_render_data


class TestMeshToRenderData:
    def test_vertex_count_and_offset(self):
        mesh = seed_flat_box(box(0, 0, 10, 6), ground_height=0.0, roof_height=3.0)
        data = mesh_to_render_data(mesh, origin=(5.0, 3.0, 0.0))
        assert len(data["vertices"]) == len(mesh.vertices) * 3
        # First vertex offset by origin.
        ox, oy, oz = 5.0, 3.0, 0.0
        x, y, z = mesh.vertices[0]
        assert data["vertices"][0:3] == [x - ox, y - oy, z - oz]

    def test_triangle_indices_reference_same_vertex_pool_as_faces(self):
        mesh = seed_flat_box(box(0, 0, 10, 6), ground_height=0.0, roof_height=3.0)
        data = mesh_to_render_data(mesh, origin=(0.0, 0.0, 0.0))
        n_vertices = len(mesh.vertices)
        assert all(0 <= i < n_vertices for i in data["triangles"])
        # One quad wall -> 2 triangles -> 6 indices; 4 walls + 1 roof(quad)
        # + 1 ground(quad) = 6 quad faces -> 12 triangles -> 36 indices.
        assert len(data["triangles"]) == 36

    def test_faces_field_matches_mesh_faces(self):
        mesh = seed_flat_box(box(0, 0, 10, 6), ground_height=0.0, roof_height=3.0)
        data = mesh_to_render_data(mesh, origin=(0.0, 0.0, 0.0))
        assert len(data["faces"]) == len(mesh.faces)
        assert data["faces"][0]["indices"] == mesh.faces[0].vertex_indices
        assert data["faces"][0]["surface_type"] == mesh.faces[0].surface_type

    def test_moving_a_vertex_is_reflected_at_same_index(self):
        mesh = seed_flat_box(box(0, 0, 10, 6), ground_height=0.0, roof_height=3.0)
        mesh.move_vertex(0, (99.0, 98.0, 97.0))
        data = mesh_to_render_data(mesh, origin=(0.0, 0.0, 0.0))
        assert data["vertices"][0:3] == [99.0, 98.0, 97.0]
