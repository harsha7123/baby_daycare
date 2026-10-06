import logging
import time
from pathlib import Path

from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from vdb.config import Settings
from vdb.db import AccessLog, AlertRow, RoomStatRow

log = logging.getLogger(__name__)
DAY = 86400


def safe_media_path(base: Path, rel: str | None) -> Path | None:
    """Resolves a stored media path, refusing anything that escapes the clips directory."""
    if not rel:
        return None
    base = base.resolve()
    path = (base / rel).resolve()
    return path if path.is_relative_to(base) else None


async def apply_retention(settings: Settings, sessions: async_sessionmaker[AsyncSession], now: float | None = None) -> dict:
    """Deletes children's footage and records past their retention period. Confirmed incidents are kept
    (evidence) until an admin removes them."""
    now = now or time.time()
    media_cutoff = now - settings.retention.media_days * DAY
    records_cutoff = now - settings.retention.records_days * DAY
    files = 0
    async with sessions() as s:
        expired = (await s.scalars(select(AlertRow).where(
            AlertRow.triggered_at < media_cutoff, AlertRow.status != "confirmed",
            or_(AlertRow.thumbnail.is_not(None), AlertRow.clip.is_not(None)),
        ))).all()
        for row in expired:
            for rel in (row.thumbnail, row.clip):
                if (path := safe_media_path(settings.clips.dir, rel)) and path.is_file():
                    path.unlink()
                    files += 1
            row.thumbnail = row.clip = None
        alerts = await s.execute(delete(AlertRow).where(AlertRow.triggered_at < records_cutoff, AlertRow.status != "confirmed"))
        stats = await s.execute(delete(RoomStatRow).where(RoomStatRow.ts < records_cutoff))
        access = await s.execute(delete(AccessLog).where(AccessLog.ts < records_cutoff))
        await s.commit()
    result = {"media_files": files, "alerts": alerts.rowcount, "room_stats": stats.rowcount, "access_log": access.rowcount}
    if any(result.values()):
        log.info("retention deleted %s", result)
    return result
