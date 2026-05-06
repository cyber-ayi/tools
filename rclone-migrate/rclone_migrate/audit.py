"""Audit context manager.

Wraps each top-level rmig operation so that:

  with audit.run(state_dir, op='check') as ev:
      ev.set_result('ok')
      ev.set_counts(src=62, dst=62, affected=0)
      ev.record_file('src', 'foo.bin', outcome='missing', hash='deadbeef')

does the following automatically:

  1. Acquire an exclusive fcntl lock on <state_dir>/job.lock so two rmig
     mutating ops can't trample each other's events / cache. Read-only
     commands (log, file-status, list-jobs) skip this.
  2. Detect orphans (previous runs that started but never finished — i.e.
     crashed or killed). Mark them result='crashed' and warn on stderr.
  3. INSERT a new event row with started_ts, op, log_path, pid, hostname.
  4. Tee stdout (and stderr) into <state_dir>/runs/<ISO-ts>-<op>-<pid>.log
     so the captured transcript survives the process exit.
  5. On normal exit: UPDATE the event row with ended_ts, result, counts,
     algo, signature, notes.
  6. On exception: UPDATE result='fail' (or whatever was set) and re-raise
     so the original traceback isn't masked.
  7. Release the lock (auto on FD close, even after SIGKILL).
"""
from __future__ import annotations

import contextlib
import errno
import fcntl
import io
import json
import os
import socket
import sys
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, TextIO

from . import state as state_mod
from . import verbose as verbose_mod


class LockContention(RuntimeError):
    """Raised when another rmig run holds the job lock."""
    def __init__(self, holder_pid: Optional[int], holder_started: Optional[str],
                 holder_op: Optional[str], lock_path: Path):
        self.holder_pid = holder_pid
        self.holder_started = holder_started
        self.holder_op = holder_op
        self.lock_path = lock_path
        msg = (
            f"another rmig run holds the job lock (pid={holder_pid}, "
            f"op={holder_op}, started={holder_started}); refusing.\n"
            f"  lock file: {lock_path}\n"
            "If the prior run crashed without releasing the lock, the OS "
            "should have done it automatically — this message means a real "
            "live process is still running. Wait or `kill` it explicitly."
        )
        super().__init__(msg)


def _acquire_job_lock(state_dir: Path, op: str):
    """Acquire an exclusive non-blocking flock on <state_dir>/job.lock.

    Returns an open file handle that owns the lock. Caller must keep the
    handle alive for the duration of the lock; closing it releases.
    The lock is auto-released by the OS on process death (even SIGKILL).

    Writes {pid, op, started} JSON into the file as a diagnostic for
    contention messages.
    """
    state_dir.mkdir(parents=True, exist_ok=True)
    lock_path = state_dir / "job.lock"
    # Open RW so we can read prior holder info on contention
    fh = open(lock_path, "a+", encoding="utf-8")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError) as e:
        if isinstance(e, OSError) and e.errno not in (errno.EAGAIN, errno.EACCES):
            fh.close()
            raise
        # Read holder info before failing
        fh.seek(0)
        body = fh.read().strip()
        fh.close()
        holder_pid: Optional[int] = None
        holder_started: Optional[str] = None
        holder_op: Optional[str] = None
        try:
            info = json.loads(body) if body else {}
            holder_pid = info.get("pid")
            holder_started = info.get("started")
            holder_op = info.get("op")
        except (json.JSONDecodeError, ValueError):
            pass
        raise LockContention(holder_pid, holder_started, holder_op, lock_path)

    # We hold the lock; replace contents with our identity for diagnostics
    fh.seek(0)
    fh.truncate()
    fh.write(json.dumps({
        "pid": os.getpid(),
        "op": op,
        "started": datetime.now().isoformat(timespec="seconds"),
        "hostname": socket.gethostname(),
    }))
    fh.flush()
    return fh


def _now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H-%M-%S")


class _Tee(io.TextIOBase):
    """Write to two text streams. Used to mirror stdout/stderr to a log file.

    The secondary (log file) gets ANSI color escapes stripped — terminals
    benefit from colors but the persisted log should be plain text so
    `cat`/`grep`/`less` produces clean output.
    """

    def __init__(self, primary: TextIO, secondary: TextIO):
        self._primary = primary
        self._secondary = secondary

    def write(self, s: str) -> int:
        # Log file: stripped of ANSI
        try:
            self._secondary.write(verbose_mod.strip_ansi(s))
            self._secondary.flush()
        except Exception:
            pass
        # Terminal / original stdout: as-is
        return self._primary.write(s)

    def flush(self) -> None:
        try:
            self._primary.flush()
        except Exception:
            pass
        try:
            self._secondary.flush()
        except Exception:
            pass

    def isatty(self) -> bool:
        try:
            return self._primary.isatty()
        except Exception:
            return False

    def fileno(self) -> int:
        return self._primary.fileno()


