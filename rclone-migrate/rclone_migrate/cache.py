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
import shutil
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

CACHE_FILENAME = ".rmig-cache.db"
# Stable per-dataset id, stored *in the data root* so it travels with the
# files. The out-of-root (fallback) cache db is then keyed by this id
# instead of the absolute mount path — so re-mounting the same physical
# data under a different path/SMB-share reuses the cache instead of
# orphaning it (which forced a full re-hash). One line, ~33 bytes,
# write-once, never churns (cf. the ascmhl/ sidecar).
MARKER_FILENAME = ".rmig-dataset"


def is_sidecar(name: str) -> bool:
    """True for rmig's own root sidecar files, which must be excluded from
    the manifest/MHL walk. The dataset marker in particular differs
    between src and dst (distinct ids) — including it would make the two
    manifests never match.

    Also matches the macOS AppleDouble companion (``._<name>``) that the
    OS auto-creates for our dotfiles on exFAT/SMB volumes (e.g. an SD
    card): ``._.rmig-dataset`` would otherwise be hashed, land in the
    manifest, and get copied as bogus data."""
    base = name[2:] if name.startswith("._") else name
    return base.startswith(CACHE_FILENAME) or base == MARKER_FILENAME


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
    # not security: just a stable filename digest of the root path
    digest = hashlib.sha1(
        str(root.resolve()).encode("utf-8"), usedforsecurity=False
    ).hexdigest()[:16]
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
    conn.execute(
        "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)"
    )
    conn.commit()
    return conn


def meta_get(conn: sqlite3.Connection, key: str) -> Optional[str]:
    row = conn.execute(
        "SELECT value FROM meta WHERE key = ?", (key,)
    ).fetchone()
    return row[0] if row else None


def meta_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value)
    )
    conn.commit()


def _legacy_fallback_db(root_path: Path, fallback_dir: Path) -> Path:
    # not security: just a stable filename digest of the root path
    digest = hashlib.sha1(
        str(root_path.resolve()).encode("utf-8"), usedforsecurity=False
    ).hexdigest()[:16]
    return fallback_dir / f"cache-{digest}.db"


def _dataset_id(root_path: Path, v=None, *, create: bool = True) -> Optional[str]:
    """Read (or create) <root>/.rmig-dataset. Returns the id, or None if
    the marker can't be read/written (e.g. read-only root) — caller then
    falls back to the legacy path-keyed db (no regression).

    ``create=False`` is read-only: an existing marker is honoured, but a
    missing one is NOT created (used by query/file-status, which must not
    mutate the data root)."""
    marker = root_path / MARKER_FILENAME
    try:
        if marker.is_file():
            txt = marker.read_text(encoding="utf-8", errors="replace").strip()
            tok = txt.split()[0] if txt else ""
            # keep it filesystem-safe; tolerate hand-edits
            tok = "".join(c for c in tok if c.isalnum() or c in "-_")
            if tok:
                return tok
    except OSError:
        return None
    if not create:
        return None
    dsid = uuid.uuid4().hex
    try:
        tmp = root_path / (MARKER_FILENAME + f".tmp.{os.getpid()}")
        tmp.write_text(dsid + "\n", encoding="utf-8")
        os.replace(tmp, marker)
    except OSError:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        if v is not None:
            v.detail(f"  [cache] cannot write {marker} (read-only?) — "
                     f"using path-keyed cache")
        return None
    if v is not None:
        v.detail(f"  [cache] dataset id {dsid} → {marker}")
    return dsid


def resolve_fallback_db(
    root_path: Path, fallback_dir: Path, v=None, *, create: bool = True
) -> Tuple[Path, Optional[str]]:
    """Return (db_path, dataset_id). db is keyed by the stable dataset id
    (mount-path independent); dataset_id is None when the marker was
    unavailable (read-only root) and we fell back to the legacy path-keyed
    db — unchanged v0.3.0 behaviour, no regression.

    ``create=True`` (mutating callers: refresh/copy/delete) writes the
    marker if absent and auto-migrates a pre-existing legacy path-keyed db
    by *copying* it (legacy kept as a rollback backup).

    ``create=False`` (read-only callers: query/file-status) never writes
    the marker or migrates. It still resolves to the SAME db the mutating
    path would read: id-db if the marker exists, else legacy; and if the
    marker exists but the id-db hasn't been materialised yet, it points at
    the legacy db (that's where the hashes still are) instead of copying."""
    fallback_dir.mkdir(parents=True, exist_ok=True)
    legacy = _legacy_fallback_db(root_path, fallback_dir)
    dsid = _dataset_id(root_path, v, create=create)
    if dsid is None:
        return legacy, None
    db = fallback_dir / f"cache-{dsid}.db"
    if not db.exists() and legacy.exists():
        if not create:
            # read-only: data still lives in the legacy db
            return legacy, dsid
        try:
            shutil.copy2(legacy, db)
            if v is not None:
                v.info(f"[cache] migrated {legacy.name} → {db.name} "
                       f"(legacy kept as backup)")
        except OSError as e:
            if v is not None:
                v.warn(f"[cache] migrate {legacy.name} failed ({e}); "
                       f"will rebuild")
    return db, dsid


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
