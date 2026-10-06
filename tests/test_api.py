import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from vdb.api import create_app, hash_key
from vdb.bus import MemoryBus
from vdb.config import Camera, Clips, Retention, Room, Settings, Site, User
from vdb.db import init_db
from vdb.retention import apply_retention
from vdb.schemas import Alert, AlertType, ClipReady, RoomStat

ADMIN_KEY = "a" * 40
OTHER_KEY = "b" * 40
ADMIN = {"Authorization": f"Bearer {ADMIN_KEY}"}
OTHER = {"Authorization": f"Bearer {OTHER_KEY}"}
USERS = [User(name="Asha", key_sha256=hash_key(ADMIN_KEY)), User(name="Ravi", key_sha256=hash_key(OTHER_KEY), sites=["s2"])]


def make_settings(tmp_path) -> Settings:
    return Settings(
        sites=[Site(id="s1", name="Test", timezone="UTC", rooms=[Room(id="r1", name="Babies")],
                    cameras=[Camera(id="c1", room="r1", source="rtsp://user:secret@cam/1")]),
               Site(id="s2", name="Other", rooms=[Room(id="r1", name="Room")], cameras=[Camera(id="c2", room="r1", source="x")])],
        clips=Clips(dir=tmp_path / "clips"),
        retention=Retention(media_days=30, records_days=365),
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'vdb.db').as_posix()}",
    )


@pytest.fixture
def env(tmp_path):
    settings = make_settings(tmp_path)
    bus = MemoryBus()
    with TestClient(create_app(settings, bus, USERS)) as client:
        yield client, bus, settings


def publish(client: TestClient, bus: MemoryBus, subject: str, msg) -> None:
    client.portal.call(bus.publish, subject, msg.model_dump_json().encode())


def make_alert(ts: float, site: str = "s1") -> Alert:
    return Alert(site_id=site, room_id="r1", camera_id="c1", type=AlertType.NO_ADULT, severity="high",
                 started_at=ts - 20, triggered_at=ts, message="No adult")


def with_clip(client, bus, settings, alert: Alert) -> None:
    clip = settings.clips.dir / alert.site_id / f"{alert.id}.mp4"
    clip.parent.mkdir(parents=True, exist_ok=True)
    clip.write_bytes(b"fake")
    publish(client, bus, f"vdb.alerts.{alert.site_id}", alert)
    publish(client, bus, f"vdb.clips.{alert.site_id}", ClipReady(site_id=alert.site_id, alert_id=alert.id, clip=f"{alert.site_id}/{alert.id}.mp4"))


def test_requires_valid_key(env):
    client, _, _ = env
    assert client.get("/api/sites").status_code == 401
    assert client.get("/api/sites", headers={"Authorization": "Bearer short"}).status_code == 401
    assert client.get("/api/sites", headers={"Authorization": "Bearer " + "c" * 40}).status_code == 401


def test_repeated_failures_are_rate_limited(env):
    client, _, _ = env
    codes = [client.get("/api/sites", headers={"Authorization": "Bearer nope"}).status_code for _ in range(21)]
    assert codes[-1] == 429
    assert client.get("/api/sites", headers=ADMIN).status_code == 429  # same IP still blocked for a minute


def test_api_docs_disabled_and_headers_set(env):
    client, _, _ = env
    assert client.get("/docs").status_code == 404
    r = client.get("/api/sites", headers=ADMIN)
    assert r.headers["cache-control"] == "no-store" and r.headers["x-content-type-options"] == "nosniff"


def test_sites_scoped_and_hide_camera_urls(env):
    client, _, _ = env
    body = client.get("/api/sites", headers=ADMIN).text
    assert "secret" not in body and "rtsp" not in body
    assert [s["id"] for s in client.get("/api/sites", headers=OTHER).json()] == ["s2"]


