"""Resumable concurrent thumbnail / preview fetcher."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
from rich.console import Console
from rich.progress import BarColumn, Progress, TaskProgressColumn, TextColumn, TimeRemainingColumn

from photo_organiser.config import Settings, get_settings
from photo_organiser.cookies import cookies_look_usable, load_netscape_cookies
from photo_organiser.db import get_db
from photo_organiser.images import describe_file_head, looks_like_image_bytes, looks_like_image_file
from photo_organiser.paths import preview_path, sized_thumb_url, thumb_path

console = Console()

# Googleusercontent often rejects non-browser UAs with an HTML interstitial.
_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)

_AUTH_HELP = """
[red]Google Photos CDN returned HTML/403 — stale thumb URLs cannot be fetched from Python.[/red]

Census thumb tokens expire. Cookies alone are not enough.

[bold]Recommended path (fresh thumbs in the browser):[/bold]
1. Open https://photos.google.com (logged in, GPTK installed)
2. Paste [cyan]browser/fetch_thumbs.js[/cyan] into the DevTools console
3. Wait for [cyan]thumbs_batch_*.zip[/cyan] downloads
4. Import:
     uv run photo-organiser fetch thumbs --import-zips ~/Downloads

Tip: set LIMIT=20 at the top of fetch_thumbs.js for a smoke test first.
""".strip()


async def _download_one(
    client: httpx.AsyncClient,
    url: str,
    dest: Path,
    retries: int,
) -> tuple[bool, str | None]:
    """Return (ok, error_kind) where error_kind is None | '403' | 'other'."""
    if dest.exists() and looks_like_image_file(dest):
        return True, None
    if dest.exists():
        dest.unlink(missing_ok=True)

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".partial")
    last_err: Exception | None = None
    saw_403 = False
    for attempt in range(1, retries + 1):
        try:
            resp = await client.get(url)
            if resp.status_code == 404:
                return False, "other"
            if resp.status_code == 403:
                saw_403 = True
                raise httpx.HTTPStatusError(
                    f"Client error '403 Forbidden' for url '{url}'",
                    request=resp.request,
                    response=resp,
                )
            resp.raise_for_status()
            content = resp.content
            if not looks_like_image_bytes(content):
                ctype = resp.headers.get("content-type", "?")
                preview = content[:80].decode("utf-8", errors="replace").replace("\n", " ")
                head = content[:64].lower()
                if b"<!doctype" in head or b"<html" in head:
                    saw_403 = True
                raise ValueError(
                    f"not an image (content-type={ctype}, head={preview[:60]!r})"
                )
            tmp.write_bytes(content)
            tmp.replace(dest)
            return True, None
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            await asyncio.sleep(min(2**attempt, 16))
    if last_err:
        console.print(f"[yellow]Failed {dest.name}: {last_err}[/yellow]")
    return False, ("403" if saw_403 else "other")


async def fetch_images(
    *,
    kind: str = "thumb",
    only_group_members: bool = False,
    limit: int | None = None,
    verify_urls: int = 0,
    repair: bool = False,
    cookies: Path | None = None,
    settings: Settings | None = None,
) -> dict:
    """Fetch 256px thumbs or 1600px previews.

    Args:
        kind: ``thumb`` or ``preview``.
        only_group_members: For previews, only fetch photos that are in a group.
        limit: Optional max items (useful for smoke tests).
        verify_urls: If >0, fetch that many and stop — used to verify auth.
        repair: If True, clear ``*_cached`` flags when the file is missing or not a
            valid image, delete bad files, then download again.
        cookies: Optional Netscape cookies.txt (Google Photos session).
    """
    settings = settings or get_settings()
    size = settings.thumb_size if kind == "thumb" else settings.preview_size
    root = settings.thumbs_dir if kind == "thumb" else settings.previews_dir
    flag_col = "thumb_cached" if kind == "thumb" else "preview_cached"
    path_fn = thumb_path if kind == "thumb" else preview_path

    cookies_path = Path(cookies) if cookies else settings.cookies_path
    cookie_jar = None
    if cookies_path.is_file():
        cookie_jar = load_netscape_cookies(cookies_path)
        present = cookies_look_usable(cookie_jar)
        console.print(
            f"[cyan]Cookies:[/cyan] {cookies_path} "
            f"({len(list(cookie_jar))} cookies; session markers={present or 'none'})"
        )
        if not present:
            console.print(
                "[yellow]Warning: no SID/SAPISID-style cookies found — "
                "export while logged into photos.google.com.[/yellow]"
            )
    else:
        console.print(
            f"[yellow]No cookies file at {cookies_path}. "
            "Google Photos CDN usually returns 403 without a session — "
            "pass --cookies PATH after exporting cookies.txt.[/yellow]"
        )

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
    auth_fails = 0
    ok_keys: list[str] = []
    stop = False

    client_kwargs: dict = {
        "timeout": settings.fetch_timeout_s,
        "follow_redirects": True,
        "headers": {
            "User-Agent": _BROWSER_UA,
            "Referer": "https://photos.google.com/",
            "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
        },
    }
    if cookie_jar is not None:
        client_kwargs["cookies"] = cookie_jar

    async with httpx.AsyncClient(**client_kwargs) as client:

        async def worker(media_key: str, thumb: str) -> None:
            nonlocal ok, failed, auth_fails, stop
            if stop:
                failed += 1
                return
            url = sized_thumb_url(thumb, size)
            dest = path_fn(media_key, root)
            async with sem:
                if stop:
                    failed += 1
                    return
                success, err_kind = await _download_one(
                    client, url, dest, settings.fetch_retries
                )
            if success:
                ok += 1
                ok_keys.append(media_key)
                auth_fails = 0
            else:
                failed += 1
                if err_kind == "403":
                    auth_fails += 1
                    if auth_fails >= 8 and ok == 0:
                        stop = True

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

            chunk = max(settings.fetch_concurrency * 4, 64)
            for i in range(0, len(items), chunk):
                if stop:
                    remaining = len(items) - i
                    failed += remaining
                    progress.advance(task, remaining)
                    break
                batch = items[i : i + chunk]
                await asyncio.gather(*(tracked(mk, th) for mk, th in batch))

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
    if stop or (ok == 0 and failed and auth_fails):
        console.print(_AUTH_HELP)
    return result


def fetch_sync(**kwargs) -> dict:
    return asyncio.run(fetch_images(**kwargs))


def import_thumb_zips(
    zips_dir: Path,
    *,
    settings: Settings | None = None,
) -> dict:
    """Import thumbs_batch_*.zip from the browser script into the local cache."""
    import zipfile

    settings = settings or get_settings()
    root = settings.thumbs_dir
    root.mkdir(parents=True, exist_ok=True)

    paths = sorted(zips_dir.expanduser().resolve().glob("thumbs_batch_*.zip"))
    if not paths:
        # Also accept any *.zip in the directory
        paths = sorted(zips_dir.expanduser().resolve().glob("*.zip"))
    if not paths:
        console.print(f"[red]No zip files found in {zips_dir}[/red]")
        return {"zips": 0, "ok": 0, "skipped": 0, "unknown": 0}

    ok = 0
    skipped = 0
    unknown = 0
    ok_keys: list[str] = []

    console.print(f"Importing {len(paths)} zip(s) from {zips_dir} → {root}")

    with get_db(settings.db_path) as conn:
        known = {
            r["media_key"]
            for r in conn.execute("SELECT media_key FROM photos WHERE is_video=0").fetchall()
        }

    for zp in paths:
        console.print(f"  [dim]{zp.name}[/dim]")
        with zipfile.ZipFile(zp, "r") as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                name = Path(info.filename).name
                if not name.lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
                    continue
                media_key = name.rsplit(".", 1)[0]
                if media_key not in known:
                    unknown += 1
                    continue
                dest = thumb_path(media_key, root)
                if dest.exists() and looks_like_image_file(dest):
                    skipped += 1
                    continue
                data = zf.read(info)
                if not looks_like_image_bytes(data):
                    unknown += 1
                    continue
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(data)
                ok += 1
                ok_keys.append(media_key)

    if ok_keys:
        with get_db(settings.db_path) as conn:
            for media_key in ok_keys:
                conn.execute(
                    "UPDATE photos SET thumb_cached=1 WHERE media_key=?",
                    (media_key,),
                )

    result = {
        "zips": len(paths),
        "ok": ok,
        "skipped": skipped,
        "unknown_or_bad": unknown,
    }
    console.print(result)
    if ok:
        console.print(
            f"[green]Imported {ok} thumbs.[/green] Next: "
            "`uv run photo-organiser status` then `uv run photo-organiser embed`"
        )
    return result


def scan_thumbs_dir(*, settings: Settings | None = None) -> dict:
    """Mark thumb_cached=1 for DB rows whose hashed thumb file is a valid image."""
    settings = settings or get_settings()
    root = settings.thumbs_dir
    marked = 0
    missing = 0
    with get_db(settings.db_path) as conn:
        rows = conn.execute(
            "SELECT media_key FROM photos WHERE is_video=0 AND thumb_cached=0"
        ).fetchall()
        for r in rows:
            dest = thumb_path(r["media_key"], root)
            if looks_like_image_file(dest):
                conn.execute(
                    "UPDATE photos SET thumb_cached=1 WHERE media_key=?",
                    (r["media_key"],),
                )
                marked += 1
            else:
                missing += 1
    result = {"marked": marked, "still_missing": missing}
    console.print(result)
    return result
