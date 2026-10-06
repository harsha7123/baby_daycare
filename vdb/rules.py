from collections import defaultdict, deque
from collections.abc import Hashable
from dataclasses import dataclass
from statistics import median_low

from vdb.config import Rules, Site
from vdb.schemas import Alert, AlertType, FrameResult, Role, RoomStat

# A camera whose last frame is older than this no longer counts towards room occupancy.
STALE_FRAME_SECONDS = 5.0


class Sustained:
    """Tracks conditions that must hold for `duration` seconds, tolerating short detection gaps.

    `update` reports the episode start once the condition has lasted long enough; the caller marks it
    `fire`d only when an alert was actually sent, so an alert held back by a cooldown is retried later.
    An episode ends when the condition has been false for longer than `gap`.
    """

    def __init__(self, duration: float, gap: float):
        self.duration = duration
        self.gap = gap
        self._start: dict[Hashable, float] = {}
        self._last_true: dict[Hashable, float] = {}
        self._fired: set[Hashable] = set()

    def update(self, key: Hashable, active: bool, now: float) -> float | None:
        if not active:
            if key in self._last_true and now - self._last_true[key] > self.gap:
                self._reset(key)
            return None
        if key not in self._start or now - self._last_true[key] > self.gap:
            self._reset(key)
            self._start[key] = now
        self._last_true[key] = now
        if key not in self._fired and now - self._start[key] >= self.duration:
            return self._start[key]
        return None

    def fire(self, key: Hashable) -> None:
        self._fired.add(key)

    def expire(self, now: float) -> None:
        """Ends episodes for keys that stopped being reported (e.g. a track left the frame)."""
        for key in [k for k, t in self._last_true.items() if now - t > self.gap]:
            self._reset(key)

    def _reset(self, key: Hashable) -> None:
        self._start.pop(key, None)
        self._last_true.pop(key, None)
        self._fired.discard(key)


@dataclass
class RoomCounts:
    adults: int
    children: int
    unknown: int
    online: int

    def no_adult(self) -> bool:
        # Unknown-role people are still being classified and might be adults, so they block this alert.
        return self.children > 0 and self.adults == 0 and self.unknown == 0

    def ratio_breach(self, max_ratio: int) -> bool:
        return self.adults > 0 and self.children > (self.adults + self.unknown) * max_ratio


