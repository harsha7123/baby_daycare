"""Pose/motion-based behaviour signals (EXPERIMENTAL): possible aggression towards a child, and a child who falls
without help and stays down.

These are heuristics over tracked boxes and COCO-17 keypoints. They flag moments for a person to review — they do
not and cannot prove abuse. They are off by default and must be tuned with `vdb eval` on real labelled footage
before being relied on. Everyday care (lifting, carrying, laying a child down, dressing, clapping games) is
deliberately excluded by requiring motion *towards* the child, back-and-forth shaking *relative to the adult*,
and a fast arm movement before any "push".
"""
from collections import defaultdict, deque
from collections.abc import Hashable
from dataclasses import dataclass, field

from vdb.config import Rules, Site
from vdb.schemas import AlertType, FrameResult, Person, Role

L_SHOULDER, R_SHOULDER, L_WRIST, R_WRIST, L_HIP, R_HIP = 5, 6, 9, 10, 11, 12
KP_MIN_CONF = 0.3
HISTORY_SECONDS = 3.0
MAX_STEP_SECONDS = 0.6  # motion is only measured between samples at most this far apart
# Consecutive boxes count as the same person only if similar in size and not far apart (else: tracker ID swap).
SAME_PERSON_MAX_JUMP = 0.75  # centre movement between samples, in body-heights
SAME_PERSON_SIZE_RATIO = 1.67
TORSO_TO_HEIGHT = 0.3  # shoulder-to-hip distance is roughly 30% of standing height
TOWARDS_CHILD_COS = 0.5  # wrist must move within ~60 degrees of the direction to the child
SHAKE_WINDOW_SECONDS = 1.5
SHAKE_MIN_REVERSALS = 2
PUSH_TO_FALL_SECONDS = 1.5
CONTACT_TO_FALL_SECONDS = 1.5
UPRIGHT_RATIO = 1.0  # box height / width
LYING_RATIO = 0.65
UP_AGAIN_RATIO = 0.95  # sitting up (about 1.0) or standing clears a fall
FALL_WINDOW_SECONDS = 1.2
FALL_DROP_FRACTION = 0.25  # centre must drop by this fraction of the upright box height
FALL_STATE_GRACE_SECONDS = 5.0  # a fallen child missed by the detector for a moment keeps their fall state
MAX_PLAUSIBLE_SPEED = 15.0  # body-heights/s; faster means a keypoint/track glitch (e.g. left/right wrist swap)
STRIKE_REACTION_SECONDS = 0.6  # a real blow moves the child; wait this long for that reaction
STRIKE_REACTION_FRACTION = 0.15  # child centre must move this fraction of their height
CRAWL_AWAY_FRACTION = 1.0  # a "fallen" child who travels this many body-widths is crawling, not hurt

Vec = tuple[float, float]


@dataclass
class Event:
    type: AlertType
    key: Hashable
    started_at: float
    message: str
    track_id: int | None = None
    region: tuple[float, float, float, float] | None = None  # normalised box around the people involved


@dataclass
class Sample:
    ts: float
    box: tuple[float, float, float, float]  # x scaled by aspect, so all distances are in frame heights
    wrists: list[Vec]
    torso: float | None
    torso_upright: bool | None = None  # from keypoints: shoulders clearly above hips (sitting/standing)

    @property
    def height(self) -> float:
        return max(self.box[3] - self.box[1], 1e-6)

    @property
    def width(self) -> float:
        return max(self.box[2] - self.box[0], 1e-6)

    @property
    def ratio(self) -> float:
        return self.height / self.width

    @property
    def centre(self) -> Vec:
        return ((self.box[0] + self.box[2]) / 2, (self.box[1] + self.box[3]) / 2)

    @property
    def body_height(self) -> float:
        # Torso length is stable when someone sits, crouches or is cut off by a table; the box is not.
        return self.torso / TORSO_TO_HEIGHT if self.torso else self.height


