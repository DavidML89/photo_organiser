"""Detect photos of books, notes, and other text-heavy pages."""

from __future__ import annotations

import io
import json
import shutil
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import cv2
import numpy as np
from PIL import Image
from rich.console import Console
from rich.progress import BarColumn, Progress, TaskProgressColumn, TextColumn, TimeRemainingColumn

from photo_organiser.config import Settings, get_settings
from photo_organiser.db import get_db
from photo_organiser.paths import preview_path, thumb_path

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

_ANALYZE_MAX_SIDE = 384
_INK_MIN = 0.025
_INK_MAX = 0.55
_MAX_LINE_HEIGHT_FRAC = 0.05
_MIN_LINE_ASPECT = 4.0
_MIN_LINE_WIDTH_FRAC = 0.22
_COLOR_MAX = 25.0
_SAT_MAX = 0.25
_SMOOTH_MAX = 0.58
_SPACING_CV_MAX = 0.7
DEFAULT_MIN_LINES = 8
DEFAULT_MIN_COVERAGE = 0.08


@dataclass
class Verdict:
    path: str
    is_text: bool
    reasons: list[str] = field(default_factory=list)
    line_count: int = 0
    coverage: float = 0.0
    ink_fraction: float = 0.0
    width: int | None = None
    height: int | None = None
    error: str | None = None


def _rgb_for_analysis(path: Path) -> tuple[np.ndarray | None, int, int, str | None]:
    try:
        data = path.read_bytes()
    except OSError as exc:
        return None, 0, 0, str(exc)
    try:
        with Image.open(io.BytesIO(data)) as im:
            rgb = im.convert("RGB")
            width, height = rgb.size
            if max(rgb.size) > _ANALYZE_MAX_SIDE:
                rgb = rgb.copy()
                rgb.thumbnail((_ANALYZE_MAX_SIDE, _ANALYZE_MAX_SIDE), Image.Resampling.BILINEAR)
            arr = np.array(rgb)
    except Exception as exc:  # noqa: BLE001
        return None, 0, 0, f"{type(exc).__name__}: {exc}"
    return arr, width, height, None


def _inner(arr: np.ndarray, margin: float = 0.12) -> np.ndarray:
    height, width = arr.shape[:2]
    dy, dx = int(height * margin), int(width * margin)
    cropped = arr[dy : height - dy or height, dx : width - dx or width]
    return cropped if cropped.size else arr


def _colorfulness(rgb: np.ndarray) -> float:
    red = rgb[:, :, 0].astype(np.float32)
    green = rgb[:, :, 1].astype(np.float32)
    blue = rgb[:, :, 2].astype(np.float32)
    rg = red - green
    yb = 0.5 * (red + green) - blue
    return float(
        np.sqrt(rg.std() ** 2 + yb.std() ** 2)
        + 0.3 * np.sqrt(rg.mean() ** 2 + yb.mean() ** 2)
    )


def _sat_mean(rgb: np.ndarray) -> float:
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    return float(hsv[:, :, 1].mean() / 255.0)


def _smooth_block_fraction(gray: np.ndarray, block: int = 16, std_max: float = 12.0) -> float:
    height, width = gray.shape
    total = 0
    smooth = 0
    for y in range(0, height - block + 1, block):
        for x in range(0, width - block + 1, block):
            total += 1
            if gray[y : y + block, x : x + block].std() < std_max:
                smooth += 1
    return smooth / total if total else 0.0


def _spacing_ok(ys: list[int]) -> bool:
    if len(ys) < 4:
        return True
    gaps = np.diff(np.sort(np.array(ys, dtype=np.float32)))
    mean = float(gaps.mean())
    if mean < 1:
        return False
    return float(gaps.std() / mean) <= _SPACING_CV_MAX


