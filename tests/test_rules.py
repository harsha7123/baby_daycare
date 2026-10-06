from vdb.config import Camera, Room, Rules, Site, Zone
from vdb.rules import RulesEngine, Sustained
from vdb.schemas import AlertType, FrameResult, Person, Role

SITE = Site(
    id="s1", name="Test",
    rooms=[Room(id="r1", name="Toddlers", max_children_per_adult=2)],
    cameras=[Camera(id="c1", room="r1", source="x", restricted_zones=[Zone(name="kitchen", polygon=[(0, 0), (1, 0), (1, 1)])])],
)
RULES = Rules(no_adult_seconds=10, ratio_seconds=10, phone_seconds=5, restricted_zone_seconds=2,
              camera_offline_seconds=30, gap_tolerance_seconds=2, cooldown_seconds=60)


def person(tid: int, role: Role, phone: bool = False, zones: list[str] | None = None) -> Person:
    return Person(track_id=tid, role=role, bbox=(0, 0, 0.1, 0.1), confidence=0.9, has_phone=phone, zones=zones or [])


def run(engine: RulesEngine, people_at: dict[float, list[Person]], camera: str = "c1") -> list:
    alerts = []
    for ts, people in sorted(people_at.items()):
        a, _ = engine.on_frame(FrameResult(site_id="s1", room_id="r1", camera_id=camera, ts=ts, people=people))
        alerts += a
    return alerts


def every(start: float, end: float, people: list[Person], step: float = 1.0) -> dict[float, list[Person]]:
    out, t = {}, start
    while t <= end:
        out[t] = people
        t += step
    return out


class TestSustained:
    def test_reports_once_fired(self):
        s = Sustained(duration=3, gap=1)
        fired = []
        for t in range(6):
            start = s.update("k", True, t)
            fired.append(start)
            if start is not None:
                s.fire("k")
        assert fired == [None, None, None, 0, None, None]

    def test_keeps_reporting_until_fired(self):
        s = Sustained(duration=3, gap=1)
        assert [s.update("k", True, t) for t in range(6)] == [None, None, None, 0, 0, 0]

    def test_short_gap_does_not_reset(self):
        s = Sustained(duration=3, gap=2)
        s.update("k", True, 0)
        s.update("k", False, 1)
        s.update("k", True, 2)
        assert s.update("k", True, 3) == 0

    def test_long_gap_starts_new_episode(self):
        s = Sustained(duration=3, gap=1)
        s.update("k", True, 0)
        assert s.update("k", True, 5) is None
        assert [s.update("k", True, t) for t in (6, 7, 8)] == [None, None, 5]


