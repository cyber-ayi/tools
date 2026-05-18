"""Manifest abstraction: uniform read of (path, hash, size) per side.

Three backend strategies:
  1. Local fs: persisted SQLite cache at <root>/.rmig-cache.db, refreshed
     against current size+mtime, missing files re-hashed in parallel.
  2. Remote with native hash support: live `rclone hashsum` (no caching needed
     — backend metadata is the source of truth; caching would risk staleness).
  3. Remote without native hash for the negotiated algorithm: persisted cache
     in central state.db `remote_hash_cache` table (best-effort, mtime-based
     invalidation).
"""
from __future__ import annotations

import contextlib
import hashlib
import os
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Dict, Iterable, Iterator, List, Optional, Set, Tuple

from . import cache, hashing, rclone, state
from . import progress as progress_mod
from . import verbose as verbose_mod
from .config import Job


@dataclass
class Entry:
    path: str   # relative to the side's root
    hash: str   # lowercase hex
    size: int


@dataclass
class RefreshStats:
    valid: int = 0
    stale: int = 0
    new: int = 0
    removed: int = 0
    rehashed: int = 0


class Manifest:
    """In-memory manifest for one side (src or dst), produced by refresh()."""

    def __init__(self, side: str, root: str, algorithm: str):
        assert side in ("src", "dst")
        self.side = side
        self.root = root
        self.algorithm = algorithm
        self.entries: List[Entry] = []
        self.stats = RefreshStats()

    def by_path(self) -> Dict[str, Entry]:
        return {e.path: e for e in self.entries}

    def hash_set(self) -> Set[str]:
        return {e.hash for e in self.entries}

    def unique_by_hash(self) -> List[Entry]:
        """One representative entry per unique hash (first by path)."""
        seen: Set[str] = set()
        out: List[Entry] = []
        for e in sorted(self.entries, key=lambda x: x.path):
            if e.hash not in seen:
                seen.add(e.hash)
                out.append(e)
        return out

    def signature(self) -> str:
        """Stable SHA-256 of all (path, hash) pairs — used as check_signature."""
        items = sorted((e.path, e.hash) for e in self.entries)
        h = hashlib.sha256()
        for p, hh in items:
            h.update(p.encode("utf-8"))
            h.update(b"\0")
            h.update(hh.encode("ascii"))
            h.update(b"\n")
        return h.hexdigest()


# ----------------- Refresh strategies -----------------

def refresh(
    side: str,
    job: Job,
    algorithm: str,
    state_conn: sqlite3.Connection,
    state_dir: Path,
    *,
    transfers: int = 8,
    download: bool = False,
    full: bool = False,
    local_cache_in_root: bool = True,
    progress: bool = True,
    size_filter: Optional[Set[int]] = None,
    v: Optional["verbose_mod.Verbose"] = None,
) -> Manifest:
    """Build an in-memory Manifest for `side`.

    `size_filter`: when supplied, only files whose `st.st_size` is in this set
    are hashed and included in the returned manifest. Useful for `check` where
    dst >> src in unrelated bytes and we only need hashes for dst files whose
    size could match a src file. Cache entries for excluded files are left
    untouched.
    """
    if v is None:
        v = verbose_mod.default()
    root = job.src if side == "src" else job.dst
    if rclone.is_local(root):
        return _refresh_local(
            side, root, algorithm,
            transfers=transfers, full=full,
            local_cache_in_root=local_cache_in_root,
            fallback_dir=state_dir / "local-cache",
            progress=progress,
            size_filter=size_filter,
            v=v,
        )
    # Remote: unified path with checkpointed cache.
    # Native-hash backends (SFTP, S3, B2, ...) skip --download; non-native
    # ones (Dropbox via content_hash, GDrive paths w/o md5, ...) need
    # download=True. Both sides cache results in state.db.remote_hash_cache
    # so partial work survives a kill.
    native = algorithm in hashing.supported_hashes(root)
    if v.is_detail():
        v.detail(f"  remote backend native hashes for {root}: "
                 f"{sorted(hashing.supported_hashes(root))}")
        v.detail(f"  → {'native' if native else 'no native'} support for {algorithm}; "
                 f"{'no --download' if native else 'using --download'}")
    return _refresh_remote(
        side, root, algorithm,
        state_conn=state_conn,
        download=download or not native,
        full=full,
        progress=progress,
        transfers=transfers,
        size_filter=size_filter,
        v=v,
    )


