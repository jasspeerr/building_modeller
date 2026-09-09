"""Fetch BGT (Basisregistratie Grootschalige Topografie) layers from the
public PDOK WFS service, for visual context only (roads, water, terrain --
not part of the modelled/exported building geometry).

Same caveat as ``bag_client``: this sandbox cannot reach PDOK, so verify
typeNames against ``{BGT_WFS_URL}?service=WFS&request=GetCapabilities`` on
your own machine.
"""
from __future__ import annotations

from typing import List

import geopandas as gpd

from .bag_client import BBox, RD_NEW_EPSG, _wfs_url

BGT_WFS_URL = "https://service.pdok.nl/lv/bgt/wfs/v1_0"

# A representative subset of BGT layers useful as scene context.
DEFAULT_CONTEXT_LAYERS = [
    "bgt:wegdeel",
    "bgt:waterdeel",
    "bgt:begroeidterreindeel",
    "bgt:onbegroeidterreindeel",
]


def fetch_context_layer(layer: str, bbox: BBox, max_features: int = 5000) -> gpd.GeoDataFrame:
    url = _wfs_url(BGT_WFS_URL, layer, bbox, max_features)
    gdf = gpd.read_file(url)
    if gdf.crs is None:
        gdf = gdf.set_crs(RD_NEW_EPSG)
    elif gdf.crs.to_string() != RD_NEW_EPSG:
        gdf = gdf.to_crs(RD_NEW_EPSG)
    return gdf


def fetch_context(bbox: BBox, layers: List[str] = None) -> dict:
    """Fetch several BGT context layers, skipping any that fail (context
    data is optional and shouldn't block modelling)."""
    layers = layers or DEFAULT_CONTEXT_LAYERS
    result = {}
    for layer in layers:
        try:
            result[layer] = fetch_context_layer(layer, bbox)
        except Exception:
            continue
    return result
