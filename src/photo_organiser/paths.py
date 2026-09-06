"""Two-level hashed media cache paths (avoids huge directories / Windows MAX_PATH)."""

from __future__ import annotations

from pathlib import Path


def hashed_subdir(media_key: str, root: Path, suffix: str = ".jpg") -> Path:
    """Return ``root / aa / bb / <media_key>.jpg`` using the first 4 hex chars."""
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in media_key)
    # Use a stable hash prefix so directory fan-out is even
    h = __import__("hashlib").sha1(media_key.encode("utf-8")).hexdigest()
    return root / h[:2] / h[2:4] / f"{safe}{suffix}"


def thumb_path(media_key: str, thumbs_dir: Path) -> Path:
    return hashed_subdir(media_key, thumbs_dir, ".jpg")


def preview_path(media_key: str, previews_dir: Path) -> Path:
    return hashed_subdir(media_key, previews_dir, ".jpg")


def full_path(media_key: str, fulls_dir: Path) -> Path:
    return hashed_subdir(media_key, fulls_dir, ".jpg")


def sized_thumb_url(thumb_url: str, size: int) -> str:
    """Rewrite a Google Photos thumb URL to a fixed size without auth.

    GPTK notes: append ``=wN-hN-k-no?authuser=0`` to drop watermark / auth.
    """
    if not thumb_url:
        return thumb_url
    base = thumb_url.split("=")[0].split("?")[0]
    return f"{base}=w{size}-h{size}-k-no?authuser=0"


def large_media_url(thumb_url: str, size: int = 4096) -> str:
    """Near-original display size from the GPTK thumb URL (not a Takeout file)."""
    return sized_thumb_url(thumb_url, size)
