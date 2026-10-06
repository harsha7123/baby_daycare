from vdb.evaluate import Event, format_report, score, wilson_lower
from vdb.schemas import Alert, AlertType


def alert(t: float, type_: AlertType = AlertType.NO_ADULT, room: str = "r1", started: float | None = None) -> Alert:
    return Alert(site_id="s", room_id=room, camera_id="c", type=type_, severity="high",
                 started_at=t - 10 if started is None else started, triggered_at=t, message="")


EVENTS = [Event(room_id="r1", type=AlertType.NO_ADULT, start=10, end=40),
          Event(room_id="r1", type=AlertType.NO_ADULT, start=100, end=130)]


def test_true_positive_and_missed_event():
    s = score([alert(30)], EVENTS, tolerance=5)[AlertType.NO_ADULT]
    assert (s.tp, s.fp, s.fn) == (1, 0, 1)


def test_alert_outside_event_is_false_positive():
    s = score([alert(70)], EVENTS, tolerance=5)[AlertType.NO_ADULT]
    assert (s.tp, s.fp, s.fn) == (0, 1, 2)


def test_condition_span_overlapping_event_matches():
    # Condition began inside the event, alert fired a bit after the labeller marked the end.
    s = score([alert(52, started=38)], EVENTS, tolerance=5)[AlertType.NO_ADULT]
    assert s.tp == 1


def test_wrong_room_does_not_match():
    s = score([alert(30, room="r2")], EVENTS, tolerance=5)[AlertType.NO_ADULT]
    assert (s.tp, s.fp) == (0, 1)


def test_duplicates_count_against_precision():
    s = score([alert(30), alert(35), alert(110)], EVENTS, tolerance=5)[AlertType.NO_ADULT]
    assert (s.tp, s.duplicates, s.fp) == (2, 1, 0)
    assert s.precision == 2 / 3


def test_replayed_camera_offline_ignored_unless_labelled():
    offline = alert(200, type_=AlertType.CAMERA_OFFLINE)
    assert AlertType.CAMERA_OFFLINE not in score([offline], EVENTS, tolerance=5)


def test_too_few_events_fails_even_when_perfect():
    _, passed = format_report(score([alert(30), alert(110)], EVENTS, tolerance=5), target=0.9, min_events=20)
    assert not passed
    _, passed = format_report(score([alert(30), alert(110)], EVENTS, tolerance=5), target=0.9, min_events=2)
    assert passed


def test_no_labels_never_passes():
    _, passed = format_report({}, target=0.9, min_events=1)
    assert not passed


def test_wilson_lower_bound_needs_sample_size():
    assert wilson_lower(10, 10) < 0.9 < wilson_lower(40, 40)
