"""Best-shot quality scoring within duplicate groups."""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
from PIL import Image
from rich.console import Console
from rich.progress import BarColumn, Progress, TaskProgressColumn, TextColumn, TimeRemainingColumn

from photo_organiser.config import Settings, get_settings
from photo_organiser.db import get_db
from photo_organiser.models import ScoreBreakdown
from photo_organiser.paths import preview_path, thumb_path

console = Console()

_face_landmarker = None


def _get_landmarker(models_dir: Path):
    """Lazy-load MediaPipe Face Landmarker with blendshapes."""
    global _face_landmarker
    if _face_landmarker is not None:
        return _face_landmarker

    try:
        import mediapipe as mp
        from mediapipe.tasks import python as mp_python
        from mediapipe.tasks.python import vision
    except ImportError:
        console.print("[yellow]mediapipe not available — face scores will be neutral[/yellow]")
        return None

    model_path = models_dir / "face_landmarker.task"
    if not model_path.exists():
        # Download official model bundle
        import urllib.request

        models_dir.mkdir(parents=True, exist_ok=True)
        url = (
            "https://storage.googleapis.com/mediapipe-models/"
            "face_landmarker/face_landmarker/float16/1/face_landmarker.task"
        )
        console.print(f"Downloading Face Landmarker model → {model_path}")
        urllib.request.urlretrieve(url, model_path)

    base = mp_python.BaseOptions(model_asset_path=str(model_path))
    options = vision.FaceLandmarkerOptions(
        base_options=base,
        running_mode=vision.RunningMode.IMAGE,
        num_faces=10,
        output_face_blendshapes=True,
        output_facial_transformation_matrixes=False,
    )
    _face_landmarker = vision.FaceLandmarker.create_from_options(options)
    return _face_landmarker


def _load_bgr(path: Path) -> np.ndarray | None:
    if not path.exists():
        return None
    data = np.fromfile(str(path), dtype=np.uint8)
    img = cv2.imdecode(data, cv2.IMREAD_COLOR)
    return img


def sharpness_score(gray: np.ndarray, roi: tuple[int, int, int, int] | None = None) -> float:
    """Laplacian variance + Tenengrad, optionally restricted to ROI (x,y,w,h)."""
    region = gray
    if roi is not None:
        x, y, w, h = roi
        region = gray[max(0, y) : y + h, max(0, x) : x + w]
        if region.size < 16:
            region = gray
    lap = cv2.Laplacian(region, cv2.CV_64F).var()
    gx = cv2.Sobel(region, cv2.CV_64F, 1, 0, ksize=3)
    gy = cv2.Sobel(region, cv2.CV_64F, 0, 1, ksize=3)
    tenengrad = np.mean(gx**2 + gy**2)
    # Soft normalise — typical sharp photos land around these scales
    lap_n = min(lap / 500.0, 1.0)
    ten_n = min(tenengrad / 2000.0, 1.0)
    return float(0.6 * lap_n + 0.4 * ten_n)


def exposure_score(gray: np.ndarray) -> float:
    hist = cv2.calcHist([gray], [0], None, [256], [0, 256]).ravel()
    hist = hist / max(hist.sum(), 1.0)
    clipped_hi = float(hist[250:].sum())
    clipped_lo = float(hist[:5].sum())
    mean = float(np.average(np.arange(256), weights=hist)) / 255.0
    # Prefer mid-tones, penalise clipping
    mid = 1.0 - abs(mean - 0.5) * 2.0
    clip_pen = min(clipped_hi + clipped_lo, 1.0)
    return float(max(0.0, mid * (1.0 - clip_pen)))


def face_scores(img_bgr: np.ndarray, landmarker) -> tuple[float, float, tuple[int, int, int, int] | None]:
    """Return (eyes_open, smile, largest_face_roi). Neutral 0.5 if no faces."""
    if landmarker is None:
        return 0.5, 0.5, None

    import mediapipe as mp

    rgb = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)
    mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
    result = landmarker.detect(mp_image)
    if not result.face_landmarks:
        return 0.5, 0.5, None

    # Pick largest face by landmark bounding box
    best_i = 0
    best_area = -1.0
    h, w = img_bgr.shape[:2]
    rois = []
    for i, lms in enumerate(result.face_landmarks):
        xs = [lm.x * w for lm in lms]
        ys = [lm.y * h for lm in lms]
        x0, x1 = int(min(xs)), int(max(xs))
        y0, y1 = int(min(ys)), int(max(ys))
        area = (x1 - x0) * (y1 - y0)
        rois.append((x0, y0, x1 - x0, y1 - y0))
        if area > best_area:
            best_area = area
            best_i = i

    eyes = 0.5
    smile = 0.5
    if result.face_blendshapes and len(result.face_blendshapes) > best_i:
        blends = {b.category_name: b.score for b in result.face_blendshapes[best_i]}
        blink = (blends.get("eyeBlinkLeft", 0.0) + blends.get("eyeBlinkRight", 0.0)) / 2.0
        eyes = float(max(0.0, 1.0 - blink))
        smile = float(
            max(
                blends.get("mouthSmileLeft", 0.0),
                blends.get("mouthSmileRight", 0.0),
                blends.get("smile", 0.0),
            )
        )
    return eyes, smile, rois[best_i]


