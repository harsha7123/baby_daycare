import os
from pathlib import Path
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator


class Zone(BaseModel):
    name: str
    # Polygon in normalised image coordinates (0-1), so it survives resolution changes.
    polygon: list[tuple[float, float]] = Field(min_length=3)


# Ids end up in message-bus subjects and file paths, so keep them to safe characters.
SafeId = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")]


class Camera(BaseModel):
    id: SafeId
    room: str
    # Video file path, stream URL, or "env:VAR_NAME" so camera credentials stay out of config files.
    source: str
    fps: float = 5.0
    enabled: bool = True  # switch off where consent is withdrawn
    # Where a person "stands": box bottom for wall cameras, box centre for ceiling/fisheye cameras.
    foot_point: Literal["bottom", "center"] = "bottom"
    restricted_zones: list[Zone] = []

    def stream_url(self) -> str:
        if not self.source.startswith("env:"):
            return self.source
        var = self.source.removeprefix("env:")
        if not os.environ.get(var):
            raise RuntimeError(f"camera {self.id}: environment variable {var} is not set")
        return os.environ[var]


class Room(BaseModel):
    id: SafeId
    name: str
    max_children_per_adult: int = 4
    # True when cameras see the same space (count = max per camera); False when they cover separate areas (sum).
    cameras_overlap: bool = True
    # EXPERIMENTAL child_fall alerts. Leave off where crawling/lying down is normal (infant and nap rooms).
    fall_detection: bool = False


class Rules(BaseModel):
    no_adult_seconds: float = 20
    ratio_seconds: float = 60
    phone_seconds: float = 10
    restricted_zone_seconds: float = 3
    camera_offline_seconds: float = 30
    # Detections flicker; a condition survives gaps up to this long before resetting.
    gap_tolerance_seconds: float = 3
    cooldown_seconds: float = 120
    stats_interval_seconds: float = 10
    # Room head-counts use the median over this window so one missed or phantom detection doesn't flip a rule.
    count_window_seconds: float = 10
    # EXPERIMENTAL possible-aggression flags (pose heuristics; always need human review). Off until `vdb eval`
    # on real labelled footage meets the target. Speeds are in body-heights per second.
    aggression_enabled: bool = False
    strike_speed: float = 1.5  # adult hand moving towards and into a child this fast (and the child reacts)
    push_speed: float = 2.0  # a hand this fast into a child just before they fall counts as a possible push
    push_detection: bool = True
    rough_handling_speed: float = 1.5  # child moving this fast relative to the adult holding them (back and forth)
    contact_margin: float = 0.15  # how far outside the child's box (fraction of its size) still counts as touching
    aggression_cooldown_seconds: float = 30
    fall_down_seconds: float = 10  # a fallen child who stays down this long raises child_fall


class Models(BaseModel):
    detector: Literal["nano", "small", "medium", "large"] = "small"
    # "tensorrt" compiles the detector for the exact GPU on first start (cached in engine_dir): 2-4x faster.
    backend: Literal["torch", "tensorrt"] = "torch"
    engine_dir: Path = Path("data/models")
    device: str = "auto"
    person_threshold: float = 0.4
    # Low-confidence people still help ByteTrack keep existing tracks alive through occlusion.
    track_low_threshold: float = 0.1
    phone_threshold: float = 0.3
    batch_size: int = 8
    role_model: str = "openai/clip-vit-base-patch32"
    role_refresh_seconds: float = 2.0
    role_min_samples: int = 2
    # A new person counts as "maybe an adult" for at most this long while being classified.
    role_grace_seconds: float = 6.0
    # Pose (RF-DETR keypoints) feeds the aggression checks. "interaction" runs it only on frames where an adult
    # is close to a child, which keeps GPU cost low; "always" also draws skeletons on every live-view frame.
    pose_mode: Literal["interaction", "always", "off"] = "interaction"
    pose_threshold: float = 0.4
    interaction_distance: float = 1.0  # gap between adult and child boxes, in adult body-heights
    pose_linger_seconds: float = 1.5  # keep running pose this long after an adult was near a child


class Clips(BaseModel):
    dir: Path = Path("data/clips")
    pre_seconds: float = 30  # max lead-in; clips start shortly before the condition began
    post_seconds: float = 5


class Verifier(BaseModel):
    """Video-language model that gives a second opinion on possible-aggression alerts (needs a big GPU)."""

    enabled: bool = False
    model: str = "Qwen/Qwen3-VL-8B-Instruct"  # Apache-2.0; Qwen/Qwen3-VL-2B-Instruct for small GPUs
    revision: str | None = None  # pin a Hugging Face commit sha in production so the model can't change underneath
    max_frames: int = 6
    frame_width: int = 448


class Retention(BaseModel):
    media_days: int = 30  # snapshots/clips; confirmed incidents are kept until deleted by an admin
    records_days: int = 365  # alert rows and room statistics


class User(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    key_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    sites: list[str] = ["*"]

    def can_see(self, site_id: str) -> bool:
        return "*" in self.sites or site_id in self.sites


class Site(BaseModel):
    id: SafeId
    name: str
    timezone: str = "UTC"
    rooms: list[Room]
    cameras: list[Camera]

    @field_validator("timezone")
    @classmethod
    def _valid_timezone(cls, tz: str) -> str:
        ZoneInfo(tz)
        return tz

    @model_validator(mode="after")
    def _cameras_reference_rooms(self) -> "Site":
        room_ids = {r.id for r in self.rooms}
        for cam in self.cameras:
            if cam.room not in room_ids:
                raise ValueError(f"camera {cam.id} references unknown room {cam.room}")
        return self


class Settings(BaseModel):
    sites: list[Site]
    users: list[User] = []
    rules: Rules = Rules()
    models: Models = Models()
    clips: Clips = Clips()
    verifier: Verifier = Verifier()
    retention: Retention = Retention()
    preview_fps: float = 1.0  # annotated live-view frames per camera per second (0 = off)
    demo: bool = False  # `vdb demo` refuses to run unless the config is explicitly a demo config
    bus_url: str | None = None  # nats://host:4222; None = in-process bus
    database_url: str = "sqlite+aiosqlite:///data/vdb.db"

    @model_validator(mode="after")
    def _unique_ids(self) -> "Settings":
        for kind, ids in (("camera", [c.id for s in self.sites for c in s.cameras]),
                          ("site", [s.id for s in self.sites]), ("user", [u.name for u in self.users])):
            if dupes := {i for i in ids if ids.count(i) > 1}:
                raise ValueError(f"{kind} ids must be unique: {sorted(dupes)}")
        return self


def load_settings(path: str | Path) -> Settings:
    """Loads YAML config; VDB_DATABASE_URL / VDB_BUS_URL env vars override it so secrets stay out of files."""
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    for key, env in (("database_url", "VDB_DATABASE_URL"), ("bus_url", "VDB_BUS_URL")):
        if os.environ.get(env):
            data[key] = os.environ[env]
    return Settings.model_validate(data)
