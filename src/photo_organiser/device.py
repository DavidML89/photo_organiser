"""Resolve compute device: CUDA -> MPS -> CPU."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DeviceInfo:
    kind: str  # cuda | mps | cpu
    name: str
    torch_device: str

    @property
    def is_cuda(self) -> bool:
        return self.kind == "cuda"

    @property
    def use_fp16(self) -> bool:
        return self.kind in {"cuda", "mps"}


def resolve_device(prefer: str | None = None) -> DeviceInfo:
    """Pick the best available accelerator.

    Args:
        prefer: Force ``cuda``, ``mps``, or ``cpu`` when set.
    """
    try:
        import torch
    except ImportError as exc:
        raise RuntimeError(
            "PyTorch is not installed. Install with:\n"
            "  Windows CUDA: uv pip install torch torchvision "
            "--index-url https://download.pytorch.org/whl/cu128\n"
            "  macOS / CPU:  uv pip install torch torchvision"
        ) from exc

    prefer = (prefer or "").lower().strip() or None

    if prefer == "cpu":
        return DeviceInfo(kind="cpu", name="CPU", torch_device="cpu")

    if prefer in {None, "cuda"} and torch.cuda.is_available():
        idx = torch.cuda.current_device()
        name = torch.cuda.get_device_name(idx)
        props = torch.cuda.get_device_properties(idx)
        vram_gb = props.total_memory / (1024**3)
        return DeviceInfo(
            kind="cuda",
            name=f"{name} ({vram_gb:.1f} GB)",
            torch_device="cuda",
        )

    if prefer in {None, "mps"} and getattr(torch.backends, "mps", None) is not None:
        if torch.backends.mps.is_available():
            return DeviceInfo(kind="mps", name="Apple MPS", torch_device="mps")

    if prefer in {"cuda", "mps"}:
        raise RuntimeError(f"Requested device '{prefer}' is not available.")

    return DeviceInfo(kind="cpu", name="CPU", torch_device="cpu")


def print_device_report(prefer: str | None = None) -> DeviceInfo:
    """Resolve device and print a short diagnostic report."""
    info = resolve_device(prefer)
    lines = [f"Selected device: {info.kind} — {info.name}"]
    try:
        import torch

        lines.append(f"torch {torch.__version__}")
        if torch.cuda.is_available():
            lines.append(f"CUDA available: yes ({torch.cuda.get_device_name(0)})")
        else:
            lines.append("CUDA available: no")
        mps = getattr(torch.backends, "mps", None)
        if mps is not None:
            lines.append(f"MPS available: {mps.is_available()}")
    except ImportError:
        lines.append("torch not installed")
    print("\n".join(lines))
    return info