class TestRules:
    def test_no_adult_alert_after_threshold(self):
        alerts = run(RulesEngine(SITE, RULES), every(0, 12, [person(1, Role.CHILD)]))
        assert [a.type for a in alerts] == [AlertType.NO_ADULT]
        assert alerts[0].triggered_at == 10

    def test_no_adult_suppressed_while_role_unknown(self):
        alerts = run(RulesEngine(SITE, RULES), every(0, 20, [person(1, Role.CHILD), person(2, Role.UNKNOWN)]))
        assert alerts == []

    def test_adult_present_no_alert(self):
        alerts = run(RulesEngine(SITE, RULES), every(0, 30, [person(1, Role.CHILD), person(2, Role.ADULT)]))
        assert alerts == []

    def test_brief_missed_adult_detection_is_smoothed_out(self):
        both = [person(1, Role.CHILD), person(2, Role.ADULT)]
        frames = every(0, 40, both)
        for t in (12.0, 13.0, 25.0, 26.0):
            frames[t] = [person(1, Role.CHILD)]
        assert run(RulesEngine(SITE, RULES), frames) == []

    def test_ratio_breach(self):
        kids = [person(i, Role.CHILD) for i in range(1, 4)]
        alerts = run(RulesEngine(SITE, RULES), every(0, 12, kids + [person(9, Role.ADULT)]))
        assert [a.type for a in alerts] == [AlertType.RATIO_BREACH]

    def test_separate_cameras_sum_counts(self):
        site = SITE.model_copy(update={
            "rooms": [Room(id="r1", name="Big", max_children_per_adult=2, cameras_overlap=False)],
            "cameras": [Camera(id="c1", room="r1", source="x"), Camera(id="c2", room="r1", source="x")],
        })
        engine = RulesEngine(site, RULES)
        alerts = []
        for t in range(0, 12):
            for cam, kids in (("c1", [1, 2]), ("c2", [3, 4])):
                people = [person(k, Role.CHILD) for k in kids] + ([person(9, Role.ADULT)] if cam == "c1" else [])
                a, _ = engine.on_frame(FrameResult(site_id="s1", room_id="r1", camera_id=cam, ts=t, people=people))
                alerts += a
        # 4 children over two areas, 1 adult, max 2 per adult: a breach that max-per-camera would miss.
        assert [a.type for a in alerts] == [AlertType.RATIO_BREACH]

    def test_phone_use_tolerates_flicker(self):
        frames = {t: [person(5, Role.ADULT, phone=(t % 3 != 0))] for t in range(0, 8)}
        alerts = run(RulesEngine(SITE, RULES), frames)
        assert [a.type for a in alerts] == [AlertType.PHONE_USE]
        assert alerts[0].track_id == 5

    def test_phone_track_id_switch_does_not_realert(self):
        frames = every(0, 6, [person(5, Role.ADULT, phone=True)]) | every(7, 20, [person(6, Role.ADULT, phone=True)])
        assert len(run(RulesEngine(SITE, RULES), frames)) == 1

    def test_phone_glance_no_alert(self):
        frames = every(0, 2, [person(5, Role.ADULT, phone=True)]) | every(3, 20, [person(5, Role.ADULT)])
        assert run(RulesEngine(SITE, RULES), frames) == []

    def test_restricted_zone(self):
        alerts = run(RulesEngine(SITE, RULES), every(0, 3, [person(1, Role.CHILD, zones=["kitchen"]), person(2, Role.ADULT)]))
        assert [a.type for a in alerts] == [AlertType.RESTRICTED_ZONE]

    def test_episode_during_cooldown_alerts_when_cooldown_ends(self):
        frames = every(0, 12, [person(1, Role.CHILD)]) | every(13, 30, [person(1, Role.CHILD), person(2, Role.ADULT)]) \
            | every(31, 100, [person(1, Role.CHILD)])
        alerts = run(RulesEngine(SITE, RULES), frames)
        assert [(a.type, a.triggered_at) for a in alerts] == [(AlertType.NO_ADULT, 10), (AlertType.NO_ADULT, 70)]

    def test_room_alert_uses_camera_seeing_children(self):
        site = SITE.model_copy(update={"cameras": [Camera(id="c1", room="r1", source="x"), Camera(id="c2", room="r1", source="x")]})
        engine = RulesEngine(site, RULES)
        alerts = []
        for t in range(0, 12):
            alerts += engine.on_frame(FrameResult(site_id="s1", room_id="r1", camera_id="c2", ts=t, people=[person(1, Role.CHILD)]))[0]
            alerts += engine.on_frame(FrameResult(site_id="s1", room_id="r1", camera_id="c1", ts=t + 0.5, people=[]))[0]
        assert [a.camera_id for a in alerts] == ["c2"]

    def test_camera_offline(self):
        engine = RulesEngine(SITE, RULES)
        assert engine.tick(0) == []
        assert [a.type for a in engine.tick(31)] == [AlertType.CAMERA_OFFLINE]
        assert engine.tick(40) == []

    def test_room_stats_interval_and_consistency(self):
        engine = RulesEngine(SITE, RULES.model_copy(update={"stats_interval_seconds": 5}))
        stats = []
        for t in range(0, 11):
            _, s = engine.on_frame(FrameResult(site_id="s1", room_id="r1", camera_id="c1", ts=t, people=[person(1, Role.CHILD)]))
            stats += s
        assert [s.ts for s in stats] == [0, 5, 10]
        assert stats[0].no_adult and not stats[0].ratio_ok
