# Building Modeller

A locally-run web app to model 3D CityGML LOD2.2 buildings from Dutch BAG
footprints, using AHN6 LiDAR point clouds as a visual reference, and
export the result as a static CityGML 2.0 file. It's a small Flask server
plus a browser-based Three.js viewer -- everything runs on your own
machine, nothing is served publicly.

**Every building starts as a simple flat box and is shaped by hand** --
select a vertex and drag it (or type exact coordinates), snapping it to
the LiDAR point cloud when you want the real surface. There is no
roof-type picker and no automatic plane-fitting reconstruction (see
"Roadmap" below); LiDAR height statistics only seed the box's starting
height and are available as an explicit per-vertex snap.

The seeded box is as close to reality as blind heights can get it: when
the LAZ carries ASPRS classification (AHN does), the roof height comes
from **building-class points only** -- so a tree overhanging a footprint
no longer inflates it -- and the ground height is sampled from a
**triangulated terrain model** built from the ground-class points, since a
building occludes the ground directly beneath it. That terrain is drawn
under the buildings in the viewport.

> This started as a PySide6 desktop GUI, but PySide6/shiboken6's compiled
> Qt bindings turned out to be too fragile on a locked-down Windows
> machine with no usable virtual environment (mismatched/missing C++
> runtime DLLs with no reliable fix available). The browser-based version
> below only replaces that presentation layer -- all the actual modelling
> logic (`model/`, `data/`, `export/`) is unchanged and still fully
> covered by the test suite.

## Requirements

- Python 3.9 (3.9.x only -- see the dependency notes below)
- pip, with the ability to install packages for your user
  (`pip install --user ...`); no `sudo`/system package manager access is
  needed for anything in `requirements.txt`
- A modern browser (Chrome, Edge, Firefox) with WebGL support

## Install

```bash
python3.9 -m venv .venv
source .venv/bin/activate   # or .venv\Scripts\activate on Windows
pip install -e .
```

If you can't create/use a virtual environment (e.g. a locked-down
machine), `pip install --user -e .` into your global environment works
too -- there's no compiled GUI toolkit in this stack anymore, so the
class of DLL/binary-compatibility problems a global Python environment
can accumulate over time is much smaller than it was with PySide6.

This intentionally avoids the heaviest, most permission-sensitive
geospatial dependencies:

