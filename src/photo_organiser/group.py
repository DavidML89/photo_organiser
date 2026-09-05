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


def _cosine(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.dot(a, b))


def build_groups(settings: Settings | None = None) -> dict:
    settings = settings or get_settings()
    keys, mat = load_all_embeddings(settings)
    if len(keys) < 2:
        console.print("[yellow]Need at least 2 embeddings to group.[/yellow]")
        return {"groups": 0, "members": 0}

    key_to_idx = {k: i for i, k in enumerate(keys)}

    with get_db(settings.db_path) as conn:
        rows = conn.execute(
            """
            SELECT media_key, timestamp FROM photos
            WHERE embedded=1 AND excluded=0 AND is_video=0
            """
        ).fetchall()
    rows = sorted(rows, key=lambda r: (r["timestamp"] is None, r["timestamp"] or 0))
    ordered = [r["media_key"] for r in rows if r["media_key"] in key_to_idx]
    ts_map = {r["media_key"]: r["timestamp"] for r in rows}

    uf = UnionFind()
    for k in ordered:
        uf.find(k)

    # --- Time-window candidates ---
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
            if t1 - t0 > settings.time_window_s:
                break
            v1 = mat[key_to_idx[mk2]]
            if _cosine(v0, v1) >= settings.time_cosine_threshold:
                uf.union(mk, mk2)
                time_pairs += 1

    console.print(f"Time-window pairs merged: {time_pairs}")

    # --- Global ANN (FAISS inner product on L2-normalised vectors = cosine) ---
    import faiss

    index = faiss.IndexFlatIP(mat.shape[1])
    index.add(mat.astype(np.float32))
    k = min(settings.ann_topk + 1, len(keys))
    sims, idxs = index.search(mat.astype(np.float32), k)

    ann_pairs = 0
    for i, mk in enumerate(keys):
        for sim, j in zip(sims[i], idxs[i], strict=True):
            if j < 0 or j == i:
                continue
            if float(sim) >= settings.global_cosine_threshold:
                uf.union(mk, keys[j])
                ann_pairs += 1

    console.print(f"ANN pairs merged: {ann_pairs}")

    components = uf.components()
    groups = {gid: members for gid, members in components.items() if len(members) >= settings.min_group_size}

    # Persist — wipe previous grouping
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
        "time_window_s": settings.time_window_s,
        "time_cosine_threshold": settings.time_cosine_threshold,
        "global_cosine_threshold": settings.global_cosine_threshold,
    }
    console.print(result)
    return result
