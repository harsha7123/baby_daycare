from collections import Counter
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vdb.config import Settings, Site
from vdb.db import AlertRow, RoomStatRow

_templates = Environment(
    loader=FileSystemLoader(Path(__file__).parent / "templates"),
    autoescape=select_autoescape(["html"]),
)


def day_bounds(site: Site, day: date) -> tuple[float, float]:
    tz = ZoneInfo(site.timezone)
    start = datetime.combine(day, time.min, tz)
    return start.timestamp(), (start + timedelta(days=1)).timestamp()


def precision(counts: Counter) -> float | None:
    reviewed = counts["confirmed"] + counts["false_alarm"]
    return round(counts["confirmed"] / reviewed, 3) if reviewed else None


async def daily_report(session: AsyncSession, settings: Settings, site: Site, day: date) -> dict:
    start, end = day_bounds(site, day)
    interval_min = settings.rules.stats_interval_seconds / 60
    tz = ZoneInfo(site.timezone)

    stats = (await session.scalars(
        select(RoomStatRow).where(RoomStatRow.site_id == site.id, RoomStatRow.ts >= start, RoomStatRow.ts < end)
    )).all()
    alerts = (await session.scalars(
        select(AlertRow).where(AlertRow.site_id == site.id, AlertRow.triggered_at >= start, AlertRow.triggered_at < end)
        .order_by(AlertRow.triggered_at)
    )).all()

    rooms = []
    for room in site.rooms:
        rs = [s for s in stats if s.room_id == room.id]
        occupied = [s for s in rs if s.children > 0]
        room_alerts = [a for a in alerts if a.room_id == room.id]
        rooms.append({
            "id": room.id,
            "name": room.name,
            "monitored_minutes": round(len(rs) * interval_min),
            "occupied_minutes": round(len(occupied) * interval_min),
            "no_adult_minutes": round(sum(s.no_adult for s in rs) * interval_min, 1),
            "ratio_compliance_pct": round(100 * sum(s.ratio_ok for s in occupied) / len(occupied), 1) if occupied else None,
            "peak_children": max((s.children for s in rs), default=0),
            "alerts": dict(Counter(a.type for a in room_alerts)),
        })

    status_by_type: dict[str, Counter] = {}
    for a in alerts:
        status_by_type.setdefault(a.type, Counter())[a.status] += 1

    return {
        "site": {"id": site.id, "name": site.name, "timezone": site.timezone},
        "date": day.isoformat(),
        "rooms": rooms,
        "alert_totals": dict(Counter(a.type for a in alerts)),
        "review": {
            t: {"open": c["open"], "confirmed": c["confirmed"], "false_alarm": c["false_alarm"], "precision": precision(c)}
            for t, c in status_by_type.items()
        },
        "alerts": [
            {
                "id": a.id, "time": datetime.fromtimestamp(a.triggered_at, tz).strftime("%H:%M:%S"),
                "room": next((r.name for r in site.rooms if r.id == a.room_id), a.room_id),
                "type": a.type, "severity": a.severity, "message": a.message, "status": a.status,
            }
            for a in alerts
        ],
    }


def render_report_html(report: dict) -> str:
    return _templates.get_template("report.html").render(r=report)
