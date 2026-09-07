"""Detect recovery-truncated photos and optionally sort them out of a folder."""

from __future__ import annotations

import io
import json
import shutil
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
from PIL import Image, ImageFile
from rich.console import Console
from rich.progress import BarColumn, Progress, TaskProgressColumn, TextColumn, TimeRemainingColumn

from photo_organiser.config import Settings, get_settings
from photo_organiser.db import get_db
from photo_organiser.images import looks_like_image_bytes
from photo_organiser.paths import thumb_path

console = Console()

IMAGE_SUFFIXES = {
    ".bmp",
    ".jfif",
    ".jpe",
    ".jpeg",
    ".jpg",
    ".png",
    ".tif",
    ".tiff",
    ".webp",
}

# libjpeg / Pillow paint this when the entropy-coded scan ends early.
_DECODER_GRAY = np.array([128, 128, 128], dtype=np.int16)
_ROW_STD_MAX = 1.5
_DECODER_FILL_MIN = 0.08
_ANALYZE_MAX_SIDE = 512


@dataclass
class Verdict:
    path: str
    corrupted: bool
    reasons: list[str] = field(default_factory=list)
    fill_fraction: float = 0.0
    fill_color: tuple[int, int, int] | None = None
    width: int | None = None
    height: int | None = None
    error: str | None = None


def _trailing_uniform_rows(rgb: np.ndarray) -> tuple[float, tuple[int, int, int]]:
    """Fraction of rows from the bottom that are almost a single colour."""
    height = rgb.shape[0]
    row_std = rgb.reshape(height, -1).astype(np.float32).std(axis=1)
    run = 0
    for std in row_std[::-1]:
        if std < _ROW_STD_MAX:
            run += 1
        else:
            break
    if run == 0:
        return 0.0, (0, 0, 0)
    mean = rgb[-run:].mean(axis=(0, 1))
    color = tuple(int(c) for c in np.rint(mean).tolist())
    return run / height, color


def _is_decoder_gray(color: tuple[int, int, int]) -> bool:
    return int(np.abs(_DECODER_GRAY - np.array(color)).max()) <= 2


def _analysis_array(image: Image.Image) -> np.ndarray:
    rgb = image.convert("RGB")
    if max(rgb.size) > _ANALYZE_MAX_SIDE:
        rgb = rgb.copy()
        rgb.thumbnail((_ANALYZE_MAX_SIDE, _ANALYZE_MAX_SIDE), Image.Resampling.NEAREST)
    return np.array(rgb)


def _jpeg_missing_eoi(data: bytes) -> bool:
    return data.startswith(b"\xff\xd8") and data.rfind(b"\xff\xd9") < 0


def _png_missing_iend(data: bytes) -> bool:
    return data.startswith(b"\x89PNG") and b"IEND" not in data[-64:]


def inspect_file(path: Path, min_fill: float = 0.12) -> Verdict:
    """Classify one image. ``min_fill`` is the generic trailing-fill threshold."""
    try:
        data = path.read_bytes()
    except OSError as exc:
        return Verdict(path=str(path), corrupted=True, reasons=["unreadable"], error=str(exc))

    if len(data) < 12:
        return Verdict(path=str(path), corrupted=True, reasons=["empty"])

    reasons: list[str] = []
    if not looks_like_image_bytes(data):
        return Verdict(path=str(path), corrupted=True, reasons=["not_an_image"])
    if _jpeg_missing_eoi(data):
        reasons.append("truncated_jpeg")
    if _png_missing_iend(data):
        reasons.append("truncated_png")

    previous = ImageFile.LOAD_TRUNCATED_IMAGES
    ImageFile.LOAD_TRUNCATED_IMAGES = True
    try:
        with Image.open(io.BytesIO(data)) as im:
            im.load()
            width, height = im.size
            arr = _analysis_array(im)
    except Exception as exc:  # noqa: BLE001 — decode can fail many ways
        reasons.append("unreadable")
        return Verdict(
            path=str(path),
            corrupted=True,
            reasons=reasons,
            error=f"{type(exc).__name__}: {exc}",
        )
    finally:
        ImageFile.LOAD_TRUNCATED_IMAGES = previous

    fill_fraction, fill_color = _trailing_uniform_rows(arr)
    if fill_fraction >= _DECODER_FILL_MIN and _is_decoder_gray(fill_color):
        reasons.append("decoder_fill")
    elif fill_fraction >= min_fill:
        reasons.append("trailing_fill")

    return Verdict(
        path=str(path),
        corrupted=bool(reasons),
        reasons=reasons,
        fill_fraction=round(fill_fraction, 4),
        fill_color=fill_color if fill_fraction > 0 else None,
        width=width,
        height=height,
    )


