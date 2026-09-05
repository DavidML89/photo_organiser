"""Census import against a tiny fixture."""

from __future__ import annotations

import json
from pathlib import Path

from photo_organiser.census import import_census
from photo_organiser.config import Settings
from photo_organiser.db import get_db
from photo_organiser.safety import apply_exclusions


def test_census_import_and_safety(tmp_path: Path):
    census = tmp_path / "census.jsonl"
    rows = [
        {"_type": "quota", "totalUsed": 100, "totalAvailable": 200, "usedByGPhotos": 90},
        {
            "media_key": "mk1",
            "dedup_key": "dk1",
            "timestamp": 1000,
            "thumb": "https://lh3.googleusercontent.com/a",
            "res_width": 100,
            "res_height": 100,
            "is_favorite": True,
        },
        {
            "media_key": "mk2",
            "dedup_key": "dk2",
            "timestamp": 1001,
            "thumb": "https://lh3.googleusercontent.com/b",
            "res_width": 200,
            "res_height": 200,
            "duration": 12.5,
        },
        {
            "media_key": "mk3",
            "dedup_key": "dk3",
            "timestamp": 1002,
            "thumb": "https://lh3.googleusercontent.com/c",
            "res_width": 300,
            "res_height": 300,
            "is_live_photo": True,
        },
        {
            "media_key": "mk4",
            "dedup_key": "dk4",
            "timestamp": 1003,
            "thumb": "https://lh3.googleusercontent.com/d",
            "res_width": 400,
            "res_height": 400,
            "in_album": True,
        },
    ]
    census.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    settings = Settings(data_root=tmp_path / "data")
    settings.ensure_dirs()
    stats = import_census(census, settings=settings)
    assert stats["total"] == 4
    assert stats["favorites"] == 1
    assert stats["videos"] == 1
    assert stats["live_photos"] == 1

    excl = apply_exclusions(settings)
    # Favorites, videos, live photos excluded; album members stay eligible for embed/group
    assert excl["total_excluded"] == 3
    assert excl["album_members"] == 0

    with get_db(settings.db_path) as conn:
        kept = [
            r["media_key"]
            for r in conn.execute("SELECT media_key FROM photos WHERE excluded=0").fetchall()
        ]
        assert kept == ["mk4"]
