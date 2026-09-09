import pytest
from shapely.geometry import box

from building_modeller.model.building import Building, ModellingStatus
from building_modeller.model.project import (
    load_session,
    save_session,
    session_from_payload,
    session_to_payload,
)
from building_modeller.model.roofshapes import RoofParams, RoofType


def make_buildings():
    a = Building(bag_id="A", footprint=box(0, 0, 10, 6))
    a.set_roof(RoofParams(roof_type=RoofType.GABLE, eave_height=3.0, ridge_height=5.0))
    b = Building(bag_id="B", footprint=box(20, 0, 28, 8), lidar_stats={"point_count": 5})
    return [a, b]


class TestPayloadRoundTrip:
    def test_round_trip_preserves_fields(self):
        buildings = make_buildings()
        payload = session_to_payload(buildings)
        restored = session_from_payload(payload)

        assert len(restored) == 2
        assert restored[0].bag_id == "A"
        assert restored[0].roof.roof_type == RoofType.GABLE
        assert restored[0].roof.ridge_height == 5.0
        assert restored[0].status == ModellingStatus.EDITED
        assert restored[1].lidar_stats == {"point_count": 5}
        assert restored[1].roof is None

    def test_footprint_geometry_preserved(self):
        buildings = make_buildings()
        restored = session_from_payload(session_to_payload(buildings))
        assert restored[0].footprint.equals(buildings[0].footprint)

    def test_unsupported_format_version_raises(self):
        payload = session_to_payload(make_buildings())
        payload["format_version"] = 999
        with pytest.raises(ValueError):
            session_from_payload(payload)


class TestFileRoundTrip:
    def test_save_and_load(self, tmp_path):
        buildings = make_buildings()
        path = tmp_path / "session.json"
        save_session(buildings, str(path))
        assert path.exists()

        restored = load_session(str(path))
        assert [b.bag_id for b in restored] == ["A", "B"]
        assert restored[0].roof.roof_type == RoofType.GABLE