def iter_image_files(root: Path, suffixes: set[str] | None = None):
    suffixes = suffixes or IMAGE_SUFFIXES
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.suffix.lower() in suffixes:
            yield path


def _unique_dest(dest: Path) -> Path:
    if not dest.exists():
        return dest
    stem, suffix, parent = dest.stem, dest.suffix, dest.parent
    n = 1
    while True:
        candidate = parent / f"{stem}_{n}{suffix}"
        if not candidate.exists():
            return candidate
        n += 1


def _relocate(src: Path, dest_root: Path, scan_root: Path, copy: bool) -> Path:
    relative = src.relative_to(scan_root)
    dest = _unique_dest(dest_root / relative)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if copy:
        shutil.copy2(src, dest)
    else:
        shutil.move(str(src), str(dest))
    return dest


def scan_tree(
    root: Path,
    *,
    move_to: Path | None = None,
    copy_to: Path | None = None,
    report: Path | None = None,
    min_fill: float = 0.12,
    limit: int | None = None,
) -> dict:
    if move_to is not None and copy_to is not None:
        raise ValueError("Use only one of move_to or copy_to")

    files = list(iter_image_files(root))
    if limit is not None:
        files = files[:limit]

    dest_root = copy_to or move_to
    copy = copy_to is not None
    report_fp = report.open("w", encoding="utf-8") if report else None

    stats = {"scanned": 0, "corrupt": 0, "ok": 0, "moved": 0, "copied": 0}
    try:
        with Progress(
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            TimeRemainingColumn(),
            console=console,
        ) as progress:
            task = progress.add_task("corrupt-scan", total=len(files))
            for path in files:
                if dest_root is not None:
                    try:
                        path.relative_to(dest_root.resolve())
                        progress.advance(task)
                        continue
                    except ValueError:
                        pass

                verdict = inspect_file(path, min_fill=min_fill)
                stats["scanned"] += 1
                if verdict.corrupted:
                    stats["corrupt"] += 1
                    if dest_root is not None:
                        relocated = _relocate(path, dest_root, root, copy=copy)
                        verdict.path = str(relocated)
                        if copy:
                            stats["copied"] += 1
                        else:
                            stats["moved"] += 1
                else:
                    stats["ok"] += 1

                if report_fp is not None:
                    report_fp.write(json.dumps(asdict(verdict)) + "\n")
                progress.advance(task)
    finally:
        if report_fp is not None:
            report_fp.close()

    console.print(stats)
    return stats


def scan_thumbs(
    settings: Settings | None = None,
    *,
    report: Path | None = None,
    min_fill: float = 0.12,
    limit: int | None = None,
) -> dict:
    """Inspect cached Google Photos thumbs and record hits in ``corrupt_flags``."""
    settings = settings or get_settings()
    report = report or (settings.data_root / "corrupt_thumbs.jsonl")
    scanned_at = datetime.now(UTC).isoformat()
    stats = {"scanned": 0, "corrupt": 0, "ok": 0, "missing": 0}

    with get_db(settings.db_path) as conn:
        rows = conn.execute(
            """
            SELECT media_key, file_name FROM photos
            WHERE thumb_cached=1 AND is_video=0
            ORDER BY timestamp
            """
        ).fetchall()
        if limit is not None:
            rows = rows[:limit]

        if not rows:
            console.print(
                "[yellow]No cached thumbs. If these photos are in Google Photos, "
                "run census import then `photo-organiser fetch thumbs`. "
                "If they only live in a Drive folder on disk, use "
                "`photo-organiser corrupt scan /path/to/folder` instead. "
                "Embedding `.npy` files are vectors, not images — skip them.[/yellow]"
            )
            return stats

        upsert = """
            INSERT INTO corrupt_flags(
                media_key, corrupted, reasons, fill_fraction, fill_color,
                scanned_at, review_status
            ) VALUES(?, ?, ?, ?, ?, ?, 'pending')
            ON CONFLICT(media_key) DO UPDATE SET
                corrupted=excluded.corrupted,
                reasons=excluded.reasons,
                fill_fraction=excluded.fill_fraction,
                fill_color=excluded.fill_color,
                scanned_at=excluded.scanned_at,
                review_status=CASE
                    WHEN excluded.corrupted=1
                         AND corrupt_flags.review_status IN ('kept','trash','skipped')
                    THEN corrupt_flags.review_status
                    ELSE 'pending'
                END
        """
        with (
            report.open("w", encoding="utf-8") as report_fp,
            Progress(
                TextColumn("[progress.description]{task.description}"),
                BarColumn(),
                TaskProgressColumn(),
                TimeRemainingColumn(),
                console=console,
            ) as progress,
        ):
            task = progress.add_task("corrupt-thumbs", total=len(rows))
            for row in rows:
                mk = row["media_key"]
                path = thumb_path(mk, settings.thumbs_dir)
                if not path.exists():
                    stats["missing"] += 1
                    progress.advance(task)
                    continue

                verdict = inspect_file(path, min_fill=min_fill)
                stats["scanned"] += 1
                if verdict.corrupted:
                    stats["corrupt"] += 1
                else:
                    stats["ok"] += 1

                record = asdict(verdict)
                record["media_key"] = mk
                record["file_name"] = row["file_name"]
                report_fp.write(json.dumps(record) + "\n")

                fill_color = (
                    ",".join(str(c) for c in verdict.fill_color)
                    if verdict.fill_color
                    else None
                )
                conn.execute(
                    upsert,
                    (
                        mk,
                        1 if verdict.corrupted else 0,
                        json.dumps(verdict.reasons),
                        verdict.fill_fraction,
                        fill_color,
                        scanned_at,
                    ),
                )
                progress.advance(task)

    console.print(stats)
    console.print(f"Report: {report}")
    return stats


