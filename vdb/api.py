import asyncio
import hashlib
import json
import logging
import secrets
import time
from collections import Counter, defaultdict, deque
from contextlib import asynccontextmanager, suppress
from datetime import date
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from vdb.bus import Bus
from vdb.config import Settings, Site, User
from vdb.db import AccessLog, AlertRow, RoomStatRow, init_db
from vdb.reports import daily_report, day_bounds, precision, render_report_html
from vdb.retention import apply_retention, safe_media_path
from vdb.schemas import Alert, AlertUpdate, ClipReady, RoomStat

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"
WS_AUTH_TIMEOUT = 5
MIN_KEY_LENGTH = 32
AUTH_FAILURES_PER_MINUTE = 20
RETENTION_INTERVAL_SECONDS = 3600
PREVIEW_MAX_AGE_SECONDS = 10
LIVE_AUDIT_INTERVAL_SECONDS = 300
THUMB_AUDIT_INTERVAL_SECONDS = 24 * 3600
CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' blob:; "
       "media-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")


class Review(BaseModel):
    status: Literal["confirmed", "false_alarm"]
    note: str | None = Field(default=None, max_length=1000)


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


class FailureLimiter:
    """Slows down API-key guessing: too many failed attempts from one IP within a minute get HTTP 429."""

    def __init__(self, limit: int):
        self.limit = limit
        self._fails: dict[str, deque[float]] = defaultdict(deque)

    def blocked(self, ip: str) -> bool:
        q = self._fails[ip]
        while q and time.monotonic() - q[0] > 60:
            q.popleft()
        return len(q) >= self.limit

    def fail(self, ip: str) -> None:
        self._fails[ip].append(time.monotonic())


