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
import pytest
from lxml import etree
from shapely.geometry import box

import building_modeller.web.server as server_module
from building_modeller.model.building import Building
from building_modeller.web.server import create_app


@pytest.fixture
def client():
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

    def test_mesh_for_unmodelled_building_is_flat_fallback(self, client):
        server_module.state.buildings = [Building(bag_id="A", footprint=box(0, 0, 10, 6))]
        res = client.get("/api/buildings/A/mesh")
        assert res.status_code == 200
        data = res.get_json()
        assert len(data["vertices"]) % 3 == 0
        assert len(data["faces"]) % 3 == 0
        assert len(data["faces"]) > 0

    def test_set_roof_updates_status_and_mesh(self, client):
        server_module.state.buildings = [Building(bag_id="A", footprint=box(0, 0, 10, 6))]
        res = client.post(
            "/api/buildings/A/roof",
            json={"roof_type": "gable", "eave_height": 3.0, "ridge_height": 5.0, "ridge_along": "long"},
        )
        assert res.status_code == 200
        data = res.get_json()
        assert data["building"]["status"] == "edited"
        assert data["building"]["roof"]["roof_type"] == "gable"
        assert len(data["mesh"]["faces"]) > 0

    def test_set_roof_on_unknown_building_404(self, client):
        res = client.post(
            "/api/buildings/nope/roof",
            json={"roof_type": "flat", "eave_height": 3.0},
        )
        assert res.status_code == 404


class TestSession:
    def test_round_trip_preserves_roof_and_recomputes_origin(self, client):
        b = Building(bag_id="A", footprint=box(0, 0, 10, 6))
        server_module.state.buildings = [b]
        client.post(
            "/api/buildings/A/roof",
            json={"roof_type": "hip", "eave_height": 3.0, "ridge_height": 6.0},
        )

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
        assert data["buildings"][0]["roof"]["roof_type"] == "hip"

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
