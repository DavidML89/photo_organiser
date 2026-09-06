"""Resumable concurrent thumbnail / preview fetcher."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
from rich.console import Console
from rich.progress import BarColumn, Progress, TaskProgressColumn, TextColumn, TimeRemainingColumn

from photo_organiser.config import Settings, get_settings
from photo_organiser.db import get_db
from photo_organiser.images import describe_file_head, looks_like_image_bytes, looks_like_image_file
from photo_organiser.paths import preview_path, sized_thumb_url, thumb_path

console = Console()

# Googleusercontent often rejects non-browser UAs with an HTML interstitial.
_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)


async def _download_one(
    client: httpx.AsyncClient,
    url: str,
    dest: Path,
    retries: int,
) -> bool:
    if dest.exists() and looks_like_image_file(dest):
        return True
    if dest.exists():
        dest.unlink(missing_ok=True)

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".partial")
    last_err: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            resp = await client.get(url)
            if resp.status_code == 404:
                return False
            resp.raise_for_status()
            content = resp.content
            if not looks_like_image_bytes(content):
                ctype = resp.headers.get("content-type", "?")
                preview = content[:80].decode("utf-8", errors="replace").replace("\n", " ")
                raise ValueError(
                    f"not an image (content-type={ctype}, head={preview[:60]!r})"
                )
            tmp.write_bytes(content)
            tmp.replace(dest)
            return True
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            await asyncio.sleep(min(2**attempt, 16))
    if last_err:
        console.print(f"[yellow]Failed {dest.name}: {last_err}[/yellow]")
    return False


async def fetch_images(
    *,
    kind: str = "thumb",
    only_group_members: bool = False,
    limit: int | None = None,
    verify_urls: int = 0,
    repair: bool = False,
    settings: Settings | None = None,
) -> dict:
    """Fetch 256px thumbs or 1600px previews.

    Args:
        kind: ``thumb`` or ``preview``.
        only_group_members: For previews, only fetch photos that are in a group.
        limit: Optional max items (useful for smoke tests).
        verify_urls: If >0, fetch that many and stop — used to verify no-auth claim.
        repair: If True, clear ``*_cached`` flags when the file is missing or not a
            valid image, delete bad files, then download again.
    """
    settings = settings or get_settings()
    size = settings.thumb_size if kind == "thumb" else settings.preview_size
    root = settings.thumbs_dir if kind == "thumb" else settings.previews_dir
    flag_col = "thumb_cached" if kind == "thumb" else "preview_cached"
    path_fn = thumb_path if kind == "thumb" else preview_path

    if repair:
        repaired = 0
        sample_bad: list[str] = []
        with get_db(settings.db_path) as conn:
            rows = conn.execute(
                f"""
                SELECT media_key FROM photos
                WHERE {flag_col}=1 AND is_video=0
                """
            ).fetchall()
            for r in rows:
                dest = path_fn(r["media_key"], root)
                if looks_like_image_file(dest):
                    continue
                if dest.exists() and len(sample_bad) < 5:
                    sample_bad.append(f"{dest.name}: {describe_file_head(dest)}")
                if dest.exists():
                    dest.unlink(missing_ok=True)
                conn.execute(
                    f"UPDATE photos SET {flag_col}=0 WHERE media_key=?",
                    (r["media_key"],),
                )
                repaired += 1
        console.print(
            f"[cyan]Repair:[/cyan] cleared {repaired} stale {flag_col} flags "
            f"(missing or not a valid image under {root})"
        )
        for line in sample_bad:
            console.print(f"  [dim]{line}[/dim]")

    with get_db(settings.db_path) as conn:
        if only_group_members and kind == "preview":
            rows = conn.execute(
                f"""
                SELECT p.media_key, p.thumb FROM photos p
                JOIN group_members gm ON gm.media_key = p.media_key
                WHERE p.thumb IS NOT NULL AND p.{flag_col}=0 AND p.is_video=0
                """
            ).fetchall()
        else:
            rows = conn.execute(
                f"""
                SELECT media_key, thumb FROM photos
                WHERE thumb IS NOT NULL AND {flag_col}=0 AND is_video=0
                ORDER BY timestamp
                """
            ).fetchall()

    items = [(r["media_key"], r["thumb"]) for r in rows if r["thumb"]]
    if limit is not None:
        items = items[:limit]
    if verify_urls:
        items = items[:verify_urls]

    if not items:
        console.print(f"[green]Nothing to fetch for {kind}.[/green]")
        return {"kind": kind, "requested": 0, "ok": 0, "failed": 0}

    console.print(f"Fetching {len(items)} {kind}s at {size}px → {root}")

    sem = asyncio.Semaphore(settings.fetch_concurrency)
    ok = 0
    failed = 0
    ok_keys: list[str] = []

    async with httpx.AsyncClient(
        timeout=settings.fetch_timeout_s,
        follow_redirects=True,
        headers={"User-Agent": _BROWSER_UA},
    ) as client:

        async def worker(media_key: str, thumb: str) -> None:
            nonlocal ok, failed
            url = sized_thumb_url(thumb, size)
            dest = path_fn(media_key, root)
            async with sem:
                success = await _download_one(client, url, dest, settings.fetch_retries)
            if success:
                ok += 1
                ok_keys.append(media_key)
            else:
                failed += 1

        with Progress(
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            TimeRemainingColumn(),
            console=console,
        ) as progress:
            task = progress.add_task(f"fetch-{kind}", total=len(items))

            async def tracked(media_key: str, thumb: str) -> None:
                await worker(media_key, thumb)
                progress.advance(task)

            await asyncio.gather(*(tracked(mk, th) for mk, th in items))

    if ok_keys:
        with get_db(settings.db_path) as conn:
            for media_key in ok_keys:
                conn.execute(
                    f"UPDATE photos SET {flag_col}=1 WHERE media_key=?",
                    (media_key,),
                )

    if verify_urls:
        sample = items[0] if items else None
        if sample:
            dest = path_fn(sample[0], root)
            ok_img = looks_like_image_file(dest)
            console.print(
                f"[cyan]Verify sample:[/cyan] {sized_thumb_url(sample[1], size)}\n"
                f"  saved={dest.exists()} size={dest.stat().st_size if dest.exists() else 0} "
                f"valid_image={ok_img}"
            )
            if dest.exists() and not ok_img:
                console.print(f"  [yellow]head: {describe_file_head(dest)}[/yellow]")

    result = {"kind": kind, "requested": len(items), "ok": ok, "failed": failed}
    console.print(result)
    return result


def fetch_sync(**kwargs) -> dict:
    return asyncio.run(fetch_images(**kwargs))
