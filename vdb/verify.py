"""Second opinion on possible-aggression alerts from a video-language model (Qwen3-VL, Apache-2.0).

Runs in its own thread so the real-time pipeline never waits for it. The verdict is attached to the alert as
"likely" / "unlikely" / "unclear" with a one-line reason; it never deletes or hides an alert — a person decides.
"""
import logging
import queue
import re
import threading
from collections.abc import Callable

import cv2
import numpy as np

from vdb.config import Verifier as VerifierConfig
from vdb.schemas import Alert, AlertUpdate
from vdb.vision import resolve_device

log = logging.getLogger(__name__)
QUEUE_SIZE = 32
MAX_NEW_TOKENS = 120
# Describe first, judge second: asking for the verdict up front invites a reflexive answer.
PROMPT = (
    "These are CCTV frames from a daycare, in time order, zoomed in on an adult and a child. "
    "First describe in one sentence any physical contact between the adult and the child. "
    "Then judge whether the adult is hitting, pushing, shaking, grabbing roughly or otherwise physically hurting "
    "the child. Answer in exactly two lines:\n"
    "CONTACT: <one sentence>\n"
    "VERDICT: <one word: likely, unlikely or unclear>"
)
_VERDICT = re.compile(r"^\s*VERDICT:\s*(likely|unlikely|unclear)\s*\.?\s*$", re.IGNORECASE | re.MULTILINE)
_NOTE = re.compile(r"^\s*(?:CONTACT|REASON):\s*(.+)$", re.IGNORECASE | re.MULTILINE)


def parse_verdict(text: str) -> tuple[str, str]:
    """Extracts (verdict, note). Anything ambiguous — no verdict line, an echoed template, or two different
    verdicts — is 'unclear', so a person looks at it."""
    verdicts = {m.lower() for m in _VERDICT.findall(text)}
    note = _NOTE.search(text)
    lines = text.strip().splitlines()
    summary = (note.group(1) if note else (lines[0] if lines else "No answer from the model.")).strip()[:300]
    return (verdicts.pop() if len(verdicts) == 1 else "unclear"), summary


CROP_PADDING = 0.5  # context around the people involved, as a fraction of their box size


def crop_region(frame: np.ndarray, region: tuple[float, float, float, float] | None) -> np.ndarray:
    """Zooms in on the adult and child involved (padded), so they aren't a few pixels tall in a wide shot."""
    if region is None:
        return frame
    h, w = frame.shape[:2]
    x1, y1, x2, y2 = region
    px, py = (x2 - x1) * CROP_PADDING, (y2 - y1) * CROP_PADDING
    left, top = max(0, int((x1 - px) * w)), max(0, int((y1 - py) * h))
    right, bottom = min(w, int((x2 + px) * w)), min(h, int((y2 + py) * h))
    return frame[top:bottom, left:right] if right - left > 8 and bottom - top > 8 else frame


def sample_frames(jpegs: list[bytes], n: int, width: int, region=None) -> list:
    """Evenly spaced frames, decoded, cropped to the region, resized and converted to RGB PIL images."""
    from PIL import Image

    if not jpegs:
        return []
    idx = np.linspace(0, len(jpegs) - 1, num=min(n, len(jpegs))).round().astype(int)
    out = []
    for i in idx:
        f = cv2.imdecode(np.frombuffer(jpegs[i], np.uint8), cv2.IMREAD_COLOR)
        if f is None:
            continue
        f = crop_region(f, region)
        h, w = f.shape[:2]
        if w > width:
            f = cv2.resize(f, (width, int(h * width / w)))
        out.append(Image.fromarray(cv2.cvtColor(f, cv2.COLOR_BGR2RGB)))
    return out


class Verifier:
    def __init__(self, cfg: VerifierConfig, device: str, publish: Callable[[str, AlertUpdate], None],
                 ask: Callable[[list], str] | None = None):
        self.cfg = cfg
        self.device = resolve_device(device)
        self.publish = publish
        self._ask = ask
        self._queue: queue.Queue = queue.Queue(maxsize=QUEUE_SIZE)
        self._thread = threading.Thread(target=self._run, daemon=True, name="verifier")
        self._thread.start()

    def submit(self, alert: Alert, jpegs: list[bytes]) -> None:
        """Queues an alert's footage; decoding and the model both run on the verifier thread."""
        try:
            self._queue.put_nowait((alert, jpegs))
        except queue.Full:
            self._send(alert, "unclear", "Automatic check skipped (busy); please review the clip.")

    def wait_idle(self) -> None:
        self._queue.join()

    def _send(self, alert: Alert, verdict: str, note: str) -> None:
        self.publish(f"vdb.alert_updates.{alert.site_id}", AlertUpdate(
            site_id=alert.site_id, alert_id=alert.id, verification=verdict, verification_note=note))

    def _run(self) -> None:
        ask = self._ask
        if ask is None:
            try:
                ask = self._load_model()
            except Exception:
                log.exception("verifier model failed to load; aggression alerts will be marked 'unclear'")
                ask = None
        while True:
            alert, jpegs = self._queue.get()
            try:
                images = sample_frames(jpegs, self.cfg.max_frames, self.cfg.frame_width, alert.region)
                if not images:
                    self._send(alert, "unclear", "No footage available for an automatic check.")
                    continue
                if ask is None:
                    self._send(alert, "unclear", "Automatic check unavailable; please review the clip.")
                    continue
                verdict, note = parse_verdict(ask(images))
                self._send(alert, verdict, note)
            except Exception:
                log.exception("verifier failed on alert %s", alert.id)
                self._send(alert, "unclear", "Automatic check failed; please review the clip.")
            finally:
                self._queue.task_done()

    def _load_model(self) -> Callable[[list], str]:
        import torch
        from transformers import AutoModelForImageTextToText, AutoProcessor

        dtype = torch.bfloat16 if self.device.startswith("cuda") else torch.float32
        rev = self.cfg.revision
        model = AutoModelForImageTextToText.from_pretrained(self.cfg.model, dtype=dtype, revision=rev).to(self.device).eval()
        processor = AutoProcessor.from_pretrained(self.cfg.model, revision=rev)
        log.info("verifier model %s loaded on %s", self.cfg.model, self.device)

        def ask(images: list) -> str:
            messages = [{"role": "user", "content": [*({"type": "image", "image": im} for im in images),
                                                     {"type": "text", "text": PROMPT}]}]
            inputs = processor.apply_chat_template(messages, tokenize=True, add_generation_prompt=True,
                                                   return_dict=True, return_tensors="pt").to(model.device)
            with torch.inference_mode():
                out = model.generate(**inputs, max_new_tokens=MAX_NEW_TOKENS, do_sample=False)
            return processor.batch_decode(out[:, inputs["input_ids"].shape[1]:], skip_special_tokens=True)[0]

        return ask
