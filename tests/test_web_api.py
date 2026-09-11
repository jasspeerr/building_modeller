"""Flask REST API tests. These exercise the same model/data/export logic
the old PySide6 UI drove, just through HTTP instead of Qt signals -- see
test_roofshapes.py / test_citygml_writer.py / test_pointcloud.py for the
underlying logic's own tests.

/api/area's BAG fetch goes out to PDOK, so it's monkeypatched here rather
than hitting the network.

/api/area also runs on a background thread now, so its worker is started
through ``server._run_in_background``. Three seams replace it here:

* ``inline_jobs`` runs the worker synchronously, which keeps the ordinary
  area tests deterministic;
* ``never_run_jobs`` creates the job but never runs it, which is the clean
  way to test the "already running" 409 without threads;
* one test uses the real thread, because that is the only way to catch a
  worker that touches request-scoped Flask state (``abort``/``jsonify``),
  which works fine inline and raises ``RuntimeError`` off-thread.
"""
import json
import threading
import time
from io import BytesIO

import geopandas as gpd
import numpy as np
import pytest
from lxml import etree
from shapely.geometry import box

import building_modeller.web.autosave as autosave_module
import building_modeller.web.server as server_module
from building_modeller.data.pointcloud import CLASS_BUILDING, CLASS_GROUND
from building_modeller.model.building import Building
from building_modeller.web.server import create_app


@pytest.fixture
def client(tmp_path, monkeypatch):
    # Never let tests read or write the developer's real autosave file.
    monkeypatch.setattr(autosave_module, "AUTOSAVE_PATH", tmp_path / "last_session.json")
    # Run area jobs inline by default so tests don't depend on thread timing.
    monkeypatch.setattr(server_module, "_run_in_background", lambda target: target())
    app = create_app()
    # A whole fresh state, so no field (including terrain) leaks between tests.
    server_module.state = server_module.AppState()
    server_module._jobs.clear()
    server_module._current_job_id = None
    return app.test_client()


def make_gdf(ids_and_boxes):
    return gpd.GeoDataFrame(
        {"identificatie": [i for i, _ in ids_and_boxes]},
        geometry=[b for _, b in ids_and_boxes],
        crs="EPSG:28992",
    )


def load_area(client, bbox=(0, 0, 100, 100), lidar_folder="", expect="done"):
    """POST an area load and return its finished job snapshot."""
    res = client.post("/api/area", json={"bbox": list(bbox), "lidar_folder": lidar_folder})
    assert res.status_code == 202, res.data
    job_id = res.get_json()["job_id"]
    job = client.get(f"/api/area/jobs/{job_id}").get_json()
    assert job["state"] == expect, job.get("error")
    return job


class TestStaticPages:
    def test_index_serves_html(self, client):
        res = client.get("/")
        assert res.status_code == 200
        assert b"Building Modeller" in res.data

    def test_vendored_threejs_served(self, client):
        res = client.get("/static/vendor/three.min.js")
        assert res.status_code == 200
        assert len(res.data) > 100_000

    def test_vendored_orbitcontrols_served(self, client):
        res = client.get("/static/vendor/OrbitControls.js")
        assert res.status_code == 200
        assert b"OrbitControls" in res.data


class TestAreaLoading:
    def test_successful_fetch_populates_buildings(self, client, monkeypatch):
        gdf = make_gdf([("1", box(0, 0, 10, 6)), ("2", box(20, 0, 28, 8))])
        monkeypatch.setattr(server_module, "fetch_pand_footprints", lambda bbox: gdf)

        job = load_area(client)
        assert job["warnings"] == []
        assert job["progress"] == 100

        listing = client.get("/api/buildings").get_json()
        assert len(listing["buildings"]) == 2
        assert listing["origin"] == [50.0, 50.0, 0.0]

    def test_bag_fetch_failure_is_reported_as_warning_not_500(self, client, monkeypatch):
        def boom(bbox):
            raise RuntimeError("PDOK unreachable")

        monkeypatch.setattr(server_module, "fetch_pand_footprints", boom)

        job = load_area(client)
        assert any("PDOK unreachable" in w for w in job["warnings"])
        assert client.get("/api/buildings").get_json()["buildings"] == []

    def test_missing_lidar_folder_contents_is_a_warning(self, client, monkeypatch, tmp_path):
        monkeypatch.setattr(server_module, "fetch_pand_footprints", lambda bbox: make_gdf([]))

        job = load_area(client, lidar_folder=str(tmp_path))
        assert any("No .laz/.las files found" in w for w in job["warnings"])

    def test_bad_bbox_is_rejected(self, client):
        res = client.post("/api/area", json={"bbox": ["nope", 0, 1, 2]})
        assert res.status_code == 400