class RulesEngine:
    """Turns per-frame tracking results into alerts and periodic room statistics for one site."""

    def __init__(self, site: Site, rules: Rules):
        self.site = site
        self.rules = rules
        self.rooms = {r.id: r for r in site.rooms}
        self.room_cams: dict[str, list[str]] = defaultdict(list)
        for cam in site.cameras:
            self.room_cams[cam.room].append(cam.id)
        self.cam_room = {c.id: c.room for c in site.cameras}

        gap = rules.gap_tolerance_seconds
        self.no_adult = Sustained(rules.no_adult_seconds, gap)
        self.ratio = Sustained(rules.ratio_seconds, gap)
        self.phone = Sustained(rules.phone_seconds, gap)
        self.zone = Sustained(rules.restricted_zone_seconds, gap)

        self.latest: dict[str, FrameResult] = {}
        self.last_seen: dict[str, float] = {}
        self.offline: set[str] = set()
        self._history: dict[str, deque[tuple[float, int, int, int]]] = defaultdict(deque)
        self._last_alert: dict[Hashable, float] = {}
        self._last_stat: dict[str, float] = {}

    def on_frame(self, fr: FrameResult) -> tuple[list[Alert], list[RoomStat]]:
        now = fr.ts
        self.latest[fr.camera_id] = fr
        self.last_seen[fr.camera_id] = now
        self.offline.discard(fr.camera_id)
        alerts: list[Alert] = []

        self.phone.expire(now)
        self.zone.expire(now)
        for p in fr.people:
            if p.role == Role.ADULT and p.has_phone:
                key = (fr.camera_id, p.track_id)
                if (start := self.phone.update(key, True, now)) is not None:
                    # Cooldown per camera, not per track: a tracker ID switch must not re-alert the same person.
                    alerts += self._emit(self.phone, key, ("phone", fr.camera_id), fr, fr.camera_id,
                                         AlertType.PHONE_USE, "medium", start, now,
                                         f"Caretaker using a phone for {now - start:.0f}s", p.track_id)
            if p.role == Role.CHILD:
                for zone in p.zones:
                    key = (fr.camera_id, p.track_id, zone)
                    if (start := self.zone.update(key, True, now)) is not None:
                        alerts += self._emit(self.zone, key, ("zone", fr.camera_id, zone), fr, fr.camera_id,
                                             AlertType.RESTRICTED_ZONE, "high", start, now,
                                             f"Child in restricted area '{zone}'", p.track_id)

        room = self.rooms[fr.room_id]
        counts = self._room_counts(room.id, now)
        if counts.online:
            name = room.name
            if (start := self.no_adult.update(room.id, counts.no_adult(), now)) is not None:
                alerts += self._emit(self.no_adult, room.id, ("no_adult", room.id), fr, self._evidence_camera(room.id, now),
                                     AlertType.NO_ADULT, "high", start, now,
                                     f"No adult in {name} for {now - start:.0f}s with {counts.children} child(ren)")
            breach = counts.ratio_breach(room.max_children_per_adult)
            if (start := self.ratio.update(room.id, breach, now)) is not None:
                alerts += self._emit(self.ratio, room.id, ("ratio", room.id), fr, self._evidence_camera(room.id, now),
                                     AlertType.RATIO_BREACH, "medium", start, now,
                                     f"{counts.children} children with {counts.adults} adult(s) in {name} "
                                     f"(max {room.max_children_per_adult} per adult)")

        stats: list[RoomStat] = []
        if now - self._last_stat.get(room.id, -1e18) >= self.rules.stats_interval_seconds:
            self._last_stat[room.id] = now
            stats.append(RoomStat(
                site_id=fr.site_id, room_id=room.id, ts=now, adults=counts.adults, children=counts.children,
                cameras_online=counts.online, no_adult=counts.no_adult(),
                ratio_ok=not counts.no_adult() and not counts.ratio_breach(room.max_children_per_adult),
            ))
        return alerts, stats

    def tick(self, now: float) -> list[Alert]:
        """Periodic check for cameras that stopped sending frames."""
        alerts = []
        for cam_id, room in self.cam_room.items():
            last = self.last_seen.setdefault(cam_id, now)
            if cam_id not in self.offline and now - last >= self.rules.camera_offline_seconds:
                self.offline.add(cam_id)
                alerts.append(Alert(
                    site_id=self.site.id, room_id=room, camera_id=cam_id,
                    type=AlertType.CAMERA_OFFLINE, severity="medium", started_at=last, triggered_at=now,
                    message=f"Camera {cam_id} in {self.rooms[room].name} has sent no video for {now - last:.0f}s",
                ))
        return alerts

    def _instant_counts(self, room: str, now: float) -> tuple[int, int, int, int]:
        combine = max if self.rooms[room].cameras_overlap else sum
        per_cam = []
        for cam_id in self.room_cams[room]:
            fr = self.latest.get(cam_id)
            if fr is None or now - fr.ts > STALE_FRAME_SECONDS:
                continue
            roles = [p.role for p in fr.people]
            per_cam.append((roles.count(Role.ADULT), roles.count(Role.CHILD), roles.count(Role.UNKNOWN)))
        if not per_cam:
            return 0, 0, 0, 0
        adults, children, unknown = (combine(c[i] for c in per_cam) for i in range(3))
        return adults, children, unknown, len(per_cam)

    def _room_counts(self, room: str, now: float) -> RoomCounts:
        adults, children, unknown, online = self._instant_counts(room, now)
        hist = self._history[room]
        hist.append((now, adults, children, unknown))
        while hist and now - hist[0][0] > self.rules.count_window_seconds:
            hist.popleft()
        return RoomCounts(
            adults=median_low(h[1] for h in hist), children=median_low(h[2] for h in hist),
            unknown=median_low(h[3] for h in hist), online=online,
        )

    def _evidence_camera(self, room: str, now: float) -> str:
        """The room camera that currently sees the most children — the best footage for a room-level alert."""
        fresh = [self.latest[c] for c in self.room_cams[room]
                 if c in self.latest and now - self.latest[c].ts <= STALE_FRAME_SECONDS]
        best = max(fresh, key=lambda fr: sum(p.role == Role.CHILD for p in fr.people))
        return best.camera_id

    def _emit(
        self, cond: Sustained, cond_key: Hashable, cooldown_key: Hashable, fr: FrameResult, camera_id: str,
        type_: AlertType, severity: str, start: float, now: float, message: str, track_id: int | None = None,
    ) -> list[Alert]:
        if now - self._last_alert.get(cooldown_key, -1e18) < self.rules.cooldown_seconds:
            return []
        self._last_alert[cooldown_key] = now
        cond.fire(cond_key)
        return [Alert(
            site_id=fr.site_id, room_id=fr.room_id, camera_id=camera_id, type=type_, severity=severity,
            started_at=start, triggered_at=now, message=message, track_id=track_id,
        )]
