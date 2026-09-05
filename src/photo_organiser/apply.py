"""Export apply payloads and record undo logs locally."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console

from photo_organiser.config import Settings, get_settings
from photo_organiser.db import get_db
from photo_organiser.safety import dry_run_report

console = Console()


def export_trash_payload(settings: Settings | None = None) -> Path:
    """Write decisions_to_trash.json for browser/apply.js."""
    settings = settings or get_settings()
    report = dry_run_report(settings)
    if report.get("favorite_conflicts", 0) > 0:
        raise RuntimeError("Refusing to export: favorites would be trashed.")

    run_id = f"run-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:6]}"
    items: list[dict] = []

    with get_db(settings.db_path) as conn:
        rows = conn.execute(
            """
            SELECT d.group_id, d.keep_media_key, d.trash_dedup_keys
            FROM decisions d
            WHERE d.applied=0
            """
        ).fetchall()
        for row in rows:
            for dedup_key in json.loads(row["trash_dedup_keys"]):
                photo = conn.execute(
                    "SELECT media_key, is_favorite, excluded FROM photos WHERE dedup_key=?",
                    (dedup_key,),
                ).fetchone()
                if not photo:
                    continue
                if photo["is_favorite"] or photo["excluded"]:
                    continue
                items.append(
                    {
                        "media_key": photo["media_key"],
                        "dedup_key": dedup_key,
                        "group_id": row["group_id"],
                    }
                )

    payload = {
        "run_id": run_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "item_count": len(items),
        "items": items,
    }
    out = settings.data_root / "decisions_to_trash.json"
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    console.print(f"[green]Wrote {out} ({len(items)} items, run {run_id})[/green]")
    console.print(
        "Next: open photos.google.com with GPTK, set "
        "window.__GPDEDUPE_TRASH__ = <file contents>, paste browser/apply.js"
    )
    return out


def import_undo_log(undo_path: Path, settings: Settings | None = None) -> dict:
    """Ingest undo_*.json produced by browser/apply.js and mark decisions applied."""
    settings = settings or get_settings()
    data = json.loads(undo_path.read_text(encoding="utf-8"))
    run_id = data["run_id"]
    items = data.get("items") or []

    settings.undo_dir.mkdir(parents=True, exist_ok=True)
    archived = settings.undo_dir / f"{run_id}.json"
    archived.write_text(json.dumps(data, indent=2), encoding="utf-8")

    with get_db(settings.db_path) as conn:
        for item in items:
            conn.execute(
                """
                INSERT INTO undo_log(run_id, dedup_key, media_key, trashed_at, restored)
                VALUES(?, ?, ?, ?, 0)
                """,
                (
                    run_id,
                    item["dedup_key"],
                    item.get("media_key"),
                    item.get("trashed_at") or datetime.now(timezone.utc).isoformat(),
                ),
            )
        # Mark decisions that are fully applied
        conn.execute(
            """
            UPDATE decisions SET applied=1, applied_at=?
            WHERE applied=0
            """,
            (datetime.now(timezone.utc).isoformat(),),
        )

    result = {"run_id": run_id, "imported": len(items), "archived": str(archived)}
    console.print(result)
    return result


def export_restore_payload(run_id: str | None = None, settings: Settings | None = None) -> Path:
    """Write restore payload for browser/restore.js from the undo log."""
    settings = settings or get_settings()
    with get_db(settings.db_path) as conn:
        if run_id:
            rows = conn.execute(
                "SELECT * FROM undo_log WHERE run_id=? AND restored=0", (run_id,)
            ).fetchall()
        else:
            row = conn.execute(
                "SELECT run_id FROM undo_log WHERE restored=0 ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if not row:
                raise RuntimeError("No unrestored undo entries found.")
            run_id = row["run_id"]
            rows = conn.execute(
                "SELECT * FROM undo_log WHERE run_id=? AND restored=0", (run_id,)
            ).fetchall()

    payload = {
        "run_id": run_id,
        "items": [
            {
                "media_key": r["media_key"],
                "dedup_key": r["dedup_key"],
                "trashed_at": r["trashed_at"],
            }
            for r in rows
        ],
    }
    out = settings.data_root / f"restore_{run_id}.json"
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    console.print(f"[green]Wrote {out} ({len(payload['items'])} items)[/green]")
    console.print(
        "Next: set window.__GPDEDUPE_UNDO__ = <file contents>, paste browser/restore.js"
    )
    return out


def mark_restored(run_id: str, settings: Settings | None = None) -> int:
    settings = settings or get_settings()
    with get_db(settings.db_path) as conn:
        cur = conn.execute("UPDATE undo_log SET restored=1 WHERE run_id=?", (run_id,))
        return cur.rowcount
