"""Import census.jsonl produced by the browser enumeration script."""

from __future__ import annotations

import json
from pathlib import Path

from rich.console import Console
from rich.table import Table

from photo_organiser.config import Settings, get_settings
from photo_organiser.db import get_db, set_meta
from photo_organiser.models import CensusItem

console = Console()


def import_census(census_path: Path | None = None, settings: Settings | None = None) -> dict:
    settings = settings or get_settings()
    path = census_path or settings.census_path
    if not path.exists():
        raise FileNotFoundError(
            f"Census file not found: {path}\n"
            "1. Install Tampermonkey + Google Photos Toolkit (GPTK)\n"
            "2. Open photos.google.com\n"
            "3. Paste browser/census.js into the DevTools console\n"
            f"4. Save the downloaded file as {path}"
        )

    inserted = 0
    updated = 0
    videos = 0
    live = 0
    favorites = 0
    quota: dict | None = None

    with get_db(settings.db_path) as conn, path.open("r", encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            raw = json.loads(line)
            if raw.get("_type") == "quota":
                quota = raw
                set_meta(conn, "quota_baseline", json.dumps(raw))
                continue
            if raw.get("_type") == "meta":
                for k, v in raw.items():
                    if k != "_type":
                        set_meta(conn, k, str(v))
                continue

            item = CensusItem.model_validate(raw)
            is_video = 1 if item.is_video else 0
            if is_video:
                videos += 1
            if item.is_live_photo:
                live += 1
            if item.is_favorite:
                favorites += 1

            existing = conn.execute(
                "SELECT media_key FROM photos WHERE media_key = ?", (item.media_key,)
            ).fetchone()

            conn.execute(
                """
                INSERT INTO photos (
                    media_key, dedup_key, timestamp, timezone_offset, creation_timestamp,
                    thumb, res_width, res_height, is_favorite, is_archived, is_live_photo,
                    is_owned, duration, description_short, file_name, space_taken,
                    is_original_quality, in_album, is_video
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(media_key) DO UPDATE SET
                    dedup_key=excluded.dedup_key,
                    timestamp=excluded.timestamp,
                    timezone_offset=excluded.timezone_offset,
                    creation_timestamp=excluded.creation_timestamp,
                    thumb=excluded.thumb,
                    res_width=excluded.res_width,
                    res_height=excluded.res_height,
                    is_favorite=excluded.is_favorite,
                    is_archived=excluded.is_archived,
                    is_live_photo=excluded.is_live_photo,
                    is_owned=excluded.is_owned,
                    duration=excluded.duration,
                    description_short=excluded.description_short,
                    file_name=excluded.file_name,
                    space_taken=excluded.space_taken,
                    is_original_quality=excluded.is_original_quality,
                    in_album=excluded.in_album,
                    is_video=excluded.is_video
                """,
                (
                    item.media_key,
                    item.dedup_key,
                    item.timestamp,
                    item.timezone_offset,
                    item.creation_timestamp,
                    item.thumb,
                    item.res_width,
                    item.res_height,
                    int(item.is_favorite),
                    int(item.is_archived),
                    int(item.is_live_photo),
                    int(item.is_owned),
                    item.duration,
                    item.description_short,
                    item.file_name,
                    item.space_taken,
                    None if item.is_original_quality is None else int(item.is_original_quality),
                    int(item.in_album),
                    is_video,
                ),
            )
            if existing:
                updated += 1
            else:
                inserted += 1

        from datetime import datetime, timezone

        total = conn.execute("SELECT COUNT(*) AS c FROM photos").fetchone()["c"]
        set_meta(conn, "census_imported_at", datetime.now(timezone.utc).isoformat())

    stats = {
        "path": str(path),
        "inserted": inserted,
        "updated": updated,
        "total": total,
        "videos": videos,
        "live_photos": live,
        "favorites": favorites,
        "quota": quota,
        "line_errors_skipped_at": None,
    }
    _print_report(stats)
    return stats


def report(settings: Settings | None = None) -> dict:
    settings = settings or get_settings()
    with get_db(settings.db_path) as conn:
        total = conn.execute("SELECT COUNT(*) AS c FROM photos").fetchone()["c"]
        videos = conn.execute("SELECT COUNT(*) AS c FROM photos WHERE is_video=1").fetchone()["c"]
        live = conn.execute("SELECT COUNT(*) AS c FROM photos WHERE is_live_photo=1").fetchone()["c"]
        fav = conn.execute("SELECT COUNT(*) AS c FROM photos WHERE is_favorite=1").fetchone()["c"]
        archived = conn.execute("SELECT COUNT(*) AS c FROM photos WHERE is_archived=1").fetchone()["c"]
        thumbs = conn.execute("SELECT COUNT(*) AS c FROM photos WHERE thumb_cached=1").fetchone()["c"]
        embedded = conn.execute("SELECT COUNT(*) AS c FROM photos WHERE embedded=1").fetchone()["c"]
        groups = conn.execute("SELECT COUNT(*) AS c FROM groups").fetchone()["c"]
        pending = conn.execute(
            "SELECT COUNT(*) AS c FROM groups WHERE status='pending'"
        ).fetchone()["c"]
        stats = {
            "total": total,
            "videos": videos,
            "live_photos": live,
            "favorites": fav,
            "archived": archived,
            "photos": total - videos,
            "thumbs_cached": thumbs,
            "embedded": embedded,
            "groups": groups,
            "groups_pending": pending,
        }
    _print_report(stats)
    return stats


def _print_report(stats: dict) -> None:
    table = Table(title="Library census")
    table.add_column("Metric")
    table.add_column("Value", justify="right")
    for k, v in stats.items():
        if k == "quota" and isinstance(v, dict):
            for qk, qv in v.items():
                if qk == "_type":
                    continue
                table.add_row(f"quota.{qk}", str(qv))
        else:
            table.add_row(k, str(v))
    console.print(table)