def corrupt_queue_counts(conn) -> dict:
    pending = conn.execute(
        """
        SELECT COUNT(*) AS c FROM corrupt_flags
        WHERE corrupted=1 AND COALESCE(review_status, 'pending')='pending'
        """
    ).fetchone()["c"]
    decided = conn.execute(
        """
        SELECT COUNT(*) AS c FROM corrupt_flags
        WHERE corrupted=1 AND COALESCE(review_status, 'pending')!='pending'
        """
    ).fetchone()["c"]
    return {"pending": pending, "decided": decided}


def next_corrupt_item(conn, after: str | None = None) -> dict | None:
    if after:
        row = conn.execute(
            """
            SELECT cf.media_key, cf.reasons, cf.fill_fraction, cf.fill_color,
                   p.file_name, p.res_width, p.res_height, p.is_favorite, p.dedup_key
            FROM corrupt_flags cf
            JOIN photos p ON p.media_key = cf.media_key
            WHERE cf.corrupted=1 AND COALESCE(cf.review_status, 'pending')='pending'
              AND cf.media_key > ?
            ORDER BY cf.media_key
            LIMIT 1
            """,
            (after,),
        ).fetchone()
        if row:
            return _corrupt_item(row)
    row = conn.execute(
        """
        SELECT cf.media_key, cf.reasons, cf.fill_fraction, cf.fill_color,
               p.file_name, p.res_width, p.res_height, p.is_favorite, p.dedup_key
        FROM corrupt_flags cf
        JOIN photos p ON p.media_key = cf.media_key
        WHERE cf.corrupted=1 AND COALESCE(cf.review_status, 'pending')='pending'
        ORDER BY cf.media_key
        LIMIT 1
        """
    ).fetchone()
    return _corrupt_item(row) if row else None


def _corrupt_item(row) -> dict:
    try:
        reasons = json.loads(row["reasons"] or "[]")
    except json.JSONDecodeError:
        reasons = [row["reasons"]] if row["reasons"] else []
    return {
        "media_key": row["media_key"],
        "dedup_key": row["dedup_key"],
        "file_name": row["file_name"],
        "reasons": reasons,
        "fill_fraction": row["fill_fraction"] or 0.0,
        "fill_color": row["fill_color"],
        "res_width": row["res_width"],
        "res_height": row["res_height"],
        "is_favorite": bool(row["is_favorite"]),
    }


def decide_corrupt(conn, media_key: str, action: str) -> dict:
    status = {"keep": "kept", "trash": "trash", "skip": "skipped"}.get(action)
    if status is None:
        raise ValueError(f"unknown action {action}")
    row = conn.execute(
        "SELECT media_key FROM corrupt_flags WHERE media_key=? AND corrupted=1",
        (media_key,),
    ).fetchone()
    if not row:
        raise KeyError(media_key)
    conn.execute(
        "UPDATE corrupt_flags SET review_status=? WHERE media_key=?",
        (status, media_key),
    )
    return {"ok": True, "status": status, "media_key": media_key}
