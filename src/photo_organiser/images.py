"""Quick image-format checks (magic bytes) — no Pillow required."""

from __future__ import annotations

from pathlib import Path

# JPEG / PNG / GIF / WEBP / BMP — Google thumbs are usually JPEG or WEBP.
_IMAGE_MAGICS: tuple[bytes, ...] = (
    b"\xff\xd8\xff",  # JPEG
    b"\x89PNG\r\n\x1a\n",  # PNG
    b"GIF87a",
    b"GIF89a",
    b"BM",  # BMP
)


def looks_like_image_bytes(data: bytes) -> bool:
    if len(data) < 12:
        return False
    if data.startswith(_IMAGE_MAGICS):
        return True
    # WEBP: RIFF....WEBP
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return True
    return False


def looks_like_image_file(path: Path) -> bool:
    try:
        if not path.is_file() or path.stat().st_size < 12:
            return False
        with path.open("rb") as f:
            head = f.read(32)
        return looks_like_image_bytes(head)
    except OSError:
        return False


def describe_file_head(path: Path, n: int = 60) -> str:
    """Short diagnostic for bad downloads (often HTML error pages)."""
    try:
        raw = path.read_bytes()[:n]
    except OSError as exc:
        return f"<unreadable: {exc}>"
    try:
        text = raw.decode("utf-8", errors="replace").replace("\n", " ")
        if text.lstrip().lower().startswith("<!DOCTYPE") or text.lstrip().lower().startswith(
            "<html"
        ):
            return f"HTML: {text[:50]!r}…"
    except Exception:  # noqa: BLE001
        pass
    return f"bytes={raw[:16]!r} size={path.stat().st_size}"