@dataclass
class TrackState:
    samples: deque[Sample] = field(default_factory=deque)
    fall_at: float | None = None
    fall_centre: Vec | None = None
    fall_width: float = 0.0
    fall_reported: bool = False
    last_seen: float = 0.0

    def mean_height(self, now: Sample) -> float:
        """Body height averaged over recent samples: a held child's box shrinks/merges, a moment's box lies."""
        hs = [s.height for s in self.samples] + [now.height]
        return sum(hs) / len(hs)

    def previous(self, now: Sample, need_wrists: bool = False) -> Sample | None:
        """Latest earlier sample close enough in time and space to be the same person a moment ago."""
        for s in reversed(self.samples):
            if now.ts - s.ts > MAX_STEP_SECONDS:
                return None
            if s.ts < now.ts and (not need_wrists or s.wrists):
                return s if _same_person(s, now) else None
        return None


def _sample(p: Person, ts: float, aspect: float) -> Sample:
    x1, y1, x2, y2 = p.bbox
    wrists: list[Vec] = []
    torso = None
    upright = None
    if p.keypoints:
        kp = p.keypoints

        def point(i: int) -> Vec | None:
            x, y, c = kp[i]
            return (x * aspect, y) if c >= KP_MIN_CONF else None

        wrists = [w for w in (point(L_WRIST), point(R_WRIST)) if w is not None]
        sh = [q for q in (point(L_SHOULDER), point(R_SHOULDER)) if q]
        hips = [q for q in (point(L_HIP), point(R_HIP)) if q]
        if sh and hips:
            s_mid, h_mid = _mean(sh), _mean(hips)
            torso = _dist(s_mid, h_mid)
            upright = (h_mid[1] - s_mid[1]) > abs(h_mid[0] - s_mid[0])  # more vertical than horizontal
    return Sample(ts, (x1 * aspect, y1, x2 * aspect, y2), wrists, torso, upright)


def _mean(pts: list[Vec]) -> Vec:
    return (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))


def _inside(pt: Vec, box: tuple[float, float, float, float], margin: float) -> bool:
    mx, my = (box[2] - box[0]) * margin, (box[3] - box[1]) * margin
    return box[0] - mx <= pt[0] <= box[2] + mx and box[1] - my <= pt[1] <= box[3] + my


def _dist(a: Vec, b: Vec) -> float:
    return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5


def _same_person(a: Sample, b: Sample) -> bool:
    ratio = a.height / b.height
    close = _dist(a.centre, b.centre) <= SAME_PERSON_MAX_JUMP * max(a.height, b.height)
    return close and 1 / SAME_PERSON_SIZE_RATIO <= ratio <= SAME_PERSON_SIZE_RATIO


def _union(a: tuple, b: tuple) -> tuple[float, float, float, float]:
    return (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))


