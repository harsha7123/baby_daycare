"""Synthetic data generator: drives the API and dashboard without cameras or a GPU (UI work, sales demos).

Everything it produces is clearly stamped DEMO DATA.
"""
import asyncio
import logging
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import cv2
import imageio_ffmpeg
import numpy as np

from vdb.config import Camera, Settings, Site
from vdb.preview import render_preview
from vdb.schemas import Alert, AlertType, AlertUpdate, ClipReady, FrameResult, Person, Role, RoomStat

log = logging.getLogger(__name__)
FRAME_W, FRAME_H = 960, 540
HISTORY_HOURS = 8
ALERT_EVERY_SECONDS = (20, 45)
BANNER = "DEMO DATA"

ALERT_TEXT = {
    AlertType.NO_ADULT: ("high", "No adult in {room} for 24s with {kids} child(ren)"),
    AlertType.RATIO_BREACH: ("medium", "{kids} children with 1 adult(s) in {room} (max {ratio} per adult)"),
    AlertType.PHONE_USE: ("medium", "Caretaker using a phone for 12s"),
    AlertType.RESTRICTED_ZONE: ("high", "Child in restricted area 'kitchen door'"),
    AlertType.POSSIBLE_AGGRESSION: ("high", "Possible rough handling: fast adult arm movement towards a child"),
    AlertType.CHILD_FALL: ("medium", "Child fell and has not got up for 12s"),
    AlertType.CAMERA_OFFLINE: ("medium", "Camera {cam} in {room} has sent no video for 31s"),
}


@dataclass
class SimPerson:
    track_id: int
    role: Role
    x: float
    y: float
    vx: float = 0.0
    vy: float = 0.0
    has_phone: bool = False

    def step(self, dt: float) -> None:
        self.vx = 0.85 * self.vx + random.gauss(0, 0.02)
        self.vy = 0.85 * self.vy + random.gauss(0, 0.012)
        self.x = min(max(self.x + self.vx * dt, 0.08), 0.92)
        self.y = min(max(self.y + self.vy * dt, 0.45), 0.92)

    def bbox(self) -> tuple[float, float, float, float]:
        h = 0.42 if self.role == Role.ADULT else 0.22
        w = h * 0.38
        return (self.x - w / 2, self.y - h, self.x + w / 2, self.y)

    def keypoints(self) -> list[tuple[float, float, float]]:
        x1, y1, x2, y2 = self.bbox()
        cx, h, w = (x1 + x2) / 2, y2 - y1, x2 - x1
        y = lambda f: y1 + f * h  # noqa: E731
        sh, hip = w * 0.45, w * 0.3
        return [
            (cx, y(0.07), 1), (cx - 0.1 * w, y(0.05), 1), (cx + 0.1 * w, y(0.05), 1),
            (cx - 0.2 * w, y(0.07), 1), (cx + 0.2 * w, y(0.07), 1),
            (cx - sh, y(0.2), 1), (cx + sh, y(0.2), 1), (cx - sh * 1.1, y(0.36), 1), (cx + sh * 1.1, y(0.36), 1),
            (cx - sh * 0.9, y(0.5), 1), (cx + sh * 0.9, y(0.5), 1),
            (cx - hip, y(0.53), 1), (cx + hip, y(0.53), 1), (cx - hip, y(0.75), 1), (cx + hip, y(0.75), 1),
            (cx - hip, y(0.97), 1), (cx + hip, y(0.97), 1),
        ]


