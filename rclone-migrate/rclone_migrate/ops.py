"""Three top-level operations: copy, check, delete.

Glue between Manifest (data layer) and rclone (action layer), plus the
state.db meta bookkeeping that gates delete on a successful prior check.
"""
from __future__ import annotations

import contextlib
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

from . import audit, hashing, manifest, mhl, rclone, state
from . import progress as progress_mod
from . import verbose as verbose_mod
from .config import Config, Job


# --- copy intra-file progress (Stage C) ------------------------------------

class _PartialWatch:
    """Stage C: while a single `rclone copyto` runs, poll its on-disk
    partial file so the copy meter advances *within* a large file.

    rclone's local backend streams to `<dst>.<hex>.partial` (size grows
    incrementally — verified) then renames. We sample the largest matching
    `<basename>*.partial` (or the final file) in the dst dir ~2.5×/s and
    feed it to `meter.set_inflight`. Watcher is deliberately best-effort:
    if nothing matches (rclone --inplace, an SMB mount that hides the
    growing size, a missing dir) inflight stays 0 and the meter silently
    behaves exactly like the wall-clock model — no warning, no special
    casing of mount type.
    """

    def __init__(self, dst_full: str, meter, interval: float = 0.4):
        self._dir = os.path.dirname(dst_full) or "."
        self._base = os.path.basename(dst_full)
        self._meter = meter
        self._interval = interval
        self._stop = threading.Event()
        self._t: Optional[threading.Thread] = None

    def __enter__(self) -> "_PartialWatch":
        self._t = threading.Thread(
            target=self._run, name="rmig-partial-watch", daemon=True
        )
        self._t.start()
        return self

    def __exit__(self, *exc) -> bool:
        self._stop.set()
        if self._t is not None:
            self._t.join(timeout=1.5)
        return False

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            best = 0
            try:
                with os.scandir(self._dir) as it:
                    for e in it:
                        n = e.name
                        if n == self._base or (
                            n.startswith(self._base) and n.endswith(".partial")
                        ):
                            try:
                                best = max(
                                    best,
                                    e.stat(follow_symlinks=False).st_size,
                                )
                            except OSError:
                                pass
            except OSError:
                continue  # dir not created yet / transient
            if best:
                self._meter.set_inflight(best)


def _clean_stale_partials(job: Job, to_copy, v) -> int:
    """Stage G: remove `.partial` temp files left by a previously
    interrupted run for the files we're about to (re)copy.

    Safe by construction: do_copy runs inside audit.run which holds an
    exclusive fcntl job lock, so no other rmig copy for this job can be
    running — any `<dst>*.partial` for a to-copy file is therefore an
    orphan from a dead/killed run, not a live transfer. rclone never
    *resumes* a file copy from a `.partial` (it's an atomicity temp, not a
    resume checkpoint; the file is re-copied whole), so removing it loses
    no resumable progress — it only unwedges subsequent runs (#157/#212).
    Only applies when dst is a local path; remote `.partial` is rclone's.
    """
    if not rclone.is_local(job.dst):
        return 0
    # group to-copy basenames by their dst directory; one scandir per dir
    by_dir: dict = {}
    for ent in to_copy:
        dst_full = _join(job.dst, ent.path)
        d = os.path.dirname(dst_full) or "."
        by_dir.setdefault(d, set()).add(os.path.basename(dst_full))
    removed = 0
    for d, bases in by_dir.items():
        try:
            entries = list(os.scandir(d))
        except OSError:
            continue
        for e in entries:
            n = e.name
            if not n.endswith(".partial"):
                continue
            if any(n == b + ".partial" or n.startswith(b + ".")
                   for b in bases):
                try:
                    os.remove(e.path)
                    removed += 1
                    v.detail(f"  [copy] removed stale partial: {n}")
                except OSError as ex:
                    v.warn(f"  [copy] could not remove {n}: {ex}")
    if removed:
        v.info(f"[copy] cleared {removed} stale .partial "
               f"(orphaned by a prior interrupted run)")
    return removed


