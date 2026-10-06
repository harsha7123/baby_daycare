import uuid
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field


class Role(StrEnum):
    ADULT = "adult"
    CHILD = "child"
    UNKNOWN = "unknown"


class AlertType(StrEnum):
    NO_ADULT = "no_adult"
    RATIO_BREACH = "ratio_breach"
    PHONE_USE = "phone_use"
    RESTRICTED_ZONE = "restricted_zone"
    CAMERA_OFFLINE = "camera_offline"


class Person(BaseModel):
    track_id: int
    role: Role
    bbox: tuple[float, float, float, float]  # normalised x1, y1, x2, y2
    confidence: float
    has_phone: bool = False
    zones: list[str] = []


class FrameResult(BaseModel):
    site_id: str
    room_id: str
    camera_id: str
    ts: float
    people: list[Person]


class Alert(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    site_id: str
    room_id: str
    camera_id: str | None
    type: AlertType
    severity: Literal["high", "medium", "low"]
    started_at: float  # when the condition began
    triggered_at: float  # when it had lasted long enough to alert
    message: str
    track_id: int | None = None
    thumbnail: str | None = None
    clip: str | None = None


class RoomStat(BaseModel):
    site_id: str
    room_id: str
    ts: float
    adults: int
    children: int
    cameras_online: int
    no_adult: bool
    ratio_ok: bool


class ClipReady(BaseModel):
    site_id: str
    alert_id: str
    clip: str
