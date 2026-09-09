# Building Modeller

A local desktop tool to model 3D CityGML LOD2.2 buildings from Dutch BAG
footprints, using AHN6 LiDAR point clouds as a visual reference, and
export the result as a static CityGML 2.0 file.

**LiDAR is currently used only to suggest height values** (ground / eave /
ridge percentiles per footprint) for a roof shape you pick manually --
there is no automatic plane-fitting reconstruction yet (see "Roadmap"
below).

## Requirements

- Python 3.9 (3.9.x only -- see the dependency notes below)
- pip, with the ability to install packages for your user
  (`pip install --user ...`); no `sudo`/system package manager access is
  needed for anything in `requirements.txt`

### A note on Linux and Qt system libraries

PySide6 (the GUI toolkit) itself is a pure `pip` install, but like
essentially any Qt/OpenGL desktop application on Linux, it needs a small
set of system libraries that are normally already present on any desktop
Linux install (Mesa/EGL/X11 libraries: `libegl1`, `libgl1`,
`libxkbcommon0`, `libxkbcommon-x11-0`). If the app fails to start with an
error mentioning `libEGL` or similar, ask your system administrator to
install those (they are small, common libraries used by most GUI
software, not a modelling-specific dependency).

## Install

```bash
python3.9 -m venv .venv
source .venv/bin/activate   # or .venv\Scripts\activate on Windows
pip install -e .
```

This intentionally avoids the heaviest, most permission-sensitive
geospatial dependencies:

| Need | What we use | Why not the "usual" choice |
|---|---|---|
| 3D viewport | `pyqtgraph` (OpenGL) | Avoids VTK (PyVista) / Open3D, both much larger installs |
| BAG/BGT footprints | `geopandas` + `pyogrio` | `pyogrio` wheels bundle GDAL -- no system GDAL/PROJ install needed |
| AHN6 LiDAR (LAZ) | `laspy[lazrs]` | Pure-wheel LAZ decompression -- avoids PDAL entirely |
| CityGML export | `lxml` | Hand-written writer; no maintained CityGML library exists |

All of the above ship prebuilt wheels for Python 3.9 on Linux/macOS/Windows.

## Running

```bash
python -m building_modeller.app
```

## Workflow

1. **Area** (left panel): enter a bounding box in RD New (EPSG:28992)
   coordinates, and optionally point "AHN6 LAZ folder" at a local folder
   of `.laz`/`.las` tiles covering that area. Click **Load area (BAG +
   LiDAR)**.
   - BAG building footprints for the bbox are fetched live from the
     [PDOK](https://www.pdok.nl/) BAG WFS.
   - Any point cloud files in the chosen folder are loaded and cropped to
     the bbox.
   - Rough ground/top height statistics are computed per building from
     the LiDAR points that fall inside its footprint.
2. **Buildings** (left panel list): select a building. Its footprint and
   a live 3D mesh preview appear in the viewport, alongside the LiDAR
   point cloud (colored by elevation) as a visual reference.
3. **Selected building** (right panel): pick a roof type (flat, shed,
   gable, hip, pyramid), click **Use LiDAR suggestion** to seed the eave/
   ridge heights from the point cloud stats, then fine-tune them. The 3D
   preview updates live.
4. Repeat for the buildings you care about. Buildings you never touch are
   still exported, as a flat-topped box, so a batch export never silently
   drops one -- check the `[unmodelled]` / `[edited]` tag in the list to
   see what still needs attention.
5. **File > Save session...** / **Load session...** to persist a batch's
   modelling state (a JSON file) across runs.
6. **File > Export CityGML...** writes one CityGML 2.0 file for every
   building currently loaded, with `WallSurface`/`RoofSurface`/
   `GroundSurface` semantic surfaces and a `lod2Solid` per building (CRS:
   EPSG:28992). No schema is bundled to validate against; open the result
   in QGIS or the [FZK Viewer](https://www.iai.kit.edu/1302.php) to
   sanity-check it, or validate with `xmllint --schema` against the
   official CityGML 2.0 XSDs if you need strict validation.

## Known MVP limitations

- **Footprint holes** (courtyard buildings) are not supported -- only the
  exterior ring of a footprint is used.
- **`gable`/`hip` roofs** are built on the footprint's minimum rotated
  bounding rectangle rather than its exact outline (a general
  straight-skeleton roof for arbitrary polygons, e.g. L-shaped buildings,
  is out of scope for now). `flat`, `shed`, and `pyramid` roofs are exact
  for any simple polygon.
- **BGT** context-layer fetching (`data/bgt_client.py`) is implemented
  but not yet wired into the UI as a rendered layer.
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
- Rendering BGT context layers (roads, water, terrain) in the viewport.
- Straight-skeleton-based roofs for non-rectangular footprints.

## Project layout

```
src/building_modeller/
  app.py                    # entry point
  data/
    bag_client.py           # PDOK BAG WFS fetch
    bgt_client.py           # PDOK BGT WFS fetch (context, not yet wired into UI)
    pointcloud.py           # LAZ/LAS loading, cropping, height stats
  model/
    building.py             # Building dataclass
    roofshapes.py           # parametric roof generators (flat/shed/gable/hip/pyramid)
    project.py              # session save/load
  export/
    citygml_writer.py       # CityGML 2.0 LOD2.2 writer
  ui/
    main_window.py
    viewport.py              # pyqtgraph GLViewWidget wrapper
    building_panel.py
    area_selector.py
tests/
```

## Tests

```bash
pip install -e ".[dev]"
pytest
```

The suite covers roof geometry, the CityGML writer, LiDAR height stats,
and headless UI smoke tests (the UI tests run under Qt's `offscreen`
platform plugin and are skipped automatically if PySide6 isn't
installed).
