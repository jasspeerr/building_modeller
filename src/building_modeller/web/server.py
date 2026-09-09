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
from . import autosave
from .meshutil import mesh_to_render_data

MAX_VIEWER_POINTS = 200_000

#: Search radius (meters) for "snap selected vertex to LiDAR".
LIDAR_SNAP_RADIUS = 1.0


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


def _building_summary(building: Building) -> dict:
    ox, oy, _ = state.origin
    ring = list(building.footprint.exterior.coords)[:-1]
    mesh = building.mesh()
    return {
        "bag_id": building.bag_id,
        "status": building.status.value,
        "ground_height": building.ground_height,
        "lidar_stats": building.lidar_stats,
        "vertex_count": len(mesh.vertices),
        "footprint": [[x - ox, y - oy] for x, y in ring],
    }


def _origin_from_buildings(buildings: List[Building]) -> Tuple[float, float, float]:
    if not buildings:
        return (0.0, 0.0, 0.0)
    minx = min(b.footprint.bounds[0] for b in buildings)
    miny = min(b.footprint.bounds[1] for b in buildings)
    maxx = max(b.footprint.bounds[2] for b in buildings)
    maxy = max(b.footprint.bounds[3] for b in buildings)
    return ((minx + maxx) / 2.0, (miny + maxy) / 2.0, 0.0)


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

    restored = autosave.load()
    if restored:
        state.buildings = restored
        state.origin = _origin_from_buildings(restored)

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
            b.mesh()  # seed the initial flat-box geometry now that heights are known

        minx, miny, maxx, maxy = bbox
        origin = ((minx + maxx) / 2.0, (miny + maxy) / 2.0, 0.0)

        with state_lock:
            state.buildings = buildings
            state.lidar_cloud = lidar_cloud
            state.origin = origin
        autosave.save(buildings)

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
        return jsonify(mesh_to_render_data(building.mesh(), state.origin))

    @app.post("/api/buildings/<bag_id>/vertex/<int:index>")
    def move_vertex(bag_id: str, index: int):
        building = _find_building(bag_id)
        data = request.get_json(force=True)
        ox, oy, oz = state.origin
        try:
            position = (
                float(data["x"]) + ox,
                float(data["y"]) + oy,
                float(data["z"]) + oz,
            )
        except (KeyError, TypeError, ValueError):
            abort(400, "expected numeric x, y, z")

        with state_lock:
            try:
                building.move_vertex(index, position)
            except IndexError as exc:
                abort(404, str(exc))
        autosave.save(state.buildings)
        return jsonify(
            {"building": _building_summary(building), "mesh": mesh_to_render_data(building.mesh(), state.origin)}
        )

    @app.post("/api/buildings/<bag_id>/vertex/<int:index>/snap_lidar")
    def snap_vertex_to_lidar(bag_id: str, index: int):
        building = _find_building(bag_id)
        mesh = building.mesh()
        if not (0 <= index < len(mesh.vertices)):
            abort(404, f"vertex index {index} out of range")

        x, y, _z = mesh.vertices[index]
        hit = state.lidar_cloud.height_near(x, y, radius=LIDAR_SNAP_RADIUS)
        if hit is None:
            abort(404, "no LiDAR points found near this vertex")

        with state_lock:
            building.move_vertex(index, (x, y, hit["z"]))
        autosave.save(state.buildings)
        return jsonify(
            {
                "building": _building_summary(building),
                "mesh": mesh_to_render_data(building.mesh(), state.origin),
                "snapped_z": hit["z"],
                "point_count": hit["point_count"],
            }
        )

    @app.post("/api/buildings/<bag_id>/vertex/<int:index>/delete")
    def delete_vertex(bag_id: str, index: int):
        building = _find_building(bag_id)
        with state_lock:
            try:
                building.delete_vertex(index)
            except IndexError as exc:
                abort(404, str(exc))
        autosave.save(state.buildings)
        return jsonify(
            {"building": _building_summary(building), "mesh": mesh_to_render_data(building.mesh(), state.origin)}
        )

    @app.post("/api/buildings/<bag_id>/edge/split")
    def split_edge(bag_id: str):
        building = _find_building(bag_id)
        data = request.get_json(force=True)
        try:
            index_a = int(data["index_a"])
            index_b = int(data["index_b"])
        except (KeyError, TypeError, ValueError):
            abort(400, "expected integer index_a, index_b")

        with state_lock:
            try:
                new_index = building.split_edge(index_a, index_b)
            except (IndexError, ValueError) as exc:
                abort(400, str(exc))
        autosave.save(state.buildings)
        return jsonify(
            {
                "building": _building_summary(building),
                "mesh": mesh_to_render_data(building.mesh(), state.origin),
                "new_vertex_index": new_index,
            }
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

        origin = _origin_from_buildings(buildings)

        with state_lock:
            state.buildings = buildings
            state.lidar_cloud = LidarPointCloud.empty()
            state.origin = origin
        autosave.save(buildings)

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
