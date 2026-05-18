"""Live progress meter: a once-per-second status line with throughput.

Used by the long-running phases (local/remote hashing, copy) to replace the
old "every N files print a line" cadence with a single carriage-return line
that refreshes ~1Hz and reports files done, bytes done, speed and ETA.

Design constraints (see also audit._Tee, verbose.Verbose):

  * The live `\\r` line is written to ``sys.__stderr__`` — the interpreter's
    *real* fd 2, which ``audit.run`` never reassigns. This keeps the spinner
    out of the persisted audit log (which would otherwise accumulate hundreds
    of duplicated, ANSI-stripped status lines).
  * Live mode is enabled only when that real stderr is a TTY *and* the
    verbosity is exactly NORMAL. At -v/-vv the per-file ``v.detail`` lines
    dominate and a `\\r` line just fights them; under --quiet there is no
    output at all; non-TTY (pipe / redirect) keeps the classic periodic
    full-line behaviour so logs stay readable.
  * The final summary is emitted through ``Verbose`` like every other status
    line, so it follows the same colour/timestamp/log rules.
"""
from __future__ import annotations

import sys
import threading
import time
from typing import Optional, TextIO

from . import verbose as verbose_mod


def _human_bytes(n: float) -> str:
    if n < 0:
        return "?"
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024.0 or unit == "TiB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.2f} {unit}"
        n /= 1024.0
    return f"{n:.2f} TiB"


def _human_rate(bps: float) -> str:
    if bps <= 0:
        return "--"
    return f"{_human_bytes(bps)}/s"


def _human_eta(seconds: float) -> str:
    if seconds < 0 or seconds != seconds or seconds == float("inf"):
        return "--:--"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h:d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