class TestAreaJobs:
    def test_post_returns_202_with_a_job_id(self, client, monkeypatch):
        monkeypatch.setattr(server_module, "fetch_pand_footprints", lambda bbox: make_gdf([]))
        res = client.post("/api/area", json={"bbox": [0, 0, 100, 100]})
        assert res.status_code == 202
        assert "job_id" in res.get_json()

    def test_unknown_job_id_404s(self, client):
        assert client.get("/api/area/jobs/does-not-exist").status_code == 404

    def test_second_load_while_one_runs_is_refused(self, client, monkeypatch):
        # Never actually run the worker, so the first job stays "running".
        monkeypatch.setattr(server_module, "_run_in_background", lambda target: None)
        monkeypatch.setattr(server_module, "fetch_pand_footprints", lambda bbox: make_gdf([]))

        first = client.post("/api/area", json={"bbox": [0, 0, 100, 100]})
        assert first.status_code == 202

        second = client.post("/api/area", json={"bbox": [0, 0, 100, 100]})
        assert second.status_code == 409
        assert second.get_json()["job_id"] == first.get_json()["job_id"]

    def test_a_finished_job_does_not_block_the_next_load(self, client, monkeypatch):
        monkeypatch.setattr(server_module, "fetch_pand_footprints", lambda bbox: make_gdf([]))
        load_area(client)
        load_area(client)  # would 409 if the guard keyed on "a job exists"

    def test_worker_failure_surfaces_as_an_error_job_not_a_500(self, client, monkeypatch):
        def boom(job, bbox, folder):
            raise RuntimeError("kaboom")

        monkeypatch.setattr(server_module, "_load_area", boom)
        job = load_area(client, expect="error")
        assert "kaboom" in job["error"]

    def test_cancel_marks_the_job_cancelled(self, client, monkeypatch):
        monkeypatch.setattr(server_module, "_run_in_background", lambda target: None)
        monkeypatch.setattr(server_module, "fetch_pand_footprints", lambda bbox: make_gdf([]))
        job_id = client.post("/api/area", json={"bbox": [0, 0, 100, 100]}).get_json()["job_id"]

        res = client.post(f"/api/area/jobs/{job_id}/cancel")
        assert res.status_code == 200
        assert server_module._jobs[job_id].cancel_requested is True

    def test_cancel_on_unknown_job_404s(self, client):
        assert client.post("/api/area/jobs/nope/cancel").status_code == 404

    def test_runs_on_a_real_thread_and_publishes_state_before_done(self, client, monkeypatch):
        """The one genuinely threaded test.

        Inline running executes the worker inside a live Flask request
        context, where ``abort``/``jsonify``/``current_app`` all happen to
        work; on a real thread they raise. So this is the only test that
        can catch request-scoped state leaking into the worker -- it shows
        up as state="error" with a RuntimeError.
        """
        started = []

        def run_threaded(target):
            thread = threading.Thread(target=target, daemon=True)
            started.append(thread)
            thread.start()
            return thread

        monkeypatch.setattr(server_module, "_run_in_background", run_threaded)
        monkeypatch.setattr(
            server_module,
            "fetch_pand_footprints",
            lambda bbox: make_gdf([("1", box(0, 0, 10, 6))]),
        )

        res = client.post("/api/area", json={"bbox": [0, 0, 100, 100]})
        assert res.status_code == 202
        job_id = res.get_json()["job_id"]
        thread = started[-1]
        try:
            deadline = time.time() + 10.0
            while time.time() < deadline:
                job = client.get(f"/api/area/jobs/{job_id}").get_json()
                if job["state"] != "running":
                    break
                time.sleep(0.02)
            assert job["state"] == "done", job["error"]
            # A stray jsonify()/abort() carried into the worker lands here.
            assert job["error"] is None
            # Happens-before: seeing "done" must mean the state is published.
            assert len(client.get("/api/buildings").get_json()["buildings"]) == 1
        finally:
            # A worker outliving the test would run with monkeypatches undone:
            # live PDOK calls and writes to the real autosave file.
            thread.join(timeout=10.0)
            assert not thread.is_alive()


