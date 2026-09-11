import numpy as np
import pytest
from shapely.geometry import box

from building_modeller.data.pointcloud import (
    CLASS_BUILDING,
    CLASS_GROUND,
    LidarPointCloud,
    stats_for_footprint,
)

FOOTPRINT = box(0.0, 0.0, 10.0, 10.0)

HIGH_VEGETATION = 5


def make_synthetic_cloud(classified=False, with_vegetation=False):
    rng = np.random.default_rng(42)
    # 1000 "ground" points at ~0m inside the footprint, 500 "roof" points at ~6m,
    # plus 200 points well outside the footprint that must be excluded.
    n_ground, n_roof, n_outside = 1000, 500, 200
    ground_xy = rng.uniform(0.0, 10.0, size=(n_ground, 2))
    roof_xy = rng.uniform(2.0, 8.0, size=(n_roof, 2))
    outside_xy = rng.uniform(50.0, 60.0, size=(n_outside, 2))

    xs = [ground_xy[:, 0], roof_xy[:, 0], outside_xy[:, 0]]
    ys = [ground_xy[:, 1], roof_xy[:, 1], outside_xy[:, 1]]
    zs = [
        rng.normal(0.0, 0.05, n_ground),
        rng.normal(6.0, 0.05, n_roof),
        rng.normal(999.0, 0.01, n_outside),
    ]
    classes = [
        np.full(n_ground, CLASS_GROUND, dtype=np.uint8),
        np.full(n_roof, CLASS_BUILDING, dtype=np.uint8),
        np.full(n_outside, CLASS_GROUND, dtype=np.uint8),
    ]

    if with_vegetation:
        # A tree overhanging the footprint, well above the roof -- this is
        # what drags an unclassified 95th percentile upward.
        n_veg = 300
        veg_xy = rng.uniform(3.0, 7.0, size=(n_veg, 2))
        xs.append(veg_xy[:, 0])
        ys.append(veg_xy[:, 1])
        zs.append(rng.normal(14.0, 0.3, n_veg))
        classes.append(np.full(n_veg, HIGH_VEGETATION, dtype=np.uint8))

    x, y, z = np.concatenate(xs), np.concatenate(ys), np.concatenate(zs)
    if not classified:
        return LidarPointCloud(x, y, z)
    return LidarPointCloud(x, y, z, np.concatenate(classes))


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

    def test_prefers_the_requested_class(self):
        cloud = make_synthetic_cloud(classified=True, with_vegetation=True)
        result = cloud.height_near(5.0, 5.0, radius=1.0, classes=[CLASS_BUILDING])
        assert result["matched_class"] is True
        assert result["z"] == pytest.approx(6.0, abs=0.3)  # roof, not the tree above

    def test_falls_back_to_all_points_when_the_class_is_absent(self):
        cloud = make_synthetic_cloud(classified=True)
        # Nothing is classified as water here, so it must not refuse to snap.
        result = cloud.height_near(5.0, 5.0, radius=1.0, classes=[9])
        assert result is not None
        assert result["matched_class"] is False

    def test_ignores_classes_on_an_unclassified_cloud(self):
        cloud = make_synthetic_cloud()
        result = cloud.height_near(5.0, 5.0, radius=1.0, classes=[CLASS_BUILDING])
        assert result is not None
        assert result["matched_class"] is False


class TestClassification:
    def test_unclassified_cloud_reports_no_classification(self):
        assert make_synthetic_cloud().has_classification is False

    def test_all_unassigned_counts_as_unclassified(self):
        cloud = make_synthetic_cloud(classified=True)
        cloud.classification[:] = 1  # ASPRS "unassigned"
        assert cloud.has_classification is False

    def test_classes_property_falls_back_to_zeros(self):
        cloud = make_synthetic_cloud()
        assert cloud.classes.shape == (len(cloud),)
        assert not cloud.classes.any()

    def test_filter_classes_include_and_exclude(self):
        cloud = make_synthetic_cloud(classified=True, with_vegetation=True)
        only_roof = cloud.filter_classes(include=[CLASS_BUILDING])
        assert len(only_roof) == 500
        assert (only_roof.classification == CLASS_BUILDING).all()

        no_veg = cloud.filter_classes(exclude=[HIGH_VEGETATION])
        assert len(no_veg) == len(cloud) - 300

    def test_filter_classes_leaves_an_unclassified_cloud_alone(self):
        # Filtering an unclassified cloud would silently discard everything.
        cloud = make_synthetic_cloud()
        assert len(cloud.filter_classes(include=[CLASS_BUILDING])) == len(cloud)

    def test_derived_clouds_preserve_classification(self):
        cloud = make_synthetic_cloud(classified=True)
        for derived in (
            cloud.crop_to_bbox(0.0, 0.0, 10.0, 10.0),
            cloud.points_in_polygon(FOOTPRINT),
            cloud.filter_classes(exclude=[HIGH_VEGETATION]),
        ):
            assert derived.classification is not None
            assert len(derived.classification) == len(derived)


