"""A local, single-user Flask app: a thin REST API in front of the same
model/data/export logic the original desktop UI used, plus a static
Three.js frontend.

State (the currently loaded batch of buildings and LiDAR point cloud) is
kept in a single process-wide ``AppState`` instance -- this is a local,
one-user-at-a-time tool, so there is no per-session/per-user isolation,
matching how the original desktop app held state in its main window.
"""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from typing import List, Tuple

from flask import Flask, Response, abort, jsonify, request, send_from_directory

from ..data.bag_client import fetch_pand_footprints
from ..data.pointcloud import LidarPointCloud, find_tiles, stats_for_footprint
from ..export.citygml_writer import write_citygml
from ..model.building import Building, ModellingStatus
from ..model.project import session_from_payload, session_to_payload
from ..model.roofshapes import RoofParams, RoofType
from .meshutil import mesh_to_triangles

MAX_VIEWER_POINTS = 200_000


@dataclass
class AppState:
    buildings: List[Building] = field(default_factory=list)
    lidar_cloud: LidarPointCloud = field(default_factory=LidarPointCloud.empty)
    origin: Tuple[float, float, float] = (0.0, 0.0, 0.0)


state = AppState()
state_lock = threading.Lock()


def _find_building(bag_id: str) -> Building:
    for b in state.buildings:
        if b.bag_id == bag_id:
            return b
    abort(404, f"unknown building: {bag_id}")


def _roof_to_dict(roof: RoofParams) -> dict:
    if roof is None:
        return None
    return {
        "roof_type": RoofType(roof.roof_type).value,
        "eave_height": roof.eave_height,
        "ridge_height": roof.ridge_height,
        "ridge_along": roof.ridge_along,
    }


def _roof_from_json(data: dict) -> RoofParams:
    return RoofParams(
        roof_type=RoofType(data["roof_type"]),
        eave_height=float(data["eave_height"]),
        ridge_height=(
            float(data["ridge_height"]) if data.get("ridge_height") is not None else None
        ),
        ridge_along=data.get("ridge_along", "long"),
    )


def _building_summary(building: Building) -> dict:
    ox, oy, _ = state.origin
    ring = list(building.footprint.exterior.coords)[:-1]
    return {
        "bag_id": building.bag_id,
        "status": building.status.value,
        "ground_height": building.ground_height,
        "lidar_stats": building.lidar_stats,
        "roof": _roof_to_dict(building.roof),
        "footprint": [[x - ox, y - oy] for x, y in ring],
    }


