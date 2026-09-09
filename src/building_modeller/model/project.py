"""Save/load a modelling session (a batch of buildings + their state) as
JSON, so a batch can be worked on across multiple runs of the app before
export. ``session_to_payload``/``session_from_payload`` work with plain
dicts so the web API can serve/accept a session without going through a
file on the server's disk; ``save_session``/``load_session`` are thin
file-based wrappers around them.

Bumping ``SESSION_FORMAT_VERSION`` is deliberate: a session saved under an
older format (e.g. the old roof-type/height model, before geometry became
a directly editable mesh) fails to load with a clear error rather than
silently misinterpreting its fields -- see ``session_from_payload``.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import List

from shapely.geometry import mapping, shape

from .building import Building, ModellingStatus
from .mesh import mesh_from_payload, mesh_to_payload

SESSION_FORMAT_VERSION = 2


def _building_to_dict(building: Building) -> dict:
    return {
        "bag_id": building.bag_id,
        "footprint": mapping(building.footprint),
        "ground_height": building.ground_height,
        "geometry": mesh_to_payload(building.geometry) if building.geometry else None,
        "status": building.status.value,
        "lidar_stats": building.lidar_stats,
    }


def _building_from_dict(data: dict) -> Building:
    geometry = mesh_from_payload(data["geometry"]) if data.get("geometry") else None
    return Building(
        bag_id=data["bag_id"],
        footprint=shape(data["footprint"]),
        ground_height=data.get("ground_height", 0.0),
        geometry=geometry,
        status=ModellingStatus(data.get("status", ModellingStatus.UNMODELLED.value)),
        lidar_stats=data.get("lidar_stats"),
    )


def session_to_payload(buildings: List[Building]) -> dict:
    return {
        "format_version": SESSION_FORMAT_VERSION,
        "buildings": [_building_to_dict(b) for b in buildings],
    }


def session_from_payload(payload: dict) -> List[Building]:
    if payload.get("format_version") != SESSION_FORMAT_VERSION:
        raise ValueError(
            f"unsupported session format version: {payload.get('format_version')!r}"
        )
    return [_building_from_dict(b) for b in payload["buildings"]]


def save_session(buildings: List[Building], path: str) -> None:
    Path(path).write_text(json.dumps(session_to_payload(buildings), indent=2))


def load_session(path: str) -> List[Building]:
    return session_from_payload(json.loads(Path(path).read_text()))
