import numpy as np
import supervision as sv

from vdb.config import Models, Zone
from vdb.schemas import Role

# RF-DETR uses 1-indexed COCO category ids.
PERSON_CLASS = 1
PHONE_CLASS = 77
MIN_CROP_PX = 24


def resolve_device(device: str) -> str:
    if device != "auto":
        return device
    import torch

    return "cuda" if torch.cuda.is_available() else "cpu"


class Detector:
    """RF-DETR person + phone detector. Takes a batch of RGB frames from any mix of cameras."""

    def __init__(self, cfg: Models):
        import torch
        import rfdetr

        self.cfg = cfg
        self.device = resolve_device(cfg.device)
        model_cls = {"nano": rfdetr.RFDETRNano, "small": rfdetr.RFDETRSmall,
                     "medium": rfdetr.RFDETRMedium, "large": rfdetr.RFDETRLarge}[cfg.detector]
        self.model = model_cls(device=self.device)
        if self.device.startswith("cuda"):
            # fp16 roughly halves GPU time; no compile so batch size can vary per call; inplace avoids a
            # second fp32 copy of the weights on the GPU.
            self.model.inference(compile=False, dtype=torch.float16, inplace=True)

    def detect(self, frames_rgb: list[np.ndarray]) -> list[tuple[sv.Detections, sv.Detections]]:
        """Returns (persons, phones) per frame. Persons include low-confidence boxes for the tracker's second pass."""
        threshold = min(self.cfg.track_low_threshold, self.cfg.phone_threshold)
        results = self.model.predict(frames_rgb, threshold=threshold, include_source_image=False)
        if not isinstance(results, list):
            results = [results]
        out = []
        for d in results:
            persons = d[(d.class_id == PERSON_CLASS) & (d.confidence >= self.cfg.track_low_threshold)]
            phones = d[(d.class_id == PHONE_CLASS) & (d.confidence >= self.cfg.phone_threshold)]
            out.append((persons, phones))
        return out


class RoleClassifier:
    """Zero-shot adult/child classifier on person crops (CLIP). Replace with a fine-tuned model once labelled data exists."""

    ADULT_PROMPTS = ["a photo of an adult", "a photo of a grown-up woman", "a photo of a grown-up man",
                     "a photo of a teacher"]
    CHILD_PROMPTS = ["a photo of a toddler", "a photo of a young child", "a photo of a baby",
                     "a photo of a little kid"]

    def __init__(self, cfg: Models):
        import torch
        from transformers import CLIPModel, CLIPProcessor

        self.torch = torch
        self.device = resolve_device(cfg.device)
        self.model = CLIPModel.from_pretrained(cfg.role_model).to(self.device).eval()
        self.processor = CLIPProcessor.from_pretrained(cfg.role_model)
        prompts = self.ADULT_PROMPTS + self.CHILD_PROMPTS
        self.text = self.processor(text=prompts, return_tensors="pt", padding=True).to(self.device)
        self.n_adult = len(self.ADULT_PROMPTS)

    def p_adult(self, crops_rgb: list[np.ndarray]) -> np.ndarray:
        if not crops_rgb:
            return np.empty(0)
        images = self.processor(images=[letterbox(c) for c in crops_rgb], return_tensors="pt").to(self.device)
        with self.torch.inference_mode():
            logits = self.model(**self.text, pixel_values=images["pixel_values"]).logits_per_image
        probs = logits.softmax(dim=-1)[:, : self.n_adult].sum(dim=-1)
        return probs.float().cpu().numpy()