def create_app(settings: Settings, bus: Bus, users: list[User]) -> FastAPI:
    sites = {s.id: s for s in settings.sites}
    camera_site = {c.id: s.id for s in settings.sites for c in s.cameras}
    clients: dict[WebSocket, User] = {}
    previews: dict[str, tuple[float, bytes]] = {}
    live_audited: dict[tuple[str, str], float] = {}
    thumbs_audited: dict[tuple[str, str], float] = {}
    state: dict = {}
    limiter = FailureLimiter(AUTH_FAILURES_PER_MINUTE)

    def lookup(key: str) -> User | None:
        if len(key) < MIN_KEY_LENGTH:
            return None
        digest = hash_key(key)
        found = None
        for u in users:  # check every user so timing doesn't reveal which hash matched
            if secrets.compare_digest(u.key_sha256, digest):
                found = u
        return found

    async def broadcast(site_id: str, kind: str, data: dict) -> None:
        payload = json.dumps({"kind": kind, "data": data})
        for ws, user in list(clients.items()):
            if not user.can_see(site_id):
                continue
            try:
                await ws.send_text(payload)
            except Exception:
                clients.pop(ws, None)

    async def on_alert(_: str, data: bytes) -> None:
        alert = Alert.model_validate_json(data)
        async with state["db"]() as s:
            if await s.get(AlertRow, alert.id) is not None:
                return  # redelivered: never overwrite a stored alert's verification, clip or review
            s.add(AlertRow(**alert.model_dump(mode="json", exclude={"region"})))
            await s.commit()
        await broadcast(alert.site_id, "alert", alert.model_dump(mode="json"))

    async def on_alert_update(_: str, data: bytes) -> None:
        msg = AlertUpdate.model_validate_json(data)
        async with state["db"]() as s:
            row = await s.get(AlertRow, msg.alert_id)
            if row is None:
                # The alert itself travels on another subject and may not be stored yet: fail so NATS redelivers.
                raise LookupError(f"update for unknown alert {msg.alert_id}")
            if row.site_id != msg.site_id:
                log.warning("dropping update for alert %s: site mismatch", msg.alert_id)
                return
            row.verification, row.verification_note = msg.verification, msg.verification_note
            site_id = row.site_id
            await s.commit()
        await broadcast(site_id, "alert_update", {**msg.model_dump(mode="json"), "site_id": site_id})

    async def on_preview(subject: str, data: bytes) -> None:
        parts = subject.split(".")  # preview.<site>.<camera>
        if len(parts) == 3 and camera_site.get(parts[2]) == parts[1]:
            previews[parts[2]] = (time.time(), data)

    async def on_stat(_: str, data: bytes) -> None:
        async with state["db"]() as s:
            s.add(RoomStatRow(**RoomStat.model_validate_json(data).model_dump()))
            await s.commit()

    async def on_clip(_: str, data: bytes) -> None:
        msg = ClipReady.model_validate_json(data)
        async with state["db"]() as s:
            row = await s.get(AlertRow, msg.alert_id)
            if row is None:
                raise LookupError(f"clip for unknown alert {msg.alert_id}")  # redelivered once the alert is stored
            if row.site_id != msg.site_id:
                log.warning("dropping clip for alert %s: site mismatch", msg.alert_id)
                return
            row.clip = msg.clip
            site_id = row.site_id
            await s.commit()
        await broadcast(site_id, "alert_update", {"site_id": site_id, "alert_id": msg.alert_id, "clip": msg.clip})

    async def retention_loop() -> None:
        while True:
            try:
                await apply_retention(settings, state["db"])
            except Exception:
                log.exception("retention run failed")
            await asyncio.sleep(RETENTION_INTERVAL_SECONDS)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        state["db"] = await init_db(settings.database_url)
        await bus.subscribe("vdb.alerts.", on_alert, durable="api-alerts")
        await bus.subscribe("vdb.stats.", on_stat, durable="api-stats")
        await bus.subscribe("vdb.clips.", on_clip, durable="api-clips")
        await bus.subscribe("vdb.alert_updates.", on_alert_update, durable="api-alert-updates")
        await bus.subscribe_ephemeral("preview.", on_preview)
        task = asyncio.create_task(retention_loop())
        yield
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    app = FastAPI(title="Vision Day Baby", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = CSP
        if request.url.path.startswith("/api/"):
            # Footage and reports of children must not linger in browser or proxy caches.
            response.headers["Cache-Control"] = "no-store"
        return response

    def client_ip(request: Request) -> str:
        return request.client.host if request.client else "unknown"

    def auth(request: Request, authorization: str | None = Header(default=None)) -> User:
        ip = client_ip(request)
        if limiter.blocked(ip):
            raise HTTPException(status_code=429, detail="too many failed attempts")
        key = authorization.removeprefix("Bearer ") if authorization and authorization.startswith("Bearer ") else ""
        if (user := lookup(key)) is None:
            limiter.fail(ip)
            raise HTTPException(status_code=401, detail="invalid or missing API key")
        return user

    def visible_site(user: User, site_id: str) -> Site:
        # Unknown and forbidden sites look the same, so keys can't probe which sites exist.
        if site_id not in sites or not user.can_see(site_id):
            raise HTTPException(status_code=404, detail="unknown site")
        return sites[site_id]

    async def visible_alert(user: User, alert_id: str) -> AlertRow:
        async with state["db"]() as s:
            row = await s.get(AlertRow, alert_id)
        if row is None or not user.can_see(row.site_id):
            raise HTTPException(status_code=404)
        return row

    async def audit(user: User, site_id: str, kind: str, request: Request, alert_id: str | None = None) -> None:
        async with state["db"]() as s:
            s.add(AccessLog(ts=time.time(), user=user.name, site_id=site_id, alert_id=alert_id, kind=kind,
                            ip=client_ip(request)))
            await s.commit()

    async def media(user: User, alert_id: str, kind: Literal["thumbnail", "clip"], request: Request) -> FileResponse:
        row = await visible_alert(user, alert_id)
        path = safe_media_path(settings.clips.dir, getattr(row, kind))
        if path is None or not path.is_file():
            raise HTTPException(status_code=404)
        # Every clip view is logged; a thumbnail at most once a day per person (alert lists reload them often).
        now = time.time()
        key = (user.name, alert_id)
        if kind == "clip" or now - thumbs_audited.get(key, 0) > THUMB_AUDIT_INTERVAL_SECONDS:
            if kind != "clip":
                thumbs_audited[key] = now
                for k in [k for k, t in thumbs_audited.items() if now - t > THUMB_AUDIT_INTERVAL_SECONDS]:
                    del thumbs_audited[k]
            await audit(user, row.site_id, kind, request, alert_id)
        return FileResponse(path)

    @app.get("/")
    async def dashboard() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    @app.get("/api/health")
    async def health() -> dict:
        return {"ok": True}

    @app.get("/api/me")
    async def me(user: User = Depends(auth)) -> dict:
        return {"name": user.name}

    @app.get("/api/sites")
    async def list_sites(user: User = Depends(auth)) -> list[dict]:
        # Camera source URLs are deliberately omitted: they can contain camera credentials.
        return [
            {"id": s.id, "name": s.name, "timezone": s.timezone,
             "rooms": [r.model_dump() for r in s.rooms],
             "cameras": [{"id": c.id, "room": c.room, "fps": c.fps, "enabled": c.enabled} for c in s.cameras]}
            for s in settings.sites if user.can_see(s.id)
        ]

    @app.get("/api/alerts")
    async def list_alerts(
        site_id: str, room_id: str | None = None, type: str | None = None, status: str | None = None,
        before: float | None = None, limit: int = Query(default=100, le=500), user: User = Depends(auth),
    ) -> list[dict]:
        visible_site(user, site_id)
        q = select(AlertRow).where(AlertRow.site_id == site_id)
        if room_id:
            q = q.where(AlertRow.room_id == room_id)
        if type:
            q = q.where(AlertRow.type == type)
        if status:
            q = q.where(AlertRow.status == status)
        if before:
            q = q.where(AlertRow.triggered_at < before)
        async with state["db"]() as s:
            rows = (await s.scalars(q.order_by(AlertRow.triggered_at.desc()).limit(limit))).all()
        return [{c.name: getattr(r, c.name) for c in AlertRow.__table__.columns} for r in rows]

    @app.get("/api/alerts/count")
    async def count_alerts(site_id: str, status: str = "open", user: User = Depends(auth)) -> dict:
        visible_site(user, site_id)
        async with state["db"]() as s:
            n = await s.scalar(select(func.count()).select_from(AlertRow)
                               .where(AlertRow.site_id == site_id, AlertRow.status == status))
        return {"site_id": site_id, "status": status, "count": n}

    @app.post("/api/alerts/{alert_id}/review")
    async def review_alert(alert_id: str, body: Review, user: User = Depends(auth)) -> dict:
        await visible_alert(user, alert_id)
        async with state["db"]() as s:
            row = await s.get(AlertRow, alert_id)
            # The reviewer is whoever owns the key — never a name the browser sends.
            row.status, row.reviewed_by, row.note, row.reviewed_at = body.status, user.name, body.note, time.time()
            site_id, reviewed_at = row.site_id, row.reviewed_at
            await s.commit()
        await broadcast(site_id, "alert_update", {"site_id": site_id, "alert_id": alert_id, "status": body.status,
                                                  "reviewed_by": user.name, "reviewed_at": reviewed_at, "note": body.note})
        return {"id": alert_id, "status": body.status, "reviewed_by": user.name}

    @app.get("/api/alerts/{alert_id}/thumbnail")
    async def alert_thumbnail(alert_id: str, request: Request, user: User = Depends(auth)) -> FileResponse:
        return await media(user, alert_id, "thumbnail", request)

    @app.get("/api/alerts/{alert_id}/clip")
    async def alert_clip(alert_id: str, request: Request, user: User = Depends(auth)) -> FileResponse:
        return await media(user, alert_id, "clip", request)

    @app.get("/api/rooms/live")
    async def rooms_live(site_id: str, user: User = Depends(auth)) -> list[dict]:
        site = visible_site(user, site_id)
        out = []
        async with state["db"]() as s:
            for room in site.rooms:
                row = await s.scalar(
                    select(RoomStatRow).where(RoomStatRow.site_id == site_id, RoomStatRow.room_id == room.id)
                    .order_by(RoomStatRow.ts.desc()).limit(1)
                )
                out.append({"room_id": room.id, "name": room.name, "max_children_per_adult": room.max_children_per_adult,
                            **({k: getattr(row, k) for k in ("ts", "adults", "children", "cameras_online", "no_adult", "ratio_ok")} if row else {})})
        return out

    @app.get("/api/cameras/{camera_id}/preview.jpg")
    async def camera_preview(camera_id: str, request: Request, user: User = Depends(auth)) -> Response:
        """Latest annotated frame (about one per second) for the live view."""
        site_id = camera_site.get(camera_id)
        if site_id is None or not user.can_see(site_id):
            raise HTTPException(status_code=404)
        ts, jpg = previews.get(camera_id, (0.0, b""))
        if not jpg or time.time() - ts > PREVIEW_MAX_AGE_SECONDS:
            raise HTTPException(status_code=404, detail="no recent frame")
        # Watching children live is logged too: once per viewer and camera every few minutes, not per frame.
        key = (user.name, camera_id)
        if time.time() - live_audited.get(key, 0) > LIVE_AUDIT_INTERVAL_SECONDS:
            live_audited[key] = time.time()
            await audit(user, site_id, "live", request)
        return Response(content=jpg, media_type="image/jpeg", headers={"X-Frame-Time": f"{ts:.3f}"})

    @app.get("/api/rooms/timeline")
    async def rooms_timeline(site_id: str, day: date, bucket_minutes: int = Query(default=5, ge=1, le=60),
                             user: User = Depends(auth)) -> dict:
        """Average adults/children per room in time buckets across a day, for charts."""
        site = visible_site(user, site_id)
        start, end = day_bounds(site, day)
        async with state["db"]() as s:
            rows = (await s.execute(
                select(RoomStatRow.room_id, RoomStatRow.ts, RoomStatRow.adults, RoomStatRow.children, RoomStatRow.no_adult)
                .where(RoomStatRow.site_id == site_id, RoomStatRow.ts >= start, RoomStatRow.ts < end)
                .order_by(RoomStatRow.ts)
            )).all()
        size = bucket_minutes * 60
        buckets: dict[str, dict[int, list]] = {r.id: {} for r in site.rooms}
        for room_id, ts, adults, children, no_adult in rows:
            if room_id in buckets:
                buckets[room_id].setdefault(int((ts - start) // size), []).append((adults, children, no_adult))
        return {
            "start": start, "bucket_seconds": size,
            "rooms": [
                {"room_id": room.id, "name": room.name, "points": [
                    {"t": start + i * size,
                     "adults": round(sum(a for a, _, _ in v) / len(v), 1),
                     "children": round(sum(c for _, c, _ in v) / len(v), 1),
                     "no_adult": any(n for _, _, n in v)}
                    for i, v in sorted(buckets[room.id].items())
                ]}
                for room in site.rooms
            ],
        }

    @app.get("/api/accuracy")
    async def accuracy(site_id: str, days: int = Query(default=30, ge=1, le=365), user: User = Depends(auth)) -> dict:
        """Live precision per alert type from staff reviews (recall is measured offline by `vdb eval`)."""
        visible_site(user, site_id)
        since = time.time() - days * 86400
        async with state["db"]() as s:
            rows = (await s.execute(
                select(AlertRow.type, AlertRow.status).where(AlertRow.site_id == site_id, AlertRow.triggered_at >= since)
            )).all()
        by_type: dict[str, Counter] = {}
        for t, st in rows:
            by_type.setdefault(t, Counter())[st] += 1
        return {t: {**c, "precision": precision(c)} for t, c in by_type.items()}

    @app.get("/api/access-log")
    async def access_log(site_id: str, limit: int = Query(default=200, le=1000), user: User = Depends(auth)) -> list[dict]:
        """Who viewed which footage — answers a parent's 'who watched my child's clip?'."""
        visible_site(user, site_id)
        async with state["db"]() as s:
            rows = (await s.scalars(select(AccessLog).where(AccessLog.site_id == site_id)
                                    .order_by(AccessLog.ts.desc()).limit(limit))).all()
        return [{"ts": r.ts, "user": r.user, "alert_id": r.alert_id, "kind": r.kind, "ip": r.ip} for r in rows]

    async def build_report(user: User, site_id: str, day: date, request: Request) -> dict:
        site = visible_site(user, site_id)
        await audit(user, site_id, "report", request)
        async with state["db"]() as s:
            return await daily_report(s, settings, site, day)

    @app.get("/api/reports/daily")
    async def report_json(site_id: str, day: date, request: Request, user: User = Depends(auth)) -> dict:
        return await build_report(user, site_id, day, request)

    @app.get("/api/reports/daily.html", response_class=HTMLResponse)
    async def report_html(site_id: str, day: date, request: Request, user: User = Depends(auth)) -> str:
        return render_report_html(await build_report(user, site_id, day, request))

    @app.websocket("/ws/alerts")
    async def alerts_ws(ws: WebSocket) -> None:
        # Browsers cannot set headers on WebSockets, so the key arrives as the first message (never in the URL).
        await ws.accept()
        try:
            first = json.loads(await asyncio.wait_for(ws.receive_text(), WS_AUTH_TIMEOUT))
            user = lookup(str(first.get("key", "")))
        except (asyncio.TimeoutError, ValueError, AttributeError, WebSocketDisconnect):
            user = None
        if user is None:
            with suppress(Exception):
                await ws.close(code=4401)
            return
        clients[ws] = user
        try:
            while True:
                await ws.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            clients.pop(ws, None)

    return app
