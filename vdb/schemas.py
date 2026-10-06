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
    POSSIBLE_AGGRESSION = "possible_aggression"
    CHILD_FALL = "child_fall"


Verification = Literal["pending", "likely", "unlikely", "unclear"]


class Person(BaseModel):
    track_id: int
    role: Role
    bbox: tuple[float, float, float, float]  # normalised x1, y1, x2, y2
    confidence: float
    has_phone: bool = False
    zones: list[str] = []
    # COCO-17 keypoints, normalised (x, y, confidence); only present when pose ran on this frame.
    keypoints: list[tuple[float, float, float]] | None = None


class FrameResult(BaseModel):
    site_id: str
    room_id: str
    camera_id: str
    ts: float
    people: list[Person]
    frame_aspect: float = 16 / 9  # width / height, so motion can be measured in equal units both ways


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
    # Second opinion from the video-language model (aggression alerts only); a human always decides.
    verification: Verification | None = None
    verification_note: str | None = None
    # Normalised box around the people involved (behaviour alerts), so reviewers and the verifier can zoom in.
    region: tuple[float, float, float, float] | None = None


class AlertUpdate(BaseModel):
    site_id: str
    alert_id: str
    verification: Verification
    verification_note: str | None = None


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
