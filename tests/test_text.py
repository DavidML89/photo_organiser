"""Text / document photo detection."""

from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from photo_organiser.config import Settings
from photo_organiser.db import get_db
from photo_organiser.paths import thumb_path
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


def test_page_is_text(tmp_path: Path):
    path = tmp_path / "page.jpg"
    _page_image().save(path, "JPEG", quality=90)
    verdict = inspect_file(path)
    assert verdict.is_text is True
    assert "text_lines" in verdict.reasons
    assert verdict.line_count >= 5


def test_landscape_is_not_text(tmp_path: Path):
    path = tmp_path / "sky.jpg"
    _landscape().save(path, "JPEG", quality=90)
    verdict = inspect_file(path)
    assert verdict.is_text is False


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
