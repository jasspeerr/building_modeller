"""A local, single-user Flask app: a thin REST API in front of the same
model/data/export logic the original desktop UI used, plus a static
Three.js frontend.

State (the currently loaded batch of buildings, LiDAR point cloud and
terrain) lives in a single process-wide ``AppState`` -- this is a local,
one-user-at-a-time tool, so there is no per-session isolation, matching
how the original desktop app held state in its main window.

``AppState`` is treated as **immutable once published**: loading an area
builds a whole new instance and rebinds the module-level ``state`` under
the lock, and every handler binds ``snap = state`` once at the top and
reads only from that. Before area loading moved onto a background thread
this did not matter much, because the load blocked the browser for its
whole duration and nothing else was ever in flight. Now that it doesn't,
mutating fields one at a time would let a reader pair the new building
list with the old origin -- and since the origin is the bbox centre, that
renders every footprint kilometres off-screen.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Tuple

from flask import Flask, Response, abort, jsonify, request, send_from_directory

from ..data.bag_client import fetch_pand_footprints
from ..data.pointcloud import (
    CLASS_BUILDING,
    NOISE_CLASSES,
    VEGETATION_CLASSES,
    LidarPointCloud,
    find_tiles,
    stats_for_footprint,
)
from ..export.citygml_writer import write_citygml
from ..model.building import Building, ModellingStatus
from ..model.project import session_from_payload, session_to_payload
from ..model.terrain import GroundGrid, TerrainMesh, build_ground_grid, triangulate
from . import autosave
from .meshutil import mesh_to_render_data, terrain_to_render_data

logger = logging.getLogger(__name__)

MAX_VIEWER_POINTS = 200_000

#: Search radius (meters) for "snap selected vertex to LiDAR".
LIDAR_SNAP_RADIUS = 1.0

#: How many finished jobs stay readable, so a poller whose job was just
#: superseded by another tab still gets its own result rather than a 404.
JOB_HISTORY = 4


@dataclass
class AppState:
    buildings: List[Building] = field(default_factory=list)
    lidar_cloud: LidarPointCloud = field(default_factory=LidarPointCloud.empty)
    origin: Tuple[float, float, float] = (0.0, 0.0, 0.0)
    terrain: Optional[TerrainMesh] = None
    ground_grid: Optional[GroundGrid] = None
    #: Bumped on every area load, so an edit that was in flight across a
    #: load can tell it is targeting geometry that no longer exists.
    generation: int = 0


state = AppState()
state_lock = threading.Lock()


class Cancelled(Exception):
    """Raised inside the area worker when the user cancels."""


class AreaJob:
    """Progress for one ``/api/area`` run.

    The worker only ever *rebinds* ``_snap`` to a fresh dict and pollers
    only ever read it once, so neither side needs a lock: a single
    attribute rebind is atomic under the GIL, and a reader can therefore
    never catch a half-updated set of fields (``progress=90`` paired with
    ``phase="Fetching BAG footprints"``).
    """

    def __init__(self, job_id: str):
        self.id = job_id
        self.cancel_requested = False
        self._snap = {
            "state": "running",
            "phase": "Starting",
            "progress": 0,
            "determinate": False,
            "message": "",
            "warnings": [],
            "error": None,
        }

    def snapshot(self) -> dict:
        return self._snap

    @property
    def running(self) -> bool:
        return self._snap["state"] == "running"

    def publish(self, **changes) -> None:
        new = dict(self._snap)
        new.update(changes)
        # Never let the bar go backwards, whatever the caller asked for.
        new["progress"] = max(self._snap["progress"], new["progress"])
        self._snap = new

    def warn(self, message: str) -> None:
        """Surface a warning immediately, rather than at the end of the run --
        'BAG fetch failed' should not wait behind a two-minute LiDAR load."""
        self.publish(warnings=list(self._snap["warnings"]) + [message])


#: Progress bands: phase -> (start %, end %, determinate). The LAZ decode
#: gets the widest band because measurement says that is where the time
#: actually goes; the per-building pass is ~1.3s for 144 buildings.
_BANDS = {
    "bag": (0, 8, False),
    "lidar": (8, 55, True),
    "grid": (55, 68, False),
    "buildings": (68, 86, True),
    "terrain": (86, 100, False),
}

_PHASE_LABELS = {
    "bag": "Fetching BAG footprints",
    "lidar": "Loading LiDAR tiles",
    "grid": "Building ground grid",
    "buildings": "Generating building models",
    "terrain": "Triangulating terrain",
}


class _Bar:
    """Maps (band, fraction within band) onto one monotonic 0-100."""

    def __init__(self, job: AreaJob):
        self.job = job

    def enter(self, band: str, message: str = "") -> None:
        lo, _hi, determinate = _BANDS[band]
        self.job.publish(
            phase=_PHASE_LABELS[band], progress=lo, determinate=determinate, message=message
        )

    def within(self, band: str, done: float, total: float, message: str = "") -> None:
        lo, hi, _ = _BANDS[band]
        frac = 1.0 if total <= 0 else min(1.0, done / float(total))
        self.job.publish(progress=int(lo + (hi - lo) * frac), message=message)

    def finish(self, band: str) -> None:
        _lo, hi, _ = _BANDS[band]
        self.job.publish(progress=hi)


_job_lock = threading.Lock()
_jobs: "OrderedDict[str, AreaJob]" = OrderedDict()
_current_job_id: Optional[str] = None


def _run_in_background(target: Callable[[], None]):
    """Start the area worker. Tests replace this to run inline.

    Returns the thread so a caller (a test) can join it -- a worker that
    outlives its test would run with the monkeypatches undone, hitting the
    live PDOK service and overwriting the developer's real autosave file.
    """
    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread


def _find_building(buildings: List[Building], bag_id: str) -> Building:
    for b in buildings:
        if b.bag_id == bag_id:
            return b
    abort(404, f"unknown building: {bag_id}")


def _building_summary(building: Building, origin: Tuple[float, float, float]) -> dict:
    ox, oy, _ = origin
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


def _point_cloud_payload(
    cloud: LidarPointCloud,
    origin: Tuple[float, float, float],
    hide_vegetation: bool = False,
) -> dict:
    """Decimate the cloud down to something a browser can draw.

    Vegetation/noise filtering happens *before* decimation on purpose: it
    is the only way hiding them actually buys more of the display budget
    for the points that matter, rather than just drawing fewer points.
    """
    if hide_vegetation:
        cloud = cloud.filter_classes(exclude=list(VEGETATION_CLASSES) + list(NOISE_CLASSES))
    n = len(cloud)
    if n == 0:
        return {"x": [], "y": [], "z": [], "classification": [], "total_points": 0, "shown_points": 0}
    step = max(1, n // MAX_VIEWER_POINTS)
    idx = slice(0, n, step)
    ox, oy, oz = origin
    return {
        "x": (cloud.x[idx] - ox).tolist(),
        "y": (cloud.y[idx] - oy).tolist(),
        "z": (cloud.z[idx] - oz).tolist(),
        "classification": cloud.classes[idx].tolist(),
        "total_points": n,
        "shown_points": len(range(*idx.indices(n))),
    }


def _load_area(job: AreaJob, bbox: tuple, lidar_folder: str) -> AppState:
    """The actual area pipeline. Runs on a worker thread, so it must not
    touch anything request-scoped -- no ``abort``, no ``jsonify``, no
    ``request``, no ``current_app``, no ``app.logger``."""
    bar = _Bar(job)

    bar.enter("bag")
    try:
        gdf = fetch_pand_footprints(bbox)
    except Exception as exc:
        job.warn(f"BAG fetch failed: {exc}")
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
    bar.finish("bag")
    _check_cancelled(job)

    lidar_cloud = LidarPointCloud.empty()
    if lidar_folder:
        bar.enter("lidar")
        try:
            tiles = find_tiles(lidar_folder)
            if not tiles:
                job.warn(f"No .laz/.las files found in: {lidar_folder}")
            else:
                lidar_cloud = _load_tiles(job, bar, tiles, bbox)
        except Cancelled:
            raise
        except Exception as exc:
            job.warn(f"LiDAR load failed: {exc}")
    bar.finish("lidar")
    _check_cancelled(job)

    bar.enter("grid")
    ground_grid = build_ground_grid(lidar_cloud, bbox)
    if (
        lidar_folder
        and len(lidar_cloud) > 0
        and ground_grid is None
        # An unclassified cloud already warned about itself while loading;
        # this one is specifically "classified, but nothing is ground".
        and lidar_cloud.has_classification
    ):
        job.warn(
            "No ground-classified LiDAR points in this area; no terrain was built "
            "and heights fall back to percentiles."
        )
    bar.finish("grid")
    _check_cancelled(job)

    bar.enter("buildings")
    for i, b in enumerate(buildings):
        if i % 25 == 0:
            _check_cancelled(job)
            bar.within("buildings", i, len(buildings), f"{i} / {len(buildings)}")
        ground = None
        if ground_grid is not None:
            ground = ground_grid.sample(*b.footprint.centroid.coords[0])
        stats = stats_for_footprint(lidar_cloud, b.footprint, ground_height=ground)
        b.lidar_stats = stats
        if stats:
            b.ground_height = stats["ground_height"]
        elif ground is not None:
            b.ground_height = ground
        if ground_grid is not None:
            # Close the ground-return hole this building punches, before the
            # TIN is built -- otherwise the long-edge cull deletes exactly
            # the triangles that bridge under it. See model/terrain.py.
            ground_grid.fill_polygon(b.footprint, b.ground_height)
        b.mesh()  # seed the flat-box geometry now that heights are known
    bar.finish("buildings")
    _check_cancelled(job)

    terrain = None
    if ground_grid is not None:
        bar.enter("terrain")
        terrain = triangulate(ground_grid)
        if terrain is None:
            job.warn("Terrain could not be triangulated (too few ground cells).")

    minx, miny, maxx, maxy = bbox
    return AppState(
        buildings=buildings,
        lidar_cloud=lidar_cloud,
        origin=((minx + maxx) / 2.0, (miny + maxy) / 2.0, 0.0),
        terrain=terrain,
        ground_grid=ground_grid,
        generation=state.generation + 1,
    )


def _load_tiles(job: AreaJob, bar: _Bar, tiles: List[str], bbox: tuple) -> LidarPointCloud:
    """Load tiles one at a time, weighting progress by file size.

    Bytes are a far better proxy for decode time than tile count -- AHN
    tile density varies enormously -- and ``os.path.getsize`` is free.
    """
    sizes = [os.path.getsize(p) for p in tiles]
    total_bytes = float(sum(sizes)) or 1.0
    done_bytes = 0.0
    clouds: List[LidarPointCloud] = []
    warnings: List[str] = []

    for path, size in zip(tiles, sizes):
        _check_cancelled(job)
        name = os.path.basename(path)

        def on_chunk(read: int, total: int, _size=size, _base=done_bytes, _name=name) -> None:
            share = _base + _size * (read / float(total or 1))
            bar.within("lidar", share, total_bytes, f"{_name} ({read:,} pts)")

        clouds.append(
            LidarPointCloud.from_files(
                [path], bbox=bbox, on_progress=on_chunk, warnings=warnings
            )
        )
        done_bytes += size
        bar.within("lidar", done_bytes, total_bytes, name)

    for message in warnings:
        job.warn(message)
    clouds = [c for c in clouds if len(c) > 0]
    if not clouds:
        return LidarPointCloud.empty()
    if len(clouds) == 1:
        return clouds[0]
    import numpy as np

    return LidarPointCloud(
        np.concatenate([c.x for c in clouds]),
        np.concatenate([c.y for c in clouds]),
        np.concatenate([c.z for c in clouds]),
        np.concatenate([c.classes for c in clouds]),
    )


def _check_cancelled(job: AreaJob) -> None:
    if job.cancel_requested:
        raise Cancelled()


def _area_worker(job: AreaJob, bbox: tuple, lidar_folder: str) -> None:
    try:
        new_state = _load_area(job, bbox, lidar_folder)
        global state
        with state_lock:
            state = new_state
        autosave.save(new_state.buildings)
        # Marked done *after* the swap and save, so a client that sees
        # "done" and immediately GETs /api/buildings cannot read stale state.
        job.publish(state="done", phase="Done", progress=100, determinate=True, message="")
    except Cancelled:
        job.publish(state="cancelled", phase="Cancelled", message="")
    except Exception as exc:  # noqa: BLE001 - a worker thread must not die silently
        logger.exception("area load failed")
        job.publish(state="error", error=f"{type(exc).__name__}: {exc}")
    finally:
        # A job that somehow left itself running would wedge /api/area on a
        # permanent 409 with no way out from the UI.
        if job.running:
            job.publish(state="error", error="worker exited without a result")


def create_app() -> Flask:
    app = Flask(__name__, static_folder="static", static_url_path="/static")

    global state
    restored = autosave.load()
    if restored:
        state = AppState(buildings=restored, origin=_origin_from_buildings(restored))

    @app.get("/")
    def index():
        return send_from_directory(app.static_folder, "index.html")

    @app.post("/api/area")
    def load_area():
        payload = request.get_json(force=True)
        try:
            bbox = tuple(float(v) for v in payload["bbox"])
        except (KeyError, TypeError, ValueError):
            abort(400, "expected a numeric bbox [minx, miny, maxx, maxy]")
        if len(bbox) != 4:
            abort(400, "expected a numeric bbox [minx, miny, maxx, maxy]")
        lidar_folder = (payload.get("lidar_folder") or "").strip()

        global _current_job_id
        with _job_lock:
            # Check-and-set must be atomic: app.run() is threaded, so two
            # POSTs genuinely race and both can pass a lock-free check.
            current = _jobs.get(_current_job_id) if _current_job_id else None
            if current is not None and current.running:
                return (
                    jsonify({"error": "an area load is already running", "job_id": current.id}),
                    409,
                )
            job = AreaJob(uuid.uuid4().hex)
            _jobs[job.id] = job
            while len(_jobs) > JOB_HISTORY:
                _jobs.popitem(last=False)
            _current_job_id = job.id

        _run_in_background(lambda: _area_worker(job, bbox, lidar_folder))
        return jsonify({"job_id": job.id}), 202

    @app.get("/api/area/jobs/<job_id>")
    def area_job(job_id: str):
        job = _jobs.get(job_id)
        if job is None:
            # Also what a poller sees after a server restart; the frontend
            # treats it as "job lost" and re-syncs from /api/buildings.
            abort(404, "unknown or expired job")
        return jsonify(dict(job.snapshot(), job_id=job.id))

    @app.post("/api/area/jobs/<job_id>/cancel")
    def cancel_area_job(job_id: str):
        job = _jobs.get(job_id)
        if job is None:
            abort(404, "unknown or expired job")
        job.cancel_requested = True
        return jsonify(dict(job.snapshot(), job_id=job.id))

    @app.get("/api/buildings")
    def list_buildings():
        snap = state
        return jsonify(
            {
                "origin": list(snap.origin),
                "buildings": [_building_summary(b, snap.origin) for b in snap.buildings],
                "generation": snap.generation,
            }
        )

    @app.get("/api/buildings/<bag_id>")
    def get_building(bag_id: str):
        snap = state
        return jsonify(_building_summary(_find_building(snap.buildings, bag_id), snap.origin))

    @app.get("/api/buildings/<bag_id>/mesh")
    def get_mesh(bag_id: str):
        snap = state
        building = _find_building(snap.buildings, bag_id)
        return jsonify(mesh_to_render_data(building.mesh(), snap.origin))

    def _edit_response(snap: AppState, building: Building, **extra) -> dict:
        autosave.save(snap.buildings)
        return dict(
            building=_building_summary(building, snap.origin),
            mesh=mesh_to_render_data(building.mesh(), snap.origin),
            **extra,
        )

    def _guard_generation(snap: AppState) -> None:
        """Refuse an edit aimed at a batch that has since been replaced."""
        if state is not snap:
            abort(409, "the loaded area changed; reload before editing")

    @app.post("/api/buildings/<bag_id>/vertex/<int:index>")
    def move_vertex(bag_id: str, index: int):
        snap = state
        building = _find_building(snap.buildings, bag_id)
        data = request.get_json(force=True)
        ox, oy, oz = snap.origin
        try:
            position = (
                float(data["x"]) + ox,
                float(data["y"]) + oy,
                float(data["z"]) + oz,
            )
        except (KeyError, TypeError, ValueError):
            abort(400, "expected numeric x, y, z")

        with state_lock:
            _guard_generation(snap)
            try:
                building.move_vertex(index, position)
            except IndexError as exc:
                abort(404, str(exc))
        return jsonify(_edit_response(snap, building))

    @app.post("/api/buildings/<bag_id>/vertex/<int:index>/snap_lidar")
    def snap_vertex_to_lidar(bag_id: str, index: int):
        snap = state
        building = _find_building(snap.buildings, bag_id)
        mesh = building.mesh()
        if not (0 <= index < len(mesh.vertices)):
            abort(404, f"vertex index {index} out of range")

        x, y, _z = mesh.vertices[index]
        # Prefer building-class points so a roof corner cannot snap onto a
        # tree overhanging it; height_near falls back to all points when
        # there is nothing of that class in range.
        hit = snap.lidar_cloud.height_near(
            x, y, radius=LIDAR_SNAP_RADIUS, classes=(CLASS_BUILDING,)
        )
        if hit is None:
            abort(404, "no LiDAR points found near this vertex")

        with state_lock:
            _guard_generation(snap)
            building.move_vertex(index, (x, y, hit["z"]))
        return jsonify(
            _edit_response(
                snap,
                building,
                snapped_z=hit["z"],
                point_count=hit["point_count"],
                matched_class=hit["matched_class"],
            )
        )

    @app.post("/api/buildings/<bag_id>/vertex/<int:index>/delete")
    def delete_vertex(bag_id: str, index: int):
        snap = state
        building = _find_building(snap.buildings, bag_id)
        with state_lock:
            _guard_generation(snap)
            try:
                building.delete_vertex(index)
            except IndexError as exc:
                abort(404, str(exc))
        return jsonify(_edit_response(snap, building))

    @app.post("/api/buildings/<bag_id>/edge/split")
    def split_edge(bag_id: str):
        snap = state
        building = _find_building(snap.buildings, bag_id)
        index_a, index_b = _vertex_pair(request.get_json(force=True))
        with state_lock:
            _guard_generation(snap)
            try:
                new_index = building.split_edge(index_a, index_b)
            except (IndexError, ValueError) as exc:
                abort(400, str(exc))
        return jsonify(_edit_response(snap, building, new_vertex_index=new_index))

    @app.post("/api/buildings/<bag_id>/face/split")
    def split_face(bag_id: str):
        snap = state
        building = _find_building(snap.buildings, bag_id)
        index_a, index_b = _vertex_pair(request.get_json(force=True))
        with state_lock:
            _guard_generation(snap)
            try:
                building.split_face(index_a, index_b)
            except (IndexError, ValueError) as exc:
                abort(400, str(exc))
        return jsonify(_edit_response(snap, building))

    @app.post("/api/buildings/<bag_id>/face/extrude")
    def extrude_face(bag_id: str):
        snap = state
        building = _find_building(snap.buildings, bag_id)
        data = request.get_json(force=True)
        index_a, index_b = _vertex_pair(data)
        try:
            distance = float(data["distance"])
        except (KeyError, TypeError, ValueError):
            abort(400, "expected integer index_a, index_b and numeric distance")

        with state_lock:
            _guard_generation(snap)
            try:
                building.extrude_face(index_a, index_b, distance)
            except (IndexError, ValueError) as exc:
                abort(400, str(exc))
        return jsonify(_edit_response(snap, building))

    @app.get("/api/pointcloud")
    def get_point_cloud():
        snap = state
        hide = request.args.get("hide_vegetation") in ("1", "true", "yes")
        return jsonify(_point_cloud_payload(snap.lidar_cloud, snap.origin, hide_vegetation=hide))

    @app.get("/api/terrain")
    def get_terrain():
        snap = state
        return jsonify(terrain_to_render_data(snap.terrain, snap.origin))

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
        global state
        with state_lock:
            # A fresh AppState, so the previous area's cloud, terrain and
            # ground grid cannot survive into the uploaded session.
            state = AppState(
                buildings=buildings, origin=origin, generation=state.generation + 1
            )
        autosave.save(buildings)
        return jsonify(
            {
                "origin": list(origin),
                "buildings": [_building_summary(b, origin) for b in buildings],
                "generation": state.generation,
            }
        )

    @app.post("/api/export")
    def export_citygml_route():
        snap = state
        if not snap.buildings:
            abort(400, "no buildings loaded")
        data = write_citygml(snap.buildings)
        with state_lock:
            for b in snap.buildings:
                b.status = ModellingStatus.EXPORTED
        return Response(
            data,
            mimetype="application/gml+xml",
            headers={"Content-Disposition": "attachment; filename=buildings.gml"},
        )

    return app


def _vertex_pair(data: dict) -> Tuple[int, int]:
    try:
        return int(data["index_a"]), int(data["index_b"])
    except (KeyError, TypeError, ValueError):
        abort(400, "expected integer index_a, index_b")
