import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

import cv2
import numpy as np
from pydantic import BaseModel

from vdb.clips import ClipRecorder
from vdb.config import Camera, Models, Settings, Site
from vdb.rules import RulesEngine
from vdb.schemas import Alert, FrameResult, Person
from vdb.sources import LiveReader, merged_file_frames
from vdb.vision import Detector, RoleClassifier, TrackRoles, assign_phones, crop, zones_for

log = logging.getLogger(__name__)
Publish = Callable[[str, BaseModel], None]
Frame = tuple[Camera, float, np.ndarray]
PERF_LOG_SECONDS = 30


@dataclass
class Perf:
    frames: int = 0
    batches: int = 0
    infer_seconds: float = 0.0
    due: int = 0
    late: int = 0
    latency_sum: float = 0.0
    errors: int = 0
    started: float = field(default_factory=time.monotonic)

    def summary(self) -> dict:
        wall = max(time.monotonic() - self.started, 1e-9)
        return {
            "frames": self.frames,
            "fps": round(self.frames / wall, 1),
            "avg_batch": round(self.frames / max(self.batches, 1), 1),
            "ms_per_frame": round(1000 * self.infer_seconds / max(self.frames, 1), 1),
            "on_time_ratio": round(1 - self.late / max(self.due, 1), 3),
            "avg_latency_ms": round(1000 * self.latency_sum / max(self.frames, 1), 1),
            "errors": self.errors,
        }


# `trackers` counts lost_track_buffer in 30 fps frames regardless of the real frame rate.
LOST_TRACK_SECONDS = 3.0


def make_tracker(fps: float, models: Models):
    from trackers import ByteTrackTracker

    return ByteTrackTracker(
        frame_rate=fps, lost_track_buffer=int(LOST_TRACK_SECONDS * 30),
        track_activation_threshold=models.person_threshold, high_conf_det_threshold=models.person_threshold,
    )


