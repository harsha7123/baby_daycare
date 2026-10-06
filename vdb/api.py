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
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import select

from vdb.bus import Bus
from vdb.config import Settings, Site, User
from vdb.db import AccessLog, AlertRow, RoomStatRow, init_db
from vdb.reports import daily_report, precision, render_report_html
from vdb.retention import apply_retention, safe_media_path
from vdb.schemas import Alert, ClipReady, RoomStat

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"
WS_AUTH_TIMEOUT = 5
MIN_KEY_LENGTH = 32
AUTH_FAILURES_PER_MINUTE = 20
RETENTION_INTERVAL_SECONDS = 3600
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
    clients: dict[WebSocket, User] = {}
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

    async def on_alert(_: str, data: bytes) -> None:
        alert = Alert.model_validate_json(data)
        async with state["db"]() as s:
            await s.merge(AlertRow(**alert.model_dump(mode="json")))  # merge: redelivered messages are harmless
            await s.commit()
        payload = alert.model_dump_json()
        for ws, user in list(clients.items()):
            if not user.can_see(alert.site_id):
                continue
            try:
                await ws.send_text(payload)
            except Exception:
                clients.pop(ws, None)

    async def on_stat(_: str, data: bytes) -> None:
        async with state["db"]() as s:
            s.add(RoomStatRow(**RoomStat.model_validate_json(data).model_dump()))
            await s.commit()

    async def on_clip(_: str, data: bytes) -> None:
        msg = ClipReady.model_validate_json(data)
        async with state["db"]() as s:
            row = await s.get(AlertRow, msg.alert_id)
            if row is None:
                log.warning("clip for unknown alert %s", msg.alert_id)
                return
            row.clip = msg.clip
            await s.commit()

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

    @app.post("/api/alerts/{alert_id}/review")
    async def review_alert(alert_id: str, body: Review, user: User = Depends(auth)) -> dict:
        await visible_alert(user, alert_id)
        async with state["db"]() as s:
            row = await s.get(AlertRow, alert_id)
            # The reviewer is whoever owns the key — never a name the browser sends.
            row.status, row.reviewed_by, row.note, row.reviewed_at = body.status, user.name, body.note, time.time()
            await s.commit()
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