def test_alert_flow_review_and_audit(env):
    client, bus, settings = env
    alert = make_alert(time.time())
    with_clip(client, bus, settings, alert)

    rows = client.get("/api/alerts", params={"site_id": "s1"}, headers=ADMIN).json()
    assert [r["id"] for r in rows] == [alert.id]
    assert client.get(f"/api/alerts/{alert.id}/clip", headers=ADMIN).content == b"fake"

    r = client.post(f"/api/alerts/{alert.id}/review", json={"status": "false_alarm", "reviewed_by": "Someone Else"}, headers=ADMIN)
    assert r.json()["reviewed_by"] == "Asha"
    assert client.get("/api/accuracy", params={"site_id": "s1"}, headers=ADMIN).json()["no_adult"]["precision"] == 0.0
    log = client.get("/api/access-log", params={"site_id": "s1"}, headers=ADMIN).json()
    assert [(e["user"], e["kind"], e["alert_id"]) for e in log] == [("Asha", "clip", alert.id)]


def test_other_site_user_cannot_see_or_review(env):
    client, bus, settings = env
    alert = make_alert(time.time())
    with_clip(client, bus, settings, alert)
    assert client.get("/api/alerts", params={"site_id": "s1"}, headers=OTHER).status_code == 404
    assert client.get(f"/api/alerts/{alert.id}/clip", headers=OTHER).status_code == 404
    assert client.post(f"/api/alerts/{alert.id}/review", json={"status": "confirmed"}, headers=OTHER).status_code == 404


def test_media_path_traversal_blocked(env):
    client, bus, _ = env
    alert = make_alert(time.time())
    alert.thumbnail = "../vdb.db"
    publish(client, bus, "vdb.alerts.s1", alert)
    assert client.get(f"/api/alerts/{alert.id}/thumbnail", headers=ADMIN).status_code == 404


def test_websocket_pushes_only_visible_sites(env):
    client, bus, _ = env
    with client.websocket_connect("/ws/alerts") as ws:
        ws.send_json({"key": OTHER_KEY})
        time.sleep(0.1)
        publish(client, bus, "vdb.alerts.s1", make_alert(time.time(), site="s1"))
        visible = make_alert(time.time(), site="s2")
        publish(client, bus, "vdb.alerts.s2", visible)
        assert ws.receive_json()["id"] == visible.id


def test_daily_report(env):
    client, bus, _ = env
    noon = datetime(2026, 10, 6, 12, tzinfo=ZoneInfo("UTC")).timestamp()
    for i in range(6):
        publish(client, bus, "vdb.stats.s1", RoomStat(site_id="s1", room_id="r1", ts=noon + 10 * i, adults=0 if i < 3 else 1,
                                                       children=3, cameras_online=1, no_adult=i < 3, ratio_ok=i >= 3))
    publish(client, bus, "vdb.alerts.s1", make_alert(noon + 30))
    rep = client.get("/api/reports/daily", params={"site_id": "s1", "day": "2026-10-06"}, headers=ADMIN).json()
    room = rep["rooms"][0]
    assert room["monitored_minutes"] == 1 and room["no_adult_minutes"] == 0.5
    assert room["ratio_compliance_pct"] == 50.0
    assert rep["alert_totals"] == {"no_adult": 1}
    html = client.get("/api/reports/daily.html", params={"site_id": "s1", "day": "2026-10-06"}, headers=ADMIN)
    assert "Babies" in html.text


async def test_retention_deletes_old_media_but_keeps_confirmed(tmp_path):
    from vdb.db import AlertRow

    settings = make_settings(tmp_path)
    sessions = await init_db(settings.database_url)
    now = time.time()
    old, confirmed = make_alert(now - 40 * 86400), make_alert(now - 40 * 86400)
    async with sessions() as s:
        for a in (old, confirmed):
            path = settings.clips.dir / "s1" / f"{a.id}.mp4"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"x")
            s.add(AlertRow(**{**a.model_dump(mode="json"), "clip": f"s1/{a.id}.mp4"}))
        await s.commit()
        (await s.get(AlertRow, confirmed.id)).status = "confirmed"
        await s.commit()

    result = await apply_retention(settings, sessions, now)

    assert result["media_files"] == 1
    assert not (settings.clips.dir / "s1" / f"{old.id}.mp4").exists()
    assert (settings.clips.dir / "s1" / f"{confirmed.id}.mp4").exists()
