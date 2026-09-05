"""Near-duplicate grouping via time window + ANN + Union-Find."""

from __future__ import annotations

import uuid
from collections import defaultdict

import numpy as np
from rich.console import Console

from photo_organiser.config import Settings, get_settings
from photo_organiser.db import get_db
from photo_organiser.embed import load_all_embeddings

console = Console()


class UnionFind:
    def __init__(self) -> None:
        self.parent: dict[str, str] = {}
        self.rank: dict[str, int] = {}

    def find(self, x: str) -> str:
        if x not in self.parent:
            self.parent[x] = x
            self.rank[x] = 0
            return x
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        if self.rank[ra] < self.rank[rb]:
            self.parent[ra] = rb
        elif self.rank[ra] > self.rank[rb]:
            self.parent[rb] = ra
        else:
            self.parent[rb] = ra
            self.rank[ra] += 1

    def components(self) -> dict[str, list[str]]:
        groups: dict[str, list[str]] = defaultdict(list)
        for x in self.parent:
            groups[self.find(x)].append(x)
        return dict(groups)


def _to_epoch_seconds(ts: int | float | None) -> int | None:
    """Normalise Google Photos timestamps to Unix seconds.

    GPTK / Photos RPCs sometimes return milliseconds (≈1.7e12) and
    sometimes seconds (≈1.7e9). A 30s window applied to ms values becomes
    30ms and finds almost no burst pairs.
    """
    if ts is None:
        return None
    t = int(ts)
    if t > 10_000_000_000:  # year 2286 in seconds — treat as ms
        return t // 1000
    return t


def build_groups(
    settings: Settings | None = None,
    *,
    time_window_s: int | None = None,
    time_cosine: float | None = None,
    global_cosine: float | None = None,
) -> dict:
    settings = settings or get_settings()
    window = time_window_s if time_window_s is not None else settings.time_window_s
    time_thr = time_cosine if time_cosine is not None else settings.time_cosine_threshold
    global_thr = global_cosine if global_cosine is not None else settings.global_cosine_threshold

    keys, mat = load_all_embeddings(settings)
    if len(keys) < 2:
        console.print("[yellow]Need at least 2 embeddings to group.[/yellow]")
        return {"groups": 0, "members": 0}

    # Ensure L2-normalised for inner-product == cosine
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms = np.clip(norms, 1e-12, None)
    mat = (mat / norms).astype(np.float32)

    key_to_idx = {k: i for i, k in enumerate(keys)}

    with get_db(settings.db_path) as conn:
        rows = conn.execute(
            """
            SELECT media_key, timestamp FROM photos
            WHERE embedded=1 AND excluded=0 AND is_video=0
            """
        ).fetchall()

    ts_raw = [r["timestamp"] for r in rows if r["timestamp"] is not None]
    ts_unit = "unknown"
    if ts_raw:
        median_ts = float(np.median(np.array(ts_raw, dtype=np.float64)))
        ts_unit = "milliseconds" if median_ts > 10_000_000_000 else "seconds"
        console.print(
            f"Timestamps: {len(ts_raw)} present, "
            f"{len(rows) - len(ts_raw)} missing; unit≈{ts_unit} "
            f"(median={median_ts:.0f})"
        )

    rows = sorted(
        rows,
        key=lambda r: (
            _to_epoch_seconds(r["timestamp"]) is None,
            _to_epoch_seconds(r["timestamp"]) or 0,
        ),
    )
    ordered = [r["media_key"] for r in rows if r["media_key"] in key_to_idx]
    ts_map = {r["media_key"]: _to_epoch_seconds(r["timestamp"]) for r in rows}

    uf = UnionFind()
    for k in ordered:
        uf.find(k)

    # --- Time-window candidates (bursts) ---
    time_pairs = 0
    for i, mk in enumerate(ordered):
        t0 = ts_map.get(mk)
        if t0 is None:
            continue
        v0 = mat[key_to_idx[mk]]
        for j in range(i + 1, len(ordered)):
            mk2 = ordered[j]
            t1 = ts_map.get(mk2)
            if t1 is None:
                break
            if t1 - t0 > window:
                break
            v1 = mat[key_to_idx[mk2]]
            if float(np.dot(v0, v1)) >= time_thr:
                uf.union(mk, mk2)
                time_pairs += 1

    console.print(f"Time-window pairs merged: {time_pairs} (window={window}s, thr={time_thr})")

    # --- Global ANN (catches re-uploads / stripped EXIF) ---
    import faiss

    index = faiss.IndexFlatIP(mat.shape[1])
    index.add(mat)
    k = min(settings.ann_topk + 1, len(keys))
    sims, idxs = index.search(mat, k)

    # Diagnostics: distribution of best non-self neighbour similarity
    best_sims: list[float] = []
    for i in range(len(keys)):
        for sim, j in zip(sims[i], idxs[i], strict=False):
            if j < 0 or int(j) == i:
                continue
            best_sims.append(float(sim))
            break
    if best_sims:
        arr = np.array(best_sims, dtype=np.float32)
        console.print(
            "Nearest-neighbour cosine: "
            f"p50={np.percentile(arr, 50):.3f}  "
            f"p90={np.percentile(arr, 90):.3f}  "
            f"p99={np.percentile(arr, 99):.3f}  "
            f"max={arr.max():.3f}"
        )
        above_global = int((arr >= global_thr).sum())
        above_time = int((arr >= time_thr).sum())
        console.print(
            f"Neighbours ≥ global thr {global_thr}: {above_global} / {len(arr)}  |  "
            f"≥ time thr {time_thr}: {above_time}"
        )

    ann_pairs = 0
    for i, mk in enumerate(keys):
        for sim, j in zip(sims[i], idxs[i], strict=False):
            if j < 0 or int(j) == i:
                continue
            if float(sim) >= global_thr:
                uf.union(mk, keys[int(j)])
                ann_pairs += 1

    console.print(f"ANN pairs merged: {ann_pairs} (thr={global_thr}, topk={settings.ann_topk})")

    components = uf.components()
    groups = {
        gid: members
        for gid, members in components.items()
        if len(members) >= settings.min_group_size
    }

    with get_db(settings.db_path) as conn:
        conn.execute("DELETE FROM group_members")
        conn.execute("DELETE FROM groups")
        conn.execute("DELETE FROM decisions")

        stored = 0
        members_total = 0
        for members in groups.values():
            group_id = str(uuid.uuid4())[:8]
            conn.execute(
                "INSERT INTO groups(group_id, size, status) VALUES(?, ?, 'pending')",
                (group_id, len(members)),
            )
            for mk in members:
                conn.execute(
                    "INSERT INTO group_members(group_id, media_key) VALUES(?, ?)",
                    (group_id, mk),
                )
            stored += 1
            members_total += len(members)

    result = {
        "candidates_embedded": len(keys),
        "groups": len(groups),
        "members": members_total,
        "time_pairs": time_pairs,
        "ann_pairs": ann_pairs,
        "time_window_s": window,
        "time_cosine_threshold": time_thr,
        "global_cosine_threshold": global_thr,
        "timestamp_unit": ts_unit,
    }
    console.print(result)
    if len(groups) == 0 and best_sims:
        hint_thr = float(np.percentile(np.array(best_sims), 99))
        console.print(
            "[yellow]0 groups. Your library's nearest-neighbour similarities peak around "
            f"{hint_thr:.3f}. Try:[/yellow]\n"
            f"  uv run photo-organiser group --time-cosine 0.85 --global-cosine {max(0.88, hint_thr - 0.02):.2f} "
            f"--time-window 300"
        )
    return result
