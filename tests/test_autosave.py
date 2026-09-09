"""Tests for best-effort session autosave/autoload -- separate from the
explicit download/upload session flow in test_web_api.py's TestSession."""
from shapely.geometry import box

import building_modeller.web.autosave as autosave_module
from building_modeller.model.building import Building


def make_buildings():
    b = Building(bag_id="A", footprint=box(0, 0, 10, 6))
    b.move_vertex(0, (1.0, 2.0, 9.0))  # seeds the mesh and edits it
    return [b]


class TestSaveLoad:
    def test_save_then_load_round_trips(self, tmp_path, monkeypatch):
        monkeypatch.setattr(autosave_module, "AUTOSAVE_PATH", tmp_path / "sub" / "last_session.json")
        buildings = make_buildings()
        autosave_module.save(buildings)
        assert autosave_module.AUTOSAVE_PATH.exists()

        restored = autosave_module.load()
        assert len(restored) == 1
        assert restored[0].bag_id == "A"
        assert restored[0].mesh().vertices[0] == (1.0, 2.0, 9.0)

    def test_load_with_no_file_returns_empty_list(self, tmp_path, monkeypatch):
        monkeypatch.setattr(autosave_module, "AUTOSAVE_PATH", tmp_path / "does_not_exist.json")
        assert autosave_module.load() == []

    def test_load_with_corrupt_file_returns_empty_list_not_raise(self, tmp_path, monkeypatch):
        path = tmp_path / "corrupt.json"
        path.write_text("not valid json{{{")
        monkeypatch.setattr(autosave_module, "AUTOSAVE_PATH", path)
        assert autosave_module.load() == []

    def test_save_creates_parent_directory(self, tmp_path, monkeypatch):
        path = tmp_path / "nested" / "dir" / "last_session.json"
        monkeypatch.setattr(autosave_module, "AUTOSAVE_PATH", path)
        autosave_module.save(make_buildings())
        assert path.exists()

    def test_save_failure_does_not_raise(self, tmp_path, monkeypatch):
        # Point at a path whose parent can't be created (a file, not a dir).
        blocker = tmp_path / "blocker"
        blocker.write_text("x")
        monkeypatch.setattr(autosave_module, "AUTOSAVE_PATH", blocker / "last_session.json")
        autosave_module.save(make_buildings())  # must not raise
