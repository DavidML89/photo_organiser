"""Unit tests for core helpers (no Google Photos / GPU required)."""

from __future__ import annotations

import numpy as np

from photo_organiser.group import UnionFind
from photo_organiser.paths import hashed_subdir, sized_thumb_url
from photo_organiser.score import _minmax_norm, exposure_score, sharpness_score


def test_sized_thumb_url_strips_and_resizes():
    url = "https://lh3.googleusercontent.com/abc=w100-h100-no"
    out = sized_thumb_url(url, 256)
    assert out.startswith("https://lh3.googleusercontent.com/abc=")
    assert "w256-h256-k-no" in out
    assert "authuser=0" in out


def test_hashed_subdir_fanout(tmp_path):
    p = hashed_subdir("mediaKey-123", tmp_path)
    assert p.parent.parent.parent == tmp_path
    assert p.suffix == ".jpg"
    assert len(p.parent.parent.name) == 2


def test_union_find_transitive():
    uf = UnionFind()
    uf.union("a", "b")
    uf.union("b", "c")
    comps = uf.components()
    root = uf.find("a")
    assert set(comps[root]) == {"a", "b", "c"}


def test_minmax_norm():
    assert _minmax_norm([1.0, 2.0, 3.0]) == [0.0, 0.5, 1.0]
    assert _minmax_norm([5.0, 5.0]) == [0.5, 0.5]


def test_sharpness_and_exposure_on_synthetic():
    # Sharp: high-frequency checkerboard
    sharp = np.zeros((64, 64), dtype=np.uint8)
    sharp[::2, ::2] = 255
    sharp[1::2, 1::2] = 255
    # Blur: constant
    blur = np.full((64, 64), 128, dtype=np.uint8)
    assert sharpness_score(sharp) > sharpness_score(blur)

    mid = np.full((64, 64), 128, dtype=np.uint8)
    clipped = np.full((64, 64), 255, dtype=np.uint8)
    assert exposure_score(mid) > exposure_score(clipped)