def _refresh_local(
    side: str,
    root: str,
    algorithm: str,
    *,
    transfers: int,
    full: bool,
    local_cache_in_root: bool,
    fallback_dir: Path,
    progress: bool,
    size_filter: Optional[Set[int]] = None,
    v: Optional["verbose_mod.Verbose"] = None,
) -> Manifest:
    if v is None:
        v = verbose_mod.default()
    root_path = Path(os.path.expanduser(root))
    if not root_path.exists():
        raise FileNotFoundError(f"local root does not exist: {root_path}")

    # Decide cache location
    if local_cache_in_root:
        db_path = cache.cache_path_for_root(root_path, fallback_dir=fallback_dir)
    else:
        db_path = cache.cache_path_for_root(
            Path("/__force_fallback__"), fallback_dir=fallback_dir
        )
        # Actually compute against the real root for fallback naming
        fallback_dir.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha1(str(root_path.resolve()).encode("utf-8")).hexdigest()[:16]
        db_path = fallback_dir / f"cache-{digest}.db"

    # Walk directory. Skip our own sidecar dirs (.rmig-cache.db family +
    # ascmhl/ when MHL emit is on) so they don't pollute the manifest or
    # become candidates for hashing.
    current: Dict[str, Tuple[int, float]] = {}
    for dirpath, dirnames, filenames in os.walk(root_path):
        # Prune ascmhl/ at any depth (matches MHL spec's default ignore).
        dirnames[:] = [d for d in dirnames if d != "ascmhl"]
        for fn in filenames:
            if fn.startswith(cache.CACHE_FILENAME):
                continue
            full_path = Path(dirpath) / fn
            try:
                st = full_path.stat()
            except FileNotFoundError:
                continue
            rel = str(full_path.relative_to(root_path))
            current[rel] = (st.st_size, st.st_mtime)

    # Apply size filter (verify-mode optimization): drop files whose size
    # can't possibly match anything we care about. Cache entries for these
    # are NOT touched — the filter is read-only with respect to cache state.
    skipped_by_size = 0
    if size_filter is not None:
        before = len(current)
        current = {p: sm for p, sm in current.items() if sm[0] in size_filter}
        skipped_by_size = before - len(current)

    conn = cache.open_db(db_path)
    cached = cache.load_for_algorithm(conn, algorithm)
    # When filtering, restrict the cached map too so unrelated cached entries
    # don't show up as "removed" (they're still on disk, just out of scope).
    if size_filter is not None:
        cached = {p: e for p, e in cached.items() if p in current}
    diff = cache.diff_against_filesystem(cached, current, full=full)

    if progress:
        extra = f" filtered_out={skipped_by_size}" if size_filter is not None else ""
        v.info(
            f"[{side}] cache @ {db_path}: "
            f"valid={len(diff.valid)} stale={len(diff.stale)} "
            f"new={len(diff.new)} removed={len(diff.removed)}{extra}"
        )
    # Detail: explain stale reasons when requested
    if v.is_detail() and diff.stale:
        for path in diff.stale[:10]:
            cached_e = cached[path]
            cur_size, cur_mtime = current[path]
            reasons = []
            if cached_e.size != cur_size:
                reasons.append(f"size {cached_e.size}→{cur_size}")
            if abs(cached_e.mtime - cur_mtime) >= 0.01:
                reasons.append(f"mtime {cached_e.mtime}→{cur_mtime}")
            v.detail(f"  stale: {path}  ({', '.join(reasons) or 'unknown'})")
        if len(diff.stale) > 10:
            v.detail(f"  ... and {len(diff.stale) - 10} more stale entries")

    # Re-hash stale + new. Resilient to per-file errors (returned as
    # exceptions, logged, and skipped) and persists partial progress to the
    # cache every FLUSH_EVERY successful entries — so a network blip
    # midway through doesn't waste already-completed work.
    FLUSH_EVERY = 25
    to_hash = list(diff.stale) + list(diff.new)
    new_entries: List[cache.CacheEntry] = []
    pending_flush: List[cache.CacheEntry] = []
    failures: List[Tuple[str, str]] = []
    new_lock = Lock()

    # Only hashlib algos stream bytes through hash_file_local's progress_cb;
    # exotic algos (xxh3, crc32, ...) fall back to a per-file `rclone
    # hashsum` subprocess with no chunk callback. For those, drive the meter
    # at file granularity with a wall-clock average instead of a windowed
    # EMA that would otherwise sit at 0 B / -- the whole run.
    streamed = algorithm in hashing.HASHLIB_SUPPORTED
    meter = progress_mod.ProgressMeter(
        v, f"[{side}] hash",
        total_files=len(to_hash),
        total_bytes=sum(current[p][0] for p in to_hash if p in current),
        cumulative=not streamed,
    )

    def hash_one(rel: str) -> None:
        full_path = root_path / rel
        t0 = time.time()
        try:
            st = full_path.stat()
            meter.set_current(rel)
            h = hashing.hash_file_local(
                str(full_path), algorithm,
                progress_cb=(meter.add_processed if streamed else None),
            )
        except (OSError, IOError) as e:
            with new_lock:
                failures.append((rel, repr(e)))
            v.detail(f"    FAIL {rel}: {e}")
            meter.file_done(ok=False)
            return
        with new_lock:
            entry = cache.CacheEntry(
                path=rel, hash=h, algorithm=algorithm,
                size=st.st_size, mtime=st.st_mtime,
            )
            new_entries.append(entry)
            pending_flush.append(entry)
        meter.file_done(committed_size=None if streamed else st.st_size)
        v.detail(f"    {rel}  {h}  ({time.time() - t0:.1f}s, {st.st_size:,}B)")

    if to_hash:
        if progress:
            v.info(f"[{side}] hashing {len(to_hash)} files with {transfers} threads...")
        with (meter if progress else contextlib.nullcontext()):
            with ThreadPoolExecutor(max_workers=transfers) as pool:
                futs = [pool.submit(hash_one, p) for p in to_hash]
                for fu in as_completed(futs):
                    fu.result()  # hash_one swallows file errors; this raises only on bugs
                    # Periodic incremental flush so a later failure doesn't lose work
                    with new_lock:
                        if len(pending_flush) >= FLUSH_EVERY:
                            cache.upsert_many(conn, pending_flush, refreshed=time.time())
                            v.debug(f"    [flush] {len(pending_flush)} rows → {db_path.name}")
                            pending_flush.clear()

    now = time.time()
    cache.upsert_many(conn, pending_flush, refreshed=now)
    cache.delete_paths(conn, diff.removed)
    if failures and progress:
        v.warn(f"[{side}] {len(failures)} files failed to hash:")
        for rel, err in failures[:10]:
            v.warn(f"    {rel}: {err}")
        if len(failures) > 10:
            v.warn(f"    ... and {len(failures) - 10} more")

    # Build manifest from valid + new
    m = Manifest(side, root, algorithm)
    m.stats = RefreshStats(
        valid=len(diff.valid), stale=len(diff.stale),
        new=len(diff.new), removed=len(diff.removed),
        rehashed=len(new_entries),
    )
    for path, entry in diff.valid.items():
        size, _mtime = current[path]
        m.entries.append(Entry(path=path, hash=entry.hash, size=size))
    for ne in new_entries:
        m.entries.append(Entry(path=ne.path, hash=ne.hash, size=ne.size))

    conn.close()
    return m