class TestBuildings:
    def test_list_empty_initially(self, client):
        res = client.get("/api/buildings")
        assert res.get_json()["buildings"] == []

    def test_unknown_building_404(self, client):
        res = client.get("/api/buildings/does-not-exist")
        assert res.status_code == 404

    def test_mesh_for_fresh_building_is_seeded_flat_box(self, client):
        server_module.state.buildings = [Building(bag_id="A", footprint=box(0, 0, 10, 6))]
        res = client.get("/api/buildings/A/mesh")
        assert res.status_code == 200
        data = res.get_json()
        assert len(data["vertices"]) % 3 == 0
        assert len(data["triangles"]) % 3 == 0
        assert len(data["triangles"]) > 0
        assert len(data["faces"]) == 6  # 4 walls + roof + ground

    def test_move_vertex_updates_status_and_mesh(self, client):
        server_module.state.buildings = [Building(bag_id="A", footprint=box(0, 0, 10, 6))]
        res = client.post("/api/buildings/A/vertex/0", json={"x": 1.0, "y": 2.0, "z": 9.0})
        assert res.status_code == 200
        data = res.get_json()
        assert data["building"]["status"] == "edited"
        assert data["mesh"]["vertices"][0:3] == [1.0, 2.0, 9.0]

    def test_move_vertex_on_unknown_building_404(self, client):
        res = client.post("/api/buildings/nope/vertex/0", json={"x": 0.0, "y": 0.0, "z": 0.0})
        assert res.status_code == 404

    def test_move_vertex_out_of_range_404(self, client):
        server_module.state.buildings = [Building(bag_id="A", footprint=box(0, 0, 10, 6))]
        res = client.post("/api/buildings/A/vertex/999", json={"x": 0.0, "y": 0.0, "z": 0.0})
        assert res.status_code == 404

    def test_move_vertex_uses_origin_relative_coordinates(self, client):
        server_module.state.buildings = [Building(bag_id="A", footprint=box(0, 0, 10, 6))]
        server_module.state.origin = (100.0, 200.0, 0.0)
        client.post("/api/buildings/A/vertex/0", json={"x": 1.0, "y": 2.0, "z": 9.0})
        building = server_module.state.buildings[0]
        assert building.mesh().vertices[0] == (101.0, 202.0, 9.0)

    def test_snap_vertex_to_lidar(self, client):
        server_module.state.buildings = [Building(bag_id="A", footprint=box(0, 0, 10, 6))]
        cloud = server_module.LidarPointCloud(
            x=np.array([1.0, 1.1, 0.9]),
            y=np.array([2.0, 2.1, 1.9]),
            z=np.array([5.0, 5.2, 4.8]),
        )
        server_module.state.lidar_cloud = cloud
        building = server_module.state.buildings[0]
        building.move_vertex(0, (1.0, 2.0, 0.0))

        res = client.post("/api/buildings/A/vertex/0/snap_lidar")
        assert res.status_code == 200
        data = res.get_json()
        assert data["snapped_z"] == pytest.approx(5.0, abs=0.2)
        assert building.mesh().vertices[0][2] == pytest.approx(5.0, abs=0.2)

    def test_snap_vertex_to_lidar_no_points_nearby_404(self, client):
        server_module.state.buildings = [Building(bag_id="A", footprint=box(0, 0, 10, 6))]
        res = client.post("/api/buildings/A/vertex/0/snap_lidar")
        assert res.status_code == 404

    def test_snap_prefers_building_points_over_overhanging_vegetation(self, client):
        server_module.state.buildings = [Building(bag_id="A", footprint=box(0, 0, 10, 6))]
        # A roof at 5m with a tree branch at 11m directly above it.
        server_module.state.lidar_cloud = server_module.LidarPointCloud(
            x=np.array([1.0, 1.05, 0.95, 1.0, 1.02]),
            y=np.array([2.0, 2.05, 1.95, 2.0, 2.02]),
            z=np.array([5.0, 5.1, 4.9, 11.0, 11.2]),
            classification=np.array([6, 6, 6, 5, 5], dtype=np.uint8),
        )
        building = server_module.state.buildings[0]
        building.move_vertex(0, (1.0, 2.0, 0.0))

        data = client.post("/api/buildings/A/vertex/0/snap_lidar").get_json()
        assert data["matched_class"] is True
        assert data["snapped_z"] == pytest.approx(5.0, abs=0.3)

    def test_snap_falls_back_to_all_points_when_no_building_class_is_near(self, client):
        server_module.state.buildings = [Building(bag_id="A", footprint=box(0, 0, 10, 6))]
        server_module.state.lidar_cloud = server_module.LidarPointCloud(
            x=np.array([1.0, 1.05]),
            y=np.array([2.0, 2.05]),
            z=np.array([3.0, 3.1]),
            classification=np.array([CLASS_GROUND, CLASS_GROUND], dtype=np.uint8),
        )
        building = server_module.state.buildings[0]
        building.move_vertex(0, (1.0, 2.0, 0.0))

        data = client.post("/api/buildings/A/vertex/0/snap_lidar").get_json()
        assert data["matched_class"] is False
        assert data["snapped_z"] == pytest.approx(3.05, abs=0.2)


