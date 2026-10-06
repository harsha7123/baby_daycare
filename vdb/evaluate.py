import json
import math
import time
from pathlib import Path

from pydantic import BaseModel

from vdb.schemas import Alert, AlertType


class Event(BaseModel):
    """A labelled ground-truth incident, in seconds of video time."""

    room_id: str
    type: AlertType
    start: float
    end: float
    site_id: str | None = None  # None matches any site


class Labels(BaseModel):
    events: list[Event]


class TypeScore(BaseModel):
    events: int = 0
    tp: int = 0
    fp: int = 0
    fn: int = 0
    duplicates: int = 0

    @property
    def precision(self) -> float | None:
        # Duplicates count against precision: staff receive every one of them.
        alerts = self.tp + self.fp + self.duplicates
        return self.tp / alerts if alerts else None

    @property
    def recall(self) -> float | None:
        return self.tp / self.events if self.events else None


def wilson_lower(successes: int, n: int, z: float = 1.96) -> float | None:
    """95% lower confidence bound: how sure we are the true rate is at least this high given the sample size."""
    if n == 0:
        return None
    p = successes / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (centre - margin) / (1 + z * z / n)


def load_labels(path: str | Path) -> Labels:
    return Labels.model_validate(json.loads(Path(path).read_text(encoding="utf-8")))


def score(alerts: list[Alert], events: list[Event], tolerance: float) -> dict[AlertType, TypeScore]:
    """Event-level matching: an alert is correct if the span from its condition start to its trigger overlaps a
    labelled event of the same type, room (and site), widened by `tolerance` seconds on each side.
    Each event is matched by at most one alert; further alerts inside it are duplicates."""
    labelled_types = {e.type for e in events}
    # Replayed files of different lengths "go offline" when they end, so only score that type if it was labelled.
    alerts = [a for a in alerts if a.type != AlertType.CAMERA_OFFLINE or AlertType.CAMERA_OFFLINE in labelled_types]

    scores: dict[AlertType, TypeScore] = {}
    for e in events:
        scores.setdefault(e.type, TypeScore()).events += 1
    matched: set[int] = set()
    for alert in sorted(alerts, key=lambda a: a.triggered_at):
        s = scores.setdefault(alert.type, TypeScore())
        hits = [i for i, e in enumerate(events)
                if e.type == alert.type and e.room_id == alert.room_id
                and (e.site_id is None or e.site_id == alert.site_id)
                and alert.started_at <= e.end + tolerance and alert.triggered_at >= e.start - tolerance]
        fresh = [i for i in hits if i not in matched]
        if fresh:
            matched.add(min(fresh, key=lambda i: events[i].start))
            s.tp += 1
        elif hits:
            s.duplicates += 1
        else:
            s.fp += 1
    for s in scores.values():
        s.fn = s.events - s.tp
    return scores


def format_report(scores: dict[AlertType, TypeScore], target: float, min_events: int,
                  camera_hours: float | None = None) -> tuple[str, bool]:
    def pct(v: float | None) -> str:
        return "n/a" if v is None else f"{100 * v:.1f}%"

    header = (f"{'alert type':<17}{'events':>7}{'TP':>5}{'FP':>5}{'dup':>5}{'FN':>5}"
              f"{'precision':>11}{'(95% low)':>11}{'recall':>9}{'(95% low)':>11}{'FA/cam-h':>10}  result")
    lines = [header]
    passed = bool(scores)
    for t, s in sorted(scores.items()):
        p_low = wilson_lower(s.tp, s.tp + s.fp + s.duplicates)
        r_low = wilson_lower(s.tp, s.events)
        fa_rate = f"{(s.fp + s.duplicates) / camera_hours:.2f}" if camera_hours else "n/a"
        if s.events < min_events:
            verdict, ok = f"FAIL: need >= {min_events} labelled events", False
        else:
            # Pass on the 95% lower bound, not the point estimate: 19/20 looks like 95% but proves little.
            ok = all(v is not None and v >= target for v in (p_low, r_low))
            verdict = "pass" if ok else "FAIL (95% lower bound below target)"
        passed &= ok
        lines.append(f"{t.value:<17}{s.events:>7}{s.tp:>5}{s.fp:>5}{s.duplicates:>5}{s.fn:>5}"
                     f"{pct(s.precision):>11}{pct(p_low):>11}{pct(s.recall):>9}{pct(r_low):>11}{fa_rate:>10}  {verdict}")
    return "\n".join(lines), passed


def run_eval(settings, labels_path: str, target: float, tolerance: float, min_events: int) -> bool:
    from vdb.sources import video_duration
    from vdb.worker import build_worker

    labels = load_labels(labels_path)
    worker = build_worker(settings, settings.sites, publish=lambda *_: None, record_clips=False)
    t0 = time.monotonic()
    alerts = worker.run_files()
    wall = time.monotonic() - t0
    video_seconds = sum(video_duration(cam.stream_url()) for cam in worker.cameras)

    table, passed = format_report(score(alerts, labels.events, tolerance), target, min_events, video_seconds / 3600)
    print(table)
    perf = worker.perf.summary()
    print(f"\nprocessed {perf['frames']} frames, {perf['ms_per_frame']} ms/frame, avg batch {perf['avg_batch']}")
    print(f"speed: {video_seconds / max(wall, 1e-9):.1f}x real time across {len(worker.cameras)} camera(s)")
    print(f"\ntarget {100 * target:.0f}% precision and recall per alert type: {'PASS' if passed else 'FAIL'}")
    return passed
