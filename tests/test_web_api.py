"""Flask REST API tests. These exercise the same model/data/export logic
the old PySide6 UI drove, just through HTTP instead of Qt signals -- see
test_roofshapes.py / test_citygml_writer.py / test_pointcloud.py for the
underlying logic's own tests.

/api/area's BAG fetch goes out to PDOK, so it's monkeypatched here rather
than hitting the network.
"""
import json
from io import BytesIO

import geopandas as gpd
import numpy as np
import pytest
from lxml import etree
from shapely.geometry import box

import building_modeller.web.autosave as autosave_module
import building_modeller.web.server as server_module
from building_modeller.model.building import Building
from building_modeller.web.server import create_app


@pytest.fixture
def client(tmp_path, monkeypatch):
    # Never let tests read or write the developer's real autosave file.
    monkeypatch.setattr(autosave_module, "AUTOSAVE_PATH", tmp_path / "last_session.json")
    app = create_app()
    server_module.state.buildings = []
    server_module.state.lidar_cloud = server_module.LidarPointCloud.empty()
    server_module.state.origin = (0.0, 0.0, 0.0)
    return app.test_client()


def make_gdf(ids_and_boxes):
    return gpd.GeoDataFrame(
        {"identificatie": [i for i, _ in ids_and_boxes]},
        geometry=[b for _, b in ids_and_boxes],
        crs="EPSG:28992",
    )


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

        res = client.post("/api/area", json={"bbox": [0, 0, 100, 100], "lidar_folder": ""})
        assert res.status_code == 200
        data = res.get_json()
        assert len(data["buildings"]) == 2
        assert data["warnings"] == []
        assert data["origin"] == [50.0, 50.0, 0.0]

    def test_bag_fetch_failure_is_reported_as_warning_not_500(self, client, monkeypatch):
        def boom(bbox):
            raise RuntimeError("PDOK unreachable")

        monkeypatch.setattr(server_module, "fetch_pand_footprints", boom)

        res = client.post("/api/area", json={"bbox": [0, 0, 100, 100], "lidar_folder": ""})
        assert res.status_code == 200
        data = res.get_json()
        assert data["buildings"] == []
        assert any("PDOK unreachable" in w for w in data["warnings"])

    def test_missing_lidar_folder_contents_is_a_warning(self, client, monkeypatch, tmp_path):
        monkeypatch.setattr(server_module, "fetch_pand_footprints", lambda bbox: make_gdf([]))

        res = client.post(
            "/api/area", json={"bbox": [0, 0, 100, 100], "lidar_folder": str(tmp_path)}
        )
        data = res.get_json()
        assert any("No .laz/.las files found" in w for w in data["warnings"])


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