class TestTerrainEndpoint:
    def test_empty_arrays_when_there_is_no_terrain(self, client):
        data = client.get("/api/terrain").get_json()
        assert data == {"vertices": [], "triangles": []}

    def test_returns_origin_relative_triangles(self, client):
        from building_modeller.model.terrain import TerrainMesh

        server_module.state.terrain = TerrainMesh(
            vertices=[(10.0, 20.0, 1.0), (12.0, 20.0, 1.0), (10.0, 22.0, 1.0)],
            triangles=[(0, 1, 2)],
        )
        server_module.state.origin = (10.0, 20.0, 0.0)

        data = client.get("/api/terrain").get_json()
        assert data["triangles"] == [0, 1, 2]
        assert data["vertices"][0:3] == [0.0, 0.0, 1.0]


class TestPointCloudEndpoint:
    def make_cloud(self):
        return server_module.LidarPointCloud(
            x=np.array([1.0, 2.0, 3.0, 4.0]),
            y=np.array([1.0, 2.0, 3.0, 4.0]),
            z=np.array([0.0, 1.0, 8.0, 12.0]),
            classification=np.array([CLASS_GROUND, CLASS_BUILDING, 5, 7], dtype=np.uint8),
        )

    def test_ships_classification_for_colouring(self, client):
        server_module.state.lidar_cloud = self.make_cloud()
        data = client.get("/api/pointcloud").get_json()
        assert data["classification"] == [CLASS_GROUND, CLASS_BUILDING, 5, 7]
        assert data["shown_points"] == 4

    def test_hide_vegetation_drops_vegetation_and_noise(self, client):
        server_module.state.lidar_cloud = self.make_cloud()
        data = client.get("/api/pointcloud?hide_vegetation=1").get_json()
        assert data["classification"] == [CLASS_GROUND, CLASS_BUILDING]
        # Filtering happens before decimation, so the total shrinks too --
        # that is what makes the display budget go further.
        assert data["total_points"] == 2


class TestEdgeSplit:
    def test_splits_shared_edge_and_selects_new_vertex(self, client):
        building = Building(bag_id="A", footprint=box(0, 0, 10, 6))
        server_module.state.buildings = [building]
        roof_face = building.mesh().faces_by_type("roof")[0]
        a, b = roof_face.vertex_indices[0], roof_face.vertex_indices[1]

        res = client.post("/api/buildings/A/edge/split", json={"index_a": a, "index_b": b})
        assert res.status_code == 200
        data = res.get_json()
        assert data["building"]["status"] == "edited"
        new_index = data["new_vertex_index"]
        assert len(data["mesh"]["vertices"]) // 3 == new_index + 1

    def test_split_non_adjacent_vertices_400(self, client):
        server_module.state.buildings = [Building(bag_id="A", footprint=box(0, 0, 10, 6))]
        res = client.post("/api/buildings/A/edge/split", json={"index_a": 0, "index_b": 5})
        assert res.status_code == 400

    def test_split_edge_on_unknown_building_404(self, client):
        res = client.post("/api/buildings/nope/edge/split", json={"index_a": 0, "index_b": 1})
        assert res.status_code == 404

    def test_split_edge_missing_fields_400(self, client):
        server_module.state.buildings = [Building(bag_id="A", footprint=box(0, 0, 10, 6))]
        res = client.post("/api/buildings/A/edge/split", json={})
        assert res.status_code == 400


