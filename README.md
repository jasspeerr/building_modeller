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

### Windows: `ImportError: DLL load failed while importing Shiboken`

If you see this (often with a Dutch/localized message like *"Kan opgegeven
procedure niet vinden"* / "the specified procedure could not be found"),
it's a version mismatch between the `PySide6` / `PySide6-Essentials` /
`PySide6-Addons` / `shiboken6` packages -- they're compiled against each
other and must all be the exact same version. This is most likely if
you're installing into a shared/global Python environment (e.g. because a
locked-down machine won't let you create or use a virtual environment)
rather than a clean venv, since a global `site-packages` can accumulate
mismatched versions across unrelated installs over time. Work through
these in order (none of them require a venv):

1. **Check for a mismatch:**
   ```
   pip show PySide6 PySide6-Essentials PySide6-Addons shiboken6
   ```
   All four must report the same `Version:`. If they don't, that's the bug.
2. **Force a clean, matched reinstall:**
   ```
   pip uninstall -y PySide6 PySide6-Essentials PySide6-Addons shiboken6
   pip install --no-cache-dir PySide6==6.7.3
   ```
   Then re-run step 1 to confirm all four now match.
3. **Check for a conflicting Qt package** in the same environment (PyQt5,
   PyQt6, PySide2, or `opencv-python`, which bundles its own Qt on
   Windows) -- Windows' DLL search order can pick up an incompatible
   same-named DLL from one of these instead of PySide6's own:
   ```
   pip list | findstr /i "qt pyside opencv"
   ```
4. **Check the Microsoft Visual C++ Redistributable (x64, 2015-2022) is
   installed.** `shiboken6`'s native module depends on `VCRUNTIME140.dll`
   / `VCRUNTIME140_1.dll` / `MSVCP140.dll`; a missing or outdated
   redistributable produces exactly this "procedure not found" error for
   compiled Python extensions on Windows. On a managed machine this may
   need IT's help, since installing it typically needs admin rights.
5. If none of these fix it, the underlying constraint (no usable venv) is
   worth raising with IT separately -- a clean virtual environment would
   sidestep all of the above, but isn't required for the fix itself.

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