# --- helpers ---------------------------------------------------------------

def _join(root: str, rel: str) -> str:
    """Join a remote-or-local root with a relative path."""
    if root.endswith("/") or root.endswith(":"):
        return f"{root}{rel}"
    # rclone treats both `local/file` and `remote:path/file` the same way
    return f"{root}/{rel}"


def negotiate_algo(job: Job, cfg: Config) -> str:
    override = job.hash or cfg.defaults.hash
    if override:
        # Single-algo override short-circuits profile resolution; profile
        # warnings are intentionally not surfaced here.
        return hashing.negotiate(job.src, job.dst, override=override)
    priority = cfg.resolve_priority(job)
    return hashing.negotiate(job.src, job.dst, priority=priority)


# --- MHL emission helpers -------------------------------------------------

def emit_mhl_generation(
    cfg: Config,
    job: Job,
    side: str,
    *,
    entries: Iterable[manifest.Entry],
    algorithm: str,
    process: str,
    action: str,
    v: verbose_mod.Verbose,
) -> Optional[Path]:
    """Write one MHL generation if conditions allow.

    Returns the manifest path on success, None if skipped (no entries,
    remote side, non-MHL algo, etc.). Caller is responsible for the
    feature-flag check (`job.resolved_emit_mhl`).
    """
    entries = list(entries)
    if not entries:
        v.detail(f"  [mhl] {side}: no entries; skipping")
        return None
    root = job.src if side == "src" else job.dst
    if not rclone.is_local(root):
        v.warn(
            f"  [mhl] {side} root '{root}' is remote; skipping emit "
            f"(MHL output supports local sides only in this version)"
        )
        return None
    if algorithm not in mhl.MHL_ALGORITHMS:
        v.warn(
            f"  [mhl] negotiated algo '{algorithm}' is not in MHL v2.0 set; "
            f"skipping emit. Pick an MHL-aligned profile (e.g. 'dit') "
            f"or include sha1/md5 in your priority."
        )
        return None
    root_path = Path(os.path.expanduser(root))
    name, email = mhl.parse_author(job.resolved_mhl_author(cfg.defaults))
    creator = mhl.CreatorInfo.default(
        author_name=name,
        author_email=email,
        author_phone=job.resolved_mhl_author_phone(cfg.defaults),
        author_role=job.resolved_mhl_author_role(cfg.defaults),
        location=job.resolved_mhl_location(cfg.defaults),
        comment=job.resolved_mhl_comment(cfg.defaults),
    )
    h_entries = [
        mhl.HashEntry(
            path=e.path,
            size=e.size,
            hashes={algorithm: e.hash},
            actions={algorithm: action},
        )
        for e in entries
    ]
    gen = mhl.Generation(
        sequencenr=mhl.next_sequencenr(root_path),
        process=process,
        creator=creator,
        entries=h_entries,
    )
    p = mhl.write_generation(root_path, gen)
    v.ok(f"  [mhl] {side} gen #{gen.sequencenr:04d} → {p.relative_to(root_path)}")
    return p


def _allowed_sides(job: Job, cfg: Config, default_sides: List[str]) -> List[str]:
    """Apply user-configured `mhl_sides` filter to per-op default sides."""
    configured = job.resolved_mhl_sides(cfg.defaults)
    if not configured:
        return default_sides
    return [s for s in default_sides if s in configured]


def _open_state(cfg: Config, job: Job) -> Tuple[sqlite3.Connection, Path]:
    state_dir = cfg.state_dir_for(job)
    state_dir.mkdir(parents=True, exist_ok=True)
    return state.open_db(state_dir), state_dir


