"""Fetch BAG (Basisregistratie Adressen en Gebouwen) building footprints
from the public PDOK WFS service.

This sandbox has no outbound network access to PDOK, so the exact
typeName/CRS parameters below are based on the published PDOK BAG WFS v2.0
schema and have **not** been exercised against the live service from here.
Verify against ``{BAG_WFS_URL}?service=WFS&request=GetCapabilities`` on
your own machine if PDOK changes their schema.
"""
from __future__ import annotations

from typing import Tuple
from urllib.parse import urlencode

import geopandas as gpd

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


def fetch_pand_footprints(
    bbox: BBox, max_features: int = 5000
) -> gpd.GeoDataFrame:
    """Fetch BAG building outlines ("pand") intersecting ``bbox``.

    Returns a GeoDataFrame in EPSG:28992 with (at least) an ``identificatie``
    column holding the BAG pand ID and a ``geometry`` column of polygons.
    """
    url = _wfs_url(BAG_WFS_URL, BAG_PAND_TYPENAME, bbox, max_features)
    gdf = gpd.read_file(url)
    if gdf.crs is None:
        gdf = gdf.set_crs(RD_NEW_EPSG)
    elif gdf.crs.to_string() != RD_NEW_EPSG:
        gdf = gdf.to_crs(RD_NEW_EPSG)
    return gdf
