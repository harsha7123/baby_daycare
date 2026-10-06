from vdb.config import Camera, Room, Rules, Site
from vdb.rules import RulesEngine
from vdb.schemas import AlertType, FrameResult, Person, Role

RULES = Rules(aggression_enabled=True, fall_down_seconds=5, aggression_cooldown_seconds=30)
SITE = Site(id="s1", name="T",
            rooms=[Room(id="r1", name="Room", fall_detection=True), Room(id="nap", name="Nap")],
            cameras=[Camera(id="c1", room="r1", source="x"), Camera(id="c2", room="nap", source="x"),
                     Camera(id="c3", room="r1", source="x", foot_point="center")])
DT = 0.2  # 5 fps


def keypoints(box, wrist: tuple[float, float] | None = None) -> list[tuple[float, float, float]]:
    """Upright COCO-17 keypoints inside a box (torso = 30% of height); both wrists at `wrist` when given."""
    x1, y1, x2, y2 = box
    cx, h = (x1 + x2) / 2, y2 - y1
    kps = [(cx, y1 + 0.1 * h, 0.9)] * 17
    for i in (5, 6):
        kps[i] = (cx, y1 + 0.2 * h, 0.9)
    for i in (11, 12):
        kps[i] = (cx, y1 + 0.5 * h, 0.9)
    w = wrist or (cx, y1 + 0.5 * h)
    kps[9] = kps[10] = (w[0], w[1], 0.9)
    return kps


def adult(box, wrist=None, tid=1, pose=True) -> Person:
    return Person(track_id=tid, role=Role.ADULT, bbox=box, confidence=0.9, keypoints=keypoints(box, wrist) if pose else None)


def child(box, tid=2) -> Person:
    return Person(track_id=tid, role=Role.CHILD, bbox=box, confidence=0.9)


def run(frames: list[list[Person]], camera: str = "c1", room: str = "r1", aspect: float = 1.0) -> list:
    engine = RulesEngine(SITE, RULES)
    alerts = []
    for i, people in enumerate(frames):
        fr = FrameResult(site_id="s1", room_id=room, camera_id=camera, ts=i * DT, people=people, frame_aspect=aspect)
        alerts += engine.on_frame(fr)[0]
    return alerts


def shifted(box, dx=0.0, dy=0.0):
    return (box[0] + dx, box[1] + dy, box[2] + dx, box[3] + dy)


ADULT_BOX = (0.10, 0.20, 0.30, 0.80)  # height 0.6
CHILD_BOX = (0.35, 0.55, 0.45, 0.80)  # height 0.25, width 0.1 -> upright
CHILD_CENTRE = (0.40, 0.675)
FAR_HAND = (0.05, 0.5)
# Hand travels ~0.39 in 0.2 s (~3.3 body-heights/s) towards and into the child, who is then knocked aside.
STRIKE = [[adult(ADULT_BOX, FAR_HAND), child(CHILD_BOX)],
          [adult(ADULT_BOX, CHILD_CENTRE), child(CHILD_BOX)],
          [adult(ADULT_BOX, (0.45, 0.68)), child(shifted(CHILD_BOX, dx=0.05))]]