class Worker:
    """Runs detection, tracking, role classification and rules for every camera of the given sites.

    Frames from all sites share GPU batches, so one process per GPU is the intended deployment.
    """

    def __init__(self, settings: Settings, sites: list[Site], publish: Publish,
                 detector: Detector, roles: RoleClassifier, record_clips: bool = True):
        self.settings = settings
        self.publish = publish
        self.detector = detector
        self.role_clf = roles
        # Disabled cameras (e.g. consent withdrawn) are dropped entirely: never read, never counted.
        sites = [s.model_copy(update={"cameras": [c for c in s.cameras if c.enabled]}) for s in sites]
        self.cameras = [c for s in sites for c in s.cameras]
        self.site_of = {c.id: s.id for s in sites for c in s.cameras}
        self.rules = {s.id: RulesEngine(s, settings.rules) for s in sites}
        m = settings.models
        self.roles = TrackRoles(m.role_refresh_seconds, m.role_min_samples, m.role_grace_seconds)
        self.trackers = {c.id: make_tracker(c.fps, m) for c in self.cameras}
        self.last_frame: dict[str, np.ndarray] = {}
        self.clips = ClipRecorder(
            settings.clips, {c.id: c.fps for c in self.cameras},
            on_clip=lambda m: self.publish(f"vdb.clips.{m.site_id}", m),
        ) if record_clips else None
        self.perf = Perf()

    def process(self, batch: list[Frame]) -> list[Alert]:
        t0 = time.perf_counter()
        rgb = [cv2.cvtColor(f, cv2.COLOR_BGR2RGB) for _, _, f in batch]
        detections = self.detector.detect(rgb)

        tracked_batch, crops, crop_keys = [], [], []
        for (cam, ts, frame), img, (persons, phones) in zip(batch, rgb, detections):
            tracked = self.trackers[cam.id].update(persons, timestamp=ts)
            tracked = tracked[tracked.tracker_id != -1]
            for xyxy, tid, conf in zip(tracked.xyxy, tracked.tracker_id, tracked.confidence):
                key = (cam.id, int(tid))
                self.roles.seen(key, ts)
                confident = conf >= self.settings.models.person_threshold
                if confident and self.roles.needs_sample(key, ts) and (c := crop(img, xyxy)) is not None:
                    crops.append(c)
                    crop_keys.append((key, ts))
            tracked_batch.append((cam, ts, frame, tracked, phones))

        # One batched classifier call across all cameras is far cheaper than per-person calls.
        for (key, ts), p in zip(crop_keys, self.role_clf.p_adult(crops)):
            self.roles.add(key, float(p), ts)
        self.roles.prune(max(ts for _, ts, _ in batch), LOST_TRACK_SECONDS + 1)

        alerts: list[Alert] = []
        for cam, ts, frame, tracked, phones in tracked_batch:
            site_id = self.site_of[cam.id]
            h, w = frame.shape[:2]
            has_phone = assign_phones(tracked.xyxy, phones.xyxy)
            people = []
            for xyxy, tid, conf, phone in zip(tracked.xyxy, tracked.tracker_id, tracked.confidence, has_phone):
                if (role := self.roles.role((cam.id, int(tid)), ts)) is None:
                    continue
                bbox = (float(xyxy[0] / w), float(xyxy[1] / h), float(xyxy[2] / w), float(xyxy[3] / h))
                people.append(Person(
                    track_id=int(tid), role=role, bbox=bbox, confidence=float(conf), has_phone=bool(phone),
                    zones=zones_for(bbox, cam.restricted_zones, cam.foot_point),
                ))
            fr = FrameResult(site_id=site_id, room_id=cam.room, camera_id=cam.id, ts=ts, people=people)
            self.last_frame[cam.id] = frame
            new_alerts, stats = self.rules[site_id].on_frame(fr)
            if self.clips:
                for alert in new_alerts:
                    self.clips.on_alert(alert, self.last_frame.get(alert.camera_id, frame))
                self.clips.add_frame(cam.id, ts, frame)
            for alert in new_alerts:
                self.publish(f"vdb.alerts.{site_id}", alert)
            for stat in stats:
                self.publish(f"vdb.stats.{site_id}", stat)
            alerts += new_alerts

        self.perf.frames += len(batch)
        self.perf.batches += 1
        self.perf.infer_seconds += time.perf_counter() - t0
        return alerts

    def tick(self, now: float) -> list[Alert]:
        alerts = []
        for site_id, engine in self.rules.items():
            for alert in engine.tick(now):
                self.publish(f"vdb.alerts.{site_id}", alert)
                alerts.append(alert)
        return alerts

    def run_files(self) -> list[Alert]:
        """Processes video files as fast as possible with video-time timestamps (evaluation / replay)."""
        alerts: list[Alert] = []
        batch: list[Frame] = []
        last_tick = 0.0
        for item in merged_file_frames(self.cameras):
            batch.append(item)
            if len(batch) >= self.settings.models.batch_size:
                alerts += self.process(batch)
                batch = []
            if item[1] - last_tick >= 1:
                last_tick = item[1]
                alerts += self.tick(item[1])
        if batch:
            alerts += self.process(batch)
        if self.clips:
            self.clips.close()
        return alerts

    def run_live(self, stop: threading.Event) -> None:
        """Samples every camera at its configured fps and batches whatever frames are due."""
        readers = {c.id: LiveReader(c, stop) for c in self.cameras}
        for r in readers.values():
            r.start()
        cams = {c.id: c for c in self.cameras}
        next_due = {c.id: time.monotonic() for c in self.cameras}
        last_tick = last_log = time.monotonic()
        max_batch = self.settings.models.batch_size
        while not stop.is_set():
            now = time.monotonic()
            batch: list[Frame] = []
            for cam_id, due in next_due.items():
                if now < due or len(batch) >= max_batch:
                    continue
                if (item := readers[cam_id].take()) is None:
                    continue
                period = 1 / cams[cam_id].fps
                self.perf.due += 1
                if now - due > period:  # more than a frame behind: the worker is over capacity
                    self.perf.late += 1
                    next_due[cam_id] = now + period
                else:
                    next_due[cam_id] = due + period
                batch.append((cams[cam_id], item[0], item[1]))
            if batch:
                try:
                    self.process(batch)
                except Exception:
                    # One bad frame or a transient CUDA error must not silently stop all monitoring.
                    self.perf.errors += 1
                    log.exception("batch failed (%d cameras)", len(batch))
                done = time.time()
                self.perf.latency_sum += sum(done - ts for _, ts, _ in batch)
            else:
                stop.wait(0.005)
            if now - last_tick >= 1:
                last_tick = now
                try:
                    self.tick(time.time())
                except Exception:
                    self.perf.errors += 1
                    log.exception("tick failed")
            if now - last_log >= PERF_LOG_SECONDS:
                last_log = now
                log.info("perf %s", self.perf.summary())
        if self.clips:
            self.clips.close()


def build_worker(settings: Settings, sites: list[Site], publish: Publish, record_clips: bool = True) -> Worker:
    return Worker(settings, sites, publish, Detector(settings.models), RoleClassifier(settings.models), record_clips)
