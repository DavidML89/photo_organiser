"""Local review UI for duplicate groups."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from photo_organiser.config import get_settings
from photo_organiser.cookies import load_netscape_cookies
from photo_organiser.corrupt import corrupt_queue_counts, decide_corrupt, next_corrupt_item
from photo_organiser.db import get_db
from photo_organiser.images import looks_like_image_bytes, looks_like_image_file
from photo_organiser.models import GroupMemberView, GroupView, ScoreBreakdown
from photo_organiser.paths import full_path, large_media_url, preview_path, thumb_path


def resolve_keep_decision(
    members: list,
    keep_keys: list[str],
    proposed_keeper: str | None,
) -> dict:
    """Keep every listed member; trash the rest (except favorites/excluded)."""
    member_keys = [m["media_key"] for m in members]
    known = set(member_keys)
    ordered: list[str] = []
    seen: set[str] = set()
    for key in keep_keys:
        if not key or key in seen:
            continue
        if key not in known:
            raise ValueError("keep_media_key is not in this group")
        ordered.append(key)
        seen.add(key)
    if not ordered:
        raise ValueError("no keeper available")

    keep_set = set(ordered)
    primary = proposed_keeper if proposed_keeper in keep_set else ordered[0]
    if keep_set == set(member_keys):
        return {
            "status": "kept_all",
            "keep": primary,
            "trash": [],
            "override_keeper": None,
        }

    trash = [
        m["dedup_key"]
        for m in members
        if m["media_key"] not in keep_set and not m["is_favorite"] and not m["excluded"]
    ]
    status = "accepted" if keep_set == {proposed_keeper} else "overridden"
    return {
        "status": status,
        "keep": primary,
        "trash": trash,
        "override_keeper": primary if status == "overridden" else None,
    }


PACKAGE_ROOT = Path(__file__).resolve().parent.parent.parent
TEMPLATES_DIR = PACKAGE_ROOT / "templates"
STATIC_DIR = PACKAGE_ROOT / "static"

app = FastAPI(title="photo-organiser review")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def _group_view(conn, group_id: str) -> GroupView | None:
    g = conn.execute("SELECT * FROM groups WHERE group_id=?", (group_id,)).fetchone()
    if not g:
        return None
    members_rows = conn.execute(
        """
        SELECT gm.*, p.dedup_key, p.thumb, p.res_width, p.res_height, p.is_favorite
        FROM group_members gm
        JOIN photos p ON p.media_key = gm.media_key
        WHERE gm.group_id=?
        ORDER BY gm.is_proposed_keeper DESC, gm.score DESC
        """,
        (group_id,),
    ).fetchall()
    settings = get_settings()
    members: list[GroupMemberView] = []
    for m in members_rows:
        mk = m["media_key"]
        local_prev = preview_path(mk, settings.previews_dir)
        local_thumb = thumb_path(mk, settings.thumbs_dir)
        members.append(
            GroupMemberView(
                media_key=mk,
                dedup_key=m["dedup_key"],
                thumb_url=m["thumb"],
                local_preview=str(local_prev) if local_prev.exists() else None,
                local_thumb=str(local_thumb) if local_thumb.exists() else None,
                res_width=m["res_width"],
                res_height=m["res_height"],
                is_favorite=bool(m["is_favorite"]),
                score=ScoreBreakdown(
                    sharpness=m["score_sharpness"] or 0,
                    exposure=m["score_exposure"] or 0,
                    eyes=m["score_eyes"] or 0,
                    smile=m["score_smile"] or 0,
                    aesthetic=m["score_aesthetic"] or 0,
                    resolution=m["score_resolution"] or 0,
                    total=m["score"] or 0,
                ),
                is_proposed_keeper=bool(m["is_proposed_keeper"]),
            )
        )
    return GroupView(
        group_id=g["group_id"],
        size=g["size"],
        status=g["status"],
        proposed_keeper=g["proposed_keeper"],
        override_keeper=g["override_keeper"],
        members=members,
    )


def _next_pending(conn, after: str | None = None) -> str | None:
    if after:
        row = conn.execute(
            """
            SELECT group_id FROM groups
            WHERE status='pending' AND group_id > ?
            ORDER BY group_id LIMIT 1
            """,
            (after,),
        ).fetchone()
        if row:
            return row["group_id"]
    row = conn.execute(
        "SELECT group_id FROM groups WHERE status='pending' ORDER BY group_id LIMIT 1"
    ).fetchone()
    return row["group_id"] if row else None


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    settings = get_settings()
    with get_db(settings.db_path) as conn:
        pending = conn.execute(
            "SELECT COUNT(*) AS c FROM groups WHERE status='pending'"
        ).fetchone()["c"]
        decided = conn.execute(
            "SELECT COUNT(*) AS c FROM groups WHERE status!='pending'"
        ).fetchone()["c"]
        gid = _next_pending(conn)
        corrupt = corrupt_queue_counts(conn)
    return templates.TemplateResponse(
        request,
        "review.html",
        {
            "pending": pending,
            "decided": decided,
            "initial_group_id": gid,
            "corrupt_pending": corrupt["pending"],
        },
    )


@app.get("/api/group/next")
async def api_next(after: str | None = None):
    settings = get_settings()
    with get_db(settings.db_path) as conn:
        gid = _next_pending(conn, after)
        if not gid:
            return JSONResponse({"done": True})
        view = _group_view(conn, gid)
    return view.model_dump() if view else JSONResponse({"done": True})


@app.get("/api/group/{group_id}")
async def api_group(group_id: str):
    settings = get_settings()
    with get_db(settings.db_path) as conn:
        view = _group_view(conn, group_id)
    if not view:
        raise HTTPException(404, "group not found")
    return view.model_dump()


async def _ensure_full_cached(media_key: str) -> Path | None:
    """Return a large local image: cache → CDN fetch → preview → thumb."""
    settings = get_settings()
    dest = full_path(media_key, settings.fulls_dir)
    if dest.exists() and looks_like_image_file(dest):
        return dest

    preview = preview_path(media_key, settings.previews_dir)
    thumb = thumb_path(media_key, settings.thumbs_dir)

    with get_db(settings.db_path) as conn:
        row = conn.execute(
            "SELECT thumb FROM photos WHERE media_key=?", (media_key,)
        ).fetchone()
    thumb_url = row["thumb"] if row else None

    cookies_file = settings.cookies_path
    if thumb_url and cookies_file.exists():
        jar = load_netscape_cookies(cookies_file)
        url = large_media_url(thumb_url, settings.full_size)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".partial")
        try:
            async with httpx.AsyncClient(
                cookies=jar,
                follow_redirects=True,
                timeout=60.0,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/122.0.0.0 Safari/537.36"
                    ),
                    "Referer": "https://photos.google.com/",
                },
            ) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                if looks_like_image_bytes(resp.content):
                    tmp.write_bytes(resp.content)
                    tmp.replace(dest)
                    return dest
        except Exception:  # noqa: BLE001 — fall back to smaller local cache
            if tmp.exists():
                tmp.unlink(missing_ok=True)

    if preview.exists() and looks_like_image_file(preview):
        return preview
    if thumb.exists() and looks_like_image_file(thumb):
        return thumb
    return None


@app.get("/media/{media_key}")
async def media(media_key: str, kind: str = "preview"):
    settings = get_settings()
    if kind == "full":
        path = await _ensure_full_cached(media_key)
        if not path:
            raise HTTPException(404, "media not cached")
        return FileResponse(path, media_type="image/jpeg")

    path = (
        preview_path(media_key, settings.previews_dir)
        if kind == "preview"
        else thumb_path(media_key, settings.thumbs_dir)
    )
    if not path.exists() and kind == "preview":
        path = thumb_path(media_key, settings.thumbs_dir)
    if not path.exists():
        raise HTTPException(404, "media not cached")
    return FileResponse(path, media_type="image/jpeg")


@app.get("/corrupt", response_class=HTMLResponse)
async def corrupt_index(request: Request):
    settings = get_settings()
    with get_db(settings.db_path) as conn:
        counts = corrupt_queue_counts(conn)
    return templates.TemplateResponse(
        request,
        "corrupt.html",
        {"pending": counts["pending"], "decided": counts["decided"]},
    )


@app.get("/api/corrupt/next")
async def api_corrupt_next(after: str | None = None):
    settings = get_settings()
    with get_db(settings.db_path) as conn:
        item = next_corrupt_item(conn, after)
        counts = corrupt_queue_counts(conn)
    if not item:
        return JSONResponse({"done": True, **counts})
    return {**item, **counts}


@app.post("/api/corrupt/{media_key}/decide")
async def api_corrupt_decide(media_key: str, request: Request):
    body = await request.json()
    action = body.get("action")
    settings = get_settings()
    with get_db(settings.db_path) as conn:
        try:
            result = decide_corrupt(conn, media_key, action)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(404, "not a flagged photo") from exc
        counts = corrupt_queue_counts(conn)
    return {**result, **counts}


@app.post("/api/group/{group_id}/decide")
async def decide(group_id: str, request: Request):
    body = await request.json()
    action = body.get("action")  # accept | override | skip | keep_all | delete_all
    keep_media_key = body.get("keep_media_key")

    settings = get_settings()
    with get_db(settings.db_path) as conn:
        g = conn.execute("SELECT * FROM groups WHERE group_id=?", (group_id,)).fetchone()
        if not g:
            raise HTTPException(404, "group not found")

        members = conn.execute(
            """
            SELECT gm.media_key, p.dedup_key, p.is_favorite, p.excluded
            FROM group_members gm
            JOIN photos p ON p.media_key = gm.media_key
            WHERE gm.group_id=?
            """,
            (group_id,),
        ).fetchall()

        now = datetime.now(timezone.utc).isoformat()

        def write_decision(
            status: str,
            keep: str,
            trash: list[str],
            *,
            override_keeper: str | None = None,
        ) -> dict:
            conn.execute(
                """
                UPDATE groups SET status=?, override_keeper=?, reviewed_at=?
                WHERE group_id=?
                """,
                (status, override_keeper, now, group_id),
            )
            conn.execute(
                """
                INSERT INTO decisions(group_id, keep_media_key, trash_dedup_keys, decided_at, applied)
                VALUES(?, ?, ?, ?, 0)
                ON CONFLICT(group_id) DO UPDATE SET
                    keep_media_key=excluded.keep_media_key,
                    trash_dedup_keys=excluded.trash_dedup_keys,
                    decided_at=excluded.decided_at,
                    applied=0
                """,
                (group_id, keep, json.dumps(trash), now),
            )
            return {
                "ok": True,
                "status": status,
                "keep": keep or None,
                "trash_count": len(trash),
            }

        if action == "skip":
            # Leave for later — no trash decision written.
            conn.execute(
                "UPDATE groups SET status='skipped', reviewed_at=? WHERE group_id=?",
                (now, group_id),
            )
            return {"ok": True, "status": "skipped", "trash_count": 0}

        if action == "keep_all":
            # False positive / keep every photo — explicit empty trash list.
            keep = g["proposed_keeper"] or (members[0]["media_key"] if members else "")
            return write_decision("kept_all", keep, [])

        if action == "delete_all":
            # Trash every deletable member. Favorites / excluded stay.
            trash = []
            protected: list[str] = []
            for m in members:
                if m["is_favorite"] or m["excluded"]:
                    protected.append(m["media_key"])
                    continue
                trash.append(m["dedup_key"])
            keep = protected[0] if protected else ""
            return write_decision("deleted_all", keep, trash)

        if action not in ("accept", "override"):
            raise HTTPException(400, f"unknown action {action}")

        raw_keys = body.get("keep_media_keys")
        if not isinstance(raw_keys, list) or not raw_keys:
            raw_keys = [keep_media_key] if keep_media_key else []
        if not raw_keys:
            fallback = g["proposed_keeper"] or (members[0]["media_key"] if members else "")
            raw_keys = [fallback] if fallback else []
        try:
            decision = resolve_keep_decision(members, [str(k) for k in raw_keys], g["proposed_keeper"])
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

        return write_decision(
            decision["status"],
            decision["keep"],
            decision["trash"],
            override_keeper=decision["override_keeper"],
        )


def run_server(host: str | None = None, port: int | None = None) -> None:
    import uvicorn

    settings = get_settings()
    host = host or settings.host
    port = port or settings.port
    print(f"Duplicates: http://{host}:{port}/")
    print(f"Corrupt:    http://{host}:{port}/corrupt")
    uvicorn.run(
        "photo_organiser.review:app",
        host=host,
        port=port,
        reload=False,
    )