@dataclass
class SimCamera:
    site: Site
    cam: Camera
    people: list[SimPerson] = field(default_factory=list)

    def frame(self, now: float) -> tuple[np.ndarray, FrameResult]:
        img = np.zeros((FRAME_H, FRAME_W, 3), np.uint8)
        img[:] = (232, 226, 218)
        img[int(FRAME_H * 0.42):] = (196, 205, 214)  # floor
        cv2.rectangle(img, (40, 60), (300, 200), (220, 214, 205), -1)  # window
        for p in self.people:
            x1, y1, x2, y2 = (int(v * s) for v, s in zip(p.bbox(), (FRAME_W, FRAME_H, FRAME_W, FRAME_H)))
            body = (120, 110, 160) if p.role == Role.ADULT else (90, 170, 220)
            cv2.rectangle(img, (x1 + 4, y1 + (y2 - y1) // 5), (x2 - 4, y2), body, -1)
            cv2.circle(img, ((x1 + x2) // 2, y1 + (y2 - y1) // 10), max(4, (x2 - x1) // 3), (170, 190, 225), -1)
        people = [
            Person(track_id=p.track_id, role=p.role, bbox=p.bbox(), confidence=0.9, has_phone=p.has_phone,
                   keypoints=p.keypoints())
            for p in self.people
        ]
        fr = FrameResult(site_id=self.site.id, room_id=self.cam.room, camera_id=self.cam.id, ts=now, people=people)
        return img, fr


class DemoGenerator:
    def __init__(self, settings: Settings, publish, publish_preview):
        self.settings = settings
        self.publish = publish
        self.publish_preview = publish_preview
        self.cams: list[SimCamera] = []
        next_id = 1
        for site in settings.sites:
            for cam in site.cameras:
                sim = SimCamera(site, cam)
                for role, n in ((Role.ADULT, random.randint(1, 2)), (Role.CHILD, random.randint(3, 7))):
                    for _ in range(n):
                        sim.people.append(SimPerson(next_id, role, random.uniform(0.15, 0.85), random.uniform(0.55, 0.9)))
                        next_id += 1
                self.cams.append(sim)

    async def seed_history(self, now: float) -> None:
        """Back-fills a working day of room statistics so charts and reports have something to show."""
        interval = self.settings.rules.stats_interval_seconds * 6
        t = now - HISTORY_HOURS * 3600
        while t < now:
            hour = datetime.fromtimestamp(t).hour
            for site in self.settings.sites:
                for room in site.rooms:
                    children = max(0, int(round(6 + 3 * np.sin(hour / 3) + random.gauss(0, 1))))
                    adults = 0 if random.random() < 0.02 else max(1, round(children / 3.5))
                    await self.publish(f"vdb.stats.{site.id}", RoomStat(
                        site_id=site.id, room_id=room.id, ts=t, adults=adults, children=children, cameras_online=1,
                        no_adult=adults == 0 and children > 0,
                        ratio_ok=adults > 0 and children <= adults * room.max_children_per_adult,
                    ))
            t += interval

    async def run(self) -> None:
        now = time.time()

        async def backfill() -> None:
            await self.seed_history(now)
            for _ in range(4):
                await self.raise_alert(now - random.uniform(600, HISTORY_HOURS * 3600))

        # History back-fills alongside the live loop so live frames appear immediately.
        self._backfill = asyncio.create_task(backfill())
        next_alert = now + random.uniform(*ALERT_EVERY_SECONDS)
        last_stat = 0.0
        while True:
            now = time.time()
            for sim in self.cams:
                for p in sim.people:
                    p.step(1.0)
                    p.has_phone = p.role == Role.ADULT and random.random() < 0.05
                img, fr = sim.frame(now)
                await self.publish_preview(sim.site.id, sim.cam.id, render_preview(img, fr, sim.cam.restricted_zones, BANNER))
            if now - last_stat >= self.settings.rules.stats_interval_seconds:
                last_stat = now
                await self.publish_room_stats(now)
            if now >= next_alert:
                next_alert = now + random.uniform(*ALERT_EVERY_SECONDS)
                await self.raise_alert(now)
            await asyncio.sleep(1.0)

    async def publish_room_stats(self, now: float) -> None:
        for site in self.settings.sites:
            for room in site.rooms:
                people = [p for s in self.cams if s.cam.room == room.id and s.site.id == site.id for p in s.people]
                adults = sum(p.role == Role.ADULT for p in people)
                children = sum(p.role == Role.CHILD for p in people)
                await self.publish(f"vdb.stats.{site.id}", RoomStat(
                    site_id=site.id, room_id=room.id, ts=now, adults=adults, children=children, cameras_online=1,
                    no_adult=adults == 0 and children > 0,
                    ratio_ok=adults > 0 and children <= adults * room.max_children_per_adult,
                ))

    async def raise_alert(self, ts: float) -> None:
        sim = random.choice(self.cams)
        type_ = random.choice(list(ALERT_TEXT))
        severity, text = ALERT_TEXT[type_]
        room = next(r for r in sim.site.rooms if r.id == sim.cam.room)
        kids = sum(p.role == Role.CHILD for p in sim.people)
        aggression = type_ == AlertType.POSSIBLE_AGGRESSION
        alert = Alert(
            site_id=sim.site.id, room_id=room.id, camera_id=sim.cam.id, type=type_, severity=severity,
            started_at=ts - 15, triggered_at=ts,
            message="[DEMO] " + text.format(room=room.name, kids=kids, ratio=room.max_children_per_adult, cam=sim.cam.id),
            verification="pending" if aggression else None,
        )
        frames = []
        for i in range(15):
            for p in sim.people:
                p.step(0.2)
            img, fr = sim.frame(ts + i / 5)
            frames.append(cv2.imdecode(np.frombuffer(render_preview(img, fr, sim.cam.restricted_zones, BANNER), np.uint8),
                                       cv2.IMREAD_COLOR))
        base = self._base(alert)
        base.parent.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(base.with_suffix(".jpg")), frames[0])
        alert.thumbnail = base.with_suffix(".jpg").relative_to(self.settings.clips.dir).as_posix()
        await self.publish(f"vdb.alerts.{alert.site_id}", alert)
        await self._write_clip(alert, frames)
        if aggression:
            verdict = random.choice(["likely", "unlikely", "unclear"])
            notes = {"likely": "An adult appears to grab a child's arm sharply.",
                     "unlikely": "Adult is lifting the child to play; no force visible.",
                     "unclear": "View is partly blocked; a person should check the clip."}
            await self.publish(f"vdb.alert_updates.{alert.site_id}", AlertUpdate(
                site_id=alert.site_id, alert_id=alert.id, verification=verdict, verification_note="[DEMO] " + notes[verdict]))

    async def _write_clip(self, alert: Alert, frames: list[np.ndarray]) -> None:
        path = self._base(alert).with_suffix(".mp4")
        h, w = frames[0].shape[:2]
        writer = imageio_ffmpeg.write_frames(str(path), (w, h), fps=5, codec="libx264", pix_fmt_out="yuv420p",
                                             macro_block_size=2, output_params=["-movflags", "+faststart"])
        writer.send(None)
        for f in frames:
            writer.send(np.ascontiguousarray(f[:, :, ::-1]))
        writer.close()
        await self.publish(f"vdb.clips.{alert.site_id}", ClipReady(
            site_id=alert.site_id, alert_id=alert.id, clip=path.relative_to(self.settings.clips.dir).as_posix()))

    def _base(self, alert: Alert):
        day = datetime.fromtimestamp(alert.triggered_at, timezone.utc).strftime("%Y-%m-%d")
        return self.settings.clips.dir / alert.site_id / day / f"demo-{alert.id}"
