from pathlib import Path

from sqlalchemy import Boolean, Float, Index, Integer, String
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class AlertRow(Base):
    __tablename__ = "alerts"
    __table_args__ = (Index("ix_alerts_site_time", "site_id", "triggered_at"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    site_id: Mapped[str] = mapped_column(String(64))
    room_id: Mapped[str] = mapped_column(String(64))
    camera_id: Mapped[str | None] = mapped_column(String(64))
    type: Mapped[str] = mapped_column(String(32))
    severity: Mapped[str] = mapped_column(String(16))
    started_at: Mapped[float] = mapped_column(Float)
    triggered_at: Mapped[float] = mapped_column(Float)
    message: Mapped[str] = mapped_column(String(500))
    track_id: Mapped[int | None] = mapped_column(Integer)
    thumbnail: Mapped[str | None] = mapped_column(String(300))
    clip: Mapped[str | None] = mapped_column(String(300))
    # Staff review feeds the live precision metric: open | confirmed | false_alarm
    status: Mapped[str] = mapped_column(String(16), default="open")
    reviewed_by: Mapped[str | None] = mapped_column(String(100))
    reviewed_at: Mapped[float | None] = mapped_column(Float)
    note: Mapped[str | None] = mapped_column(String(1000))
    verification: Mapped[str | None] = mapped_column(String(16))
    verification_note: Mapped[str | None] = mapped_column(String(500))


class RoomStatRow(Base):
    __tablename__ = "room_stats"
    __table_args__ = (Index("ix_stats_site_room_time", "site_id", "room_id", "ts"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    site_id: Mapped[str] = mapped_column(String(64))
    room_id: Mapped[str] = mapped_column(String(64))
    ts: Mapped[float] = mapped_column(Float)
    adults: Mapped[int] = mapped_column(Integer)
    children: Mapped[int] = mapped_column(Integer)
    cameras_online: Mapped[int] = mapped_column(Integer)
    no_adult: Mapped[bool] = mapped_column(Boolean)
    ratio_ok: Mapped[bool] = mapped_column(Boolean)


class AccessLog(Base):
    """Append-only record of who viewed children's footage or reports, and when."""

    __tablename__ = "access_log"
    __table_args__ = (Index("ix_access_site_time", "site_id", "ts"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[float] = mapped_column(Float)
    user: Mapped[str] = mapped_column(String(100))
    site_id: Mapped[str] = mapped_column(String(64))
    alert_id: Mapped[str | None] = mapped_column(String(32))
    kind: Mapped[str] = mapped_column(String(16))  # thumbnail | clip | report
    ip: Mapped[str | None] = mapped_column(String(64))


async def init_db(url: str) -> async_sessionmaker[AsyncSession]:
    parsed = make_url(url)
    if parsed.get_backend_name() == "sqlite" and parsed.database:
        Path(parsed.database).parent.mkdir(parents=True, exist_ok=True)
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return async_sessionmaker(engine, expire_on_commit=False)