def _refresh_remote(
    side: str,
    root: str,
    algorithm: str,
    *,
    state_conn: sqlite3.Connection,
    download: bool,
    full: bool,
    progress: bool,
    transfers: int = 4,
    size_filter: Optional[Set[int]] = None,
    v: Optional["verbose_mod.Verbose"] = None,
) -> Manifest:
    if v is None:
        v = verbose_mod.default()
    """Unified remote-side hash refresh with checkpointed cache.

    Strategy:
      1. `lsf` the remote → (path, size, mtime) for every file.
      2. Apply size_filter if set (verify-mode: dst >> src).
      3. Load `state.db.remote_hash_cache` rows; classify each path as
         valid (size+mtime match), stale (file exists but changed),
         new (not cached), or removed (cached but no longer present).
      4. For everything that needs (re)hashing:
           - WITHOUT size_filter (typical for src): use streaming bulk
             `rclone hashsum REMOTE`. Each line yielded by rclone is
             persisted to `remote_hash_cache` immediately. A SIGKILL
             of Python after the Nth line keeps N hashes on disk.
           - WITH size_filter (typical for dst): per-file
             `rclone hashsum_file` in a ThreadPoolExecutor. Each result
             is committed in batches of FLUSH_EVERY for the same reason.
      5. Build the in-memory Manifest from cache + freshly-hashed.

    `download=True` forces `--download` on the rclone hashsum invocations,
    which means dragging file bytes back to compute the hash locally.
    Used when a backend doesn't natively expose the negotiated algorithm.
    """
    listing = rclone.lsf(root)
    current: Dict[str, Tuple[int, Optional[float]]] = {
        e.path: (e.size, e.mtime) for e in listing
    }
    total_listed = len(current)
    if size_filter is not None:
        current = {p: sm for p, sm in current.items() if sm[0] in size_filter}

    cached_all = state.rhc_load(state_conn, side, algorithm)
    if size_filter is not None:
        cached = {p: e for p, e in cached_all.items() if p in current}
    else:
        cached = cached_all

    valid: Dict[str, state.RemoteCacheEntry] = {}
    stale: List[str] = []
    removed: List[str] = []
    for p, ent in cached.items():
        if p not in current:
            removed.append(p)
            continue
        size, mtime = current[p]
        match = ent.size == size and (
            ent.modtime is None or mtime is None
            or abs((ent.modtime or 0) - (mtime or 0)) < 1.0
        )
        if not full and match:
            valid[p] = ent
        else:
            stale.append(p)
    new = [p for p in current if p not in cached]

    if progress:
        extra = (
            f" filtered_out={total_listed - len(current)}"
            if size_filter is not None else ""
        )
        v.info(
            f"[{side}] remote cache: valid={len(valid)} stale={len(stale)} "
            f"new={len(new)} removed={len(removed)}{extra}"
        )
    if v.is_detail() and stale:
        for path in stale[:10]:
            ce = cached[path]
            cs, cm = current[path]
            reasons = []
            if ce.size != cs:
                reasons.append(f"size {ce.size}→{cs}")
            cmt = cm or 0
            ot = ce.modtime or 0
            if abs(cmt - ot) >= 1.0:
                reasons.append(f"mtime {ot}→{cmt}")
            v.detail(f"  stale: {path}  ({', '.join(reasons) or 'unknown'})")
        if len(stale) > 10:
            v.detail(f"  ... and {len(stale) - 10} more stale entries")

    to_hash = list(stale) + list(new)
    fresh: List[state.RemoteCacheEntry] = []

    FLUSH_EVERY = 25

    def _flush(items: List[state.RemoteCacheEntry]) -> None:
        if items:
            state.rhc_upsert(state_conn, items, refreshed=time.time())
            v.debug(f"    [flush] {len(items)} rows → remote_hash_cache")
            items.clear()

    if to_hash:
        if size_filter is not None:
            # Per-file path: parallel rclone hashsum_file with checkpointing.
            if progress:
                action = "downloading + hashing" if download else "remote-hashing"
                v.info(
                    f"[{side}] {action} {len(to_hash)} files "
                    f"({transfers} threads)"
                )
            pending: List[state.RemoteCacheEntry] = []
            fails: List[Tuple[str, str]] = []
            rlock = Lock()
            meter = progress_mod.ProgressMeter(
                v, f"[{side}] remote-hash",
                total_files=len(to_hash),
                total_bytes=sum(
                    current[p][0] for p in to_hash if p in current
                ),
                cumulative=True,  # bytes land per-file, not streamed
            )

            def hash_one(rel: str) -> None:
                full_path = (
                    f"{root}{rel}" if root.endswith("/")
                    else f"{root}/{rel}"
                )
                t0 = time.time()
                meter.set_current(rel)
                v.detail(f"    rclone hashsum {algorithm} {full_path}"
                         f"{' --download' if download else ''}")
                try:
                    h = rclone.hashsum_file(algorithm, full_path,
                                            download=download)
                except Exception as e:
                    with rlock:
                        fails.append((rel, repr(e)))
                    v.detail(f"    FAIL {rel}: {e}")
                    meter.file_done(ok=False)
                    return
                if h is None:
                    with rlock:
                        fails.append((rel, "hashsum returned None"))
                    v.detail(f"    FAIL {rel}: hashsum returned None")
                    meter.file_done(ok=False)
                    return
                size, mtime = current[rel]
                entry = state.RemoteCacheEntry(
                    side=side, path=rel, algorithm=algorithm,
                    hash=h, size=size, modtime=mtime,
                )
                with rlock:
                    pending.append(entry)
                    fresh.append(entry)
                    if len(pending) >= FLUSH_EVERY:
                        _flush(pending)
                meter.file_done(committed_size=size)
                v.detail(f"    {rel}  {h}  ({time.time() - t0:.1f}s)")

            with (meter if progress else contextlib.nullcontext()):
                with ThreadPoolExecutor(max_workers=transfers) as pool:
                    futs = [pool.submit(hash_one, p) for p in to_hash]
                    for fu in as_completed(futs):
                        fu.result()
            with rlock:
                _flush(pending)

            if fails and progress:
                v.warn(f"[{side}] {len(fails)} files failed to hash:")
                for rel, err in fails[:10]:
                    v.warn(f"    {rel}: {err}")
                if len(fails) > 10:
                    v.warn(f"    ... and {len(fails) - 10} more")
        else:
            # Bulk streaming path: one rclone subprocess walks the whole
            # tree, we read its stdout line-by-line and persist each hash
            # as it arrives. If `to_hash` doesn't include everything (some
            # files have valid cache rows already), we still bulk-hash and
            # accept some redundant work — bulk is much more efficient
            # than N per-file SSH calls when N is large.
            if progress:
                action = "downloading + hashing" if download else "remote-hashing"
                v.info(
                    f"[{side}] {action} bulk (streaming, "
                    f"{len(to_hash)} need refresh)"
                )
            v.detail(
                f"    rclone hashsum {algorithm} {root}"
                f"{' --download' if download else ''}  (streaming)"
            )
            pending: List[state.RemoteCacheEntry] = []
            meter = progress_mod.ProgressMeter(
                v, f"[{side}] remote-hash (stream)",
                total_files=len(current) or None,
                total_bytes=sum(
                    s for s, _ in current.values() if s and s > 0
                ),
                cumulative=True,
            )
            ctx = meter if progress else contextlib.nullcontext()
            with ctx:
                try:
                    for h, p in rclone.hashsum_streaming(
                        algorithm, root, download=download,
                    ):
                        if p not in current:
                            # File listed by hashsum but not by our prior lsf
                            # (race window) — store with what we have
                            size, mtime = -1, None
                        else:
                            size, mtime = current[p]
                        entry = state.RemoteCacheEntry(
                            side=side, path=p, algorithm=algorithm,
                            hash=h, size=size, modtime=mtime,
                        )
                        pending.append(entry)
                        fresh.append(entry)
                        meter.set_current(p)
                        meter.file_done(
                            committed_size=size if size >= 0 else None
                        )
                        v.detail(f"    {p}  {h}")
                        if len(pending) >= FLUSH_EVERY:
                            _flush(pending)
                finally:
                    _flush(pending)

    state.rhc_delete(state_conn, side, removed, algorithm)

    # Build manifest from cache (valid) + just-hashed (fresh).
    m = Manifest(side, root, algorithm)
    fresh_paths = {e.path for e in fresh}
    for p, e in valid.items():
        if p in fresh_paths:
            continue   # bulk re-hash may have re-covered this
        m.entries.append(Entry(path=p, hash=e.hash, size=e.size))
    for e in fresh:
        m.entries.append(Entry(path=e.path, hash=e.hash, size=e.size))

    m.stats = RefreshStats(
        valid=len(valid), stale=len(stale),
        new=len(new), removed=len(removed),
        rehashed=len(fresh),
    )
    return m