class TestAggression:
    def test_fast_strike_that_moves_child_is_flagged(self):
        alerts = run(STRIKE)
        assert [a.type for a in alerts] == [AlertType.POSSIBLE_AGGRESSION]
        assert alerts[0].severity == "high" and "review" in alerts[0].message
        assert alerts[0].region is not None

    def test_fast_touch_without_child_reacting_is_play(self):
        # Clapping game / tickle: fast hand, but the child is not moved by it.
        frames = STRIKE[:2] + [[adult(ADULT_BOX, CHILD_CENTRE), child(CHILD_BOX)]] * 4
        assert run(frames) == []

    def test_widescreen_aspect(self):
        assert [a.type for a in run(STRIKE, aspect=16 / 9)] == [AlertType.POSSIBLE_AGGRESSION]

    def test_slow_reach_to_child_is_normal_care(self):
        path = [(0.20 + 0.04 * i, 0.5 + 0.03 * i) for i in range(6)]
        frames = [[adult(ADULT_BOX, w), child(CHILD_BOX)] for w in path]
        assert run(frames) == []

    def test_fast_hand_moving_away_from_child_ignored(self):
        frames = [[adult(ADULT_BOX, (0.44, 0.70)), child(CHILD_BOX)], [adult(ADULT_BOX, (0.36, 0.56)), child(shifted(CHILD_BOX, dx=0.05))],
                  [adult(ADULT_BOX, FAR_HAND), child(shifted(CHILD_BOX, dx=0.1))]]
        assert run(frames) == []

    def test_fast_wave_not_touching_child_ignored(self):
        frames = [[adult(ADULT_BOX, (0.05, 0.2)), child(CHILD_BOX)], [adult(ADULT_BOX, (0.25, 0.25)), child(CHILD_BOX)]]
        assert run(frames) == []

    def test_missing_wrists_never_crash_or_flag(self):
        frames = [[adult(ADULT_BOX, pose=False), child(CHILD_BOX)]] + STRIKE[1:]
        assert run(frames) == []

    def test_tracker_id_swap_is_not_a_fast_hand(self):
        # Track 1 jumps across the room between frames (a different person took its id).
        frames = [[adult(shifted(ADULT_BOX, dx=0.6), (0.65, 0.5)), child(CHILD_BOX)]] + STRIKE[1:]
        assert run(frames) == []

    def test_shaking_back_and_forth_while_held(self):
        boxes = [CHILD_BOX, shifted(CHILD_BOX, dx=0.1), CHILD_BOX, shifted(CHILD_BOX, dx=0.1)]
        frames = [[adult(ADULT_BOX, ((b[0] + b[2]) / 2, 0.65)), child(b)] for b in boxes]
        assert [a.type for a in run(frames)] == [AlertType.POSSIBLE_AGGRESSION]

    def test_carrying_child_while_walking_is_care(self):
        frames = [[adult(shifted(ADULT_BOX, dx=0.08 * i), (0.4 + 0.08 * i, 0.65)), child(shifted(CHILD_BOX, dx=0.08 * i))]
                  for i in range(6)]
        assert run(frames) == []

    def test_lifting_child_is_care(self):
        frames = [[adult(ADULT_BOX, (0.4, 0.65 - 0.08 * i)), child(shifted(CHILD_BOX, dy=-0.08 * i))] for i in range(5)]
        assert run(frames) == []

    def test_child_running_without_contact_is_play(self):
        frames = [[adult(ADULT_BOX, (0.15, 0.5)), child(shifted(CHILD_BOX, dx=0.1 * i))] for i in range(4)]
        assert run(frames) == []

    def test_ceiling_camera_skipped(self):
        assert run(STRIKE, camera="c3") == []

    def test_aggression_cooldown_per_pair(self):
        assert len(run(STRIKE * 3)) == 1

    def test_verification_marked_pending_when_verifier_configured(self):
        engine = RulesEngine(SITE, RULES)
        engine.verification_pending = True
        alerts = []
        for i, people in enumerate(STRIKE):
            alerts += engine.on_frame(FrameResult(site_id="s1", room_id="r1", camera_id="c1", ts=i * DT,
                                                  people=people, frame_aspect=1.0))[0]
        assert alerts[0].verification == "pending"

    def test_off_by_default(self):
        engine = RulesEngine(SITE, Rules())
        alerts = []
        for i, people in enumerate(STRIKE):
            alerts += engine.on_frame(FrameResult(site_id="s1", room_id="r1", camera_id="c1", ts=i * DT, people=people))[0]
        assert alerts == []


LYING = (0.30, 0.72, 0.55, 0.80)  # wide and low
SITTING = (0.33, 0.65, 0.48, 0.80)  # about as wide as tall


class TestFalls:
    def test_child_falls_and_stays_down(self):
        frames = [[child(CHILD_BOX)]] * 3 + [[child(LYING)]] * 30
        alerts = run(frames)
        assert [a.type for a in alerts] == [AlertType.CHILD_FALL]
        assert alerts[0].started_at == 3 * DT

    def test_child_falls_and_gets_up_quickly(self):
        frames = [[child(CHILD_BOX)]] * 3 + [[child(LYING)]] * 5 + [[child(CHILD_BOX)]] * 30
        assert run(frames) == []

    def test_child_falls_then_sits_up(self):
        frames = [[child(CHILD_BOX)]] * 3 + [[child(LYING)]] * 3 + [[child(SITTING)]] * 40
        assert run(frames) == []

    def test_crawling_away_after_dropping_down(self):
        frames = [[child(CHILD_BOX)]] * 3 + [[child(shifted(LYING, dx=0.05 * i))] for i in range(30)]
        assert run(frames) == []

    def test_lying_down_slowly_for_nap_is_not_a_fall(self):
        mids = [(0.35 - 0.02 * i, 0.55 + 0.03 * i, 0.45 + 0.02 * i, 0.80) for i in range(1, 6)]
        frames = [[child(CHILD_BOX)]] + [[child(b)] for b in mids for _ in range(4)] + [[child(LYING)]] * 40
        assert run(frames) == []

    def test_fall_detection_off_in_nap_room(self):
        frames = [[child(CHILD_BOX)]] * 3 + [[child(LYING)]] * 30
        assert run(frames, camera="c2", room="nap") == []

    def test_child_laid_down_by_adult_is_not_a_fall(self):
        frames = ([[adult(ADULT_BOX, CHILD_CENTRE), child(CHILD_BOX)]] * 3
                  + [[adult(ADULT_BOX, (0.42, 0.74)), child(LYING)]] * 40)
        assert run(frames) == []

    def test_fall_right_after_fast_hand_flags_one_possible_aggression(self):
        frames = ([[adult(ADULT_BOX, FAR_HAND), child(CHILD_BOX)], [adult(ADULT_BOX, CHILD_CENTRE), child(CHILD_BOX)]]
                  + [[adult(ADULT_BOX, (0.15, 0.5)), child(LYING)]] * 30)
        types = [a.type for a in run(frames)]
        assert types.count(AlertType.POSSIBLE_AGGRESSION) == 1
        assert AlertType.CHILD_FALL in types