def refresh_both(
    cfg: Config, job: Job, *,
    full: bool = False, progress: bool = True,
    filter_dst_by_src_size: bool = True,
    v: Optional[verbose_mod.Verbose] = None,
) -> Tuple[manifest.Manifest, manifest.Manifest, str, sqlite3.Connection, Path]:
    """Refresh src then dst manifests.

    `filter_dst_by_src_size`: when True (default), the dst refresh only
    hashes files whose size matches some src file. This is correct for
    copy/check/delete because a hash collision across non-matching sizes
    is impossible — so dst files outside the src size set can't possibly
    match any src file. Critical when dst >> src (verify a small SD card
    against a large NAS archive).

    Set False when you want a full dst manifest (e.g. for inventory).
    """
    if v is None:
        v = verbose_mod.default()
    conn, state_dir = _open_state(cfg, job)
    algo = negotiate_algo(job, cfg)
    state.meta_set(conn, "hash_algorithm", algo)
    if progress:
        v.info(f"[job={job.name}] hash algorithm = {algo}")
    if v.is_detail():
        v.detail(f"  src backend: {job.src}")
        v.detail(f"  dst backend: {job.dst}")

    with v.phase("refresh src"):
        src_mf = manifest.refresh(
            "src", job, algo, conn, state_dir,
            transfers=job.resolved_transfers(cfg.defaults),
            download=job.resolved_download(cfg.defaults),
            full=full,
            local_cache_in_root=job.resolved_local_cache_in_root(cfg.defaults),
            progress=progress,
            v=v,
        )
    src_size_set = {e.size for e in src_mf.entries} if filter_dst_by_src_size else None
    if progress and src_size_set is not None:
        v.info(f"[refresh] src size set: {len(src_size_set)} unique sizes")

    with v.phase("refresh dst"):
        dst_mf = manifest.refresh(
            "dst", job, algo, conn, state_dir,
            transfers=job.resolved_transfers(cfg.defaults),
            download=job.resolved_download(cfg.defaults),
            full=full,
            local_cache_in_root=job.resolved_local_cache_in_root(cfg.defaults),
            progress=progress,
            size_filter=src_size_set,
            v=v,
        )
    return src_mf, dst_mf, algo, conn, state_dir


# --- operations ------------------------------------------------------------

@dataclass
class CopyPlan:
    to_copy: List[manifest.Entry]   # one representative per missing hash
    src_total: int
    dst_total: int


def plan_copy(src_mf: manifest.Manifest, dst_mf: manifest.Manifest) -> CopyPlan:
    dst_hashes = dst_mf.hash_set()
    to_copy = [e for e in src_mf.unique_by_hash() if e.hash not in dst_hashes]
    return CopyPlan(to_copy=to_copy, src_total=len(src_mf.entries),
                    dst_total=len(dst_mf.entries))