class TestClassifiedStats:
    def test_vegetation_no_longer_inflates_the_roof_height(self):
        veg = make_synthetic_cloud(classified=False, with_vegetation=True)
        unclassified = stats_for_footprint(veg, FOOTPRINT)
        # Without classes the tree at ~14m dominates the 95th percentile.
        assert unclassified["top_height"] > 10.0

        classified = make_synthetic_cloud(classified=True, with_vegetation=True)
        stats = stats_for_footprint(classified, FOOTPRINT)
        assert stats["classified"] is True
        assert stats["top_height"] == pytest.approx(6.0, abs=0.3)

    def test_unclassified_cloud_reproduces_the_old_behaviour(self):
        cloud = make_synthetic_cloud()
        stats = stats_for_footprint(cloud, FOOTPRINT)
        assert stats["classified"] is False
        assert stats["ground_height"] == pytest.approx(0.0, abs=0.3)
        assert stats["top_height"] == pytest.approx(6.0, abs=0.3)

    def test_supplied_ground_height_is_used_verbatim(self):
        # The terrain model is the right source for ground: a building
        # occludes the ground returns underneath it.
        cloud = make_synthetic_cloud(classified=True)
        stats = stats_for_footprint(cloud, FOOTPRINT, ground_height=-1.25)
        assert stats["ground_height"] == pytest.approx(-1.25)
        assert stats["eave_estimate"] > -1.25

    def test_falls_back_when_nothing_is_classified_as_building(self):
        cloud = make_synthetic_cloud(classified=True, with_vegetation=True)
        cloud.classification[cloud.classification == CLASS_BUILDING] = CLASS_GROUND
        stats = stats_for_footprint(cloud, FOOTPRINT)
        # Vegetation is still excluded, so the roof estimate survives.
        assert stats["top_height"] == pytest.approx(6.0, abs=0.3)

    def test_returns_none_with_no_points_over_the_footprint(self):
        cloud = LidarPointCloud(
            np.array([100.0]), np.array([100.0]), np.array([5.0]),
            np.array([CLASS_GROUND], dtype=np.uint8),
        )
        assert stats_for_footprint(cloud, FOOTPRINT) is None


class TestFromFiles:
    """The only tests that go through laspy's real reader -- everything
    else builds clouds straight from numpy arrays."""

    def write_las(self, path, xs, ys, zs, classes):
        import laspy

        header = laspy.LasHeader(version="1.4", point_format=6)
        header.offsets = np.array([0.0, 0.0, 0.0])
        header.scales = np.array([0.01, 0.01, 0.01])
        las = laspy.LasData(header)
        las.x = np.asarray(xs, dtype=float)
        las.y = np.asarray(ys, dtype=float)
        las.z = np.asarray(zs, dtype=float)
        las.classification = np.asarray(classes, dtype=np.uint8)
        las.write(str(path))
        return str(path)

    def test_reads_classification_from_a_real_file(self, tmp_path):
        path = self.write_las(
            tmp_path / "tile.las",
            [1.0, 2.0, 3.0],
            [1.0, 2.0, 3.0],
            [0.0, 5.0, 9.0],
            [CLASS_GROUND, CLASS_BUILDING, HIGH_VEGETATION],
        )
        cloud = LidarPointCloud.from_files([path])
        assert len(cloud) == 3
        assert cloud.has_classification is True
        assert sorted(cloud.classification.tolist()) == [
            CLASS_GROUND,
            HIGH_VEGETATION,
            CLASS_BUILDING,
        ]

    def test_bbox_crop_keeps_coordinates_and_classes_aligned(self, tmp_path):
        path = self.write_las(
            tmp_path / "tile.las",
            [1.0, 5.0, 9.0],
            [1.0, 5.0, 9.0],
            [1.0, 2.0, 3.0],
            [CLASS_GROUND, CLASS_BUILDING, HIGH_VEGETATION],
        )
        cloud = LidarPointCloud.from_files([path], bbox=(4.0, 4.0, 6.0, 6.0))
        assert len(cloud) == 1
        assert cloud.x[0] == pytest.approx(5.0)
        assert cloud.classification.tolist() == [CLASS_BUILDING]

    def test_tiles_outside_the_bbox_are_skipped(self, tmp_path):
        near = self.write_las(
            tmp_path / "near.las", [1.0, 2.0], [1.0, 2.0], [0.0, 1.0],
            [CLASS_GROUND, CLASS_GROUND],
        )
        far = self.write_las(
            tmp_path / "far.las", [900.0, 901.0], [900.0, 901.0], [0.0, 1.0],
            [CLASS_GROUND, CLASS_GROUND],
        )
        cloud = LidarPointCloud.from_files([near, far], bbox=(0.0, 0.0, 10.0, 10.0))
        assert len(cloud) == 2  # the far tile contributed nothing

    def test_on_progress_is_called_with_a_total(self, tmp_path):
        path = self.write_las(
            tmp_path / "tile.las", [1.0, 2.0], [1.0, 2.0], [0.0, 1.0],
            [CLASS_GROUND, CLASS_BUILDING],
        )
        seen = []
        LidarPointCloud.from_files([path], on_progress=lambda r, t: seen.append((r, t)))
        assert seen == [(2, 2)]

    def test_unclassified_file_is_warned_about(self, tmp_path):
        path = self.write_las(
            tmp_path / "tile.las", [1.0, 2.0], [1.0, 2.0], [0.0, 1.0], [1, 1]
        )
        warnings = []
        cloud = LidarPointCloud.from_files([path], warnings=warnings)
        assert cloud.has_classification is False
        assert any("no usable classification" in w for w in warnings)