class BehaviorAnalyzer:
    def __init__(self, site: Site, rules: Rules):
        self.rules = rules
        self.fall_rooms = {r.id for r in site.rooms if r.fall_detection}
        # From above, box shape can't tell standing from lying and box height isn't body height.
        self.skip_cameras = {c.id for c in site.cameras if c.foot_point == "center"}
        self.tracks: dict[tuple[str, int], TrackState] = defaultdict(TrackState)
        self.last_contact: dict[tuple[str, int], float] = {}  # child -> last time an adult's hand touched them
        self.last_push: dict[tuple[str, int], tuple[float, int]] = {}  # child -> (time, adult) of a fast hand into them
        self.shakes: dict[tuple[str, int, int], deque[tuple[float, Vec]]] = defaultdict(deque)
        # (cam, adult, child) -> (motion start, hand time, child centre before, child height, speed)
        self.pending_strikes: dict[tuple[str, int, int], tuple[float, float, Vec, float, float]] = {}
        self.recent_strike: dict[tuple[str, int], float] = {}  # child -> when a strike alert was raised for them

    def update(self, fr: FrameResult) -> list[Event]:
        ts, cam = fr.ts, fr.camera_id
        if cam in self.skip_cameras:
            return []
        now = {p.track_id: _sample(p, ts, fr.frame_aspect) for p in fr.people}
        norm_box = {p.track_id: p.bbox for p in fr.people}
        adults = [p.track_id for p in fr.people if p.role == Role.ADULT]
        children = [p.track_id for p in fr.people if p.role == Role.CHILD]
        events: list[Event] = []

        if self.rules.aggression_enabled:
            for a in adults:
                for c in children:
                    events += self._pair(cam, ts, a, c, now, _union(norm_box[a], norm_box[c]))
        if fr.room_id in self.fall_rooms:
            for c in children:
                events += self._fall(cam, ts, c, now[c], norm_box[c])

        for tid, s in now.items():
            st = self.tracks[(cam, tid)]
            st.samples.append(s)
            st.last_seen = ts
            while st.samples and ts - st.samples[0].ts > HISTORY_SECONDS:
                st.samples.popleft()
        self._forget(ts)
        return events

    def _pair(self, cam: str, ts: float, a: int, c: int, now: dict[int, Sample], region: tuple) -> list[Event]:
        adult, child = now[a], now[c]
        margin = self.rules.contact_margin
        touching = [w for w in adult.wrists if _inside(w, child.box, margin)]
        if touching:
            self.last_contact[(cam, c)] = ts
        events: list[Event] = []
        pc = self.tracks[(cam, c)].previous(child)

        # A candidate blow becomes an alert only if the child visibly reacts (is moved) shortly after.
        pending = self.pending_strikes.get((cam, a, c))
        if pending is not None:
            start, hand_ts, before, child_h, speed = pending
            if ts - hand_ts > STRIKE_REACTION_SECONDS:
                del self.pending_strikes[(cam, a, c)]
            elif ts > hand_ts and _dist(child.centre, before) >= STRIKE_REACTION_FRACTION * child_h:
                del self.pending_strikes[(cam, a, c)]
                self.recent_strike[(cam, c)] = ts
                events.append(Event(AlertType.POSSIBLE_AGGRESSION, (cam, a, c), start,
                                    f"Fast adult hand movement into a child ({speed:.1f} body-heights/s) and the child "
                                    "was moved by it. AI flag only: please review the clip.", a, region))

        pa = self.tracks[(cam, a)].previous(adult, need_wrists=True)
        if pa is not None and touching:
            dt = ts - pa.ts
            for w in touching:
                # Nearest earlier wrist, so a left/right keypoint swap doesn't look like a huge jump.
                wp = min(pa.wrists, key=lambda q: _dist(q, w))
                v = ((w[0] - wp[0]) / dt, (w[1] - wp[1]) / dt)
                speed = (v[0] ** 2 + v[1] ** 2) ** 0.5 / adult.body_height
                to_child = (child.centre[0] - wp[0], child.centre[1] - wp[1])
                norm = (v[0] ** 2 + v[1] ** 2) ** 0.5 * max((to_child[0] ** 2 + to_child[1] ** 2) ** 0.5, 1e-9)
                towards = norm > 0 and (v[0] * to_child[0] + v[1] * to_child[1]) / norm >= TOWARDS_CHILD_COS
                if not towards or speed > MAX_PLAUSIBLE_SPEED:
                    continue
                if speed >= self.rules.push_speed:
                    self.last_push[(cam, c)] = (ts, a)
                if speed >= self.rules.strike_speed and (cam, a, c) not in self.pending_strikes:
                    before = pc.centre if pc is not None else child.centre
                    child_h = self.tracks[(cam, c)].mean_height(child)
                    self.pending_strikes[(cam, a, c)] = (pa.ts, ts, before, child_h, speed)
                    break

        pa_box = self.tracks[(cam, a)].previous(adult)
        if touching and pc is not None and pa_box is not None:
            dt = ts - pc.ts
            # Child motion relative to the adult: carrying or lifting moves both together (or one way only);
            # shaking moves the child back and forth against the adult.
            step = ((child.centre[0] - pc.centre[0]) - (adult.centre[0] - pa_box.centre[0]),
                    (child.centre[1] - pc.centre[1]) - (adult.centre[1] - pa_box.centre[1]))
            child_h = self.tracks[(cam, c)].mean_height(child)
            if (step[0] ** 2 + step[1] ** 2) ** 0.5 / dt / child_h >= self.rules.rough_handling_speed:
                q = self.shakes[(cam, a, c)]
                q.append((ts, step))
                while q and ts - q[0][0] > SHAKE_WINDOW_SECONDS:
                    q.popleft()
                steps = [s for _, s in q]
                reversals = sum(1 for u, v in zip(steps, steps[1:]) if u[0] * v[0] + u[1] * v[1] < 0)
                if reversals >= SHAKE_MIN_REVERSALS:
                    events.append(Event(AlertType.POSSIBLE_AGGRESSION, (cam, a, c), q[0][0],
                                        "Child moved back and forth forcefully while held (possible shaking). "
                                        "AI flag only: please review the clip.", a, region))
        return events

    def _fall(self, cam: str, ts: float, c: int, now: Sample, box: tuple) -> list[Event]:
        state = self.tracks[(cam, c)]
        events: list[Event] = []
        if state.fall_at is None:
            earlier = [s for s in state.samples if ts - s.ts <= FALL_WINDOW_SECONDS and s.ratio >= UPRIGHT_RATIO]
            dropped = earlier and now.centre[1] - earlier[-1].centre[1] >= FALL_DROP_FRACTION * earlier[-1].height
            # When pose is available, a torso still upright means sitting down, not falling over.
            if not (dropped and now.ratio <= LYING_RATIO) or now.torso_upright is True:
                return events
            push = self.last_push.get((cam, c))
            pushed = self.rules.push_detection and push is not None and ts - push[0] <= PUSH_TO_FALL_SECONDS
            struck = ts - self.recent_strike.get((cam, c), -1e18) <= PUSH_TO_FALL_SECONDS
            if pushed and not struck:  # one incident, one alert: a strike that floored the child is already flagged
                events.append(Event(AlertType.POSSIBLE_AGGRESSION, (cam, "push", c), push[0],
                                    "Child fell right after a fast adult hand movement into them (possible push). "
                                    "AI flag only: please review the clip.", push[1], box))
            touched = self.last_contact.get((cam, c))
            if pushed or struck or touched is None or ts - touched > CONTACT_TO_FALL_SECONDS:
                state.fall_at, state.fall_reported = ts, False
                state.fall_centre, state.fall_width = now.centre, now.width
            # Otherwise an adult was gently placing the child down (nap, nappy change): not a fall.
        elif now.ratio >= UP_AGAIN_RATIO or now.torso_upright is True:
            state.fall_at = None
        elif state.fall_centre and _dist(now.centre, state.fall_centre) >= CRAWL_AWAY_FRACTION * state.fall_width:
            state.fall_at = None  # moving around on the floor: crawling, not lying hurt
        elif not state.fall_reported and ts - state.fall_at >= self.rules.fall_down_seconds:
            state.fall_reported = True
            events.append(Event(AlertType.CHILD_FALL, (cam, c), state.fall_at,
                                f"Child fell and has not got up for {ts - state.fall_at:.0f}s", c, box))
        return events

    def _forget(self, ts: float) -> None:
        def stale(st: TrackState) -> bool:
            keep = FALL_STATE_GRACE_SECONDS if st.fall_at is not None else HISTORY_SECONDS
            return ts - st.last_seen > keep

        for key in [k for k, st in self.tracks.items() if stale(st)]:
            del self.tracks[key]
        for d in (self.last_contact,):
            for key in [k for k, t in d.items() if ts - t > HISTORY_SECONDS]:
                del d[key]
        for key in [k for k, (t, _) in self.last_push.items() if ts - t > HISTORY_SECONDS]:
            del self.last_push[key]
        for key in [k for k, q in self.shakes.items() if not q or ts - q[-1][0] > HISTORY_SECONDS]:
            del self.shakes[key]
        for key in [k for k, p in self.pending_strikes.items() if ts - p[1] > STRIKE_REACTION_SECONDS]:
            del self.pending_strikes[key]
        for key in [k for k, t in self.recent_strike.items() if ts - t > HISTORY_SECONDS]:
            del self.recent_strike[key]
