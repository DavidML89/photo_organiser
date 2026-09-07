"""Corruption detection for truncated recovered photos."""

from __future__ import annotations

import io
import json
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from photo_organiser.config import Settings
from photo_organiser.corrupt import inspect_file, scan_thumbs, scan_tree
from photo_organiser.db import get_db
from photo_organiser.paths import thumb_path


def _save_jpeg(image: Image.Image, path: Path, quality: int = 85) -> None:
    image.save(path, "JPEG", quality=quality)


def _content_image(width: int = 256, height: int = 192) -> Image.Image:
    image = Image.new("RGB", (width, height), (40, 90, 170))
    draw = ImageDraw.Draw(image)
    for y in range(0, height, 6):
        draw.line([(0, y), (width, y)], fill=(y % 255, 180, 50))
    draw.rectangle([12, 12, 90, 90], fill=(240, 20, 120))
    return image


def _truncated_jpeg_bytes(image: Image.Image, keep_frac: float = 0.3) -> bytes:
    buf = io.BytesIO()
    image.save(buf, "JPEG", quality=85)
    data = buf.getvalue()
    cut = data[: max(64, int(len(data) * keep_frac))]
    return cut + b"\x00" * 64 + b"\xff\xd9"


def test_intact_jpeg_is_ok(tmp_path: Path):
    path = tmp_path / "ok.jpg"
    _save_jpeg(_content_image(), path)
    verdict = inspect_file(path)
    assert verdict.corrupted is False
    assert verdict.reasons == []


def test_truncated_jpeg_decoder_fill(tmp_path: Path):
    path = tmp_path / "bad.jpg"
    path.write_bytes(_truncated_jpeg_bytes(_content_image(), keep_frac=0.25))
    verdict = inspect_file(path)
    assert verdict.corrupted is True
    assert "decoder_fill" in verdict.reasons
    assert verdict.fill_fraction >= 0.08
    assert verdict.fill_color == (128, 128, 128)


def test_missing_jpeg_eoi(tmp_path: Path):
    path = tmp_path / "noeoi.jpg"
    buf = io.BytesIO()
    _content_image().save(buf, "JPEG", quality=85)
    data = buf.getvalue()
    if data.endswith(b"\xff\xd9"):
        data = data[:-2]
    path.write_bytes(data)
    verdict = inspect_file(path)
    assert verdict.corrupted is True
    assert "truncated_jpeg" in verdict.reasons


def test_html_file_is_not_an_image(tmp_path: Path):
    path = tmp_path / "page.jpg"
    path.write_bytes(b"<!DOCTYPE html><html><body>nope</body></html>")
    verdict = inspect_file(path)
    assert verdict.corrupted is True
    assert verdict.reasons == ["not_an_image"]


def test_sky_on_top_is_not_trailing_fill(tmp_path: Path):
    image = Image.new("RGB", (128, 96), (200, 210, 230))
    pixels = np.array(image)
    pixels[48:] = np.random.default_rng(0).integers(0, 255, size=(48, 128, 3), dtype=np.uint8)
    path = tmp_path / "sky.jpg"
    _save_jpeg(Image.fromarray(pixels), path, quality=95)
    verdict = inspect_file(path)
    assert verdict.corrupted is False


def test_scan_moves_corrupt_and_keeps_relative_path(tmp_path: Path):
    src = tmp_path / "recovered" / "DCIM"
    src.mkdir(parents=True)
    ok = src / "ok.jpg"
    bad = src / "bad.jpg"
    _save_jpeg(_content_image(), ok)
    bad.write_bytes(_truncated_jpeg_bytes(_content_image(), keep_frac=0.2))

    dest = tmp_path / "corrupt"
    report = tmp_path / "report.jsonl"
    stats = scan_tree(tmp_path / "recovered", move_to=dest, report=report)

    assert stats["scanned"] == 2
    assert stats["corrupt"] == 1
    assert stats["moved"] == 1
    assert ok.exists()
    assert not bad.exists()
    assert (dest / "DCIM" / "bad.jpg").exists()

    lines = report.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    rows = [json.loads(line) for line in lines]
    assert sum(1 for row in rows if row["corrupted"]) == 1


def test_scan_thumbs_uses_cached_jpegs(tmp_path: Path):
    settings = Settings(data_root=tmp_path / "data")
    settings.ensure_dirs()
    with get_db(settings.db_path) as conn:
        conn.execute(
            "INSERT INTO photos(media_key, dedup_key, thumb_cached, file_name) VALUES(?,?,1,?)",
            ("mk_ok", "dk_ok", "ok.jpg"),
        )
        conn.execute(
            "INSERT INTO photos(media_key, dedup_key, thumb_cached, file_name) VALUES(?,?,1,?)",
            ("mk_bad", "dk_bad", "bad.jpg"),
        )

    ok_path = thumb_path("mk_ok", settings.thumbs_dir)
    bad_path = thumb_path("mk_bad", settings.thumbs_dir)
    ok_path.parent.mkdir(parents=True, exist_ok=True)
    bad_path.parent.mkdir(parents=True, exist_ok=True)
    _save_jpeg(_content_image(), ok_path)
    bad_path.write_bytes(_truncated_jpeg_bytes(_content_image(), keep_frac=0.2))

    stats = scan_thumbs(settings)
    assert stats["scanned"] == 2
    assert stats["corrupt"] == 1
    assert stats["ok"] == 1

    with get_db(settings.db_path) as conn:
        flagged = conn.execute(
            "SELECT media_key FROM corrupt_flags WHERE corrupted=1"
        ).fetchall()
        assert [r["media_key"] for r in flagged] == ["mk_bad"]
