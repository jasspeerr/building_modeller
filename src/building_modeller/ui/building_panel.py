"""Property panel for editing the selected building's roof.

LiDAR only *seeds* the eave/ridge height fields here (via "Use LiDAR
suggestion") -- there is no automatic plane fitting; the user picks the
roof type and confirms/adjusts the heights.
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..model.building import Building
from ..model.roofshapes import RoofParams, RoofType


class BuildingPanel(QWidget):
    roof_changed = Signal(object)  # emits a RoofParams

    def __init__(self, parent=None):
        super().__init__(parent)
        self._building: Optional[Building] = None
        self._updating = False

        self.bag_id_label = QLabel("-")
        self.lidar_label = QLabel("no LiDAR stats")

        self.roof_type_combo = QComboBox()
        for rt in RoofType:
            self.roof_type_combo.addItem(rt.value, rt)

        self.ridge_along_combo = QComboBox()
        self.ridge_along_combo.addItems(["long", "short"])

        self.eave_spin = QDoubleSpinBox()
        self.eave_spin.setRange(-50.0, 500.0)
        self.eave_spin.setDecimals(2)
        self.eave_spin.setSuffix(" m")

        self.ridge_spin = QDoubleSpinBox()
        self.ridge_spin.setRange(-50.0, 500.0)
        self.ridge_spin.setDecimals(2)
        self.ridge_spin.setSuffix(" m")

        self.suggest_button = QPushButton("Use LiDAR suggestion")

        form = QFormLayout()
        form.addRow("BAG ID", self.bag_id_label)
        form.addRow("LiDAR stats", self.lidar_label)
        form.addRow("Roof type", self.roof_type_combo)
        form.addRow("Ridge along", self.ridge_along_combo)
        form.addRow("Eave height", self.eave_spin)
        form.addRow("Ridge height", self.ridge_spin)

        box = QGroupBox("Selected building")
        box.setLayout(form)

        layout = QVBoxLayout(self)
        layout.addWidget(box)
        layout.addWidget(self.suggest_button)
        layout.addStretch(1)

        self.roof_type_combo.currentIndexChanged.connect(self._on_roof_type_changed)
        self.ridge_along_combo.currentIndexChanged.connect(self._emit_change)
        self.eave_spin.valueChanged.connect(self._emit_change)
        self.ridge_spin.valueChanged.connect(self._emit_change)
        self.suggest_button.clicked.connect(self._apply_lidar_suggestion)

        self._on_roof_type_changed()
        self.setEnabled(False)

    def set_building(self, building: Optional[Building]) -> None:
        self._building = building
        self._updating = True
        try:
            if building is None:
                self.setEnabled(False)
                self.bag_id_label.setText("-")
                self.lidar_label.setText("no LiDAR stats")
                return
            self.setEnabled(True)
            self.bag_id_label.setText(str(building.bag_id))
            self._refresh_lidar_label()

            roof = building.roof
            if roof is None:
                stats = building.lidar_stats or {}
                eave = stats.get("eave_estimate", building.ground_height + 3.0)
                ridge = stats.get("ridge_estimate", eave)
                self.roof_type_combo.setCurrentText(RoofType.FLAT.value)
                self.eave_spin.setValue(eave)
                self.ridge_spin.setValue(ridge)
            else:
                # PySide6's QComboBox userData round-trips a (str, Enum) member
                # as a plain str, so normalize back to RoofType before use.
                self.roof_type_combo.setCurrentText(RoofType(roof.roof_type).value)
                self.ridge_along_combo.setCurrentText(roof.ridge_along)
                self.eave_spin.setValue(roof.eave_height)
                self.ridge_spin.setValue(
                    roof.ridge_height if roof.ridge_height is not None else roof.eave_height
                )
        finally:
            self._updating = False
        self._on_roof_type_changed()

    def _refresh_lidar_label(self) -> None:
        stats = self._building.lidar_stats if self._building else None
        if not stats:
            self.lidar_label.setText("no LiDAR stats")
            return
        self.lidar_label.setText(
            f"{stats['point_count']} pts, ground {stats['ground_height']:.2f} m, "
            f"top {stats['top_height']:.2f} m"
        )

    def _on_roof_type_changed(self) -> None:
        is_flat = self.roof_type_combo.currentData() == RoofType.FLAT
        self.ridge_spin.setEnabled(not is_flat)
        is_hip_or_gable = self.roof_type_combo.currentData() in (RoofType.GABLE, RoofType.HIP)
        self.ridge_along_combo.setEnabled(is_hip_or_gable)
        self._emit_change()

    def _apply_lidar_suggestion(self) -> None:
        if self._building is None or not self._building.lidar_stats:
            return
        stats = self._building.lidar_stats
        self._updating = True
        try:
            self.eave_spin.setValue(stats["eave_estimate"])
            self.ridge_spin.setValue(stats["ridge_estimate"])
        finally:
            self._updating = False
        self._emit_change()

    def _emit_change(self) -> None:
        if self._updating or self._building is None:
            return
        # Same PySide6 str-Enum round-trip quirk as in set_building(): normalize.
        roof_type = RoofType(self.roof_type_combo.currentData())
        roof = RoofParams(
            roof_type=roof_type,
            eave_height=self.eave_spin.value(),
            ridge_height=None if roof_type == RoofType.FLAT else self.ridge_spin.value(),
            ridge_along=self.ridge_along_combo.currentText(),
        )
        self.roof_changed.emit(roof)
