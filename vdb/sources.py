import heapq
import logging
import os
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import cv2
import numpy as np

from vdb.config import Camera

log = logging.getLogger(__name__)
MAX_BACKOFF_SECONDS = 5
STREAM_TIMEOUT_MS = 5000
# Must be set before OpenCV opens its first stream; applies process-wide. TCP avoids RTSP packet loss,
# nobuffer/low_delay stop FFmpeg from adding its own latency.
os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp|fflags;nobuffer|flags;low_delay")


def open_capture(url: str) -> cv2.VideoCapture:
    """Opens a file or stream with timeouts, so a stalled camera triggers a reconnect instead of hanging forever."""
    return cv2.VideoCapture(url, cv2.CAP_FFMPEG, [
        cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, STREAM_TIMEOUT_MS, cv2.CAP_PROP_READ_TIMEOUT_MSEC, STREAM_TIMEOUT_MS,
    ])


class LiveReader(threading.Thread):
    """Reads a camera continuously and keeps only the newest frame, so a slow consumer never builds up lag.

    A local video file is looped at its native frame rate to simulate a camera.
    """

    def __init__(self, cam: Camera, stop: threading.Event):
        super().__init__(daemon=True, name=f"reader-{cam.id}")
        self.cam = cam
        self.stop = stop
        self._lock = threading.Lock()
        self._latest: tuple[float, np.ndarray] | None = None

    def take(self) -> tuple[float, np.ndarray] | None:
        with self._lock:
            latest, self._latest = self._latest, None
        return latest

    def run(self) -> None:
        backoff = 1.0
        url = self.cam.stream_url()
        is_file = Path(url).exists()
        while not self.stop.is_set():
            cap = open_capture(url)
            if not cap.isOpened():
                log.warning("camera %s: cannot open source, retrying in %.0fs", self.cam.id, backoff)
                self.stop.wait(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)
                continue
            backoff = 1.0
            period = 1 / (cap.get(cv2.CAP_PROP_FPS) or 25) if is_file else 0.0
            next_at = time.monotonic()
            while not self.stop.is_set():
                ok, frame = cap.read()
                if not ok:
                    if is_file:
                        cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                        continue
                    log.warning("camera %s: stream ended, reconnecting", self.cam.id)
                    break
                with self._lock:
                    self._latest = (time.time(), frame)
                if period:
                    next_at += period
                    time.sleep(max(0.0, next_at - time.monotonic()))
            cap.release()


def video_duration(path: str) -> float:
    cap = open_capture(path)
    try:
        return cap.get(cv2.CAP_PROP_FRAME_COUNT) / (cap.get(cv2.CAP_PROP_FPS) or 25)
    finally:
        cap.release()


def file_frames(cam: Camera) -> Iterator[tuple[float, np.ndarray]]:
    """Yields frames from a video file at the camera's analysis fps, timestamped by video time (deterministic)."""
    cap = open_capture(cam.stream_url())
    if not cap.isOpened():
        raise FileNotFoundError(f"cannot open video for camera {cam.id}")
    src_fps = cap.get(cv2.CAP_PROP_FPS) or 25
    stride = max(1, round(src_fps / cam.fps))
    idx = 0
    try:
        while True:
            if idx % stride == 0:
                ok, frame = cap.read()
                if not ok:
                    return
                yield idx / src_fps, frame
            elif not cap.grab():  # grab() skips the colour conversion for frames we don't analyse
                return
            idx += 1
    finally:
        cap.release()


def merged_file_frames(cams: list[Camera]) -> Iterator[tuple[Camera, float, np.ndarray]]:
    """Interleaves several video files in timestamp order, as if they were live cameras."""
    def tagged(i: int, cam: Camera) -> Iterator[tuple[float, int, np.ndarray]]:
        for ts, frame in file_frames(cam):
            yield ts, i, frame

    streams = [tagged(i, cam) for i, cam in enumerate(cams)]
    for ts, i, frame in heapq.merge(*streams, key=lambda x: (x[0], x[1])):
        yield cams[i], ts, frame
