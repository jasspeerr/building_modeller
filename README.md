# Building Modeller

A locally-run web app to model 3D CityGML LOD2.2 buildings from Dutch BAG
footprints, using AHN6 LiDAR point clouds as a visual reference, and
export the result as a static CityGML 2.0 file. It's a small Flask server
plus a browser-based Three.js viewer -- everything runs on your own
machine, nothing is served publicly.

**LiDAR is currently used only to suggest height values** (ground / eave /
ridge percentiles per footprint) for a roof shape you pick manually --
there is no automatic plane-fitting reconstruction yet (see "Roadmap"
below).

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
   - BAG building footprints for the bbox are fetched live from the
     [PDOK](https://www.pdok.nl/) BAG WFS.
   - Any point cloud files in the chosen folder are loaded and cropped to
     the bbox.
   - Rough ground/top height statistics are computed per building from
     the LiDAR points that fall inside its footprint.
   - Fetch/load problems (PDOK unreachable, no tiles found, etc.) show up
     as warnings under the form rather than failing the whole page.
2. **Buildings** (left panel list): click a building. Its live 3D mesh
   preview appears in the viewport, alongside the LiDAR point cloud
   (colored by elevation) and all footprint outlines as a visual
   reference.
3. **Selected building** (right panel): pick a roof type (flat, shed,
   gable, hip, pyramid), click **Use LiDAR suggestion** to seed the eave/
   ridge heights from the point cloud stats, then fine-tune them. The 3D
   preview updates live as you change values.
4. Repeat for the buildings you care about. Buildings you never touch are
   still exported, as a flat-topped box, so a batch export never silently
   drops one -- check the status tag (`unmodelled` / `edited` /
   `exported`) in the list to see what still needs attention.
5. **Save session** downloads a JSON file with the batch's modelling
   state; **Load session** re-uploads one to continue later (this does
   not restore the point cloud view, only footprints/roofs).
6. **Export CityGML** downloads one CityGML 2.0 file for every building
   currently loaded, with `WallSurface`/`RoofSurface`/`GroundSurface`
   semantic surfaces and a `lod2Solid` per building (CRS: EPSG:28992). No
   schema is bundled to validate against; open the result in QGIS or the
   [FZK Viewer](https://www.iai.kit.edu/1302.php) to sanity-check it, or
   validate with `xmllint --schema` against the official CityGML 2.0
   XSDs if you need strict validation.

## Known MVP limitations

- **Footprint holes** (courtyard buildings) are not supported -- only the
  exterior ring of a footprint is used.
- **`gable`/`hip` roofs** are built on the footprint's minimum rotated
  bounding rectangle rather than its exact outline (a general
  straight-skeleton roof for arbitrary polygons, e.g. L-shaped buildings,
  is out of scope for now). `flat`, `shed`, and `pyramid` roofs are exact
  for any simple polygon.
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

## Roadmap

- **Automatic LOD2.2 reconstruction** from LiDAR (RANSAC roof-plane
  fitting, or wrapping the [`roofer`](https://github.com/3DBAG/roofer)
  engine that powers 3DBAG) as an alternative to manually picking a roof
  shape -- deliberately postponed for this first build.
- Rendering BGT context layers (roads, water, terrain) in the viewer.
- Straight-skeleton-based roofs for non-rectangular footprints.

## Project layout

```
src/building_modeller/
  app.py                    # entry point: starts Flask + opens a browser tab
  data/
    bag_client.py           # PDOK BAG WFS fetch
    bgt_client.py           # PDOK BGT WFS fetch (context, not yet wired into the viewer)
    pointcloud.py           # LAZ/LAS loading, cropping, height stats
  model/
    building.py             # Building dataclass
    roofshapes.py           # parametric roof generators (flat/shed/gable/hip/pyramid)
    project.py              # session (de)serialization, file- and payload-based
  export/
    citygml_writer.py       # CityGML 2.0 LOD2.2 writer
  web/
    server.py               # Flask app + REST API (create_app())
    meshutil.py             # BuildingMesh -> flat triangle arrays for Three.js
    static/
      index.html
      css/app.css
      js/app.js              # Three.js scene + API calls
      vendor/                 # vendored three.min.js + OrbitControls.js (no CDN dependency)
tests/
```

## Tests

```bash
pip install -e ".[dev]"
pytest
```

The suite covers roof geometry, the CityGML writer, LiDAR height stats,
session (de)serialization, and the Flask REST API (via Flask's test
client -- `/api/area`'s PDOK call is monkeypatched rather than hitting
the network).