def append_to_local_cache(
    job: Job,
    side: str,
    new_paths: Iterable[str],
    algorithm: str,
    state_dir: Path,
    *,
    local_cache_in_root: bool = True,
) -> None:
    """Incrementally upsert newly-copied dest files into the local cache.

    Used by `rmig-copy` after successful copy so the next run sees them as
    cached without a full re-walk.
    """
    root = job.src if side == "src" else job.dst
    if not rclone.is_local(root):
        return  # nothing to do for live/remote-cached strategies
    root_path = Path(os.path.expanduser(root))
    fallback = state_dir / "local-cache"
    if local_cache_in_root:
        db_path = cache.cache_path_for_root(root_path, fallback_dir=fallback)
    else:
        fallback.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha1(str(root_path.resolve()).encode("utf-8")).hexdigest()[:16]
        db_path = fallback / f"cache-{digest}.db"

    conn = cache.open_db(db_path)
    entries: List[cache.CacheEntry] = []
    for rel in new_paths:
        fp = root_path / rel
        try:
            st = fp.stat()
        except FileNotFoundError:
            continue
        h = hashing.hash_file_local(str(fp), algorithm)
        entries.append(cache.CacheEntry(
            path=rel, hash=h, algorithm=algorithm,
            size=st.st_size, mtime=st.st_mtime,
        ))
    cache.upsert_many(conn, entries, refreshed=time.time())
    conn.close()
