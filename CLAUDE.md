# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

A locally-run Flask + Three.js tool for hand-modelling CityGML LOD2.2 buildings from
Dutch BAG footprints and AHN LiDAR. See README.md for what it does and why each
dependency was picked; this file covers what spans multiple files or is easy to break.

## Commands

```bash
pip install -e ".[dev]"            # Python 3.9.x only (pinned <3.10); numpy pinned <2
pytest                             # whole suite
pytest tests/test_terrain.py -q    # one file
pytest tests/test_terrain.py::TestBuildingHoles::test_filling_the_footprint_first_keeps_the_terrain_closed
pytest -k "classified or terrain"  # by name

PYTHONPATH=src pytest              # works without installing the package

BUILDING_MODELLER_NO_BROWSER=1 python -m building_modeller.app   # serves 127.0.0.1:5057
```

`pytest` is the only gate — no linter, formatter, type checker or CI. The lone dev
dependency is pytest.

## Architecture

**Geometry is one indexed mesh.** `model/mesh.py`'s `EditableMesh` is a single shared
vertex pool plus faces that reference it *by index*. That is the central invariant:
moving vertex 7 moves it everywhere it is used, so a roof eave corner and the wall
top below it are the same vertex and edits cannot tear the mesh apart. Every editing
operation (`move_vertex`, `split_edge`, `delete_vertex`, `split_face`, `extrude_face`)
preserves it. `Face.surface_type` (`wall`/`roof`/`ground`) is what
`export/citygml_writer.py` groups on to emit `WallSurface`/`RoofSurface`/`GroundSurface`;
an unrecognised value is silently dropped from the export.

`delete_vertex` **renumbers** every higher index, so any index the client is holding
is invalidated — the frontend deselects after a delete for exactly this reason.

**Two coordinate systems, one boundary.** Everything server-side is absolute RD New
(EPSG:28992), values in the hundreds of thousands. `web/meshutil.py` is the only place
that converts, subtracting `AppState.origin` on the way to the browser, because WebGL
buffers are float32 (~7 significant digits) and absolute coordinates would swamp
sub-metre detail. Vertex *indices* survive that conversion unchanged: the index the
browser sends to `POST /api/buildings/<id>/vertex/<i>` is the same index in
`mesh.vertices`. Vertex move requests arrive origin-relative and the server adds the
origin back.

**`AppState` is immutable once published.** Loading an area builds a whole new
`AppState` and rebinds the module global under `state_lock`; handlers bind
`snap = state` once at the top and read only from `snap`. This matters because the
area load runs on a background thread — mutating fields one at a time would let a
reader pair a new building list with the old origin, which (origin being the bbox
centre) renders every footprint kilometres off-screen. Mutating endpoints re-check
`state is snap` under the lock and 409 if a load landed underneath them.

**Area loading is a background job.** `POST /api/area` returns **202** with a
`job_id`; progress is polled at `GET /api/area/jobs/<job_id>`; a second load while one
runs is refused with **409**; `POST /api/area/jobs/<job_id>/cancel` sets a flag the
worker checks at phase boundaries. Jobs are addressed *by id* and the last few are
retained (`JOB_HISTORY`) so two browser tabs cannot read each other's results. The
job snapshot carries status and warnings only — the client fetches results from the
existing `/api/buildings`, `/api/pointcloud` and `/api/terrain` endpoints once it sees
`done`, so the worker's ordering is load-bearing: **swap state → autosave → mark done**.

> The worker runs **outside any Flask request context**. `abort()`, `jsonify()`,
> `request`, `current_app` and `app.logger` all raise `RuntimeError` there. They work
> fine under the inline test runner, so this only fails in the browser — the one
> threaded test exists to catch it.

**Terrain ordering is load-bearing.** `model/terrain.py` bins ground-class points into
a `GroundGrid` (median elevation per cell, NaN where none landed) and Delaunay-
triangulates the occupied cells. The pipeline order in `server._load_area` is not
arbitrary:

1. build the grid — it has a building-shaped hole under every footprint, because a
   roof occludes the ground returns beneath it;
