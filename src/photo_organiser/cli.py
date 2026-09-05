"""CLI entrypoint for photo-organiser."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer
from rich.console import Console

app = typer.Typer(
    name="photo-organiser",
    help="Local Google Photos duplicate finder and best-shot picker.",
    no_args_is_help=True,
)
console = Console()

census_app = typer.Typer(help="Import / report library census")
fetch_app = typer.Typer(help="Download thumbnails and previews")
apply_app = typer.Typer(help="Export trash / restore payloads")
app.add_typer(census_app, name="census")
app.add_typer(fetch_app, name="fetch")
app.add_typer(apply_app, name="apply")


@app.callback()
def _root() -> None:
    from photo_organiser.config import get_settings

    get_settings()  # ensure dirs exist


@app.command("status")
def status_cmd() -> None:
    """Show pipeline funnel counts and the suggested next step."""
    from photo_organiser.status import status

    status()


@app.command("device")
def device_cmd(
    prefer: Optional[str] = typer.Option(None, help="Force cuda|mps|cpu"),
) -> None:
    """Show which accelerator will be used."""
    from photo_organiser.device import print_device_report

    print_device_report(prefer)


@app.command("init")
def init_cmd() -> None:
    """Create data directories and initialise the SQLite database."""
    from photo_organiser.config import get_settings
    from photo_organiser.db import connect

    settings = get_settings()
    settings.ensure_dirs()
    conn = connect(settings.db_path)
    conn.close()
    console.print(f"[green]Ready.[/green] data_root={settings.data_root}")
    console.print(f"db={settings.db_path}")


@census_app.command("import")
def census_import(
    path: Optional[Path] = typer.Argument(None, help="Path to census.jsonl"),
) -> None:
    """Import census.jsonl produced by browser/census.js."""
    from photo_organiser.census import import_census

    import_census(path)


@census_app.command("report")
def census_report() -> None:
    """Print library statistics from the local database."""
    from photo_organiser.census import report

    report()


@fetch_app.command("thumbs")
def fetch_thumbs(
    limit: Optional[int] = typer.Option(None, help="Max items"),
    verify: int = typer.Option(0, help="Fetch N items then stop (auth check)"),
    repair: bool = typer.Option(
        False,
        "--repair",
        help="Clear thumb_cached when the file is missing on disk, then re-download",
    ),
) -> None:
    """Fetch 256px thumbnails for grouping."""
    from photo_organiser.fetch import fetch_sync

    fetch_sync(kind="thumb", limit=limit, verify_urls=verify, repair=repair)


@fetch_app.command("previews")
def fetch_previews(
    limit: Optional[int] = typer.Option(None, help="Max items"),
    all_photos: bool = typer.Option(
        False, "--all", help="Fetch for all photos, not only group members"
    ),
) -> None:
    """Fetch 1600px previews (group members only by default)."""
    from photo_organiser.fetch import fetch_sync

    fetch_sync(kind="preview", only_group_members=not all_photos, limit=limit)


@app.command("embed")
def embed_cmd(
    batch_size: Optional[int] = typer.Option(None),
    limit: Optional[int] = typer.Option(None),
    device: Optional[str] = typer.Option(None, help="cuda|mps|cpu"),
) -> None:
    """Compute DINOv2 embeddings for cached thumbnails."""
    from photo_organiser.embed import embed

    embed(batch_size=batch_size, limit=limit, device_prefer=device)


@app.command("group")
def group_cmd(
    time_window: int | None = typer.Option(
        None, "--time-window", help="Burst window in seconds (default from settings)"
    ),
    time_cosine: float | None = typer.Option(
        None, "--time-cosine", help="Min cosine for time-window pairs (lower=more groups)"
    ),
    global_cosine: float | None = typer.Option(
        None, "--global-cosine", help="Min cosine for global ANN pairs (lower=more groups)"
    ),
) -> None:
    """Build near-duplicate groups (time window + ANN + Union-Find)."""
    from photo_organiser.group import build_groups

    build_groups(
        time_window_s=time_window,
        time_cosine=time_cosine,
        global_cosine=global_cosine,
    )


@app.command("score")
def score_cmd() -> None:
    """Score group members and propose keepers."""
    from photo_organiser.score import score_groups

    score_groups()


@app.command("safety")
def safety_cmd() -> None:
    """Apply exclusion rules (favorites, videos, live photos, albums)."""
    from photo_organiser.safety import apply_exclusions

    apply_exclusions()


@app.command("dry-run")
def dry_run_cmd() -> None:
    """Report what would be trashed from accepted decisions."""
    from photo_organiser.safety import dry_run_report

    dry_run_report()


@app.command("review")
def review_cmd(
    host: Optional[str] = typer.Option(None),
    port: Optional[int] = typer.Option(None),
) -> None:
    """Launch the local review UI."""
    from photo_organiser.review import run_server

    run_server(host=host, port=port)


@apply_app.command("export")
def apply_export() -> None:
    """Write decisions_to_trash.json for browser/apply.js."""
    from photo_organiser.apply import export_trash_payload

    export_trash_payload()


@apply_app.command("import-undo")
def apply_import_undo(
    path: Path = typer.Argument(..., help="undo_*.json from apply.js"),
) -> None:
    """Import the undo log produced after trashing."""
    from photo_organiser.apply import import_undo_log

    import_undo_log(path)


@apply_app.command("export-restore")
def apply_export_restore(
    run_id: Optional[str] = typer.Option(None),
) -> None:
    """Write restore_*.json for browser/restore.js."""
    from photo_organiser.apply import export_restore_payload

    export_restore_payload(run_id)


@apply_app.command("mark-restored")
def apply_mark_restored(
    run_id: str = typer.Argument(...),
) -> None:
    """Mark a run as restored in the local undo log."""
    from photo_organiser.apply import mark_restored

    n = mark_restored(run_id)
    console.print(f"Marked {n} rows restored for {run_id}")


@app.command("pipeline")
def pipeline_cmd(
    skip_embed: bool = typer.Option(False, help="Skip embedding (already done)"),
) -> None:
    """Run safety → embed → group → fetch previews → score (no review/apply)."""
    from photo_organiser.embed import embed
    from photo_organiser.fetch import fetch_sync
    from photo_organiser.group import build_groups
    from photo_organiser.safety import apply_exclusions
    from photo_organiser.score import score_groups

    from photo_organiser.db import get_db
    from photo_organiser.config import get_settings

    apply_exclusions()
    if not skip_embed:
        embed()
    group_stats = build_groups()
    if not group_stats.get("groups"):
        console.print(
            "[yellow]Pipeline stopped: 0 duplicate groups found. "
            "Scoring/review need groups. Try lowering thresholds or "
            "confirm `embed` covered your library.[/yellow]"
        )
        return
    fetch_sync(kind="preview", only_group_members=True)
    score_groups()
    settings = get_settings()
    with get_db(settings.db_path) as conn:
        n = conn.execute("SELECT COUNT(*) AS c FROM groups").fetchone()["c"]
    console.print(
        f"[green]Pipeline done ({n} groups). Next: "
        "uv run photo-organiser review[/green]"
    )


def main() -> None:
    app()


if __name__ == "__main__":
    main()
