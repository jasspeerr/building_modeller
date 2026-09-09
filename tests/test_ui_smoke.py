"""Headless smoke tests for the Qt UI.

Runs under the "offscreen" Qt platform plugin so it works without a real
display (e.g. in CI); skipped entirely if PySide6/pyqtgraph aren't
installed. This is what caught a real bug during development: PySide6's
QComboBox round-trips a (str, Enum) userData value back as a plain str,
which broke re-selecting a building after editing its roof.
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("pyqtgraph")

from shapely.geometry import box

from building_modeller.model.building import Building
from building_modeller.model.roofshapes import RoofType

_qapp = None


@pytest.fixture(scope="module")
def qapp():
    global _qapp
    from PySide6.QtWidgets import QApplication

    _qapp = QApplication.instance() or QApplication([])
    return _qapp


@pytest.fixture
def main_window(qapp):
    from building_modeller.ui.main_window import MainWindow

    return MainWindow()


def test_main_window_constructs(main_window):
    assert main_window.buildings == []


def test_select_and_edit_roof_then_switch_buildings(main_window):
    b1 = Building(bag_id="A", footprint=box(0, 0, 10, 6))
    b2 = Building(bag_id="B", footprint=box(0, 0, 8, 8))
    main_window.buildings = [b1, b2]
    main_window._refresh_viewport_footprints()
    main_window._refresh_building_list()

    main_window.building_list.setCurrentRow(0)
    main_window.building_panel.roof_type_combo.setCurrentText("gable")
    main_window.building_panel.eave_spin.setValue(3.0)
    main_window.building_panel.ridge_spin.setValue(5.0)
    assert b1.roof.roof_type == RoofType.GABLE
    assert isinstance(b1.roof.roof_type, RoofType)

    # Switching away and back must not crash (regression: QComboBox
    # userData str-coercion broke `roof.roof_type.value` on re-select).
    main_window.building_list.setCurrentRow(1)
    main_window.building_list.setCurrentRow(0)
    assert b1.roof.roof_type == RoofType.GABLE


def test_export_after_editing(main_window, tmp_path):
    from building_modeller.export.citygml_writer import export_citygml
    from lxml import etree
    from building_modeller.export.citygml_writer import CITYGML_NS

    b1 = Building(bag_id="A", footprint=box(0, 0, 10, 6))
    main_window.buildings = [b1]
    main_window._refresh_viewport_footprints()
    main_window._refresh_building_list()
    main_window.building_list.setCurrentRow(0)
    main_window.building_panel.roof_type_combo.setCurrentText("hip")
    main_window.building_panel.eave_spin.setValue(3.0)
    main_window.building_panel.ridge_spin.setValue(6.0)

    out = tmp_path / "out.gml"
    export_citygml(main_window.buildings, str(out))
    root = etree.fromstring(out.read_bytes())
    assert root.tag == f"{{{CITYGML_NS}}}CityModel"
