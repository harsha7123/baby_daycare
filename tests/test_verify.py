import cv2
import numpy as np

from vdb.config import Verifier as VerifierConfig
from vdb.schemas import Alert, AlertType
from vdb.verify import Verifier, crop_region, parse_verdict, sample_frames


def make_alert() -> Alert:
    return Alert(site_id="s1", room_id="r1", camera_id="c1", type=AlertType.POSSIBLE_AGGRESSION, severity="high",
                 started_at=0, triggered_at=1, message="x", verification="pending")


class TestParseVerdict:
    def test_well_formed(self):
        assert parse_verdict("VERDICT: likely\nREASON: Adult pulls the child's arm hard.") == (
            "likely", "Adult pulls the child's arm hard.")

    def test_case_and_noise_tolerant(self):
        assert parse_verdict("Sure!\nverdict: Unlikely\nreason: they are hugging")[0] == "unlikely"

    def test_contact_line_used_as_note(self):
        assert parse_verdict("CONTACT: The adult holds the child's hand.\nVERDICT: unlikely") == (
            "unlikely", "The adult holds the child's hand.")

    def test_echoed_template_is_unclear(self):
        assert parse_verdict("CONTACT: ...\nVERDICT: likely | unlikely | unclear")[0] == "unclear"

    def test_two_different_verdicts_is_unclear(self):
        assert parse_verdict("VERDICT: likely\nVERDICT: unlikely")[0] == "unclear"

    def test_garbage_is_unclear(self):
        assert parse_verdict("I cannot tell")[0] == "unclear"
        assert parse_verdict("")[0] == "unclear"


def jpeg(h: int = 10, w: int = 10) -> bytes:
    return cv2.imencode(".jpg", np.zeros((h, w, 3), np.uint8))[1].tobytes()


def test_sample_frames_even_and_resized():
    out = sample_frames([jpeg(100, 1000)] * 20, n=4, width=500)
    assert len(out) == 4 and out[0].size == (500, 50)


def test_region_crop_zooms_in_with_padding():
    frame = np.zeros((1000, 2000, 3), np.uint8)
    # People occupy the middle 10% each way; with 50% padding the crop is 20% of the frame.
    assert crop_region(frame, (0.45, 0.45, 0.55, 0.55)).shape[:2] == (200, 400)
    assert crop_region(frame, None).shape == frame.shape


def test_verifier_publishes_verdict_in_background():
    sent = []
    v = Verifier(VerifierConfig(enabled=True, max_frames=2), "cpu", lambda s, m: sent.append((s, m)),
                 ask=lambda images: f"VERDICT: unclear\nREASON: saw {len(images)} frames")
    alert = make_alert()
    v.submit(alert, [jpeg()] * 5)
    v.wait_idle()
    subject, update = sent[0]
    assert subject == "vdb.alert_updates.s1" and update.alert_id == alert.id
    assert (update.verification, update.verification_note) == ("unclear", "saw 2 frames")


def test_verifier_without_footage_says_so():
    sent = []
    v = Verifier(VerifierConfig(enabled=True), "cpu", lambda s, m: sent.append(m), ask=lambda images: "")
    v.submit(make_alert(), [])
    v.wait_idle()
    assert sent[0].verification == "unclear" and "No footage" in sent[0].verification_note


def test_model_errors_fall_back_to_human_review():
    sent = []

    def boom(images):
        raise RuntimeError("cuda oom")

    v = Verifier(VerifierConfig(enabled=True), "cpu", lambda s, m: sent.append(m), ask=boom)
    v.submit(make_alert(), [jpeg()])
    v.wait_idle()
    assert sent[0].verification == "unclear" and "review" in sent[0].verification_note
