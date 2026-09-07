"""SQLite persistence (WAL mode)."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS photos (
    media_key TEXT PRIMARY KEY,
    dedup_key TEXT NOT NULL,
    timestamp INTEGER,
    timezone_offset INTEGER,
    creation_timestamp INTEGER,
    thumb TEXT,
    res_width INTEGER,
    res_height INTEGER,
    is_favorite INTEGER DEFAULT 0,
    is_archived INTEGER DEFAULT 0,
    is_live_photo INTEGER DEFAULT 0,
    is_owned INTEGER DEFAULT 1,
    duration REAL,
    description_short TEXT,
    file_name TEXT,
    space_taken INTEGER,
    is_original_quality INTEGER,
    in_album INTEGER DEFAULT 0,
    is_video INTEGER DEFAULT 0,
    thumb_cached INTEGER DEFAULT 0,
    preview_cached INTEGER DEFAULT 0,
    embedded INTEGER DEFAULT 0,
    excluded INTEGER DEFAULT 0,
    exclude_reason TEXT
);

CREATE INDEX IF NOT EXISTS idx_photos_ts ON photos(timestamp);
CREATE INDEX IF NOT EXISTS idx_photos_dedup ON photos(dedup_key);
CREATE INDEX IF NOT EXISTS idx_photos_embedded ON photos(embedded);

CREATE TABLE IF NOT EXISTS embeddings (
    media_key TEXT PRIMARY KEY REFERENCES photos(media_key) ON DELETE CASCADE,
    dim INTEGER NOT NULL,
    vector BLOB NOT NULL
);

CREATE TABLE IF NOT EXISTS groups (
    group_id TEXT PRIMARY KEY,
    size INTEGER NOT NULL,
    proposed_keeper TEXT,
    status TEXT DEFAULT 'pending',  -- pending | accepted | overridden | skipped | kept_all | deleted_all
    override_keeper TEXT,
    reviewed_at TEXT
);

CREATE TABLE IF NOT EXISTS group_members (
    group_id TEXT NOT NULL REFERENCES groups(group_id) ON DELETE CASCADE,
    media_key TEXT NOT NULL REFERENCES photos(media_key) ON DELETE CASCADE,
    score REAL,
    score_sharpness REAL,
    score_exposure REAL,
    score_eyes REAL,
    score_smile REAL,
    score_aesthetic REAL,
    score_resolution REAL,
    is_proposed_keeper INTEGER DEFAULT 0,
    PRIMARY KEY (group_id, media_key)
);

CREATE INDEX IF NOT EXISTS idx_members_group ON group_members(group_id);

CREATE TABLE IF NOT EXISTS decisions (
    group_id TEXT PRIMARY KEY REFERENCES groups(group_id),
    keep_media_key TEXT NOT NULL,
    trash_dedup_keys TEXT NOT NULL,  -- JSON array
    decided_at TEXT NOT NULL,
    applied INTEGER DEFAULT 0,
    applied_at TEXT
);

CREATE TABLE IF NOT EXISTS undo_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    dedup_key TEXT NOT NULL,
    media_key TEXT,
    trashed_at TEXT NOT NULL,
    restored INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_undo_run ON undo_log(run_id);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS corrupt_flags (
    media_key TEXT PRIMARY KEY REFERENCES photos(media_key) ON DELETE CASCADE,
    corrupted INTEGER NOT NULL DEFAULT 0,
    reasons TEXT,
    fill_fraction REAL,
    fill_color TEXT,
    scanned_at TEXT NOT NULL,
    review_status TEXT NOT NULL DEFAULT 'pending'
);

CREATE TABLE IF NOT EXISTS text_flags (
    media_key TEXT PRIMARY KEY REFERENCES photos(media_key) ON DELETE CASCADE,
    is_text INTEGER NOT NULL DEFAULT 0,
    reasons TEXT,
    line_count INTEGER,
    coverage REAL,
    ink_fraction REAL,
    scanned_at TEXT NOT NULL,
    review_status TEXT NOT NULL DEFAULT 'pending'
);
"""


def _migrate(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(corrupt_flags)")}
    if cols and "review_status" not in cols:
        conn.execute(
            "ALTER TABLE corrupt_flags ADD COLUMN review_status TEXT NOT NULL DEFAULT 'pending'"
        )


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=60.0)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


@contextmanager
def get_db(db_path: Path) -> Iterator[sqlite3.Connection]:
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def get_meta(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default
