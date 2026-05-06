"""Local-side hash cache: <root>/.rmig-cache.db (SQLite).

Stores per-file hashes keyed by relative path; rows invalidated by size+mtime
mismatch. Multiple algorithms can coexist in the same DB (algorithm column),
so switching the negotiated hash doesn't blow away the cache.

Schema borrowed from backup-verification/.verify_cache.db
(see verify_backup.py:141-159) with two changes:
  - `algorithm` column added (PK becomes (path, algorithm))
  - `data_sha256` column dropped (no JPEG-aware double-hash here)
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

CACHE_FILENAME = ".rmig-cache.db"


@dataclass
class CacheEntry:
    path: str       # relative to root
    hash: str
    algorithm: str
    size: int
    mtime: float


def cache_path_for_root(root: Path, fallback_dir: Optional[Path] = None) -> Path:
    """Decide where the cache lives.

    Prefer <root>/.rmig-cache.db if root is writable. Otherwise fall back to
    <fallback_dir>/<sha1(root)>.db so read-only mounts still work.
    """
    primary = root / CACHE_FILENAME
    if _writable(root):
        return primary
    if fallback_dir is None:
        # No fallback configured — caller will fail later when trying to write
        return primary
    fallback_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha1(str(root.resolve()).encode("utf-8")).hexdigest()[:16]
    return fallback_dir / f"cache-{digest}.db"


def _writable(p: Path) -> bool:
    if not p.exists():
        try:
            p.mkdir(parents=True, exist_ok=True)
        except OSError:
            return False
    return os.access(p, os.W_OK)


def open_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False: manifest._refresh_local hashes via threads
    # and flushes per-batch through an external lock — see state.open_db.
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS hash_cache (
            path       TEXT NOT NULL,
            algorithm  TEXT NOT NULL,
            hash       TEXT NOT NULL,
            size       INTEGER NOT NULL,
            mtime      REAL NOT NULL,
            refreshed  REAL NOT NULL,
            PRIMARY KEY (path, algorithm)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_hash ON hash_cache(algorithm, hash)")
    conn.commit()
    return conn


def load_for_algorithm(conn: sqlite3.Connection, algorithm: str) -> Dict[str, CacheEntry]:
    """Load all entries for a given algorithm into a dict keyed by relative path."""
    rows = conn.execute(
        "SELECT path, hash, size, mtime FROM hash_cache WHERE algorithm = ?",
        (algorithm,),
    ).fetchall()
    return {
        row[0]: CacheEntry(
            path=row[0], hash=row[1], algorithm=algorithm, size=row[2], mtime=row[3]
        )
        for row in rows
    }


def upsert_many(
    conn: sqlite3.Connection,
    entries: Iterable[CacheEntry],
    refreshed: float,
) -> None:
    payload = [
        (e.path, e.algorithm, e.hash, e.size, e.mtime, refreshed)
        for e in entries
    ]
    if not payload:
        return
    conn.executemany(
        "INSERT OR REPLACE INTO hash_cache "
        "(path, algorithm, hash, size, mtime, refreshed) VALUES (?, ?, ?, ?, ?, ?)",
        payload,
    )
    conn.commit()


def delete_paths(conn: sqlite3.Connection, paths: Iterable[str]) -> None:
    payload = [(p,) for p in paths]
    if not payload:
        return
    conn.executemany("DELETE FROM hash_cache WHERE path = ?", payload)
    conn.commit()


def delete_paths_for_algorithm(
    conn: sqlite3.Connection, paths: Iterable[str], algorithm: str
) -> None:
    payload = [(p, algorithm) for p in paths]
    if not payload:
        return
    conn.executemany(
        "DELETE FROM hash_cache WHERE path = ? AND algorithm = ?", payload
    )
    conn.commit()


@dataclass
class Diff:
    """Result of comparing a directory's current state vs cache."""
    valid: Dict[str, CacheEntry]    # cached entries still trustworthy
    stale: List[str]                # cached but size/mtime changed
    new: List[str]                  # on disk but not in cache
    removed: List[str]              # in cache but no longer on disk


def diff_against_filesystem(
    cached: Dict[str, CacheEntry],
    current: Dict[str, Tuple[int, float]],   # path → (size, mtime)
    full: bool = False,
) -> Diff:
    """Classify each path. If full=True, no cache entry is considered valid."""
    valid: Dict[str, CacheEntry] = {}
    stale: List[str] = []
    removed: List[str] = []

    for path, entry in cached.items():
        if path not in current:
            removed.append(path)
            continue
        size, mtime = current[path]
        if not full and entry.size == size and abs(entry.mtime - mtime) < 0.01:
            valid[path] = entry
        else:
            stale.append(path)

    new = [p for p in current if p not in cached]
    return Diff(valid=valid, stale=stale, new=new, removed=removed)