def _point_cloud_payload(cloud: LidarPointCloud, origin: Tuple[float, float, float]) -> dict:
    n = len(cloud)
    if n == 0:
        return {"x": [], "y": [], "z": [], "total_points": 0, "shown_points": 0}
    step = max(1, n // MAX_VIEWER_POINTS)
    idx = slice(0, n, step)
    ox, oy, oz = origin
    return {
        "x": (cloud.x[idx] - ox).tolist(),
        "y": (cloud.y[idx] - oy).tolist(),
        "z": (cloud.z[idx] - oz).tolist(),
        "total_points": n,
        "shown_points": len(range(*idx.indices(n))),
    }


def create_app() -> Flask:
    app = Flask(__name__, static_folder="static", static_url_path="/static")

    @app.get("/")
    def index():
        return send_from_directory(app.static_folder, "index.html")

    @app.post("/api/area")
    def load_area():
        payload = request.get_json(force=True)
        bbox = tuple(float(v) for v in payload["bbox"])
        lidar_folder = (payload.get("lidar_folder") or "").strip()
        warnings: List[str] = []

        try:
            gdf = fetch_pand_footprints(bbox)
        except Exception as exc:
            warnings.append(f"BAG fetch failed: {exc}")
            gdf = None

        buildings: List[Building] = []
        if gdf is not None:
            id_col = "identificatie" if "identificatie" in gdf.columns else gdf.columns[0]
            for _, row in gdf.iterrows():
                geom = row.geometry
                if geom is None or geom.is_empty:
                    continue
                if geom.geom_type == "MultiPolygon":
                    geom = max(geom.geoms, key=lambda g: g.area)
                buildings.append(Building(bag_id=str(row[id_col]), footprint=geom))

        lidar_cloud = LidarPointCloud.empty()
        if lidar_folder:
            try:
                tiles = find_tiles(lidar_folder)
                if not tiles:
                    warnings.append(f"No .laz/.las files found in: {lidar_folder}")
                else:
                    lidar_cloud = LidarPointCloud.from_files(tiles, bbox=bbox)
            except Exception as exc:
                warnings.append(f"LiDAR load failed: {exc}")

        for b in buildings:
            stats = stats_for_footprint(lidar_cloud, b.footprint)
            b.lidar_stats = stats
            if stats:
                b.ground_height = stats["ground_height"]

        minx, miny, maxx, maxy = bbox
        origin = ((minx + maxx) / 2.0, (miny + maxy) / 2.0, 0.0)

        with state_lock:
            state.buildings = buildings
            state.lidar_cloud = lidar_cloud
            state.origin = origin

        return jsonify(
            {
                "origin": list(origin),
                "buildings": [_building_summary(b) for b in buildings],
                "point_cloud": _point_cloud_payload(lidar_cloud, origin),
                "warnings": warnings,
            }
        )

    @app.get("/api/buildings")
    def list_buildings():
        return jsonify(
            {"origin": list(state.origin), "buildings": [_building_summary(b) for b in state.buildings]}
        )

    @app.get("/api/buildings/<bag_id>")
    def get_building(bag_id: str):
        return jsonify(_building_summary(_find_building(bag_id)))

    @app.get("/api/buildings/<bag_id>/mesh")
    def get_mesh(bag_id: str):
        building = _find_building(bag_id)
        vertices, faces = mesh_to_triangles(building.mesh(), state.origin)
        return jsonify({"vertices": vertices, "faces": faces})

    @app.post("/api/buildings/<bag_id>/roof")
    def set_roof(bag_id: str):
        building = _find_building(bag_id)
        data = request.get_json(force=True)
        with state_lock:
            building.set_roof(_roof_from_json(data))
            vertices, faces = mesh_to_triangles(building.mesh(), state.origin)
        return jsonify(
            {"building": _building_summary(building), "mesh": {"vertices": vertices, "faces": faces}}
        )

    @app.get("/api/pointcloud")
    def get_point_cloud():
        return jsonify(_point_cloud_payload(state.lidar_cloud, state.origin))

    @app.get("/api/session")
    def download_session():
        data = json.dumps(session_to_payload(state.buildings), indent=2).encode("utf-8")
        return Response(
            data,
            mimetype="application/json",
            headers={"Content-Disposition": "attachment; filename=session.json"},
        )

    @app.post("/api/session")
    def upload_session():
        if "file" not in request.files:
            abort(400, "missing 'file' in upload")
        try:
            payload = json.load(request.files["file"].stream)
            buildings = session_from_payload(payload)
        except Exception as exc:
            abort(400, f"invalid session file: {exc}")

        if buildings:
            minx = min(b.footprint.bounds[0] for b in buildings)
            miny = min(b.footprint.bounds[1] for b in buildings)
            maxx = max(b.footprint.bounds[2] for b in buildings)
            maxy = max(b.footprint.bounds[3] for b in buildings)
            origin = ((minx + maxx) / 2.0, (miny + maxy) / 2.0, 0.0)
        else:
            origin = (0.0, 0.0, 0.0)

        with state_lock:
            state.buildings = buildings
            state.lidar_cloud = LidarPointCloud.empty()
            state.origin = origin

        return jsonify({"origin": list(origin), "buildings": [_building_summary(b) for b in buildings]})

    @app.post("/api/export")
    def export_citygml_route():
        if not state.buildings:
            abort(400, "no buildings loaded")
        data = write_citygml(state.buildings)
        with state_lock:
            for b in state.buildings:
                b.status = ModellingStatus.EXPORTED
        return Response(
            data,
            mimetype="application/gml+xml",
            headers={"Content-Disposition": "attachment; filename=buildings.gml"},
        )

    return app