2. per building, sample a ground height (the ring search interpolates across the
   hole) and **`fill_polygon` that footprint's cells** with it;
3. *then* triangulate and cull long edges.

Skip step 2 and the long-edge cull (needed so the TIN doesn't sheet across a river)
deletes exactly the triangles bridging each footprint, leaving buildings floating over
building-shaped voids. `tests/test_terrain.py::TestBuildingHoles` pins both directions.

Triangulation runs in **integer cell-index space**, not world space: uniform scaling
preserves a Delaunay triangulation, and integer coordinates make the output-vertex →
source-cell lookup exact rather than a float-equality gamble. GEOS does not guarantee
winding, so triangles are normalised to CCW or half the terrain renders black.

**Classification is optional everywhere.** `LidarPointCloud.classification` is a
4th, optional field. Roof heights come from class-6 (building) points and terrain from
class-2 (ground), but a LAZ with everything class 0/1 must still work: every consumer
falls back to the old percentile-over-everything behaviour and raises a warning.
`has_classification` treats an all-unassigned cloud as unclassified.

The terrain and point cloud are **derived, in-memory only** — neither is exported to
CityGML nor saved in sessions. Re-run *Load area* to rebuild them.

## Traps

- **`[hidden]` needs the reset in `app.css`.** The attribute is honoured only by the
  UA rule `[hidden]{display:none}` at specificity 0,1,0, so any id- or class-level
  `display` silently beats it. `#loading-overlay { display: flex }` once made the
  loading overlay impossible to close. `[hidden] { display: none !important }` at the
  top of the stylesheet fixes it globally; `!important` is required, since a plain
  rule loses to an id selector wherever it sits. `test_stylesheet_keeps_the_hidden_reset`
  guards it.
- **Deriving a `LidarPointCloud` must carry classification.** `empty()`,
  `crop_to_bbox`, `points_in_polygon` and `filter_classes` all construct one; missing
  the 4th argument drops classes silently. Use `_subset(mask)`. The dataclass is
  `eq=False, repr=False` on purpose — the generated `__eq__` compares ndarrays and the
  generated `__repr__` would dump millions of points into a traceback.
- **Bumping `SESSION_FORMAT_VERSION`** (`model/project.py`) invalidates every saved
  and autosaved session — the check is strict equality with no migration path. It has
  been bumped once already; avoid a second break unless the payload really changed.
- **`autosave.save()` writes to `~/.building_modeller/last_session.json`** on every
  mutating request. Tests must monkeypatch `autosave_module.AUTOSAVE_PATH`.

## Testing conventions

Plain `class TestX:` groupings, `monkeypatch` only (no `unittest.mock`), seeded
`np.random.default_rng`, `tmp_path` for files, real XML parsing with XPath for export
assertions. The single fixture is `client` in `tests/test_web_api.py`, which patches
`AUTOSAVE_PATH`, runs jobs inline and resets `server_module.state` to a fresh
`AppState`.

`/api/area` tests go through `server._run_in_background`, replaced three ways:
inline (`lambda t: t()`) for determinism, a no-op (`lambda t: None`) to test the 409
guard without threads, and one genuinely threaded test. That last one must join its
thread before finishing — a worker outliving its test runs with the monkeypatches
undone, calling the live PDOK service and overwriting the developer's real autosave
file.

## Frontend

`web/static/js/app.js` is one IIFE — no modules, no bundler, no npm. three.js **r128**
and OrbitControls are vendored as globals in `static/vendor/`. The scene is **Z-up**
(`camera.up.set(0, 0, 1)`), matching RD/NAP.

`showWarnings()` is the only user-facing message channel and it *replaces* the list
rather than appending. `applyBuildingsResult()` unconditionally overwrites
`#viewport-hint`, which is why loading progress has its own element.

## Dormant code

`model/roofshapes.py` (parametric flat/shed/gable/hip/pyramid generators) and
`data/bgt_client.py` (PDOK BGT context layers) are **not wired into the app** — no
importers outside their own tests. They are kept and still tested. `mesh.seed_flat_box`
is the only shape generator the live app uses.
