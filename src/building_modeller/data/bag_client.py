"""Fetch BAG (Basisregistratie Adressen en Gebouwen) building footprints
from the public PDOK WFS service.

The GeoJSON is fetched with ``requests`` rather than handed to
``geopandas.read_file()`` as a URL: the latter delegates the actual HTTP
fetch to GDAL/pyogrio's own internal libcurl client via its ``/vsicurl/``
virtual filesystem, which does not share Python's proxy environment
variables or certificate store -- on a corporate network with a
TLS-inspecting proxy (which re-signs HTTPS traffic with an internal root
CA the OS/browser trusts) this makes GDAL's fetch fail with an opaque
"Failed to open dataset" error even though the exact same URL works fine
in a browser or via ``requests`` (which does share that configuration,
same as ``pip`` and everything else in this app).

This sandbox has no outbound network access to PDOK, so the exact
typeName/CRS parameters below are based on the published PDOK BAG WFS v2.0
schema; a fetch of this exact URL shape has been confirmed to return valid
GeoJSON from a real browser. Verify against
``{BAG_WFS_URL}?service=WFS&request=GetCapabilities`` on your own machine
if PDOK ever changes their schema.
"""
from __future__ import annotations

from typing import Tuple
from urllib.parse import urlencode

import geopandas as gpd
import requests

BAG_WFS_URL = "https://service.pdok.nl/lv/bag/wfs/v2_0"
BAG_PAND_TYPENAME = "bag:pand"
RD_NEW_EPSG = "EPSG:28992"

BBox = Tuple[float, float, float, float]  # minx, miny, maxx, maxy in EPSG:28992


def _wfs_url(base_url: str, type_name: str, bbox: BBox, count: int) -> str:
    minx, miny, maxx, maxy = bbox
    params = {
        "service": "WFS",
        "version": "2.0.0",
        "request": "GetFeature",
        "typeNames": type_name,
        "outputFormat": "application/json",
        "srsName": RD_NEW_EPSG,
        "bbox": f"{minx},{miny},{maxx},{maxy},{RD_NEW_EPSG}",
        "count": str(count),
    }
    return f"{base_url}?{urlencode(params)}"


def _fetch_wfs_geojson(url: str, timeout: float = 30.0) -> dict:
    response = requests.get(url, timeout=timeout)
    response.raise_for_status()
    return response.json()


def _geojson_to_gdf(geojson: dict) -> gpd.GeoDataFrame:
    # The WFS request asks for srsName=EPSG:28992, so the returned coordinate
    # values are already in RD New regardless of whether the GeoJSON itself
    # declares a "crs" member -- label the GeoDataFrame accordingly.
    features = geojson.get("features", [])
    if not features:
        # from_features([], crs=...) errors: with no geometry column to
        # assign a CRS to, an empty bbox (zero buildings) would crash.
        return gpd.GeoDataFrame(geometry=[], crs=RD_NEW_EPSG)
    return gpd.GeoDataFrame.from_features(features, crs=RD_NEW_EPSG)


def fetch_pand_footprints(
    bbox: BBox, max_features: int = 5000
) -> gpd.GeoDataFrame:
    """Fetch BAG building outlines ("pand") intersecting ``bbox``.

    Returns a GeoDataFrame in EPSG:28992 with (at least) an ``identificatie``
    column holding the BAG pand ID and a ``geometry`` column of polygons.
    """
    url = _wfs_url(BAG_WFS_URL, BAG_PAND_TYPENAME, bbox, max_features)
    return _geojson_to_gdf(_fetch_wfs_geojson(url))
