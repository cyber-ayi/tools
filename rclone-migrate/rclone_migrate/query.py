"""File-level status queries.

Answer questions like "is this src file backed up?", "what dst files have
this hash?", "what's the audit history for this file?".

Reads from per-side hash caches (built by `rmig-hash`) and the central
state.db (events + file_events). Does NOT trigger fresh hashing — strictly
read-only on the existing snapshots. To force a fresh re-hash, run
`rmig-hash` first.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import TYPE_CHECKING, Dict, Iterable, List, Optional

if TYPE_CHECKING:
    import sqlite3

from . import cache as cache_mod
from . import rclone, state
from .config import Config, Job


@dataclass
class FileMatch:
    side: str
    path: str
    hash: str
    size: int
    last_hashed: float           # epoch


@dataclass
class FileStatus:
    side: str
    path: str
    found_in_cache: bool
    hash: Optional[str] = None
    algorithm: Optional[str] = None
    size: Optional[int] = None
    last_hashed: Optional[float] = None
    matches: List[FileMatch] = field(default_factory=list)  # on the other side
    status: str = "unknown"      # backed_up | missing | unknown | orphan
    events: List[Dict] = field(default_factory=list)        # raw event rows
    warnings: List[str] = field(default_factory=list)


def _open_local_cache_for_side(
    cfg: Config, job: Job, side: str
) -> Optional["sqlite3.Connection"]:
    """Open the SQLite cache that stores hashes for this side, or return None
    if the side is a remote (no local cache, hashes are queried live)."""
    import sqlite3
    root = job.src if side == "src" else job.dst
    if not rclone.is_local(root):
        return None
    state_dir = cfg.state_dir_for(job)
    fallback = state_dir / "local-cache"
    root_path = Path(os.path.expanduser(root))
    in_root = job.resolved_local_cache_in_root(cfg.defaults)
    if in_root:
        db_path = cache_mod.cache_path_for_root(root_path, fallback_dir=fallback)
    else:
        # Same db the refresh/copy/delete paths use — but read-only:
        # create=False never writes the .rmig-dataset marker nor migrates
        # (file-status must not mutate the data root). Resolves to the
        # id-db when the marker exists, else the legacy path-keyed db.
        db_path, _ = cache_mod.resolve_fallback_db(
            root_path, fallback, create=False
        )
    if not db_path.exists():
        return None
    return cache_mod.open_db(db_path)


def _algorithm_for_job(state_conn) -> Optional[str]:
    return state.meta_get(state_conn, "hash_algorithm")


def file_status(
    cfg: Config, job: Job,
    *, side: str, path: str,
) -> FileStatus:
    """Look up cached state for one file."""
    state_dir = cfg.state_dir_for(job)
    state_conn = state.open_db(state_dir)
    algo = _algorithm_for_job(state_conn)

    out = FileStatus(side=side, path=path, found_in_cache=False)
    if algo is None:
        out.warnings.append(
            "no hash_algorithm recorded in state; run rmig-hash or "
            "rmig-check first to populate"
        )
        state_conn.close()
        return out
    out.algorithm = algo

    cache_conn = _open_local_cache_for_side(cfg, job, side)
    if cache_conn is None:
        # Remote side — we don't store anything, can't answer from cache alone.
        out.warnings.append(
            f"side={side} is remote with no local cache; query is "
            "best-effort. Run rmig-check to populate manifests."
        )
        state_conn.close()
        return out

    row = cache_conn.execute(
        "SELECT hash, size, mtime, refreshed FROM hash_cache "
        "WHERE path=? AND algorithm=?",
        (path, algo),
    ).fetchone()
    if not row:
        cache_conn.close()
        state_conn.close()
        out.status = "unknown"
        out.warnings.append(f"path '{path}' not in {side} cache")
        return out
    out.found_in_cache = True
    out.hash, out.size, _mtime, out.last_hashed = row[0], row[1], row[2], row[3]

    # Find matches on the OTHER side
    other_side = "dst" if side == "src" else "src"
    other_conn = _open_local_cache_for_side(cfg, job, other_side)
    if other_conn is None:
        out.warnings.append(
            f"other side ({other_side}) is remote / no local cache; "
            "matches reflect what's been hashed during past runs only"
        )
        # Fallback: derive matches from file_events for this hash
        # (won't be definitive for never-hashed remote files)
    else:
        match_rows = other_conn.execute(
            "SELECT path, hash, size, refreshed FROM hash_cache "
            "WHERE algorithm=? AND hash=?",
            (algo, out.hash),
        ).fetchall()
        for mr in match_rows:
            out.matches.append(FileMatch(
                side=other_side, path=mr[0], hash=mr[1],
                size=mr[2], last_hashed=mr[3],
            ))
        other_conn.close()

    out.events = state.query_file_events(
        state_conn, side=side, path=path, limit=50,
    )

    if out.side == "src":
        if out.matches:
            out.status = "backed_up"
        elif _was_matched_in_latest_check(state_conn, side, path):
            # Other side is a remote with no local cache, but the most
            # recent completed check covered this file (it would appear
            # in file_events as 'missing' otherwise → it's actually
            # backed up server-side, just not in our local index).
            out.status = "backed_up"
            out.warnings.append(
                "no local match found, but latest completed check did not "
                "flag this file as missing — inferred backed_up "
                "(remote side has no local cache to confirm directly)"
            )
        else:
            out.status = "missing"
    else:
        out.status = "backed_up" if out.matches else "orphan"

    cache_conn.close()
    state_conn.close()
    return out


def _was_matched_in_latest_check(state_conn, side: str, path: str) -> bool:
    """True iff the most recent COMPLETED check (ok OR fail — but not crashed)
    exists AND this (side, path) does NOT appear in file_events for that
    event with outcome='missing'.

    A 'fail' check still produces definitive per-file answers: files not in
    the missing list ran through hash comparison and matched. Only 'crashed'
    runs give no per-file information at all.

    Because matched files are intentionally NOT recorded (saves rows),
    absence of a 'missing' row in a completed check is equivalent to
    "matched."
    """
    row = state_conn.execute(
        "SELECT id FROM events WHERE op='check' "
        "AND result IN ('ok', 'fail') "
        "ORDER BY started_ts DESC LIMIT 1"
    ).fetchone()
    if not row:
        return False
    last_check_id = row[0]
    miss = state_conn.execute(
        "SELECT 1 FROM file_events "
        "WHERE event_id=? AND side=? AND path=? AND outcome='missing'",
        (last_check_id, side, path),
    ).fetchone()
    return miss is None


def list_status(
    cfg: Config, job: Job,
    *, side: str = "src", filter_kind: Optional[str] = None,
) -> List[FileStatus]:
    """Return statuses for every file on `side`.

    `filter_kind`: 'missing' | 'backed_up' | 'orphan' | None (all).
    """
    state_dir = cfg.state_dir_for(job)
    state_conn = state.open_db(state_dir)
    algo = _algorithm_for_job(state_conn)

    out: List[FileStatus] = []
    if algo is None:
        state_conn.close()
        return out

    cache_conn = _open_local_cache_for_side(cfg, job, side)
    other_conn = _open_local_cache_for_side(cfg, job, "dst" if side == "src" else "src")

    other_hash_index: Dict[str, List[tuple]] = {}
    if other_conn is not None:
        rows = other_conn.execute(
            "SELECT path, hash, size, refreshed FROM hash_cache "
            "WHERE algorithm=?",
            (algo,),
        ).fetchall()
        for p, h, sz, r in rows:
            other_hash_index.setdefault(h, []).append((p, sz, r))

    if cache_conn is None:
        state_conn.close()
        return out

    # Pre-fetch latest COMPLETED check's missing set (ok or fail; not crashed).
    # See _was_matched_in_latest_check for rationale.
    last_check = state_conn.execute(
        "SELECT id FROM events WHERE op='check' "
        "AND result IN ('ok', 'fail') "
        "ORDER BY started_ts DESC LIMIT 1"
    ).fetchone()
    missing_in_last_check: set = set()
    if last_check:
        missing_in_last_check = {
            row[0] for row in state_conn.execute(
                "SELECT path FROM file_events WHERE event_id=? AND side=? "
                "AND outcome='missing'",
                (last_check[0], side),
            ).fetchall()
        }

    for row in cache_conn.execute(
        "SELECT path, hash, size, refreshed FROM hash_cache "
        "WHERE algorithm=?", (algo,),
    ).fetchall():
        p, h, sz, r = row
        st = FileStatus(
            side=side, path=p, found_in_cache=True,
            hash=h, algorithm=algo, size=sz, last_hashed=r,
        )
        for op_, sz2, r2 in other_hash_index.get(h, []):
            other_side_name = "dst" if side == "src" else "src"
            st.matches.append(FileMatch(
                side=other_side_name, path=op_, hash=h, size=sz2, last_hashed=r2,
            ))
        if side == "src":
            if st.matches:
                st.status = "backed_up"
            elif last_check and p not in missing_in_last_check:
                st.status = "backed_up"
            else:
                st.status = "missing"
        else:
            st.status = "backed_up" if st.matches else "orphan"
        if filter_kind is None or st.status == filter_kind:
            out.append(st)

    cache_conn.close()
    if other_conn is not None:
        other_conn.close()
    state_conn.close()
    return out


def find_by_hash(cfg: Config, job: Job, *, hash: str) -> List[FileMatch]:
    """Return all (side, path) tuples in any cache that match the given hash."""
    out: List[FileMatch] = []
    state_dir = cfg.state_dir_for(job)
    state_conn = state.open_db(state_dir)
    algo = _algorithm_for_job(state_conn)
    state_conn.close()
    if algo is None:
        return out
    for s in ("src", "dst"):
        cc = _open_local_cache_for_side(cfg, job, s)
        if cc is None:
            continue
        for row in cc.execute(
            "SELECT path, size, refreshed FROM hash_cache "
            "WHERE algorithm=? AND hash=?",
            (algo, hash),
        ).fetchall():
            out.append(FileMatch(
                side=s, path=row[0], hash=hash, size=row[1], last_hashed=row[2],
            ))
        cc.close()
    return out


def status_to_dict(s: FileStatus) -> dict:
    """JSON-friendly conversion."""
    return {
        "side": s.side,
        "path": s.path,
        "status": s.status,
        "hash": s.hash,
        "algorithm": s.algorithm,
        "size": s.size,
        "last_hashed": s.last_hashed,
        "matches": [asdict(m) for m in s.matches],
        "events": s.events,
        "warnings": s.warnings,
        "found_in_cache": s.found_in_cache,
    }