def do_copy(
    cfg: Config,
    job: Job,
    *,
    no_refresh: bool = False,
    full: bool = False,
    dry_run: bool = False,
    clean_partial: bool = True,
    progress: bool = True,
    v: Optional[verbose_mod.Verbose] = None,
) -> int:
    """Returns 0 on success, non-zero if any individual copy failed."""
    if v is None:
        v = verbose_mod.default()
    state_dir = cfg.state_dir_for(job)
    state_dir.mkdir(parents=True, exist_ok=True)
    op_name = "copy-dry" if dry_run else "copy"
    with audit.run(state_dir, op=op_name) as ev:
        src_mf, dst_mf, algo, conn, _state_dir = refresh_both(
            cfg, job, full=full, progress=progress, v=v,
        )
        ev.set_algo(algo)

        plan = plan_copy(src_mf, dst_mf)
        ev.set_counts(src=plan.src_total, dst=plan.dst_total)
        if progress:
            v.info(
                f"\n[copy] src={plan.src_total} dst={plan.dst_total} "
                f"to_copy={len(plan.to_copy)} (algo={algo})"
            )

        if not plan.to_copy:
            state.meta_set(conn, "last_copy_ts", str(time.time()))
            ev.set_counts(affected=0)
            ev.set_result("ok")
            conn.close()
            return 0

        # Stage G: under the job lock, clear partials orphaned by a prior
        # interrupted run so they can't wedge this one (#157/#212). Skip
        # for dry-run and when explicitly opted out.
        if clean_partial and not dry_run:
            _clean_stale_partials(job, plan.to_copy, v)

        failures = 0
        copied_dst_paths: List[str] = []
        transfers = job.resolved_transfers(cfg.defaults)
        extra: List[str] = []
        if job.resolved_download(cfg.defaults):
            extra.append("--download")

        meter = progress_mod.ProgressMeter(
            v, "[copy]",
            total_files=len(plan.to_copy),
            total_bytes=sum(max(e.size, 0) for e in plan.to_copy),
            periodic=False,  # per-file v.info lines below already log progress
            # Stage H: windowed EMA (not cumulative). Stage C's .partial
            # watcher feeds continuous inflight bytes, so processed
            # (committed+inflight) grows smoothly within a large file →
            # real instantaneous speed + ETA mid-file instead of "--"
            # until completion. If the watcher finds nothing (rclone
            # --inplace / a mount that hides the growing size) processed
            # only jumps at file_done → windowed degrades to per-file
            # spikes — acceptable rare fallback, no longer the common path.
        )
        live_copy = progress and not dry_run
        with v.phase(f"copy {len(plan.to_copy)} files"), (
            meter if live_copy else contextlib.nullcontext()
        ):
            for i, ent in enumerate(plan.to_copy, 1):
                src_full = _join(job.src, ent.path)
                dst_full = _join(job.dst, ent.path)
                if dry_run:
                    v.info(f"  DRY [{i}/{len(plan.to_copy)}] {ent.path}  ({ent.hash})")
                    continue
                if progress:
                    v.info(f"  [{i}/{len(plan.to_copy)}] {ent.path}")
                    meter.set_current(ent.path)
                v.detail(f"    rclone copyto {src_full} {dst_full}")
                try:
                    with (_PartialWatch(dst_full, meter)
                          if live_copy else contextlib.nullcontext()):
                        rclone.copyto(
                            src_full, dst_full, algo, transfers=transfers,
                            extra=extra,
                        )
                    copied_dst_paths.append(ent.path)
                    meter.file_done(committed_size=max(ent.size, 0))
                    ev.record_file("dst", ent.path, outcome="copied", hash=ent.hash)
                except rclone.RcloneError as e:
                    failures += 1
                    meter.file_done(ok=False)
                    v.error(f"    FAIL: {e}")
                    ev.record_file("dst", ent.path, outcome="failed",
                                   hash=ent.hash, detail=str(e))

        if not dry_run and copied_dst_paths:
            # Update local-side dst cache so next run sees these without full rescan
            manifest.append_to_local_cache(
                job, "dst", copied_dst_paths, algo, state_dir,
                local_cache_in_root=job.resolved_local_cache_in_root(cfg.defaults),
            )

        ev.set_counts(affected=len(copied_dst_paths))
        if dry_run:
            ev.set_result("ok")
        elif failures == 0:
            ev.set_result("ok")
        elif copied_dst_paths:
            ev.set_result("partial")
        else:
            ev.set_result("fail")

        # MHL emit on dst side after successful (full or partial) copy.
        # Generation is the *delta*: only the files that were just copied.
        # Existing dst files retain their attestation from earlier generations.
        if (not dry_run and copied_dst_paths
                and job.resolved_emit_mhl(cfg.defaults)):
            for side in _allowed_sides(job, cfg, ["dst"]):
                src_by_path = {e.path: e for e in src_mf.entries}
                delta = [
                    src_by_path[p] for p in copied_dst_paths
                    if p in src_by_path
                ]
                emit_mhl_generation(
                    cfg, job, side,
                    entries=delta, algorithm=algo,
                    process="transfer", action="original", v=v,
                )

        state.meta_set(conn, "last_copy_ts", str(time.time()))
        # check_signature is invalidated whenever copy runs (src may have changed)
        state.meta_clear(conn, "check_signature")
        conn.close()
        return 0 if failures == 0 else 1


@dataclass
class CheckResult:
    ok: bool
    missing: List[manifest.Entry]
    src_total: int
    dst_total: int
    signature: Optional[str]


