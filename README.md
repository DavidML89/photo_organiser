# photo-organiser

Local Google Photos duplicate finder and best-shot picker.

Replaces paid tools like TopPics: enumerate your library via GPTK's browser
API, analyse locally with DINOv2 + quality metrics, review groups in a local
UI, then trash losers through the Google Photos web UI (recoverable 60 days).

## Quick start

See the full guide:

- [Setup](../docs/development/photo_organiser_setup.md)
- [Architecture](../docs/architecture/photo_organiser.md)
- [Epic](../docs/epics/photo-organiser/PHOTO_ORGANISER_EPIC.md)

```bash
# On the Yoga (Windows) or Mac
uv sync
uv pip install torch torchvision transformers timm   # see setup docs for CUDA index
photo-organiser init
# … then follow the census → fetch → embed → group → score → review → apply flow
```

## License

MIT (project code). DINOv2 weights are Apache 2.0. GPTK is MIT — install separately.
