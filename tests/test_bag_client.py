"""Unit tests for the BAG/BGT WFS fetch logic. These mock ``requests.get``
rather than hitting the network -- the fetch used to be delegated to
``geopandas.read_file()``, which internally uses GDAL's own libcurl and
can't be mocked this way, so this coverage didn't previously exist. See
data/bag_client.py's module docstring for why the fetch was moved to
``requests``.
"""
import requests
import pytest

import building_modeller.data.bag_client as bag_client
import building_modeller.data.bgt_client as bgt_client


def make_response(status_code=200, json_data=None):
    class FakeResponse:
        def __init__(self):
            self.status_code = status_code
            self._json = json_data

        def raise_for_status(self):
            if self.status_code >= 400:
                raise requests.exceptions.HTTPError(f"{self.status_code} error")

        def json(self):
            return self._json

    return FakeResponse()


SAMPLE_GEOJSON = {
    "type": "FeatureCollection",
    "features": [
        {
            "type": "Feature",
            "properties": {"identificatie": "0503100000012345"},
            "geometry": {
                "type": "Polygon",
                "coordinates": [[[0, 0], [10, 0], [10, 6], [0, 6], [0, 0]]],
            },
        },
        {
            "type": "Feature",
            "properties": {"identificatie": "0503100000067890"},
            "geometry": {
                "type": "Polygon",
                "coordinates": [[[20, 0], [28, 0], [28, 8], [20, 8], [20, 0]]],
            },
        },
    ],
}


class TestFetchPandFootprints:
    def test_returns_geodataframe_with_expected_rows(self, monkeypatch):
        monkeypatch.setattr(
            bag_client.requests, "get", lambda url, timeout=30.0: make_response(200, SAMPLE_GEOJSON)
        )
        gdf = bag_client.fetch_pand_footprints((0, 0, 100, 100))
        assert len(gdf) == 2
        assert set(gdf["identificatie"]) == {"0503100000012345", "0503100000067890"}
        assert gdf.crs.to_string() == bag_client.RD_NEW_EPSG

    def test_http_error_status_raises(self, monkeypatch):
        monkeypatch.setattr(
            bag_client.requests, "get", lambda url, timeout=30.0: make_response(503, None)
        )
        with pytest.raises(requests.exceptions.HTTPError):
            bag_client.fetch_pand_footprints((0, 0, 100, 100))

    def test_empty_feature_collection_returns_empty_gdf(self, monkeypatch):
        monkeypatch.setattr(
            bag_client.requests,
            "get",
            lambda url, timeout=30.0: make_response(200, {"type": "FeatureCollection", "features": []}),
        )
        gdf = bag_client.fetch_pand_footprints((0, 0, 100, 100))
        assert len(gdf) == 0

    def test_url_requests_rd_new_and_geojson(self, monkeypatch):
        captured = {}

        def fake_get(url, timeout=30.0):
            captured["url"] = url
            return make_response(200, SAMPLE_GEOJSON)

        monkeypatch.setattr(bag_client.requests, "get", fake_get)
        bag_client.fetch_pand_footprints((1, 2, 3, 4))
        assert "srsName=EPSG%3A28992" in captured["url"]
        assert "outputFormat=application%2Fjson" in captured["url"]
        assert "typeNames=bag%3Apand" in captured["url"]


class TestFetchContextLayer:
    def test_returns_geodataframe(self, monkeypatch):
        # fetch_context_layer calls the shared _fetch_wfs_geojson helper,
        # which is defined in (and calls requests.get from) bag_client's
        # module namespace regardless of which module imports it.
        monkeypatch.setattr(
            bag_client.requests, "get", lambda url, timeout=30.0: make_response(200, SAMPLE_GEOJSON)
        )
        gdf = bgt_client.fetch_context_layer("bgt:wegdeel", (0, 0, 100, 100))
        assert len(gdf) == 2

    def test_fetch_context_skips_failing_layers(self, monkeypatch):
        def fake_get(url, timeout=30.0):
            if "wegdeel" in url:
                return make_response(200, SAMPLE_GEOJSON)
            return make_response(500, None)

        monkeypatch.setattr(bag_client.requests, "get", fake_get)
        result = bgt_client.fetch_context(
            (0, 0, 100, 100), layers=["bgt:wegdeel", "bgt:waterdeel"]
        )
        assert list(result.keys()) == ["bgt:wegdeel"]
        assert len(result["bgt:wegdeel"]) == 2