def plan_check(
    src_mf: manifest.Manifest, dst_mf: manifest.Manifest
) -> CheckResult:
    dst_hashes = dst_mf.hash_set()
    missing = [e for e in src_mf.entries if e.hash not in dst_hashes]
    return CheckResult(
        ok=(len(missing) == 0),
        missing=missing,
        src_total=len(src_mf.entries),
        dst_total=len(dst_mf.entries),
        signature=src_mf.signature() if not missing else None,
    )


def do_check(
    cfg: Config,
    job: Job,
    *,
    rehash_all: bool = False,
    progress: bool = True,
    v: Optional[verbose_mod.Verbose] = None,
) -> int:
    if v is None:
        v = verbose_mod.default()
    state_dir = cfg.state_dir_for(job)
    state_dir.mkdir(parents=True, exist_ok=True)
    with audit.run(state_dir, op="check") as ev:
        src_mf, dst_mf, algo, conn, _state_dir = refresh_both(
            cfg, job, full=rehash_all, progress=progress, v=v,
        )
        ev.set_algo(algo)
        result = plan_check(src_mf, dst_mf)
        ev.set_counts(src=result.src_total, dst=result.dst_total,
                      affected=len(result.missing))

        if progress:
            v.info(
                f"\n[check] src={result.src_total} dst={result.dst_total} "
                f"missing={len(result.missing)} (algo={algo})"
            )

        if not result.ok:
            v.info("\nMISSING (src files whose hash is absent from dst):")
            for e in result.missing[:50]:
                v.info(f"  {e.path}   {e.hash}")
                ev.record_file("src", e.path, outcome="missing", hash=e.hash)
            for e in result.missing[50:]:
                ev.record_file("src", e.path, outcome="missing", hash=e.hash)
            if len(result.missing) > 50:
                v.info(f"  ... and {len(result.missing) - 50} more")
            state.meta_clear(conn, "check_signature")
            conn.close()
            ev.set_result("fail")
            return 1

        state.meta_set(conn, "check_signature", result.signature or "")
        state.meta_set(conn, "last_check_ts", str(time.time()))
        v.ok(f"\nOK: signature = {result.signature}")
        ev.set_signature(result.signature)
        ev.set_result("ok")

        # MHL emit on src side: every src file just got verified against dst.
        # process="in-place" + action="verified" — the canonical attestation.
        if job.resolved_emit_mhl(cfg.defaults):
            for side in _allowed_sides(job, cfg, ["src"]):
                emit_mhl_generation(
                    cfg, job, side,
                    entries=src_mf.entries, algorithm=algo,
                    process="in-place", action="verified", v=v,
                )

        conn.close()
        return 0


def do_delete(
    cfg: Config,
    job: Job,
    *,
    confirm: bool = False,
    progress: bool = True,
    v: Optional[verbose_mod.Verbose] = None,
) -> int:
    if v is None:
        v = verbose_mod.default()
    sd = cfg.state_dir_for(job)
    sd.mkdir(parents=True, exist_ok=True)
    op_name = "delete" if confirm else "delete-dry"
    with audit.run(sd, op=op_name) as ev:
        return _do_delete_inner(cfg, job, confirm=confirm, progress=progress,
                                ev=ev, v=v)


