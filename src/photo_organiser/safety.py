"""Safety rules: mark photos that must never be proposed for deletion."""

from __future__ import annotations

from rich.console import Console
from rich.table import Table

from photo_organiser.config import Settings, get_settings
from photo_organiser.db import get_db

console = Console()


def apply_exclusions(settings: Settings | None = None) -> dict:
    """Flag photos that should be excluded from grouping/deletion."""
    settings = settings or get_settings()
    counts = {
        "favorites": 0,
        "album_members": 0,
        "videos": 0,
        "live_photos": 0,
        "total_excluded": 0,
    }

    with get_db(settings.db_path) as conn:
        # Reset then re-apply
        conn.execute("UPDATE photos SET excluded=0, exclude_reason=NULL")

        if settings.exclude_favorites:
            cur = conn.execute(
                "UPDATE photos SET excluded=1, exclude_reason='favorite' WHERE is_favorite=1"
            )
            counts["favorites"] = cur.rowcount

        if settings.exclude_album_members:
            cur = conn.execute(
                """
                UPDATE photos SET excluded=1,
                    exclude_reason=COALESCE(exclude_reason || ',album', 'album')
                WHERE in_album=1
                """
            )
            counts["album_members"] = cur.rowcount

        if settings.exclude_videos:
            cur = conn.execute(
                """
                UPDATE photos SET excluded=1,
                    exclude_reason=COALESCE(exclude_reason || ',video', 'video')
                WHERE is_video=1
                """
            )
            counts["videos"] = cur.rowcount

        if settings.exclude_live_photos:
            cur = conn.execute(
                """
                UPDATE photos SET excluded=1,
                    exclude_reason=COALESCE(exclude_reason || ',live_photo', 'live_photo')
                WHERE is_live_photo=1
                """
            )
            counts["live_photos"] = cur.rowcount

        counts["total_excluded"] = conn.execute(
            "SELECT COUNT(*) AS c FROM photos WHERE excluded=1"
        ).fetchone()["c"]

        # Drop group membership for excluded photos and shrink/remove groups
        excluded_keys = [
            r["media_key"]
            for r in conn.execute("SELECT media_key FROM photos WHERE excluded=1").fetchall()
        ]
        if excluded_keys:
            conn.executemany(
                "DELETE FROM group_members WHERE media_key=?",
                [(k,) for k in excluded_keys],
            )
            # Remove groups that shrank below 2
            small = conn.execute(
                """
                SELECT group_id FROM groups g
                WHERE (SELECT COUNT(*) FROM group_members gm WHERE gm.group_id=g.group_id) < 2
                """
            ).fetchall()
            for row in small:
                conn.execute("DELETE FROM group_members WHERE group_id=?", (row["group_id"],))
                conn.execute("DELETE FROM groups WHERE group_id=?", (row["group_id"],))
                conn.execute("DELETE FROM decisions WHERE group_id=?", (row["group_id"],))

            # Fix proposed keepers that were excluded
            conn.execute(
                """
                UPDATE groups SET proposed_keeper=NULL
                WHERE proposed_keeper IN (SELECT media_key FROM photos WHERE excluded=1)
                """
            )
            # Recount sizes
            for row in conn.execute("SELECT group_id FROM groups").fetchall():
                size = conn.execute(
                    "SELECT COUNT(*) AS c FROM group_members WHERE group_id=?",
                    (row["group_id"],),
                ).fetchone()["c"]
                conn.execute("UPDATE groups SET size=? WHERE group_id=?", (size, row["group_id"]))

    table = Table(title="Safety exclusions")
    table.add_column("Rule")
    table.add_column("Count", justify="right")
    for k, v in counts.items():
        table.add_row(k, str(v))
    console.print(table)
    return counts


def dry_run_report(settings: Settings | None = None) -> dict:
    """Summarise what would be trashed if all accepted decisions were applied."""
    settings = settings or get_settings()
    with get_db(settings.db_path) as conn:
        pending = conn.execute(
            "SELECT COUNT(*) AS c FROM groups WHERE status='pending'"
        ).fetchone()["c"]
        accepted = conn.execute(
            "SELECT COUNT(*) AS c FROM groups WHERE status IN ('accepted','overridden')"
        ).fetchone()["c"]
        decisions = conn.execute(
            "SELECT trash_dedup_keys, keep_media_key FROM decisions WHERE applied=0"
        ).fetchall()

        import json

        trash_keys: list[str] = []
        keep_keys: list[str] = []
        for d in decisions:
            trash_keys.extend(json.loads(d["trash_dedup_keys"]))
            keep_keys.append(d["keep_media_key"])

        # Space estimate from photos table
        space = 0
        if trash_keys:
            # Map dedup_key -> space_taken
            placeholders = ",".join("?" * len(trash_keys))
            rows = conn.execute(
                f"SELECT space_taken FROM photos WHERE dedup_key IN ({placeholders})",
                trash_keys,
            ).fetchall()
            space = sum(r["space_taken"] or 0 for r in rows)

        # Safety check: never trash favorites
        fav_hits = 0
        if trash_keys:
            placeholders = ",".join("?" * len(trash_keys))
            fav_hits = conn.execute(
                f"""
                SELECT COUNT(*) AS c FROM photos
                WHERE dedup_key IN ({placeholders}) AND is_favorite=1
                """,
                trash_keys,
            ).fetchone()["c"]

    report = {
        "groups_pending_review": pending,
        "groups_decided": accepted,
        "items_to_trash": len(trash_keys),
        "items_to_keep": len(keep_keys),
        "estimated_bytes": space,
        "estimated_gb": round(space / (1024**3), 3) if space else 0,
        "favorite_conflicts": fav_hits,
        "safe_to_apply": fav_hits == 0 and len(trash_keys) > 0,
    }
    table = Table(title="Dry-run apply report")
    table.add_column("Metric")
    table.add_column("Value", justify="right")
    for k, v in report.items():
        table.add_row(k, str(v))
    console.print(table)
    if fav_hits:
        console.print("[red]ABORT: favorites present in trash list. Re-run safety.[/red]")
    return report
