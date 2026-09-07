"""Text / document photo detection."""

from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from photo_organiser.config import Settings
from photo_organiser.db import get_db
from photo_organiser.paths import preview_path, thumb_path
from photo_organiser.text import decide_text, inspect_file, next_text_item, scan_thumbs, scan_tree


def _page_image(width: int = 256, height: int = 320) -> Image.Image:
    image = Image.new("RGB", (width, height), (245, 242, 230))
    draw = ImageDraw.Draw(image)
    for i, y in enumerate(range(24, height - 20, 14)):
        draw.rectangle([18, y, width - 18, y + 6], fill=(30, 30, 30) if i % 7 else (20, 20, 80))
    return image


def _landscape() -> Image.Image:
    pixels = np.zeros((192, 256, 3), dtype=np.uint8)
    pixels[:80, :, 0] = 80
    pixels[:80, :, 1] = 140
    pixels[:80, :, 2] = 220
    pixels[80:, :, 0] = 40
    pixels[80:, :, 1] = 120
    pixels[80:, :, 2] = 50
    return Image.fromarray(pixels)


def _face() -> Image.Image:
    image = Image.new("RGB", (256, 320), (70, 130, 200))
    draw = ImageDraw.Draw(image)
    draw.ellipse([48, 36, 208, 268], fill=(214, 162, 128))
    draw.ellipse([88, 110, 118, 140], fill=(50, 35, 30))
    draw.ellipse([148, 110, 178, 140], fill=(50, 35, 30))
    draw.ellipse([112, 168, 150, 208], fill=(180, 90, 90))
    return image


def _mountain() -> Image.Image:
    pixels = np.zeros((256, 320, 3), dtype=np.uint8)
    for y in range(110):
        pixels[y, :, 0] = 90 + y
        pixels[y, :, 1] = 140 + y // 2
        pixels[y, :, 2] = 210
    for y in range(110, 256):
        t = y - 110
        pixels[y, :, 0] = 90 + t // 2
        pixels[y, :, 1] = 100
        pixels[y, :, 2] = 70
    rng = np.random.default_rng(0)
    noise = rng.integers(-12, 13, pixels.shape, dtype=np.int16)
    return Image.fromarray(np.clip(pixels.astype(np.int16) + noise, 0, 255).astype(np.uint8))


def _sunset_bands() -> Image.Image:
    pixels = np.zeros((192, 256, 3), dtype=np.uint8)
    bands = [(255, 120, 40), (240, 80, 60), (90, 40, 110), (20, 20, 50)]
    height = 192 // len(bands)
    for i, color in enumerate(bands):
        pixels[i * height : (i + 1) * height] = color
    return Image.fromarray(pixels)


def test_page_is_text(tmp_path: Path):
    path = tmp_path / "page.jpg"
    _page_image().save(path, "JPEG", quality=90)
    verdict = inspect_file(path)
    assert verdict.is_text is True
    assert "text_lines" in verdict.reasons
    assert verdict.line_count >= 8


def test_landscape_is_not_text(tmp_path: Path):
    path = tmp_path / "sky.jpg"
    _landscape().save(path, "JPEG", quality=90)
    verdict = inspect_file(path)
    assert verdict.is_text is False


def test_face_is_not_text(tmp_path: Path):
    path = tmp_path / "face.jpg"
    _face().save(path, "JPEG", quality=90)
    assert inspect_file(path).is_text is False


def test_mountain_is_not_text(tmp_path: Path):
    path = tmp_path / "ridge.jpg"
    _mountain().save(path, "JPEG", quality=90)
    assert inspect_file(path).is_text is False


def test_sunset_bands_are_not_text(tmp_path: Path):
    path = tmp_path / "sunset.jpg"
    _sunset_bands().save(path, "JPEG", quality=90)
    assert inspect_file(path).is_text is False


def test_scan_moves_text_pages(tmp_path: Path):
    src = tmp_path / "lib"
    src.mkdir()
    page = src / "note.jpg"
    photo = src / "view.jpg"
    _page_image().save(page, "JPEG", quality=90)
    _landscape().save(photo, "JPEG", quality=90)
    dest = tmp_path / "text"
    stats = scan_tree(src, move_to=dest)
    assert stats["text"] == 1
    assert stats["other"] == 1
    assert (dest / "note.jpg").exists()
    assert photo.exists()
    assert not page.exists()


def test_scan_thumbs_and_accept(tmp_path: Path):
    settings = Settings(data_root=tmp_path / "data")
    settings.ensure_dirs()
    with get_db(settings.db_path) as conn:
        conn.execute(
            "INSERT INTO photos(media_key, dedup_key, thumb_cached, file_name) VALUES(?,?,1,?)",
            ("mk_note", "dk_note", "note.jpg"),
        )
        conn.execute(
            "INSERT INTO photos(media_key, dedup_key, thumb_cached, file_name) VALUES(?,?,1,?)",
            ("mk_view", "dk_view", "view.jpg"),
        )
    note = thumb_path("mk_note", settings.thumbs_dir)
    view = thumb_path("mk_view", settings.thumbs_dir)
    note.parent.mkdir(parents=True, exist_ok=True)
    view.parent.mkdir(parents=True, exist_ok=True)
    _page_image().save(note, "JPEG", quality=90)
    _landscape().save(view, "JPEG", quality=90)

    stats = scan_thumbs(settings)
    assert stats["text"] == 1
    assert stats["other"] == 1

    with get_db(settings.db_path) as conn:
        item = next_text_item(conn)
        assert item is not None
        assert item["media_key"] == "mk_note"
        decide_text(conn, "mk_note", "accept")
        assert next_text_item(conn) is None
        status = conn.execute(
            "SELECT review_status FROM text_flags WHERE media_key='mk_note'"
        ).fetchone()["review_status"]
        assert status == "accepted"


def test_scan_thumbs_prefers_preview(tmp_path: Path):
    settings = Settings(data_root=tmp_path / "data")
    settings.ensure_dirs()
    with get_db(settings.db_path) as conn:
        conn.execute(
            "INSERT INTO photos(media_key, dedup_key, thumb_cached, file_name) VALUES(?,?,1,?)",
            ("mk_mix", "dk_mix", "mix.jpg"),
        )
    thumb = thumb_path("mk_mix", settings.thumbs_dir)
    preview = preview_path("mk_mix", settings.previews_dir)
    thumb.parent.mkdir(parents=True, exist_ok=True)
    preview.parent.mkdir(parents=True, exist_ok=True)
    _landscape().save(thumb, "JPEG", quality=90)
    _page_image().save(preview, "JPEG", quality=90)

    stats = scan_thumbs(settings)
    assert stats["text"] == 1