def _do_delete_inner(cfg, job, *, confirm, progress, ev, v):
    src_mf, dst_mf, algo, conn, state_dir = refresh_both(
        cfg, job, full=False, progress=progress, v=v,
    )
    ev.set_algo(algo)

    saved_sig = state.meta_get(conn, "check_signature")
    saved_algo = state.meta_get(conn, "hash_algorithm")
    last_check_s = state.meta_get(conn, "last_check_ts")

    if not saved_sig:
        v.error("REFUSE: no check_signature in state. Run rmig-check first.")
        conn.close()
        ev.set_result("refused")
        ev.set_notes("no check_signature in state")
        return 2

    cur_sig = src_mf.signature()
    if cur_sig != saved_sig:
        # Distinguish between "src changed" and "hash algorithm changed".
        algo_hint = ""
        if saved_algo and saved_algo != algo:
            algo_hint = (
                f"\nNote: saved signature was computed with hash={saved_algo}, "
                f"but current run uses hash={algo}. If you switched the hash "
                "algorithm (or dst backend), re-run rmig-check before deleting."
            )
        v.error(
            f"REFUSE: src signature changed since last check.\n"
            f"  saved   = {saved_sig}\n  current = {cur_sig}\n"
            f"Run rmig-check again.{algo_hint}"
        )
        conn.close()
        ev.set_result("refused")
        ev.set_notes(f"signature mismatch: saved={saved_sig} current={cur_sig}")
        return 2

    if last_check_s:
        age = time.time() - float(last_check_s)
        if age > cfg.delete.require_check_within_s:
            v.error(
                f"REFUSE: last check is {age:.0f}s old "
                f"(> {cfg.delete.require_check_within_s:.0f}s). "
                "Run rmig-check again."
            )
            conn.close()
            ev.set_result("refused")
            ev.set_notes(f"check too old: {age:.0f}s")
            return 2

    dst_hashes = dst_mf.hash_set()
    to_delete = [e for e in src_mf.entries if e.hash in dst_hashes]
    ev.set_counts(src=len(src_mf.entries), dst=len(dst_mf.entries))

    if progress:
        v.info(
            f"\n[delete] src={len(src_mf.entries)} "
            f"to_delete={len(to_delete)} (algo={algo})"
        )

    require_confirm = cfg.delete.require_confirm
    if (require_confirm and not confirm):
        v.info("\nDRY-RUN (pass --confirm to actually delete):")
        for e in to_delete[:50]:
            v.info(f"  {e.path}   {e.hash}")
        if len(to_delete) > 50:
            v.info(f"  ... and {len(to_delete) - 50} more")
        conn.close()
        ev.set_counts(affected=0)
        ev.set_result("ok")
        ev.set_notes(f"dry-run: would delete {len(to_delete)} files")
        return 0

    failures = 0
    deleted_paths: List[str] = []
    with v.phase(f"delete {len(to_delete)} files"):
        for i, ent in enumerate(to_delete, 1):
            full = _join(job.src, ent.path)
            if progress:
                v.info(f"  [{i}/{len(to_delete)}] del {ent.path}")
            v.detail(f"    rclone deletefile {full}")
            try:
                rclone.deletefile(full)
                deleted_paths.append(ent.path)
                ev.record_file("src", ent.path, outcome="deleted", hash=ent.hash)
            except rclone.RcloneError as e:
                failures += 1
                v.error(f"    FAIL: {e}")
                ev.record_file("src", ent.path, outcome="failed",
                               hash=ent.hash, detail=str(e))

    # Remove deleted entries from src local cache
    if deleted_paths and rclone.is_local(job.src):
        from . import cache as cache_mod
        root_path = Path(__import__("os").path.expanduser(job.src))
        fallback = state_dir / "local-cache"
        in_root = job.resolved_local_cache_in_root(cfg.defaults)
        if in_root:
            db_path = cache_mod.cache_path_for_root(root_path, fallback_dir=fallback)
        else:
            db_path, _ = cache_mod.resolve_fallback_db(root_path, fallback)
        if db_path.exists():
            cc = cache_mod.open_db(db_path)
            cache_mod.delete_paths(cc, deleted_paths)
            cc.close()

    if cfg.delete.remove_empty_src_dirs:
        try:
            rclone.rmdirs(job.src, leave_root=True)
        except rclone.RcloneError as e:
            print(f"  rmdirs warning: {e}")

    # signature is invalid after delete (src changed)
    state.meta_clear(conn, "check_signature")
    conn.close()
    ev.set_counts(affected=len(deleted_paths))
    if failures == 0:
        ev.set_result("ok")
    elif deleted_paths:
        ev.set_result("partial")
    else:
        ev.set_result("fail")
    return 0 if failures == 0 else 1