def aesthetic_score(img_bgr: np.ndarray) -> float:
    """Lightweight aesthetic proxy (BRISQUE-ish via Laplacian entropy). Optional pyiqa later."""
    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    # Simple: reward detail without noise — use normalised entropy of gradients
    gx = cv2.Sobel(gray, cv2.CV_32F, 1, 0)
    gy = cv2.Sobel(gray, cv2.CV_32F, 0, 1)
    mag = np.sqrt(gx**2 + gy**2)
    hist, _ = np.histogram(mag.ravel(), bins=32, range=(0, 255), density=True)
    hist = hist + 1e-9
    entropy = float(-np.sum(hist * np.log(hist)))
    return float(min(entropy / 4.0, 1.0))


def score_image(
    path: Path,
    res_width: int | None,
    res_height: int | None,
    landmarker,
    settings: Settings,
) -> ScoreBreakdown:
    img = _load_bgr(path)
    if img is None:
        return ScoreBreakdown()
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    eyes, smile, roi = face_scores(img, landmarker)
    sharp = sharpness_score(gray, roi)
    expo = exposure_score(gray)
    aes = aesthetic_score(img)
    pixels = (res_width or img.shape[1]) * (res_height or img.shape[0])
    # Soft resolution score relative to 12 MP
    reso = float(min(pixels / 12_000_000, 1.0))

    total = (
        settings.weight_sharpness * sharp
        + settings.weight_exposure * expo
        + settings.weight_eyes * eyes
        + settings.weight_smile * smile
        + settings.weight_aesthetic * aes
        + settings.weight_resolution * reso
    )
    return ScoreBreakdown(
        sharpness=sharp,
        exposure=expo,
        eyes=eyes,
        smile=smile,
        aesthetic=aes,
        resolution=reso,
        total=float(total),
    )


def _minmax_norm(values: list[float]) -> list[float]:
    if not values:
        return []
    lo, hi = min(values), max(values)
    if hi - lo < 1e-9:
        return [0.5] * len(values)
    return [(v - lo) / (hi - lo) for v in values]


def score_groups(settings: Settings | None = None) -> dict:
    settings = settings or get_settings()
    landmarker = _get_landmarker(settings.models_dir)

    with get_db(settings.db_path) as conn:
        groups = conn.execute("SELECT group_id FROM groups").fetchall()
        group_ids = [g["group_id"] for g in groups]

    if not group_ids:
        console.print("[yellow]No groups to score. Run `photo-organiser group` first.[/yellow]")
        return {"scored_groups": 0}

    scored = 0
    with Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        TimeRemainingColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("score", total=len(group_ids))

        for gid in group_ids:
            with get_db(settings.db_path) as conn:
                members = conn.execute(
                    """
                    SELECT gm.media_key, p.res_width, p.res_height, p.is_favorite
                    FROM group_members gm
                    JOIN photos p ON p.media_key = gm.media_key
                    WHERE gm.group_id = ?
                    """,
                    (gid,),
                ).fetchall()

            breakdowns: list[ScoreBreakdown] = []
            media_keys: list[str] = []
            favorites: list[bool] = []
            for m in members:
                mk = m["media_key"]
                path = preview_path(mk, settings.previews_dir)
                if not path.exists():
                    path = thumb_path(mk, settings.thumbs_dir)
                bd = score_image(path, m["res_width"], m["res_height"], landmarker, settings)
                breakdowns.append(bd)
                media_keys.append(mk)
                favorites.append(bool(m["is_favorite"]))

            # Within-group min-max on total (and keep components as-is for UI)
            totals = [b.total for b in breakdowns]
            normed = _minmax_norm(totals)
            for i, n in enumerate(normed):
                breakdowns[i].total = n

            # Favorites always win if present
            if any(favorites):
                keeper_i = favorites.index(True)
            else:
                keeper_i = int(np.argmax([b.total for b in breakdowns]))

            with get_db(settings.db_path) as conn:
                for i, mk in enumerate(media_keys):
                    b = breakdowns[i]
                    conn.execute(
                        """
                        UPDATE group_members SET
                            score=?, score_sharpness=?, score_exposure=?,
                            score_eyes=?, score_smile=?, score_aesthetic=?,
                            score_resolution=?, is_proposed_keeper=?
                        WHERE group_id=? AND media_key=?
                        """,
                        (
                            b.total,
                            b.sharpness,
                            b.exposure,
                            b.eyes,
                            b.smile,
                            b.aesthetic,
                            b.resolution,
                            1 if i == keeper_i else 0,
                            gid,
                            mk,
                        ),
                    )
                conn.execute(
                    "UPDATE groups SET proposed_keeper=? WHERE group_id=?",
                    (media_keys[keeper_i], gid),
                )
            scored += 1
            progress.advance(task)

    result = {"scored_groups": scored}
    console.print(result)
    return result