class TrackRoles:
    """Accumulates per-track adult probability so each person is classified a few times, not every frame."""

    MAX_WEIGHT = 20

    def __init__(self, refresh_seconds: float, min_samples: int, grace_seconds: float):
        self.refresh = refresh_seconds
        self.min_samples = min_samples
        self.grace = grace_seconds
        self._mean: dict[tuple[str, int], float] = {}
        self._n: dict[tuple[str, int], int] = {}
        self._sampled: dict[tuple[str, int], float] = {}
        self._first: dict[tuple[str, int], float] = {}
        self._seen: dict[tuple[str, int], float] = {}

    def seen(self, key: tuple[str, int], ts: float) -> None:
        self._first.setdefault(key, ts)
        self._seen[key] = ts

    def needs_sample(self, key: tuple[str, int], ts: float) -> bool:
        return ts - self._sampled.get(key, -1e18) >= self.refresh

    def add(self, key: tuple[str, int], p_adult: float, ts: float) -> None:
        n = min(self._n.get(key, 0) + 1, self.MAX_WEIGHT)
        mean = self._mean.get(key, 0.0)
        self._mean[key] = mean + (p_adult - mean) / n
        self._n[key] = n
        self._sampled[key] = ts

    def role(self, key: tuple[str, int], ts: float) -> Role | None:
        """UNKNOWN while a new person is being classified; None if they could never be classified
        (e.g. too small) — such people are left out of counts rather than blocking alerts forever."""
        n = self._n.get(key, 0)
        young = ts - self._first.get(key, ts) < self.grace
        if n >= self.min_samples or (n > 0 and not young):
            return Role.ADULT if self._mean[key] >= 0.5 else Role.CHILD
        return Role.UNKNOWN if young else None

    def prune(self, now: float, max_age: float) -> None:
        """Forgets tracks unseen for longer than the tracker keeps lost tracks, so brief occlusions keep their role."""
        for key in [k for k, t in self._seen.items() if now - t > max_age]:
            for d in (self._mean, self._n, self._sampled, self._first, self._seen):
                d.pop(key, None)


def crop(frame: np.ndarray, xyxy: np.ndarray) -> np.ndarray | None:
    h, w = frame.shape[:2]
    x1, y1, x2, y2 = xyxy.astype(int)
    x1, y1, x2, y2 = max(x1, 0), max(y1, 0), min(x2, w), min(y2, h)
    if x2 - x1 < MIN_CROP_PX or y2 - y1 < MIN_CROP_PX:
        return None
    return frame[y1:y2, x1:x2]


def letterbox(img: np.ndarray, fill: int = 114) -> np.ndarray:
    """Pads to a square so CLIP's centre-crop keeps the whole person (head-to-body ratio is the key cue)."""
    h, w = img.shape[:2]
    side = max(h, w)
    out = np.full((side, side, 3), fill, dtype=img.dtype)
    y, x = (side - h) // 2, (side - w) // 2
    out[y:y + h, x:x + w] = img
    return out


def assign_phones(persons_xyxy: np.ndarray, phones_xyxy: np.ndarray) -> np.ndarray:
    """Marks which persons hold a phone: the phone centre must lie inside the person box,
    and among candidates the person whose upper body (hands/face) is nearest wins."""
    has_phone = np.zeros(len(persons_xyxy), dtype=bool)
    if len(persons_xyxy) == 0 or len(phones_xyxy) == 0:
        return has_phone
    cx = (phones_xyxy[:, 0] + phones_xyxy[:, 2]) / 2
    cy = (phones_xyxy[:, 1] + phones_xyxy[:, 3]) / 2
    px1, py1, px2, py2 = persons_xyxy.T
    inside = (cx[:, None] >= px1) & (cx[:, None] <= px2) & (cy[:, None] >= py1) & (cy[:, None] <= py2)
    upper_x = (px1 + px2) / 2
    upper_y = py1 + (py2 - py1) / 3
    dist = np.hypot(cx[:, None] - upper_x, cy[:, None] - upper_y)
    dist[~inside] = np.inf
    for i in range(len(phones_xyxy)):
        j = int(np.argmin(dist[i]))
        if np.isfinite(dist[i, j]):
            has_phone[j] = True
    return has_phone


def point_in_polygon(x: float, y: float, polygon: list[tuple[float, float]]) -> bool:
    inside = False
    n = len(polygon)
    for i in range(n):
        x1, y1 = polygon[i]
        x2, y2 = polygon[(i + 1) % n]
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
            inside = not inside
    return inside


def zones_for(bbox_norm: tuple[float, float, float, float], zones: list[Zone], foot_point: str = "bottom") -> list[str]:
    """A person is in a zone when the point they stand on is inside it: the box bottom-centre for wall
    cameras, the box centre for ceiling/fisheye cameras looking straight down."""
    fx = (bbox_norm[0] + bbox_norm[2]) / 2
    fy = bbox_norm[3] if foot_point == "bottom" else (bbox_norm[1] + bbox_norm[3]) / 2
    return [z.name for z in zones if point_in_polygon(fx, fy, z.polygon)]