class ProgressMeter:
    """A thread-safe progress accumulator with an optional 1Hz live line.

    Bytes are accumulated as ``committed`` — either streamed in via
    ``add_processed`` (local hashlib/xxhash chunk callbacks) or credited
    per file on ``file_done`` (remote hash, copy). Speed is either a
    windowed EMA of the processed-byte delta between samples, or, when
    ``cumulative=True``, simply committed / wall-elapsed (honest for
    phases whose bytes only land at file-completion granularity).
    """

    _SMOOTH = 0.4  # EMA weight for self-computed speed

    def __init__(
        self,
        v: verbose_mod.Verbose,
        label: str,
        *,
        total_files: Optional[int] = None,
        total_bytes: Optional[int] = None,
        interval: float = 1.0,
        periodic: bool = True,
        cumulative: bool = False,
    ):
        self._v = v
        self._label = label
        self._periodic = periodic
        # cumulative: speed = committed / wall-elapsed. Honest for phases
        # whose bytes only land at file-completion granularity (copy, remote
        # hash). windowed EMA (the default) is better when bytes stream in
        # continuously (local hashlib chunk callbacks).
        self._cumulative = cumulative
        self._total_files = total_files
        self._total_bytes = total_bytes if total_bytes and total_bytes > 0 else None
        self._interval = interval

        self._lock = threading.Lock()
        self._committed = 0
        self._inflight = 0    # current in-flight file bytes (copy .partial
                              # watch); display-only — speed stays committed-
                              # based so it can't be inflated. 0 ⇒ no watch
                              # (graceful degrade to wall-clock behaviour).
        self._files_done = 0
        self._current = ""
        self._active = 0      # files between set_current() and file_done()
        self._failures = 0

        # Multi-worker mode (parallel hashing): one render line per worker
        # slot + the aggregate line. Activated lazily the first time
        # worker_slot() is called; single-stream callers (copy, remote
        # streaming) never touch it and keep the classic one-line render.
        self._multiline = False
        self._slots: dict = {}            # wid -> {path,size,bytes}
        self._slot_of_thread: dict = {}   # thread ident -> wid
        self._nslots = 0                  # max distinct wids seen
        self._max_worker_lines = 12
        self._block_h = 0                 # lines currently drawn in the block

        self._start_t = time.time()
        self._last_sample_t = self._start_t
        self._last_sample_bytes = 0
        self._speed = 0.0

        # Live mode decision. sys.__stderr__ is the genuine terminal even when
        # audit.run has swapped sys.stderr for a Tee.
        term: Optional[TextIO] = sys.__stderr__
        is_tty = False
        try:
            is_tty = bool(term and term.isatty())
        except Exception:
            is_tty = False
        self._term = term
        self.live = is_tty and v.level == verbose_mod.NORMAL
        self._color = self.live and v.color

        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_periodic_t = 0.0
        self._line_len = 0

    # --- lifecycle ---------------------------------------------------------

    def __enter__(self) -> "ProgressMeter":
        self.start()
        return self

    def __exit__(self, exc_type=None, *exc) -> None:
        self.stop(interrupted=exc_type is not None
                  and issubclass(exc_type, KeyboardInterrupt))

    def start(self) -> None:
        if self.live and self._thread is None:
            self._thread = threading.Thread(
                target=self._run, name="rmig-progress", daemon=True
            )
            self._thread.start()

    def stop(self, interrupted: bool = False) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        if self.live:
            if self._multiline:
                self._erase_block()
            else:
                self._erase_line()
        self._emit_summary(interrupted=interrupted)

    # --- producer-side updates --------------------------------------------

    def set_current(self, path: str) -> None:
        with self._lock:
            self._current = path

    def add_processed(self, nbytes: int) -> None:
        """Incremental bytes processed (local hashlib/xxhash chunks)."""
        with self._lock:
            self._committed += nbytes

    def set_inflight(self, nbytes: int) -> None:
        """Absolute byte size of the current in-flight file, sampled from
        rclone's on-disk ``.partial`` (copy). Display-only: makes the bar
        advance within a large file without touching the committed-based
        speed. If the watcher finds nothing this stays 0 and the meter
        behaves exactly like the wall-clock model."""
        with self._lock:
            self._inflight = max(0, nbytes)

    def file_done(self, committed_size: Optional[int] = None,
                   ok: bool = True) -> None:
        """Mark one file finished.

        ``committed_size`` is added to the committed total for byte-unaware
        callers (remote hash, copy). Streaming local hashing passes ``None``
        because the bytes already arrived via :meth:`add_processed`.
        """
        with self._lock:
            self._files_done += 1
            if not ok:
                self._failures += 1
            if committed_size is not None and committed_size >= 0:
                self._committed += committed_size
            self._inflight = 0
        if not self.live:
            self._maybe_periodic()

    # --- per-worker (parallel hash) ---------------------------------------

    def worker_slot(self) -> int:
        """Stable small slot id for the calling thread (ThreadPoolExecutor
        reuses threads, so a thread keeps its slot for the whole phase).
        First call switches the meter into multi-line mode."""
        tid = threading.get_ident()
        with self._lock:
            wid = self._slot_of_thread.get(tid)
            if wid is None:
                wid = len(self._slot_of_thread)
                self._slot_of_thread[tid] = wid
                self._nslots = max(self._nslots, wid + 1)
            self._multiline = True
            return wid

    def worker_start(self, wid: int, path: str, size: Optional[int] = None) -> None:
        with self._lock:
            self._slots[wid] = {"path": path, "size": size or 0, "bytes": 0}
            self._current = path

    def worker_add(self, wid: int, nbytes: int) -> None:
        """Per-chunk bytes for a worker's in-flight file (streamed hash).
        Also feeds the aggregate."""
        with self._lock:
            s = self._slots.get(wid)
            if s is not None:
                s["bytes"] += nbytes
            self._committed += nbytes

    def worker_done(self, wid: int, committed_size: Optional[int] = None,
                    ok: bool = True) -> None:
        with self._lock:
            self._files_done += 1
            if not ok:
                self._failures += 1
            if committed_size is not None and committed_size >= 0:
                self._committed += committed_size
            self._slots.pop(wid, None)
        if not self.live:
            self._maybe_periodic()

    # --- rendering ---------------------------------------------------------

    def _processed(self) -> int:
        return self._committed + self._inflight

    def _refresh_speed(self) -> None:
        if self._cumulative:
            elapsed = time.time() - self._start_t
            if elapsed > 0:
                self._speed = self._committed / elapsed
            return
        now = time.time()
        dt = now - self._last_sample_t
        if dt <= 0:
            return
        cur = self._processed()
        inst = (cur - self._last_sample_bytes) / dt
        if inst < 0:
            inst = 0.0
        self._speed = (
            inst if self._speed == 0.0
            else self._SMOOTH * inst + (1 - self._SMOOTH) * self._speed
        )
        self._last_sample_t = now
        self._last_sample_bytes = cur

    def _format_line(self) -> str:
        with self._lock:
            files_done = self._files_done
            processed = self._processed()
            current = self._current
            failures = self._failures
            total_files = self._total_files
            total_bytes = self._total_bytes
        speed = self._speed

        parts = [self._label]
        if total_files:
            parts.append(f"{files_done}/{total_files} files")
        else:
            parts.append(f"{files_done} files")

        if total_bytes:
            pct = 100.0 * processed / total_bytes
            parts.append(
                f"{_human_bytes(processed)}/{_human_bytes(total_bytes)} "
                f"({pct:.2f}%)"  # 2 decimals: a 226 GiB job spends a long
                                 # time under 1% — integer % reads "0%"
            )
        elif processed:
            parts.append(_human_bytes(processed))

        parts.append(_human_rate(speed))

        if total_bytes and speed > 0:
            parts.append(f"ETA {_human_eta((total_bytes - processed) / speed)}")

        if failures:
            parts.append(f"{failures} failed")

        line = "  ".join(parts)
        if current:
            # Keep the line from wrapping; trim the (least important) cur path.
            budget = 110 - len(line) - len("  cur: ")
            if budget > 8:
                disp = current if len(current) <= budget else "…" + current[-(budget - 1):]
                line += f"  cur: {disp}"
        return line

    def _format_worker_lines(self) -> list:
        with self._lock:
            nslots = self._nslots
            slots = {k: dict(v) for k, v in self._slots.items()}
        out = []
        shown = min(nslots, self._max_worker_lines)
        for wid in range(shown):
            s = slots.get(wid)
            if s is None:
                out.append(f"  · w{wid}  idle")
                continue
            size = s["size"]
            b = s["bytes"]
            pct = f"{100.0*b/size:3.0f}%" if size else "  ? "
            path = s["path"]
            stem = f"{_human_bytes(b)}/{_human_bytes(size)}" if size else _human_bytes(b)
            head = f"  ▸ w{wid}  {pct}  {stem}  "
            budget = 110 - len(head)
            disp = path if len(path) <= budget else "…" + path[-(budget - 1):]
            out.append(head + disp)
        if nslots > shown:
            out.append(f"  … +{nslots - shown} more workers")
        return out

    def _write_block(self) -> None:
        if self._term is None:
            return
        worker_lines = self._format_worker_lines()
        agg = self._format_line()
        if self._color:
            agg = verbose_mod.CYAN + agg + verbose_mod.RESET
        lines = worker_lines + [agg]
        h = len(lines)
        try:
            if self._block_h == 0:
                self._term.write("\n".join(lines) + "\n")
            else:
                # grew? reserve the extra rows first
                if h > self._block_h:
                    self._term.write("\n" * (h - self._block_h))
                self._term.write(f"\033[{max(h, self._block_h)}F")
                self._term.write("".join("\033[2K" + ln + "\n" for ln in lines))
            self._term.flush()
        except (BrokenPipeError, ValueError, OSError):
            pass
        self._block_h = h

    def _erase_block(self) -> None:
        if self._term is None or self._block_h == 0:
            return
        try:
            self._term.write(f"\033[{self._block_h}F\033[0J")
            self._term.flush()
        except (BrokenPipeError, ValueError, OSError):
            pass
        self._block_h = 0

    def _write_live(self, line: str) -> None:
        if self._term is None:
            return
        pad = max(0, self._line_len - len(line))
        out = "\r" + line + " " * pad
        if self._color:
            out = "\r" + verbose_mod.CYAN + line + verbose_mod.RESET + " " * pad
        try:
            self._term.write(out)
            self._term.flush()
        except (BrokenPipeError, ValueError, OSError):
            pass
        self._line_len = len(line)

    def _erase_line(self) -> None:
        if self._term is None or self._line_len == 0:
            return
        try:
            self._term.write("\r" + " " * self._line_len + "\r")
            self._term.flush()
        except (BrokenPipeError, ValueError, OSError):
            pass
        self._line_len = 0

    def _maybe_periodic(self) -> None:
        """Non-TTY fallback: emit a classic full line, rate-limited to
        ``interval`` so piped/log output stays the same shape as before."""
        if not self._periodic:
            return
        now = time.time()
        if now - self._last_periodic_t < self._interval:
            return
        self._last_periodic_t = now
        self._refresh_speed()
        self._v.info("  " + self._format_line())

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            self._refresh_speed()
            if self._multiline:
                self._write_block()
            else:
                self._write_live(self._format_line())

    def _emit_summary(self, interrupted: bool = False) -> None:
        elapsed = max(time.time() - self._start_t, 1e-6)
        with self._lock:
            files_done = self._files_done
            processed = self._committed
            failures = self._failures
            total_files = self._total_files
        avg = processed / elapsed
        verb = "interrupted" if interrupted else "done"
        tail = f"/{total_files}" if interrupted and total_files else ""
        msg = (
            f"{self._label} {verb}: {files_done}{tail} files"
            + (f", {_human_bytes(processed)}" if processed else "")
            + f" in {elapsed:.1f}s (avg {_human_rate(avg)})"
        )
        if interrupted:
            self._v.warn(msg)
        elif failures:
            self._v.warn(msg + f" — {failures} failed")
        else:
            self._v.info(msg)
