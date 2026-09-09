"""Best-effort persistence of the current modelling session between runs.

This is separate from the explicit "Save session" / "Load session"
download/upload flow (`/api/session` in `server.py`) -- that stays as
deliberate, user-controlled file handling. This module additionally
writes the same session payload to a fixed local file after every
state-mutating request, and reads it back once at server startup, so the
last session comes back automatically without the user having to
remember to save it.

A missing or corrupt autosave file is never fatal: `load()` returns an
empty list and `save()` silently gives up (after logging) rather than
breaking the request or the server startup.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import List

from ..model.building import Building
from ..model.project import session_from_payload, session_to_payload

logger = logging.getLogger(__name__)

#: Overridable (e.g. by tests via monkeypatch) path to the autosave file.
AUTOSAVE_PATH = Path.home() / ".building_modeller" / "last_session.json"


def save(buildings: List[Building]) -> None:
    try:
        AUTOSAVE_PATH.parent.mkdir(parents=True, exist_ok=True)
        AUTOSAVE_PATH.write_text(json.dumps(session_to_payload(buildings), indent=2))
    except Exception:
        logger.warning("Could not write autosave file %s", AUTOSAVE_PATH, exc_info=True)


def load() -> List[Building]:
    try:
        if not AUTOSAVE_PATH.exists():
            return []
        return session_from_payload(json.loads(AUTOSAVE_PATH.read_text()))
    except Exception:
        logger.warning("Could not read autosave file %s", AUTOSAVE_PATH, exc_info=True)
        return []
