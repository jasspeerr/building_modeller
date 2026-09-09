import numpy as np
import pytest
from shapely.geometry import box

from building_modeller.data.pointcloud import LidarPointCloud, stats_for_footprint

FOOTPRINT = box(0.0, 0.0, 10.0, 10.0)


def make_synthetic_cloud():
    rng = np.random.default_rng(42)
    # 1000 "ground" points at ~0m inside the footprint, 500 "roof" points at ~6m,
    # plus 200 points well outside the footprint that must be excluded.
    n_ground, n_roof, n_outside = 1000, 500, 200
    ground_xy = rng.uniform(0.0, 10.0, size=(n_ground, 2))
    roof_xy = rng.uniform(2.0, 8.0, size=(n_roof, 2))
    outside_xy = rng.uniform(50.0, 60.0, size=(n_outside, 2))

    x = np.concatenate([ground_xy[:, 0], roof_xy[:, 0], outside_xy[:, 0]])
    y = np.concatenate([ground_xy[:, 1], roof_xy[:, 1], outside_xy[:, 1]])
    z = np.concatenate(
        [
            rng.normal(0.0, 0.05, n_ground),
            rng.normal(6.0, 0.05, n_roof),
            rng.normal(999.0, 0.01, n_outside),
        ]
    )
    return LidarPointCloud(x, y, z)


class TestMaskInPolygon:
    def test_excludes_points_outside_footprint(self):
        cloud = make_synthetic_cloud()
        inside = cloud.points_in_polygon(FOOTPRINT)
        assert len(inside) == 1500
        assert inside.z.max() < 100.0

    def test_empty_cloud_returns_empty(self):
        empty = LidarPointCloud.empty()
        result = empty.points_in_polygon(FOOTPRINT)
        assert len(result) == 0


class TestStatsForFootprint:
    def test_returns_none_when_no_points_inside(self):
        cloud = LidarPointCloud(np.array([100.0]), np.array([100.0]), np.array([5.0]))
        assert stats_for_footprint(cloud, FOOTPRINT) is None

    def test_ground_and_top_heights_recovered(self):
        cloud = make_synthetic_cloud()
        stats = stats_for_footprint(cloud, FOOTPRINT, ground_percentile=5, top_percentile=95)
        assert stats["point_count"] == 1500
        assert stats["ground_height"] == pytest.approx(0.0, abs=0.3)
        assert stats["top_height"] == pytest.approx(6.0, abs=0.3)
        assert stats["ridge_estimate"] == stats["top_height"]
        assert stats["ground_height"] < stats["eave_estimate"] < stats["ridge_estimate"]


class TestCropToBbox:
    def test_crop_reduces_to_bbox(self):
        cloud = make_synthetic_cloud()
        cropped = cloud.crop_to_bbox(0.0, 0.0, 10.0, 10.0)
        assert len(cropped) == 1500
        assert cropped.x.max() <= 10.0
        assert cropped.y.max() <= 10.0


class TestHeightNear:
    def test_returns_median_z_of_nearby_points(self):
        cloud = make_synthetic_cloud()
        # Deep inside the "roof" patch (2..8, 2..8), away from ground points.
        result = cloud.height_near(5.0, 5.0, radius=1.0)
        assert result is not None
        assert result["z"] == pytest.approx(6.0, abs=0.3)
        assert result["point_count"] > 0

    def test_returns_none_when_nothing_within_radius(self):
        cloud = make_synthetic_cloud()
        assert cloud.height_near(500.0, 500.0, radius=1.0) is None

    def test_returns_none_for_empty_cloud(self):
        assert LidarPointCloud.empty().height_near(0.0, 0.0) is None
