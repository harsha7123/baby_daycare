import numpy as np

from vdb.config import Zone
from vdb.schemas import Role
from vdb.vision import TrackRoles, assign_phones, crop, letterbox, zones_for


class TestAssignPhones:
    def test_phone_goes_to_person_containing_it(self):
        persons = np.array([[0, 0, 100, 300], [200, 0, 300, 300]], dtype=float)
        phones = np.array([[230, 80, 250, 110]], dtype=float)
        assert assign_phones(persons, phones).tolist() == [False, True]

    def test_phone_outside_everyone_is_ignored(self):
        persons = np.array([[0, 0, 100, 300]], dtype=float)
        phones = np.array([[500, 500, 520, 530]], dtype=float)
        assert assign_phones(persons, phones).tolist() == [False]

    def test_overlap_prefers_upper_body(self):
        # Adult standing behind a seated child; phone at adult's chest height.
        persons = np.array([[0, 0, 200, 400], [50, 250, 150, 400]], dtype=float)
        phones = np.array([[90, 120, 110, 150]], dtype=float)
        assert assign_phones(persons, phones).tolist() == [True, False]


class TestZones:
    KITCHEN = Zone(name="kitchen", polygon=[(0.5, 0.5), (1, 0.5), (1, 1), (0.5, 1)])

    def test_feet_inside_zone(self):
        assert zones_for((0.6, 0.3, 0.7, 0.8), [self.KITCHEN]) == ["kitchen"]

    def test_body_overlaps_but_feet_outside(self):
        assert zones_for((0.6, 0.1, 0.7, 0.4), [self.KITCHEN]) == []

    def test_overhead_camera_uses_box_centre(self):
        assert zones_for((0.6, 0.3, 0.7, 0.8), [self.KITCHEN], "center") == ["kitchen"]
        assert zones_for((0.6, 0.2, 0.7, 0.6), [self.KITCHEN], "center") == []


class TestTrackRoles:
    def test_unknown_until_min_samples(self):
        roles = TrackRoles(refresh_seconds=1, min_samples=2, grace_seconds=6)
        roles.seen(("c", 1), 0)
        roles.add(("c", 1), 0.9, 0)
        assert roles.role(("c", 1), 0) == Role.UNKNOWN
        roles.add(("c", 1), 0.8, 1)
        assert roles.role(("c", 1), 1) == Role.ADULT

    def test_single_sample_used_after_grace(self):
        roles = TrackRoles(refresh_seconds=1, min_samples=2, grace_seconds=6)
        roles.seen(("c", 1), 0)
        roles.add(("c", 1), 0.2, 0)
        assert roles.role(("c", 1), 7) == Role.CHILD

    def test_never_classified_person_excluded_after_grace(self):
        roles = TrackRoles(refresh_seconds=1, min_samples=2, grace_seconds=6)
        roles.seen(("c", 1), 0)
        assert roles.role(("c", 1), 3) == Role.UNKNOWN
        assert roles.role(("c", 1), 7) is None

    def test_refresh_interval(self):
        roles = TrackRoles(refresh_seconds=2, min_samples=1, grace_seconds=6)
        roles.add(("c", 1), 0.1, 0)
        assert not roles.needs_sample(("c", 1), 1)
        assert roles.needs_sample(("c", 1), 2)

    def test_role_survives_brief_occlusion_but_not_long_absence(self):
        roles = TrackRoles(refresh_seconds=1, min_samples=1, grace_seconds=6)
        roles.seen(("c", 1), 0)
        roles.add(("c", 1), 0.1, 0)
        roles.prune(now=3, max_age=4)
        assert roles.role(("c", 1), 3) == Role.CHILD
        roles.prune(now=5, max_age=4)
        assert roles.role(("c", 1), 5) == Role.UNKNOWN


def test_crop_rejects_tiny_boxes():
    frame = np.zeros((100, 100, 3), dtype=np.uint8)
    assert crop(frame, np.array([10, 10, 20, 20])) is None
    assert crop(frame, np.array([-5, 0, 50, 60])).shape == (60, 50, 3)


def test_letterbox_keeps_whole_person():
    img = np.ones((300, 100, 3), dtype=np.uint8)
    out = letterbox(img)
    assert out.shape == (300, 300, 3)
    assert out[:, 100:200].min() == 1
