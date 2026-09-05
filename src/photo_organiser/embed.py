"""DINOv2 embedding pass with checkpointing and OOM-aware batch sizing."""

from __future__ import annotations

import struct
from pathlib import Path

import numpy as np
from PIL import Image
from rich.console import Console
from rich.progress import BarColumn, Progress, TaskProgressColumn, TextColumn, TimeRemainingColumn

from photo_organiser.config import Settings, get_settings
from photo_organiser.db import get_db
from photo_organiser.device import DeviceInfo, resolve_device
from photo_organiser.paths import thumb_path

console = Console()


def _vector_to_blob(vec: np.ndarray) -> bytes:
    flat = vec.astype(np.float32).ravel()
    return struct.pack(f"{len(flat)}f", *flat.tolist())


def _blob_to_vector(blob: bytes, dim: int) -> np.ndarray:
    return np.array(struct.unpack(f"{dim}f", blob), dtype=np.float32)


def load_dinov2(device: DeviceInfo, model_name: str):
    """Load DINOv2 via transformers or timm, falling back with a clear error."""
    import torch

    try:
        from transformers import AutoImageProcessor, AutoModel

        processor = AutoImageProcessor.from_pretrained(model_name)
        model = AutoModel.from_pretrained(model_name)
        model.eval()
        model.to(device.torch_device)
        if device.use_fp16 and device.is_cuda:
            model.half()
        return ("transformers", processor, model)
    except Exception as primary:  # noqa: BLE001
        try:
            import timm

            # timm naming: vit_base_patch14_dinov2.lvd142m
            timm_name = "vit_base_patch14_dinov2.lvd142m"
            model = timm.create_model(timm_name, pretrained=True, num_classes=0)
            model.eval()
            model.to(device.torch_device)
            if device.use_fp16 and device.is_cuda:
                model.half()
            data_cfg = timm.data.resolve_model_data_config(model)
            transform = timm.data.create_transform(**data_cfg, is_training=False)
            return ("timm", transform, model)
        except Exception as secondary:  # noqa: BLE001
            raise RuntimeError(
                "Failed to load DINOv2. Install ML deps:\n"
                "  uv pip install torch torchvision transformers timm\n"
                f"transformers error: {primary}\n"
                f"timm error: {secondary}"
            ) from secondary


def _encode_batch(backend, images: list[Image.Image], device: DeviceInfo):
    import torch

    kind, prep, model = backend
    with torch.no_grad():
        if kind == "transformers":
            inputs = prep(images=images, return_tensors="pt")
            inputs = {k: v.to(device.torch_device) for k, v in inputs.items()}
            if device.use_fp16 and device.is_cuda:
                inputs = {
                    k: (v.half() if v.dtype.is_floating_point else v) for k, v in inputs.items()
                }
            out = model(**inputs)
            # CLS token
            feats = out.last_hidden_state[:, 0, :]
        else:
            tensors = torch.stack([prep(img) for img in images]).to(device.torch_device)
            if device.use_fp16 and device.is_cuda:
                tensors = tensors.half()
            feats = model(tensors)
            if isinstance(feats, (tuple, list)):
                feats = feats[0]

        feats = torch.nn.functional.normalize(feats.float(), p=2, dim=1)
        return feats.cpu().numpy()


def embed(
    *,
    batch_size: int | None = None,
    limit: int | None = None,
    device_prefer: str | None = None,
    settings: Settings | None = None,
) -> dict:
    settings = settings or get_settings()
    device = resolve_device(device_prefer)
    console.print(f"Embedding on {device.kind} — {device.name}")

    batch_size = batch_size or settings.embed_batch_size
    backend = load_dinov2(device, settings.dinov2_model)

    with get_db(settings.db_path) as conn:
        rows = conn.execute(
            """
            SELECT media_key FROM photos
            WHERE thumb_cached=1 AND embedded=0 AND is_video=0 AND excluded=0
            ORDER BY timestamp
            """
        ).fetchall()

    keys = [r["media_key"] for r in rows]
    if limit is not None:
        keys = keys[:limit]

    if not keys:
        console.print("[green]Nothing to embed.[/green]")
        return {"embedded": 0, "failed": 0, "device": device.kind}

    console.print(f"Embedding {len(keys)} images (batch={batch_size})")

    embedded = 0
    failed = 0
    i = 0

    with Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        TimeRemainingColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("embed", total=len(keys))

        while i < len(keys):
            chunk_keys = keys[i : i + batch_size]
            images: list[Image.Image] = []
            valid_keys: list[str] = []
            for mk in chunk_keys:
                path = thumb_path(mk, settings.thumbs_dir)
                try:
                    img = Image.open(path).convert("RGB")
                    images.append(img)
                    valid_keys.append(mk)
                except Exception:  # noqa: BLE001
                    failed += 1
                    progress.advance(task)
                    continue

            if not images:
                i += batch_size
                continue

            try:
                vectors = _encode_batch(backend, images, device)
            except RuntimeError as exc:
                if "out of memory" in str(exc).lower() and batch_size > 1:
                    batch_size = max(1, batch_size // 2)
                    console.print(f"[yellow]OOM — reducing batch size to {batch_size}[/yellow]")
                    if device.is_cuda:
                        import torch

                        torch.cuda.empty_cache()
                    continue
                raise

            with get_db(settings.db_path) as conn:
                for mk, vec in zip(valid_keys, vectors, strict=True):
                    conn.execute(
                        """
                        INSERT INTO embeddings(media_key, dim, vector) VALUES(?, ?, ?)
                        ON CONFLICT(media_key) DO UPDATE SET dim=excluded.dim, vector=excluded.vector
                        """,
                        (mk, int(vec.shape[0]), _vector_to_blob(vec)),
                    )
                    conn.execute("UPDATE photos SET embedded=1 WHERE media_key=?", (mk,))
                    # Also dump numpy sidecar for debugging / ANN export
                    npy = settings.embeddings_dir / f"{mk}.npy"
                    # hashed path would be nicer but media keys can be long; use flat with hash
                    from hashlib import sha1

                    h = sha1(mk.encode()).hexdigest()
                    npy = settings.embeddings_dir / h[:2] / f"{h}.npy"
                    npy.parent.mkdir(parents=True, exist_ok=True)
                    np.save(npy, vec.astype(np.float32))

            embedded += len(valid_keys)
            progress.advance(task, len(chunk_keys))
            i += batch_size

            if embedded % (settings.embed_checkpoint_every * max(batch_size, 1)) == 0:
                console.print(f"[dim]checkpoint: {embedded} embedded[/dim]")

    result = {"embedded": embedded, "failed": failed, "device": device.kind, "batch_size": batch_size}
    console.print(result)
    return result


def load_all_embeddings(settings: Settings | None = None) -> tuple[list[str], np.ndarray]:
    """Return (media_keys, N×D float32 matrix) for all embedded photos."""
    settings = settings or get_settings()
    with get_db(settings.db_path) as conn:
        rows = conn.execute(
            """
            SELECT e.media_key, e.dim, e.vector
            FROM embeddings e
            JOIN photos p ON p.media_key = e.media_key
            WHERE p.excluded=0 AND p.is_video=0
            ORDER BY p.timestamp
            """
        ).fetchall()
    if not rows:
        return [], np.zeros((0, 0), dtype=np.float32)
    dim = rows[0]["dim"]
    keys = [r["media_key"] for r in rows]
    mat = np.vstack([_blob_to_vector(r["vector"], dim) for r in rows])
    return keys, mat
