"""Verbose output helper.

Centralizes the level + style of CLI status output so individual sites
don't repeat `if verbose >= N: print(...)` boilerplate.

Levels:
  0 = quiet     (only warnings / errors)
  1 = normal    (default; current progress lines)
  2 = -v        (per-file details, cache stale reasons, phase timings,
                 rclone subprocess argv, subprocess stderr always)
  3 = -vv       (also: SQL queries, flush events, internal state transitions)

Style:
  - ANSI colors auto-enabled when stdout is a TTY; --no-color forces off.
  - Timestamps prepended at -v or higher (or `timestamps=True`).
  - Audit log files get the same lines but with ANSI stripped — see
    `audit._Tee` which routes color-stripped text to the log handle.
"""
from __future__ import annotations

import contextlib
import re
import sys
import time
from datetime import datetime
from typing import Optional, TextIO


# --- ANSI ---

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
BLUE = "\033[34m"
CYAN = "\033[36m"
GRAY = "\033[90m"

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def strip_ansi(s: str) -> str:
    """Remove ANSI color escape sequences from a string."""
    return ANSI_RE.sub("", s)


# --- Levels ---

QUIET = 0
NORMAL = 1
DETAIL = 2     # -v
DEBUG = 3      # -vv


class Verbose:
    """User-facing output gateway.

    All status-bearing print() calls in ops/manifest/rclone should funnel
    through this object so the level + style decisions stay in one place.
    """

    def __init__(
        self,
        level: int = NORMAL,
        color: Optional[bool] = None,
        timestamps: Optional[bool] = None,
        stream: Optional[TextIO] = None,
        err_stream: Optional[TextIO] = None,
    ):
        self.level = level
        self._stream = stream or sys.stdout
        self._err = err_stream or sys.stderr

        if color is None:
            try:
                color = self._stream.isatty()
            except Exception:
                color = False
        self.color = bool(color)

        # Timestamps default: on at DETAIL+, off at NORMAL/QUIET
        if timestamps is None:
            timestamps = level >= DETAIL
        self.timestamps = bool(timestamps)

    # --- Level helpers ---

    def is_normal(self) -> bool:
        return self.level >= NORMAL

    def is_detail(self) -> bool:
        return self.level >= DETAIL

    def is_debug(self) -> bool:
        return self.level >= DEBUG

    # --- Output methods ---

    def info(self, msg: str) -> None:
        """Default-level progress. Suppressed only when --quiet."""
        if self.level >= NORMAL:
            self._emit(msg)

    def detail(self, msg: str) -> None:
        """-v: per-file details, cache reasons, phase timings."""
        if self.level >= DETAIL:
            self._emit(msg, color=GRAY)

    def debug(self, msg: str) -> None:
        """-vv: internal events (SQL, flushes, state transitions)."""
        if self.level >= DEBUG:
            self._emit(msg, color=DIM)

    def warn(self, msg: str) -> None:
        """Warnings always print (stderr), even in --quiet."""
        self._emit(f"WARN: {msg}", color=YELLOW, err=True)

    def error(self, msg: str) -> None:
        """Errors always print (stderr)."""
        self._emit(f"ERROR: {msg}", color=RED, err=True)

    def ok(self, msg: str) -> None:
        if self.level >= NORMAL:
            self._emit(msg, color=GREEN)

    def _emit(self, msg: str, color: Optional[str] = None, err: bool = False) -> None:
        prefix = ""
        if self.timestamps:
            prefix = f"[{datetime.now().strftime('%H:%M:%S')}] "
        if self.color and color:
            line = f"{prefix}{color}{msg}{RESET}"
        else:
            line = prefix + msg
        stream = self._err if err else self._stream
        try:
            print(line, file=stream, flush=True)
        except (BrokenPipeError, ValueError):
            pass

    # --- Phase timing context manager ---

    @contextlib.contextmanager
    def phase(self, name: str):
        """Time a named phase; emits start/end at DETAIL level.

        Usage:
            with v.phase("refresh src"):
                ...
        """
        start = time.time()
        self.detail(f"→ {name} starting")
        try:
            yield
        finally:
            self.detail(f"← {name} done in {time.time() - start:.2f}s")


# --- Singleton fallback for code paths that didn't get a Verbose handle ---

_DEFAULT = Verbose(level=NORMAL)


def default() -> Verbose:
    """Return a process-wide normal-level Verbose for code paths that lack
    one. New code should pass an explicit Verbose object instead."""
    return _DEFAULT
