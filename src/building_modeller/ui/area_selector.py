"""Area selection: a bounding box (RD New, EPSG:28992) plus a local
folder of AHN6 LAZ/LAS tiles.

BAG footprints are fetched live from PDOK for the bbox; AHN6 point
clouds are too large to fetch generically, so the user points the app at
a local folder of tiles instead.
"""
from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


class AreaSelector(QWidget):
    load_requested = Signal(tuple, str)  # (minx, miny, maxx, maxy), lidar_folder

    def __init__(self, parent=None):
        super().__init__(parent)

        def make_spin(value: float) -> QDoubleSpinBox:
            spin = QDoubleSpinBox()
            spin.setRange(-1_000_000.0, 1_000_000.0)
            spin.setDecimals(1)
            spin.setValue(value)
            return spin

        # Arbitrary small default bbox (central Delft), just to give the
        # fields a sane RD New starting point.
        self.minx = make_spin(84000.0)
        self.miny = make_spin(447000.0)
        self.maxx = make_spin(84300.0)
        self.maxy = make_spin(447300.0)

        self.lidar_folder_edit = QLineEdit()
        browse_button = QPushButton("Browse...")
        browse_button.clicked.connect(self._browse_folder)

        form = QFormLayout()
        form.addRow("min X (RD)", self.minx)
        form.addRow("min Y (RD)", self.miny)
        form.addRow("max X (RD)", self.maxx)
        form.addRow("max Y (RD)", self.maxy)

        folder_row = QHBoxLayout()
        folder_row.addWidget(self.lidar_folder_edit)
        folder_row.addWidget(browse_button)
        form.addRow("AHN6 LAZ folder", folder_row)

        box = QGroupBox("Area")
        box.setLayout(form)

        load_button = QPushButton("Load area (BAG + LiDAR)")
        load_button.clicked.connect(self._emit_load)

        note = QLabel(
            "Fetches BAG footprints from PDOK for this bounding box; the\n"
            "LiDAR folder is scanned for local AHN6 .laz/.las tiles."
        )
        note.setWordWrap(True)

        layout = QVBoxLayout(self)
        layout.addWidget(box)
        layout.addWidget(load_button)
        layout.addWidget(note)
        layout.addStretch(1)

    def _browse_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Select AHN6 tile folder")
        if folder:
            self.lidar_folder_edit.setText(folder)

    def _emit_load(self) -> None:
        bbox = (self.minx.value(), self.miny.value(), self.maxx.value(), self.maxy.value())
        self.load_requested.emit(bbox, self.lidar_folder_edit.text())