@dataclass
class AuditEvent:
    event_id: int
    op: str
    log_path: Path
    started_ts: float
    result: str = "ok"
    algo: Optional[str] = None
    signature: Optional[str] = None
    src_count: Optional[int] = None
    dst_count: Optional[int] = None
    affected: Optional[int] = None
    notes: Optional[str] = None
    _file_events: List[Dict] = field(default_factory=list)
    _state_conn: Optional[object] = None  # sqlite3.Connection

    # --- API used by ops.* ---

    def set_result(self, result: str) -> None:
        self.result = result

    def set_algo(self, algo: str) -> None:
        self.algo = algo

    def set_signature(self, signature: str) -> None:
        self.signature = signature

    def set_counts(self, *, src: Optional[int] = None,
                   dst: Optional[int] = None,
                   affected: Optional[int] = None) -> None:
        if src is not None:
            self.src_count = src
        if dst is not None:
            self.dst_count = dst
        if affected is not None:
            self.affected = affected

    def set_notes(self, notes: str) -> None:
        self.notes = notes

    def record_file(self, side: str, path: str, *,
                    outcome: str, hash: Optional[str] = None,
                    detail: Optional[str] = None) -> None:
        """Buffer a per-file event; flushed at context exit."""
        self._file_events.append({
            "side": side, "path": path, "outcome": outcome,
            "hash": hash, "detail": detail,
        })


@contextlib.contextmanager
def run(state_dir: Path, *, op: str,
        warn_orphans: bool = True, capture_stdout: bool = True,
        acquire_lock: bool = True):
    """Context manager wrapping a top-level rmig operation.

    Opens its own short-lived state.db connection for event bookkeeping
    (independent of any conn that ops.py opens for refresh_both etc. —
    SQLite WAL allows concurrent readers/writers on the same file).

    `acquire_lock=False` skips fcntl locking (used for read-only commands).
    """
    lock_fh = _acquire_job_lock(state_dir, op) if acquire_lock else None
    own_conn = state_mod.open_db(state_dir)

    # 1. Orphan detection
    if warn_orphans:
        orphans = state_mod.detect_orphans(own_conn)
        if orphans:
            sys.stderr.write(
                f"WARN: {len(orphans)} previous run(s) didn't complete "
                f"(event ids: {orphans}). Marked as 'crashed'. "
                f"Inspect their log_path for tracebacks.\n"
            )

    # 2. Allocate log file
    runs_dir = state_dir / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    pid = os.getpid()
    log_path = runs_dir / f"{_now_iso()}-{op}-{pid}.log"
    rel_log = log_path.relative_to(state_dir).as_posix()

    # 3. Insert event row
    event_id = state_mod.event_start(
        own_conn, op=op, log_path=rel_log,
        pid=pid, hostname=socket.gethostname(),
    )

    ev = AuditEvent(
        event_id=event_id, op=op, log_path=log_path,
        started_ts=time.time(), _state_conn=own_conn,
    )

    # 4. Tee stdout/stderr to log
    log_fh = open(log_path, "a", encoding="utf-8", buffering=1)
    log_fh.write(f"=== rmig {op} started {datetime.now().isoformat()} pid={pid} ===\n")
    orig_stdout, orig_stderr = sys.stdout, sys.stderr
    if capture_stdout:
        sys.stdout = _Tee(orig_stdout, log_fh)
        sys.stderr = _Tee(orig_stderr, log_fh)

    try:
        yield ev
    except SystemExit as e:
        if ev.result == "ok":
            ev.set_result("fail")
        ev.set_notes(f"SystemExit({e.code})")
        raise
    except BaseException:
        if ev.result == "ok":
            ev.set_result("fail")
        ev.set_notes("exception:\n" + traceback.format_exc())
        raise
    finally:
        if capture_stdout:
            sys.stdout, sys.stderr = orig_stdout, orig_stderr
        log_fh.write(
            f"=== rmig {op} ended {datetime.now().isoformat()} "
            f"result={ev.result} ===\n"
        )
        log_fh.close()

        # Flush file_events through the audit's own connection
        if ev._file_events:
            state_mod.record_file_events_batch(own_conn, event_id, ev._file_events)

        state_mod.event_finish(
            own_conn, event_id,
            result=ev.result, algo=ev.algo, signature=ev.signature,
            src_count=ev.src_count, dst_count=ev.dst_count,
            affected=ev.affected, notes=ev.notes,
        )
        own_conn.close()
        if lock_fh is not None:
            lock_fh.close()  # releases fcntl lock
