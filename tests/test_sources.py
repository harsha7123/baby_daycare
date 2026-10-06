import cv2
import numpy as np

from vdb.config import Camera
from vdb.sources import file_frames, merged_file_frames


def write_video(path, frames: int, fps: float = 10) -> str:
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), fps, (64, 48))
    for i in range(frames):
        writer.write(np.full((48, 64, 3), i * 10 % 255, dtype=np.uint8))
    writer.release()
    return str(path)


def test_file_frames_samples_at_camera_fps(tmp_path):
    cam = Camera(id="a", room="r", source=write_video(tmp_path / "a.avi", 20, fps=10), fps=5)
    assert [round(ts, 2) for ts, _ in file_frames(cam)] == [0.0, 0.2, 0.4, 0.6, 0.8, 1.0, 1.2, 1.4, 1.6, 1.8]


def test_merged_frames_keep_their_own_camera(tmp_path):
    cams = [Camera(id="a", room="r", source=write_video(tmp_path / "a.avi", 10), fps=5),
            Camera(id="b", room="r", source=write_video(tmp_path / "b.avi", 10), fps=5)]
    out = [(cam.id, round(ts, 2)) for cam, ts, _ in merged_file_frames(cams)]
    assert out[:4] == [("a", 0.0), ("b", 0.0), ("a", 0.2), ("b", 0.2)]
    assert sum(c == "a" for c, _ in out) == sum(c == "b" for c, _ in out) == 5
