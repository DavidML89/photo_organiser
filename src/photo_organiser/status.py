"""Pipeline status / funnel diagnostics."""

from __future__ import annotations

from rich.console import Console
from rich.table import Table

from photo_organiser.config import Settings, get_settings
from photo_organiser.db import get_db
from photo_organiser.images import looks_like_image_file
from photo_organiser.paths import thumb_path

console = Console()


def _sample_thumbs_on_disk(conn, thumbs_dir, sample_size: int = 50) -> tuple[int, int]:
    """Return (valid_images, sampled) for thumb_cached rows (magic-byte check)."""
    rows = conn.execute(
        """
        SELECT media_key FROM photos
        WHERE thumb_cached=1 AND is_video=0
        LIMIT ?
        """,
        (sample_size,),
    ).fetchall()
    if not rows:
        return 0, 0
    present = 0
    for r in rows:
        path = thumb_path(r["media_key"], thumbs_dir)
        if looks_like_image_file(path):
            present += 1
    return present, len(rows)


def status(settings: Settings | None = None) -> dict:
    """Print how many items sit at each pipeline stage."""
    settings = settings or get_settings()
    with get_db(settings.db_path) as conn:
        def count(sql: str) -> int:
            return conn.execute(sql).fetchone()["c"]

        on_disk, sampled = _sample_thumbs_on_disk(conn, settings.thumbs_dir)
        stats = {
            "photos_total": count("SELECT COUNT(*) AS c FROM photos"),
            "videos": count("SELECT COUNT(*) AS c FROM photos WHERE is_video=1"),
            "excluded": count("SELECT COUNT(*) AS c FROM photos WHERE excluded=1"),
            "excluded_favorite": count(
                "SELECT COUNT(*) AS c FROM photos WHERE excluded=1 AND exclude_reason LIKE '%favorite%'"
            ),
            "excluded_album": count(
                "SELECT COUNT(*) AS c FROM photos WHERE excluded=1 AND exclude_reason LIKE '%album%'"
            ),
            "with_thumb_url": count(
                "SELECT COUNT(*) AS c FROM photos WHERE thumb IS NOT NULL AND is_video=0"
            ),
            "thumbs_cached": count(
                "SELECT COUNT(*) AS c FROM photos WHERE thumb_cached=1 AND is_video=0"
            ),
            "thumbs_on_disk_sample": f"{on_disk}/{sampled}",
            "ready_to_embed": count(
                """
                SELECT COUNT(*) AS c FROM photos
                WHERE thumb_cached=1 AND embedded=0 AND is_video=0 AND excluded=0
                """
            ),
            "embedded_flag": count(
                "SELECT COUNT(*) AS c FROM photos WHERE embedded=1 AND is_video=0 AND excluded=0"
            ),
            "embeddings_rows": count("SELECT COUNT(*) AS c FROM embeddings"),
            "usable_for_group": count(
                """
                SELECT COUNT(*) AS c FROM embeddings e
                JOIN photos p ON p.media_key = e.media_key
                WHERE p.excluded=0 AND p.is_video=0
                """
            ),
            "groups": count("SELECT COUNT(*) AS c FROM groups"),
            "corrupt_thumbs": count(
                "SELECT COUNT(*) AS c FROM corrupt_flags WHERE corrupted=1"
            ),
        }

    table = Table(title="Pipeline status")
    table.add_column("Stage")
    table.add_column("Count", justify="right")
    for k, v in stats.items():
        table.add_row(k, str(v))
    console.print(table)

    thumbs_missing = sampled > 0 and on_disk < sampled // 2

    # Actionable next step
    if stats["photos_total"] == 0:
        console.print("[yellow]Next: import census.jsonl → `photo-organiser census import`[/yellow]")
    elif thumbs_missing:
        console.print(
            f"[red]DB says thumbs_cached={stats['thumbs_cached']} but only "
            f"{on_disk}/{sampled} sampled files are valid images under "
            f"{settings.thumbs_dir} (missing or HTML/corrupt).[/red]\n"
            "[yellow]Next: re-download → "
            "`uv run photo-organiser fetch thumbs --repair`[/yellow]"
        )
    elif stats["thumbs_cached"] < 2:
        console.print(
            "[yellow]Next: download thumbs → `uv run photo-organiser fetch thumbs`[/yellow]"
        )
    elif stats["usable_for_group"] < 2:
        if stats["ready_to_embed"] > 0:
            console.print(
                f"[yellow]Next: embed {stats['ready_to_embed']} thumbs → "
                "`uv run photo-organiser embed`[/yellow]"
            )
        elif stats["excluded"] and stats["thumbs_cached"]:
            console.print(
                "[yellow]Thumbs exist but almost everything is excluded "
                f"({stats['excluded']} excluded, album={stats['excluded_album']}). "
                "Re-run safety without album exclusion, or:\n"
                "  GPDEDUPE_EXCLUDE_ALBUM_MEMBERS=false uv run photo-organiser safety\n"
                "  uv run photo-organiser embed[/yellow]"
            )
        else:
            console.print(
                "[yellow]Next: embed → `uv run photo-organiser embed`[/yellow]"
            )
    elif stats["groups"] == 0:
        console.print(
            "[yellow]Next: group → `uv run photo-organiser group`[/yellow]"
        )
    else:
        console.print(
            f"[green]Ready to score/review ({stats['groups']} groups).[/green]"
        )
    return stats