def _thin_line_stats(gray: np.ndarray) -> tuple[int, float, float, list[int]]:
    """Wide, short bars after adaptive threshold + horizontal close."""
    height, width = gray.shape
    binary = cv2.adaptiveThreshold(
        gray,
        255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY_INV,
        21,
        10,
    )
    ink = float((binary > 0).mean())
    kernel_w = max(9, width // 12)
    closed = cv2.morphologyEx(
        binary,
        cv2.MORPH_CLOSE,
        cv2.getStructuringElement(cv2.MORPH_RECT, (kernel_w, 1)),
    )
    contours, _ = cv2.findContours(closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    max_h = max(3, int(height * _MAX_LINE_HEIGHT_FRAC))
    min_width = width * _MIN_LINE_WIDTH_FRAC
    lines = 0
    area = 0
    ys: list[int] = []
    for contour in contours:
        _x, y, box_w, box_h = cv2.boundingRect(contour)
        if box_h < 2 or box_h > max_h or box_w < min_width:
            continue
        if box_w / box_h < _MIN_LINE_ASPECT:
            continue
        lines += 1
        area += box_w * box_h
        ys.append(y)
    coverage = area / max(width * height, 1)
    return lines, coverage, ink, ys


def _photographic(rgb: np.ndarray, gray: np.ndarray) -> bool:
    sample = _inner(rgb)
    return (
        _colorfulness(sample) >= _COLOR_MAX
        or _sat_mean(sample) >= _SAT_MAX
        or _smooth_block_fraction(gray) >= _SMOOTH_MAX
    )


def inspect_file(
    path: Path,
    *,
    min_lines: int = DEFAULT_MIN_LINES,
    min_coverage: float = DEFAULT_MIN_COVERAGE,
) -> Verdict:
    rgb, width, height, error = _rgb_for_analysis(path)
    if rgb is None:
        return Verdict(path=str(path), is_text=False, reasons=["unreadable"], error=error)
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    lines, coverage, ink, ys = _thin_line_stats(gray)
    reasons: list[str] = []
    if (
        not _photographic(rgb, gray)
        and lines >= min_lines
        and coverage >= min_coverage
        and _INK_MIN <= ink <= _INK_MAX
        and _spacing_ok(ys)
    ):
        reasons.append("text_lines")
    return Verdict(
        path=str(path),
        is_text=bool(reasons),
        reasons=reasons,
        line_count=lines,
        coverage=round(coverage, 4),
        ink_fraction=round(ink, 4),
        width=width,
        height=height,
    )


def iter_image_files(root: Path):
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES:
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
    dest = _unique_dest(dest_root / src.relative_to(scan_root))
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
    min_lines: int = DEFAULT_MIN_LINES,
    min_coverage: float = DEFAULT_MIN_COVERAGE,
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
    stats = {"scanned": 0, "text": 0, "other": 0, "moved": 0, "copied": 0}
    try:
        with Progress(
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            TimeRemainingColumn(),
            console=console,
        ) as progress:
            task = progress.add_task("text-scan", total=len(files))
            for path in files:
                if dest_root is not None:
                    try:
                        path.relative_to(dest_root.resolve())
                        progress.advance(task)
                        continue
                    except ValueError:
                        pass
                verdict = inspect_file(path, min_lines=min_lines, min_coverage=min_coverage)
                stats["scanned"] += 1
                if verdict.is_text:
                    stats["text"] += 1
                    if dest_root is not None:
                        relocated = _relocate(path, dest_root, root, copy=copy)
                        verdict.path = str(relocated)
                        if copy:
                            stats["copied"] += 1
                        else:
                            stats["moved"] += 1
                else:
                    stats["other"] += 1
                if report_fp is not None:
                    report_fp.write(json.dumps(asdict(verdict)) + "\n")
                progress.advance(task)
    finally:
        if report_fp is not None:
            report_fp.close()
    console.print(stats)
    return stats


def _best_local_image(media_key: str, settings: Settings) -> Path | None:
    preview = preview_path(media_key, settings.previews_dir)
    if preview.exists():
        return preview
    thumb = thumb_path(media_key, settings.thumbs_dir)
    return thumb if thumb.exists() else None


def scan_thumbs(
    settings: Settings | None = None,
    *,
    report: Path | None = None,
    min_lines: int = DEFAULT_MIN_LINES,
    min_coverage: float = DEFAULT_MIN_COVERAGE,
    limit: int | None = None,
) -> dict:
    settings = settings or get_settings()
    report = report or (settings.data_root / "text_thumbs.jsonl")
    scanned_at = datetime.now(UTC).isoformat()
    stats = {"scanned": 0, "text": 0, "other": 0, "missing": 0}

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
                "[yellow]No cached thumbs. Run census import then "
                "`photo-organiser fetch thumbs`, or `photo-organiser text scan PATH` "
                "on a local folder.[/yellow]"
            )
            return stats

        upsert = """
            INSERT INTO text_flags(
                media_key, is_text, reasons, line_count, coverage, ink_fraction,
                scanned_at, review_status
            ) VALUES(?, ?, ?, ?, ?, ?, ?, 'pending')
            ON CONFLICT(media_key) DO UPDATE SET
                is_text=excluded.is_text,
                reasons=excluded.reasons,
                line_count=excluded.line_count,
                coverage=excluded.coverage,
                ink_fraction=excluded.ink_fraction,
                scanned_at=excluded.scanned_at,
                review_status=CASE
                    WHEN excluded.is_text=1
                         AND text_flags.review_status IN ('accepted','rejected','skipped')
                    THEN text_flags.review_status
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
            task = progress.add_task("text-thumbs", total=len(rows))
            for row in rows:
                mk = row["media_key"]
                path = _best_local_image(mk, settings)
                if path is None:
                    stats["missing"] += 1
                    progress.advance(task)
                    continue
                verdict = inspect_file(path, min_lines=min_lines, min_coverage=min_coverage)
                stats["scanned"] += 1
                if verdict.is_text:
                    stats["text"] += 1
                else:
                    stats["other"] += 1
                record = asdict(verdict)
                record["media_key"] = mk
                record["file_name"] = row["file_name"]
                report_fp.write(json.dumps(record) + "\n")
                conn.execute(
                    upsert,
                    (
                        mk,
                        1 if verdict.is_text else 0,
                        json.dumps(verdict.reasons),
                        verdict.line_count,
                        verdict.coverage,
                        verdict.ink_fraction,
                        scanned_at,
                    ),
                )
                progress.advance(task)

    console.print(stats)
    console.print(f"Report: {report}")
    return stats


def text_queue_counts(conn) -> dict:
    pending = conn.execute(
        """
        SELECT COUNT(*) AS c FROM text_flags
        WHERE is_text=1 AND COALESCE(review_status, 'pending')='pending'
        """
    ).fetchone()["c"]
    decided = conn.execute(
        """
        SELECT COUNT(*) AS c FROM text_flags
        WHERE is_text=1 AND COALESCE(review_status, 'pending')!='pending'
        """
    ).fetchone()["c"]
    return {"pending": pending, "decided": decided}


def next_text_item(conn, after: str | None = None) -> dict | None:
    if after:
        row = conn.execute(
            """
            SELECT tf.media_key, tf.reasons, tf.line_count, tf.coverage, tf.ink_fraction,
                   p.file_name, p.res_width, p.res_height, p.is_favorite, p.dedup_key
            FROM text_flags tf
            JOIN photos p ON p.media_key = tf.media_key
            WHERE tf.is_text=1 AND COALESCE(tf.review_status, 'pending')='pending'
              AND tf.media_key > ?
            ORDER BY tf.media_key
            LIMIT 1
            """,
            (after,),
        ).fetchone()
        if row:
            return _text_item(row)
    row = conn.execute(
        """
        SELECT tf.media_key, tf.reasons, tf.line_count, tf.coverage, tf.ink_fraction,
               p.file_name, p.res_width, p.res_height, p.is_favorite, p.dedup_key
        FROM text_flags tf
        JOIN photos p ON p.media_key = tf.media_key
        WHERE tf.is_text=1 AND COALESCE(tf.review_status, 'pending')='pending'
        ORDER BY tf.media_key
        LIMIT 1
        """
    ).fetchone()
    return _text_item(row) if row else None


def _text_item(row) -> dict:
    try:
        reasons = json.loads(row["reasons"] or "[]")
    except json.JSONDecodeError:
        reasons = [row["reasons"]] if row["reasons"] else []
    return {
        "media_key": row["media_key"],
        "dedup_key": row["dedup_key"],
        "file_name": row["file_name"],
        "reasons": reasons,
        "line_count": row["line_count"] or 0,
        "coverage": row["coverage"] or 0.0,
        "ink_fraction": row["ink_fraction"] or 0.0,
        "res_width": row["res_width"],
        "res_height": row["res_height"],
        "is_favorite": bool(row["is_favorite"]),
    }


def decide_text(conn, media_key: str, action: str) -> dict:
    status = {"accept": "accepted", "reject": "rejected", "skip": "skipped"}.get(action)
    if status is None:
        raise ValueError(f"unknown action {action}")
    row = conn.execute(
        "SELECT media_key FROM text_flags WHERE media_key=? AND is_text=1",
        (media_key,),
    ).fetchone()
    if not row:
        raise KeyError(media_key)
    conn.execute(
        "UPDATE text_flags SET review_status=? WHERE media_key=?",
        (status, media_key),
    )
    return {"ok": True, "status": status, "media_key": media_key}


def export_accepted(settings: Settings | None = None) -> Path:
    settings = settings or get_settings()
    out = settings.data_root / "text_accepted.jsonl"
    with get_db(settings.db_path) as conn:
        rows = conn.execute(
            """
            SELECT tf.media_key, tf.line_count, tf.coverage, p.file_name, p.dedup_key
            FROM text_flags tf
            JOIN photos p ON p.media_key = tf.media_key
            WHERE tf.is_text=1 AND tf.review_status='accepted'
            ORDER BY tf.media_key
            """
        ).fetchall()
    with out.open("w", encoding="utf-8") as fp:
        for row in rows:
            fp.write(
                json.dumps({name: row[name] for name in row.keys()}) + "\n"  # noqa: SIM118
            )
    console.print(f"[green]Wrote {out} ({len(rows)} accepted text photos)[/green]")
    return out
