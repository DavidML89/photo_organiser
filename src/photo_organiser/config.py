"""Application settings and paths."""

from __future__ import annotations

import platform
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def default_data_root() -> Path:
    """Short, non-OneDrive path on Windows; project-local data/ elsewhere."""
    if platform.system() == "Windows":
        return Path(r"C:\gpdedupe")
    return Path.home() / ".gpdedupe"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="GPDEDUPE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    data_root: Path = Field(default_factory=default_data_root)
    host: str = "127.0.0.1"
    port: int = 8787

    # Thumbnail sizes
    thumb_size: int = 256
    preview_size: int = 1600

    # Fetch
    fetch_concurrency: int = 16
    fetch_retries: int = 4
    fetch_timeout_s: float = 30.0

    # Embedding
    dinov2_model: str = "facebook/dinov2-base"
    embed_batch_size: int = 32
    embed_checkpoint_every: int = 64

    # Grouping — defaults tuned for 256px DINOv2 thumbs (stricter = fewer groups).
    # Bursts are usually caught by the time window; global ANN catches re-uploads.
    time_window_s: int = 120
    time_cosine_threshold: float = 0.88
    global_cosine_threshold: float = 0.94
    ann_topk: int = 20
    min_group_size: int = 2

    # Scoring weights (relative within group)
    weight_sharpness: float = 0.35
    weight_exposure: float = 0.15
    weight_eyes: float = 0.25
    weight_smile: float = 0.10
    weight_aesthetic: float = 0.10
    weight_resolution: float = 0.05

    # Safety — album members are NOT excluded from embed/group by default.
    # They are still blocked from trash at apply time (see apply.py).
    exclude_favorites: bool = True
    exclude_album_members: bool = False
    exclude_videos: bool = True
    exclude_live_photos: bool = True

    @property
    def db_path(self) -> Path:
        return self.data_root / "gpdedupe.db"

    @property
    def census_path(self) -> Path:
        return self.data_root / "census.jsonl"

    @property
    def thumbs_dir(self) -> Path:
        return self.data_root / "thumbs"

    @property
    def previews_dir(self) -> Path:
        return self.data_root / "previews"

    @property
    def embeddings_dir(self) -> Path:
        return self.data_root / "embeddings"

    @property
    def undo_dir(self) -> Path:
        return self.data_root / "undo"

    @property
    def models_dir(self) -> Path:
        return self.data_root / "models"

    @property
    def logs_dir(self) -> Path:
        return self.data_root / "logs"

    def ensure_dirs(self) -> None:
        for path in (
            self.data_root,
            self.thumbs_dir,
            self.previews_dir,
            self.embeddings_dir,
            self.undo_dir,
            self.models_dir,
            self.logs_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
        _settings.ensure_dirs()
    return _settings
