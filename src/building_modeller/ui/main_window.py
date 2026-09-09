"""Main application window: area selection + building list on the left,
the 3D viewport in the center, and the roof property panel on the right."""
from __future__ import annotations

from typing import List

from PySide6.QtWidgets import (
    QFileDialog,
    QLabel,
    QListWidget,
    QMainWindow,
    QMessageBox,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from ..data.bag_client import fetch_pand_footprints
from ..data.pointcloud import LidarPointCloud, find_tiles, stats_for_footprint
from ..model.building import Building
from ..model.project import load_session, save_session
from ..model.roofshapes import RoofParams
from ..export.citygml_writer import export_citygml
from .area_selector import AreaSelector
from .building_panel import BuildingPanel
from .viewport import Viewport


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("CityGML Building Modeller")
        self.buildings: List[Building] = []
        self.lidar_cloud = LidarPointCloud.empty()

        self.viewport = Viewport()
        self.area_selector = AreaSelector()
        self.building_panel = BuildingPanel()
        self.building_list = QListWidget()

        left_panel = QWidget()
        left_layout = QVBoxLayout(left_panel)
        left_layout.addWidget(self.area_selector)
        left_layout.addWidget(QLabel("Buildings"))
        left_layout.addWidget(self.building_list, 1)

        splitter = QSplitter()
        splitter.addWidget(left_panel)
        splitter.addWidget(self.viewport)
        splitter.addWidget(self.building_panel)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setStretchFactor(2, 0)
        self.setCentralWidget(splitter)

        self._build_menu()

        self.area_selector.load_requested.connect(self._on_load_area)
        self.building_list.currentRowChanged.connect(self._on_building_selected)
        self.building_panel.roof_changed.connect(self._on_roof_changed)

        self.resize(1400, 900)

    def _build_menu(self) -> None:
        menu = self.menuBar().addMenu("&File")
        save_action = menu.addAction("Save session...")
        save_action.triggered.connect(self._on_save_session)
        load_action = menu.addAction("Load session...")
        load_action.triggered.connect(self._on_load_session)
        menu.addSeparator()
        export_action = menu.addAction("Export CityGML...")
        export_action.triggered.connect(self._on_export_citygml)

    # -- area loading ----------------------------------------------------

    def _on_load_area(self, bbox: tuple, lidar_folder: str) -> None:
        minx, miny, maxx, maxy = bbox

        try:
            gdf = fetch_pand_footprints(bbox)
        except Exception as exc:
            QMessageBox.warning(self, "BAG fetch failed", f"Could not fetch BAG footprints:\n{exc}")
            gdf = None

        self.buildings = []
        if gdf is not None:
            id_col = "identificatie" if "identificatie" in gdf.columns else gdf.columns[0]
            for _, row in gdf.iterrows():
                geom = row.geometry
                if geom is None or geom.is_empty:
                    continue
                if geom.geom_type == "MultiPolygon":
                    geom = max(geom.geoms, key=lambda g: g.area)
                self.buildings.append(Building(bag_id=str(row[id_col]), footprint=geom))

        self.lidar_cloud = LidarPointCloud.empty()
        if lidar_folder:
            try:
                tiles = find_tiles(lidar_folder)
                if not tiles:
                    QMessageBox.information(
                        self, "No LiDAR tiles", f"No .laz/.las files found in:\n{lidar_folder}"
                    )
                else:
                    self.lidar_cloud = LidarPointCloud.from_files(tiles, bbox=bbox)
            except Exception as exc:
                QMessageBox.warning(self, "LiDAR load failed", f"Could not load point cloud tiles:\n{exc}")

        for building in self.buildings:
            stats = stats_for_footprint(self.lidar_cloud, building.footprint)
            building.lidar_stats = stats
            if stats:
                building.ground_height = stats["ground_height"]

        self.viewport.set_origin((minx + maxx) / 2.0, (miny + maxy) / 2.0, 0.0)
        self._refresh_viewport_footprints()
        self.viewport.set_point_cloud(self.lidar_cloud)
        self._refresh_building_list()

    def _refresh_viewport_footprints(self) -> None:
        self.viewport.clear_footprints()
        self.viewport.clear_building_mesh()
        for building in self.buildings:
            ring = list(building.footprint.exterior.coords)[:-1]
            self.viewport.add_footprint_outline(ring, z=building.ground_height)

    # -- building list -----------------------------------------------------

    def _list_label(self, building: Building) -> str:
        return f"{building.bag_id} [{building.status.value}]"

    def _refresh_building_list(self) -> None:
        self.building_list.blockSignals(True)
        self.building_list.clear()
        for building in self.buildings:
            self.building_list.addItem(self._list_label(building))
        self.building_list.blockSignals(False)
        self.building_panel.set_building(None)
        self.viewport.clear_building_mesh()

    def _on_building_selected(self, row: int) -> None:
        if row < 0 or row >= len(self.buildings):
            self.building_panel.set_building(None)
            self.viewport.clear_building_mesh()
            return
        building = self.buildings[row]
        self.building_panel.set_building(building)
        self.viewport.set_building_mesh(building.mesh())

    def _on_roof_changed(self, roof: RoofParams) -> None:
        row = self.building_list.currentRow()
        if row < 0 or row >= len(self.buildings):
            return
        building = self.buildings[row]
        building.set_roof(roof)
        self.building_list.item(row).setText(self._list_label(building))
        self.viewport.set_building_mesh(building.mesh())

    # -- session / export --------------------------------------------------

    def _on_save_session(self) -> None:
        if not self.buildings:
            QMessageBox.information(self, "Nothing to save", "No buildings loaded yet.")
            return
        path, _ = QFileDialog.getSaveFileName(self, "Save session", filter="Session (*.json)")
        if not path:
            return
        save_session(self.buildings, path)

    def _on_load_session(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Load session", filter="Session (*.json)")
        if not path:
            return
        try:
            self.buildings = load_session(path)
        except Exception as exc:
            QMessageBox.warning(self, "Load failed", f"Could not load session:\n{exc}")
            return
        if self.buildings:
            minx = min(b.footprint.bounds[0] for b in self.buildings)
            miny = min(b.footprint.bounds[1] for b in self.buildings)
            maxx = max(b.footprint.bounds[2] for b in self.buildings)
            maxy = max(b.footprint.bounds[3] for b in self.buildings)
            self.viewport.set_origin((minx + maxx) / 2.0, (miny + maxy) / 2.0, 0.0)
        self._refresh_viewport_footprints()
        self._refresh_building_list()

    def _on_export_citygml(self) -> None:
        if not self.buildings:
            QMessageBox.information(self, "Nothing to export", "No buildings loaded yet.")
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export CityGML", filter="CityGML (*.gml)")
        if not path:
            return
        try:
            export_citygml(self.buildings, path)
        except Exception as exc:
            QMessageBox.critical(self, "Export failed", f"Could not export CityGML:\n{exc}")
            return
        self._refresh_building_list()
        QMessageBox.information(
            self, "Export complete", f"Exported {len(self.buildings)} buildings to:\n{path}"
        )
