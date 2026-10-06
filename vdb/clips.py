import logging
from collections import deque
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import cv2
import imageio_ffmpeg
import numpy as np

from vdb.config import Clips
from vdb.schemas import Alert, ClipReady

log = logging.getLogger(__name__)
CLIP_MAX_WIDTH = 960
JPEG_QUALITY = 80
LEAD_IN_SECONDS = 2


def downscale(frame: np.ndarray) -> np.ndarray:
    h, w = frame.shape[:2]
    return cv2.resize(frame, (CLIP_MAX_WIDTH, int(h * CLIP_MAX_WIDTH / w))) if w > CLIP_MAX_WIDTH else frame


class ClipRecorder:
    """Keeps a short rolling buffer per camera and writes an evidence clip (before + after) for each alert."""

    def __init__(self, cfg: Clips, fps_by_camera: dict[str, float], on_clip: Callable[[ClipReady], None]):
        self.cfg = cfg
        self.fps = fps_by_camera
        self.on_clip = on_clip
        window = cfg.pre_seconds + cfg.post_seconds + 2
        self._buf: dict[str, deque[tuple[float, bytes]]] = {
            cam: deque(maxlen=int(window * fps) + 1) for cam, fps in fps_by_camera.items()
        }
        self._pending: list[tuple[Alert, float]] = []
        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="clips")

    def add_frame(self, camera_id: str, ts: float, frame_bgr: np.ndarray) -> None:
        ok, jpg = cv2.imencode(".jpg", downscale(frame_bgr), [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
        if ok:
            self._buf[camera_id].append((ts, jpg.tobytes()))
        ready = [(a, until) for a, until in self._pending if a.camera_id == camera_id and ts >= until]
        for alert, until in ready:
            self._pending.remove((alert, until))
            # Start just before the condition began (e.g. when the last adult left), capped at pre_seconds.
            begin = max(alert.started_at - LEAD_IN_SECONDS, alert.triggered_at - self.cfg.pre_seconds)
            frames = [f for t, f in self._buf[camera_id] if begin <= t <= until]
            self._pool.submit(self._write_clip, alert, frames, self.fps[camera_id])

    def on_alert(self, alert: Alert, frame_bgr: np.ndarray) -> None:
        """Saves a thumbnail immediately (for the push notification) and schedules the clip."""
        base = self._base_path(alert)
        base.parent.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(base.with_suffix(".jpg")), downscale(frame_bgr), [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
        alert.thumbnail = self._relative(base.with_suffix(".jpg"))
        if alert.camera_id in self._buf:
            self._pending.append((alert, alert.triggered_at + self.cfg.post_seconds))

    def close(self) -> None:
        self._pool.shutdown(wait=True)

    def _write_clip(self, alert: Alert, frames: list[bytes], fps: float) -> None:
        if not frames:
            return
        path = self._base_path(alert).with_suffix(".mp4")
        try:
            first = cv2.imdecode(np.frombuffer(frames[0], np.uint8), cv2.IMREAD_COLOR)
            h, w = first.shape[:2]
            writer = imageio_ffmpeg.write_frames(
                str(path), (w, h), fps=fps, codec="libx264", pix_fmt_out="yuv420p",
                macro_block_size=2, output_params=["-movflags", "+faststart"],
            )
            writer.send(None)
            for jpg in frames:
                img = cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_COLOR)
                if img.shape[:2] != (h, w):
                    img = cv2.resize(img, (w, h))
                writer.send(np.ascontiguousarray(img[:, :, ::-1]))
            writer.close()
            self.on_clip(ClipReady(site_id=alert.site_id, alert_id=alert.id, clip=self._relative(path)))
        except Exception:
            log.exception("failed to write clip for alert %s", alert.id)

    def _base_path(self, alert: Alert) -> Path:
        day = datetime.fromtimestamp(alert.triggered_at, timezone.utc).strftime("%Y-%m-%d")
        return self.cfg.dir / alert.site_id / day / alert.id

    def _relative(self, path: Path) -> str:
        return path.relative_to(self.cfg.dir).as_posix()