| Need | What we use | Why not the "usual" choice |
|---|---|---|
| GUI / 3D viewport | Browser + [Three.js](https://threejs.org/) (vendored in `web/static/vendor/`) | No native GUI toolkit at all -- avoids PySide6/Qt, VTK (PyVista), and Open3D, all much larger and more fragile installs |
| Local server | `Flask` | Pure Python, tiny dependency tree, no compiled extensions |
| BAG/BGT footprints | `geopandas` + `pyogrio` | `pyogrio` wheels bundle GDAL -- no system GDAL/PROJ install needed |
| AHN6 LiDAR (LAZ) | `laspy[lazrs]` | Pure-wheel LAZ decompression -- avoids PDAL entirely |
| CityGML export | `lxml` | Hand-written writer; no maintained CityGML library exists |

All of the above ship prebuilt wheels for Python 3.9 on Linux/macOS/Windows.

## Running

```bash
python -m building_modeller.app
```

This starts a local server at `http://127.0.0.1:5057/` and opens it in
your default browser automatically. Set `BUILDING_MODELLER_NO_BROWSER=1`
to skip the auto-open (e.g. on a headless machine) and navigate there
yourself. Stop the server with Ctrl+C in the terminal it's running in.

## Workflow

1. **Area** (left panel): enter a bounding box in RD New (EPSG:28992)
   coordinates, and optionally an **AHN6 LAZ folder** path -- this is a
   path *on the machine running the server*, not a browser file upload,
   since AHN6 tiles are typically too large to upload through a browser
   and the server already has local filesystem access. Click **Load area
   (BAG + LiDAR)**.
   Loading runs in the background with a progress overlay over the
   viewport, naming each phase and showing a bar (with a **Cancel** button
   -- a folder full of tiles can take minutes, and cancelling beats
   restarting the server). Phases that genuinely can't be measured, like
   the PDOK request, say so with a moving bar rather than inventing a
   percentage. The phases are:
   - BAG building footprints for the bbox are fetched live from the
     [PDOK](https://www.pdok.nl/) BAG WFS.
   - Point cloud files in the chosen folder are loaded and cropped to the
     bbox. Tiles whose own header bounds fall outside the bbox are skipped
     without decompressing them. This is normally the slowest phase, so
     its progress is weighted by file size and reported per tile.
   - A **terrain model** is built from the ground-classified points: their
     median elevation per 2m cell, Delaunay-triangulated into a TIN.
   - Each building's model is pre-generated: the roof height from the
     building-class points inside its footprint, the ground height sampled
     from the terrain, and a flat box seeded between them.
   - Fetch/load problems (PDOK unreachable, no tiles found, unclassified
     LiDAR, etc.) show up as warnings under the form rather than failing
     the whole page -- and appear as soon as they happen, not at the end.
2. **Buildings** (left panel list): click a building. Its live 3D mesh
   preview appears in the viewport, alongside the terrain, the LiDAR point
   cloud and all footprint outlines as a visual reference. The **Point
   cloud** panel colours the cloud by elevation or by classification
   (ground / building / vegetation / water / noise), and can hide
   vegetation and noise entirely -- that filtering happens server-side
   before the display budget is applied, so hiding trees buys you more of
   the points you actually care about rather than just drawing fewer.
3. **Shape it** (viewport + right panel): the building starts as a flat
   box seeded from its BAG footprint and a LiDAR-suggested height. Small
   markers show every vertex (wall corners, roof corners) -- click one to
   select it, or click-and-drag it vertically (hold and move the mouse up/
   down) to push/pull it in real time. The right panel shows the selected
   vertex's exact X/Y/Z (editable directly) and which surfaces it belongs
   to (wall/roof/ground); **Snap to LiDAR (Z)** sets its height to the
   median of nearby LiDAR points (within 1m) instead of eyeballing it --
   preferring building-class points, so a roof corner can't snap onto a
   branch overhanging it, and falling back to all nearby points when
   there's nothing classified as building in range.
   There's no roof-type picker -- shape roofs, walls, anything, by editing
   the vertices that make them up:
   - **Delete vertex** removes the selected vertex from every face it's
     part of (a face that would collapse below 3 points is dropped).
   - **Shift+click a second vertex** to select a pair, shown in the
     "Selected pair" panel, offering whichever of these applies:
     - **Split edge** (if the two are directly connected by an edge) --
       inserts a new vertex at its midpoint, e.g. split both long edges
       of a roof face at their midpoints, then drag each new midpoint up
       to form a ridge.
     - **Split face into two** (if the two share a face as a diagonal,
       not an edge) -- e.g. split a roof plane into two separate
       pitches along that diagonal, ready to angle independently.
     - **Extrude face** (same diagonal-sharing condition, with a
       distance field) -- pushes that face outward along its own normal,
       connecting the original boundary to the new position with fresh
       wall faces. Combine with the above to carve out and pop up a
       dormer: split off a small sub-region of the roof, then extrude
       just that piece.
4. Repeat for the buildings you care about. Buildings you never touch stay
   as their seeded flat box, so a batch export never silently drops one --
   check the status tag (`unmodelled` / `edited` / `exported`) in the list
   to see what still needs attention.
5. **Save session** downloads a JSON file with the batch's modelling
   state; **Load session** re-uploads one to continue later (this restores
   footprints/geometry only, not the point cloud or terrain). This is
   separate from -- and in addition to -- automatic persistence: the app
   also autosaves the current batch to `~/.building_modeller/last_session.json`
   after every area load, vertex edit, or session upload, and restores it
   automatically the next time you open the page or restart the server
   (again without the cloud or terrain -- reload the area for those).
6. **Export CityGML** downloads one CityGML 2.0 file for every building
   currently loaded, with `WallSurface`/`RoofSurface`/`GroundSurface`
   semantic surfaces and a `lod2Solid` per building (CRS: EPSG:28992). The
   terrain is *not* exported -- it is a viewport aid only. No schema is
   bundled to validate against; open the result in QGIS or the
   [FZK Viewer](https://www.iai.kit.edu/1302.php) to sanity-check it, or
   validate with `xmllint --schema` against the official CityGML 2.0
   XSDs if you need strict validation.

## Known MVP limitations

- **Footprint holes** (courtyard buildings) are not supported -- only the
  exterior ring of a footprint is used to seed a building's starting box
  (you can still shape the box's own vertices freely afterward).
- **The terrain is a viewport aid and is never exported.** The CityGML
  output is buildings only -- no `dem:ReliefFeature`/`TINRelief`. It is
  also not saved in sessions: like the point cloud it is derived data, so
  re-running *Load area* rebuilds it rather than bloating every session
  file. (This kept `SESSION_FORMAT_VERSION` at 2, so existing sessions
  still load.)
- **Terrain resolution gives way before triangle count.** Cells are 2m,
  but `MAX_TERRAIN_CELLS` in `model/terrain.py` is really a browser
  triangle budget (~2 triangles per occupied cell), so a large bbox
  coarsens the cells rather than producing a million triangles.
- **Triangles spanning wide no-data gaps are dropped** (more than
  ~4 cells across), so the TIN doesn't sheet flat across a river. Building
  footprints are stamped into the grid with their own ground height
  *before* triangulating, so this cull doesn't punch a hole under every
  building -- which it otherwise would, since a roof occludes the ground
  returns beneath it and a typical block is wider than the cull threshold.
- **Unclassified LiDAR degrades gracefully, not silently.** If a LAZ has
  no usable ASPRS classification (everything class 0/1), heights fall back
  to the old percentile-over-everything behaviour, no terrain is built,
  and both facts are reported as warnings. Expect worse roof heights
  there: on the test tile, an overhanging tree pushed a roof estimate from
  11.8m to 19.5m.
- **Only one area load can run at a time.** A second *Load area* while one
  is running is refused (HTTP 409) rather than queued; the UI disables the
  button for the duration and offers Cancel instead.
- **Face split/extrude both require a *non-adjacent* vertex pair to
  identify the face** (a "diagonal" -- two of its vertices that aren't
  directly connected by an edge). This is deliberate, not an
  oversight: an actual edge is normally shared by two faces (e.g. a
  roof's eave edge also belongs to the wall below it), so picking an
  edge wouldn't tell the tool *which* of the two faces you mean --
  a diagonal only ever belongs to one. If your two selected vertices are
  adjacent, or belong to more than one face together, split/extrude
  won't be offered; pick two corners that skip at least one vertex
  between them along the face you want.
- **Edge/face splitting only inserts straight midpoints/diagonals, and
  deleting a vertex naively reconnects its former neighbors** -- for
  convex-ish faces (true of everything the seeded flat box, plus any
  split/extrude of it, produces) this always gives a sane, simple
  polygon; heavily reshaped, concave, or self-intersecting faces from
  many edits could in principle produce an unusual-looking face this
  way. There's no validation guarding against it -- the tool trusts you.
- **`model/roofshapes.py`'s parametric roof generators (flat/shed/gable/
  hip/pyramid) are no longer wired into the app** -- they're dormant,
  still-tested pure-geometry code, kept in case they're useful again
  later (e.g. as one-click starting shapes) rather than deleted outright.
  `model/mesh.py`'s `seed_flat_box` is the only shape generator the live
  app actually uses now.
- **BGT** context-layer fetching (`data/bgt_client.py`) is implemented
  but not yet wired into the viewer as a rendered layer.
- **Point cloud rendering is decimated** to a fixed cap (200,000 points,
  see `MAX_VIEWER_POINTS` in `web/server.py`) for browser performance;
  height statistics are still computed from the full point cloud, only
  the *visualization* is thinned.
- **State is a single, process-wide "current batch"** (matching the old
  desktop app's single-window model) -- this is a local, one-user-at-a-time
  tool, not a multi-user server; opening the page in two tabs shares the
  same state.
- The exact PDOK BAG/BGT WFS `typeName`s used in `data/bag_client.py` and
  `data/bgt_client.py` are based on the published schema and have not
  been exercised against the live service in this repo's dev environment
  (no outbound network access to `pdok.nl` there) -- if PDOK has changed
  something, check `{WFS_URL}?service=WFS&request=GetCapabilities`.
- **WFS fetches use `requests`, not `geopandas.read_file()`'s built-in URL
  reading** -- the latter delegates to GDAL/pyogrio's own internal libcurl
  client, which doesn't share Python's proxy environment variables or
  certificate store. On a corporate network with a TLS-inspecting proxy
  (which re-signs HTTPS traffic with an internal root CA), that caused a
  cryptic `Failed to open dataset` error even though the same URL worked
  fine in a browser. Routing through `requests` fixes this since it picks
  up the same configuration `pip` already uses successfully. If a fetch
  still fails with a certificate error, point the `REQUESTS_CA_BUNDLE` (or
  `SSL_CERT_FILE`) environment variable at your organization's root CA
  certificate -- don't disable certificate verification.
- **Session format is versioned and not backward-compatible.** Moving
  from the roof-type/height model to an editable mesh (`geometry`
  replacing `roof` in the session JSON) bumped `SESSION_FORMAT_VERSION`
  to 2; a session saved (or autosaved) by an older version of this app
  fails to load with a clear "unsupported session format version" error
  (autosave specifically just starts empty rather than crashing) instead
  of being silently misinterpreted. There's no migration path -- reload
  the area and re-shape the buildings.
- **No undo/redo** for vertex edits yet.

## Roadmap

Freeform editing was built in three stages of increasing engineering risk
rather than all at once -- all three are done:

- ~~**Stage 1**: select/drag any vertex, edit its X/Y/Z directly, snap it
  to LiDAR.~~
- ~~**Stage 2**: add a vertex (split an edge, shared correctly across
  every face that has it), delete a vertex (patch the faces it was
  part of).~~
- ~~**Stage 3**: split one face into two along a diagonal (e.g. break a
  roof plane into two separate pitches), and extrude a face outward
  along its own normal (e.g. add a dormer or bay).~~

Not yet planned in detail, but natural next steps if you want to keep
going: undo/redo, arbitrary-direction extrude (currently always along
the face's own normal), and a more direct way to select a whole face
than the current "shift+click two non-adjacent corners" scheme.

Other planned/deferred items:
- **Automatic LOD2.2 reconstruction** from LiDAR (RANSAC roof-plane
  fitting, or wrapping the [`roofer`](https://github.com/3DBAG/roofer)
  engine that powers 3DBAG) as an alternative to manual shaping --
  deliberately postponed; LiDAR is currently only an explicit per-vertex
  snap target, never fit automatically.
- Rendering BGT context layers (roads, water) in the viewer. Terrain now
  comes from the LiDAR ground class instead, so it is off this list.
- Exporting the terrain as a CityGML `dem:TINRelief`, if the DEM ever
  needs to leave the viewport.
- Live drag feedback for X/Y (not just the vertical Z-drag), and a proper
  3D transform gizmo if the simple vertical-drag interaction proves too
  limiting in practice.

## Project layout

```
src/building_modeller/
  app.py                    # entry point: starts Flask + opens a browser tab
  data/
    bag_client.py           # PDOK BAG WFS fetch
    bgt_client.py           # PDOK BGT WFS fetch (context, not yet wired into the viewer)
    pointcloud.py           # LAZ/LAS loading + classification, cropping, height stats, snap
  model/
    building.py             # Building dataclass: footprint + editable geometry
    mesh.py                 # EditableMesh: seed_flat_box + move/split/delete/extrude ops
    terrain.py              # GroundGrid (median z per cell) + Delaunay TIN for the viewport
    roofshapes.py           # dormant: parametric roof generators, not wired into the app (see limitations)
    project.py              # session (de)serialization, file- and payload-based
  export/
    citygml_writer.py       # CityGML 2.0 LOD2.2 writer (buildings only -- no terrain)
  web/
    server.py               # Flask app + REST API (create_app()), background area jobs
    meshutil.py             # EditableMesh/TerrainMesh -> Three.js render data
    autosave.py             # best-effort background session persistence
    static/
      index.html
      css/app.css
      js/app.js              # Three.js scene, vertex picking/dragging, job polling, API calls
      vendor/                 # vendored three.min.js + OrbitControls.js (no CDN dependency)
tests/
```

## Tests

```bash
pip install -e ".[dev]"
pytest
```

The suite covers the editable mesh model, roof geometry (dormant code),
the CityGML writer, LiDAR classification/height stats and per-vertex
snapping, the terrain model, session (de)serialization, the BAG/BGT WFS
fetch logic (`requests.get` is mocked), and the Flask REST API (via
Flask's test client -- `/api/area`'s BAG fetch is monkeypatched at the
function level rather than hitting the network).

Two areas are worth knowing about when changing them:

- **Area loading is a background job**, so its worker is started through
  `server._run_in_background`, which tests replace. Most tests run it
  inline for determinism; one deliberately uses a real thread, because a
  worker that touches request-scoped Flask state (`abort`, `jsonify`)
  works fine inline and only fails off-thread. That test joins the thread
  before finishing -- a worker outliving its test would run with the
  monkeypatches undone, calling the live PDOK service and overwriting your
  real autosave file.
- **`tests/test_terrain.py` guards the building-hole behaviour** in both
  directions: that an unfilled footprint really does lose its bridging
  triangles, and that stamping the footprint in first keeps the terrain
  closed. If you touch the cull threshold, those are the tests that will
  tell you whether buildings are about to start floating over voids.