class TestVertexDelete:
    def test_deletes_vertex_and_updates_status(self, client):
        server_module.state.buildings = [Building(bag_id="A", footprint=box(0, 0, 10, 6))]
        res = client.post("/api/buildings/A/vertex/0/delete")
        assert res.status_code == 200
        data = res.get_json()
        assert data["building"]["status"] == "edited"
        assert len(data["mesh"]["vertices"]) // 3 == 7

    def test_delete_vertex_on_unknown_building_404(self, client):
        res = client.post("/api/buildings/nope/vertex/0/delete")
        assert res.status_code == 404

    def test_delete_vertex_out_of_range_404(self, client):
        server_module.state.buildings = [Building(bag_id="A", footprint=box(0, 0, 10, 6))]
        res = client.post("/api/buildings/A/vertex/999/delete")
        assert res.status_code == 404


class TestFaceSplit:
    def test_splits_roof_into_two_pitches(self, client):
        building = Building(bag_id="A", footprint=box(0, 0, 10, 6))
        server_module.state.buildings = [building]
        roof = list(building.mesh().faces_by_type("roof")[0].vertex_indices)

        a = client.post("/api/buildings/A/edge/split", json={"index_a": roof[0], "index_b": roof[1]}).get_json()[
            "new_vertex_index"
        ]
        b = client.post("/api/buildings/A/edge/split", json={"index_a": roof[3], "index_b": roof[2]}).get_json()[
            "new_vertex_index"
        ]

        res = client.post("/api/buildings/A/face/split", json={"index_a": a, "index_b": b})
        assert res.status_code == 200
        data = res.get_json()
        assert data["building"]["status"] == "edited"
        roof_faces = [f for f in data["mesh"]["faces"] if f["surface_type"] == "roof"]
        assert len(roof_faces) == 2

    def test_split_face_on_adjacent_vertices_400(self, client):
        building = Building(bag_id="A", footprint=box(0, 0, 10, 6))
        server_module.state.buildings = [building]
        roof = building.mesh().faces_by_type("roof")[0].vertex_indices
        res = client.post("/api/buildings/A/face/split", json={"index_a": roof[0], "index_b": roof[1]})
        assert res.status_code == 400

    def test_split_face_on_unknown_building_404(self, client):
        res = client.post("/api/buildings/nope/face/split", json={"index_a": 0, "index_b": 1})
        assert res.status_code == 404

    def test_split_face_missing_fields_400(self, client):
        server_module.state.buildings = [Building(bag_id="A", footprint=box(0, 0, 10, 6))]
        res = client.post("/api/buildings/A/face/split", json={})
        assert res.status_code == 400


class TestFaceExtrude:
    def test_extrudes_roof_face(self, client):
        building = Building(bag_id="A", footprint=box(0, 0, 10, 6))
        server_module.state.buildings = [building]
        roof = building.mesh().faces_by_type("roof")[0].vertex_indices

        res = client.post(
            "/api/buildings/A/face/extrude",
            json={"index_a": roof[0], "index_b": roof[2], "distance": 1.5},
        )
        assert res.status_code == 200
        data = res.get_json()
        assert data["building"]["status"] == "edited"
        roof_faces = [f for f in data["mesh"]["faces"] if f["surface_type"] == "roof"]
        assert len(roof_faces) == 1
        cap_zs = {
            round(data["mesh"]["vertices"][i * 3 + 2], 3) for i in roof_faces[0]["indices"]
        }
        assert cap_zs == {4.5}

    def test_extrude_on_adjacent_vertices_400(self, client):
        building = Building(bag_id="A", footprint=box(0, 0, 10, 6))
        server_module.state.buildings = [building]
        roof = building.mesh().faces_by_type("roof")[0].vertex_indices
        res = client.post(
            "/api/buildings/A/face/extrude",
            json={"index_a": roof[0], "index_b": roof[1], "distance": 1.0},
        )
        assert res.status_code == 400

    def test_extrude_on_unknown_building_404(self, client):
        res = client.post(
            "/api/buildings/nope/face/extrude", json={"index_a": 0, "index_b": 1, "distance": 1.0}
        )
        assert res.status_code == 404

    def test_extrude_missing_fields_400(self, client):
        server_module.state.buildings = [Building(bag_id="A", footprint=box(0, 0, 10, 6))]
        res = client.post("/api/buildings/A/face/extrude", json={"index_a": 0, "index_b": 2})
        assert res.status_code == 400


