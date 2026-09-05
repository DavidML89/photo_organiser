"""Shared data models."""

from __future__ import annotations

from pydantic import BaseModel, Field


class CensusItem(BaseModel):
    media_key: str
    dedup_key: str
    timestamp: int | None = None
    timezone_offset: int | None = None
    creation_timestamp: int | None = None
    thumb: str | None = None
    res_width: int | None = None
    res_height: int | None = None
    is_favorite: bool = False
    is_archived: bool = False
    is_live_photo: bool = False
    is_owned: bool = True
    duration: float | None = None
    description_short: str | None = None
    file_name: str | None = None
    space_taken: int | None = None
    is_original_quality: bool | None = None
    in_album: bool = False

    @property
    def is_video(self) -> bool:
        return self.duration is not None and self.duration > 0 and not self.is_live_photo

    @property
    def pixel_count(self) -> int:
        return (self.res_width or 0) * (self.res_height or 0)


class StorageQuota(BaseModel):
    total_used: int | None = None
    total_available: int | None = None
    used_by_gphotos: int | None = None


class ScoreBreakdown(BaseModel):
    sharpness: float = 0.0
    exposure: float = 0.0
    eyes: float = 0.0
    smile: float = 0.0
    aesthetic: float = 0.0
    resolution: float = 0.0
    total: float = 0.0


class GroupMemberView(BaseModel):
    media_key: str
    dedup_key: str
    thumb_url: str | None = None
    local_preview: str | None = None
    local_thumb: str | None = None
    res_width: int | None = None
    res_height: int | None = None
    is_favorite: bool = False
    score: ScoreBreakdown = Field(default_factory=ScoreBreakdown)
    is_proposed_keeper: bool = False


class GroupView(BaseModel):
    group_id: str
    size: int
    status: str = "pending"
    proposed_keeper: str | None = None
    override_keeper: str | None = None
    members: list[GroupMemberView] = Field(default_factory=list)
