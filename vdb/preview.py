import cv2
import numpy as np

from vdb.clips import downscale
from vdb.config import Zone
from vdb.schemas import FrameResult, Role

# BGR, matching the dashboard palette (adult = blue, child = orange, unknown = grey, alert = red).
COLORS = {Role.ADULT: (255, 132, 10), Role.CHILD: (10, 159, 255), Role.UNKNOWN: (150, 150, 142)}
ALERT_COLOR = (58, 69, 255)
# COCO-17 limbs: arms, shoulders/hips, legs, head-to-shoulders.
SKELETON = [(5, 7), (7, 9), (6, 8), (8, 10), (5, 6), (5, 11), (6, 12), (11, 12),
            (11, 13), (13, 15), (12, 14), (14, 16), (0, 5), (0, 6)]
KEYPOINT_MIN_CONF = 0.3
PREVIEW_JPEG_QUALITY = 70


def _label(img: np.ndarray, text: str, x: int, y: int, color: tuple[int, int, int]) -> None:
    font, scale, thick = cv2.FONT_HERSHEY_SIMPLEX, 0.45, 1
    (tw, th), _ = cv2.getTextSize(text, font, scale, thick)
    y = max(y, th + 8)
    cv2.rectangle(img, (x, y - th - 8), (x + tw + 10, y), color, -1, cv2.LINE_AA)
    cv2.putText(img, text, (x + 5, y - 5), font, scale, (255, 255, 255), thick, cv2.LINE_AA)


def render_preview(frame_bgr: np.ndarray, fr: FrameResult, zones: list[Zone], banner: str | None = None) -> bytes:
    """Annotated, downscaled JPEG of a camera frame for the dashboard's live view."""
    img = downscale(frame_bgr).copy()
    h, w = img.shape[:2]

    if zones:
        overlay = img.copy()
        for z in zones:
            pts = np.array([(int(x * w), int(y * h)) for x, y in z.polygon], dtype=np.int32)
            cv2.fillPoly(overlay, [pts], ALERT_COLOR)
        img = cv2.addWeighted(overlay, 0.18, img, 0.82, 0)
        for z in zones:
            pts = np.array([(int(x * w), int(y * h)) for x, y in z.polygon], dtype=np.int32)
            cv2.polylines(img, [pts], True, ALERT_COLOR, 1, cv2.LINE_AA)

    for p in fr.people:
        color = ALERT_COLOR if p.has_phone or p.zones else COLORS[p.role]
        x1, y1, x2, y2 = int(p.bbox[0] * w), int(p.bbox[1] * h), int(p.bbox[2] * w), int(p.bbox[3] * h)
        cv2.rectangle(img, (x1, y1), (x2, y2), color, 2, cv2.LINE_AA)
        if p.keypoints:
            pts = [(int(x * w), int(y * h), c) for x, y, c in p.keypoints]
            for a, b in SKELETON:
                if pts[a][2] >= KEYPOINT_MIN_CONF and pts[b][2] >= KEYPOINT_MIN_CONF:
                    cv2.line(img, pts[a][:2], pts[b][:2], color, 2, cv2.LINE_AA)
            for x, y, c in pts:
                if c >= KEYPOINT_MIN_CONF:
                    cv2.circle(img, (x, y), 3, (255, 255, 255), -1, cv2.LINE_AA)
        text = f"{p.role.value} #{p.track_id}" + (" | phone" if p.has_phone else "")
        _label(img, text, x1, y1, color)

    if banner:
        _label(img, banner, 10, 28, (40, 40, 40))
    ok, jpg = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, PREVIEW_JPEG_QUALITY])
    return jpg.tobytes() if ok else b""