class TestSession:
    def test_round_trip_preserves_edited_geometry_and_recomputes_origin(self, client):
        b = Building(bag_id="A", footprint=box(0, 0, 10, 6))
        server_module.state.buildings = [b]
        client.post("/api/buildings/A/vertex/0", json={"x": 1.0, "y": 2.0, "z": 9.0})

        download = client.get("/api/session")
        assert download.status_code == 200
        assert "session.json" in download.headers["Content-Disposition"]

        upload = client.post(
            "/api/session",
            data={"file": (BytesIO(download.data), "session.json")},
            content_type="multipart/form-data",
        )
        assert upload.status_code == 200
        data = upload.get_json()
        assert len(data["buildings"]) == 1
        assert data["buildings"][0]["status"] == "edited"
        restored = server_module.state.buildings[0]
        assert restored.mesh().vertices[0] == (1.0, 2.0, 9.0)

    def test_upload_missing_file_400(self, client):
        res = client.post("/api/session", data={}, content_type="multipart/form-data")
        assert res.status_code == 400

    def test_upload_invalid_json_400(self, client):
        res = client.post(
            "/api/session",
            data={"file": (BytesIO(b"not json"), "session.json")},
            content_type="multipart/form-data",
        )
        assert res.status_code == 400


class TestExport:
    def test_export_with_no_buildings_400(self, client):
        res = client.post("/api/export")
        assert res.status_code == 400

    def test_export_returns_valid_citygml_and_marks_exported(self, client):
        server_module.state.buildings = [Building(bag_id="A", footprint=box(0, 0, 10, 6))]
        res = client.post("/api/export")
        assert res.status_code == 200
        assert "buildings.gml" in res.headers["Content-Disposition"]
        root = etree.fromstring(res.data)
        assert root.tag.endswith("CityModel")

        listing = client.get("/api/buildings").get_json()
        assert listing["buildings"][0]["status"] == "exported"


class TestSessionAutosave:
    """The background autosave/autoload flow (separate from the explicit
    download/upload session endpoints in TestSession above)."""

    def test_moving_a_vertex_autosaves(self, client, tmp_path, monkeypatch):
        monkeypatch.setattr(autosave_module, "AUTOSAVE_PATH", tmp_path / "auto.json")
        server_module.state.buildings = [Building(bag_id="A", footprint=box(0, 0, 10, 6))]

        client.post("/api/buildings/A/vertex/0", json={"x": 1.0, "y": 2.0, "z": 9.0})

        assert (tmp_path / "auto.json").exists()
        restored = autosave_module.load()
        assert restored[0].bag_id == "A"
        assert restored[0].mesh().vertices[0] == (1.0, 2.0, 9.0)

    def test_new_app_instance_restores_autosaved_state(self, tmp_path, monkeypatch):
        monkeypatch.setattr(autosave_module, "AUTOSAVE_PATH", tmp_path / "auto.json")
        b = Building(bag_id="A", footprint=box(0, 0, 10, 6))
        autosave_module.save([b])

        app = create_app()
        client = app.test_client()
        listing = client.get("/api/buildings").get_json()
        assert len(listing["buildings"]) == 1
        assert listing["buildings"][0]["bag_id"] == "A"

    def test_fresh_start_with_no_autosave_file_is_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(autosave_module, "AUTOSAVE_PATH", tmp_path / "does_not_exist.json")
        # create_app() only *overwrites* state.buildings when the autosave
        # file has something to restore, so start from a known-clean state
        # rather than relying on test execution order.
        server_module.state.buildings = []
        app = create_app()
        client = app.test_client()
        listing = client.get("/api/buildings").get_json()
        assert listing["buildings"] == []
