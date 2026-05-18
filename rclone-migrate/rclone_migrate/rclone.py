"""rclone subprocess wrapper."""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import threading
import urllib.request
from dataclasses import dataclass
from typing import Dict, Iterator, List, Optional, Tuple


class RcloneError(RuntimeError):
    pass


def _bin() -> str:
    b = shutil.which("rclone")
    if not b:
        raise RcloneError("rclone not found in PATH")
    return b


_VERBOSE_HOOK = None  # set by verbose_mod.Verbose at runtime


def set_verbose(v) -> None:
    """Register a Verbose object to receive subprocess-argv logging.

    Optional; rclone.py functions that don't get an explicit verbose
    parameter check this global. Cleared by passing None.
    """
    global _VERBOSE_HOOK
    _VERBOSE_HOOK = v


def _run(args: List[str], check: bool = True, capture: bool = True) -> subprocess.CompletedProcess:
    full = [_bin(), *args]
    if _VERBOSE_HOOK is not None and _VERBOSE_HOOK.is_detail():
        # Print as a single shell-quoted line so user can copy-paste
        import shlex
        _VERBOSE_HOOK.detail("    $ " + " ".join(shlex.quote(a) for a in full))
    cp = subprocess.run(full, capture_output=capture, text=True)
    if check and cp.returncode != 0:
        raise RcloneError(
            f"rclone {args[0] if args else ''} failed (exit {cp.returncode}):\n{cp.stderr}"
        )
    # At -v, always surface stderr even on success (rclone often emits warnings)
    if (
        _VERBOSE_HOOK is not None and _VERBOSE_HOOK.is_detail()
        and capture and cp.stderr.strip()
    ):
        for line in cp.stderr.splitlines():
            _VERBOSE_HOOK.detail(f"    | {line}")
    return cp


def is_local(path: str) -> bool:
    """A remote path looks like 'name:...' (with no leading slash). Local otherwise."""
    if path.startswith("/") or path.startswith("./") or path.startswith("../"):
        return True
    if ":" not in path:
        return True
    head = path.split(":", 1)[0]
    # Windows drive letter (single char) — treat as local
    if len(head) == 1 and head.isalpha():
        return True
    return False


def remote_name(path: str) -> Optional[str]:
    """Return the remote backend name (without colon), or None if local."""
    if is_local(path):
        return None
    return path.split(":", 1)[0]


def backend_features(remote_or_path: str) -> Dict:
    """Return rclone backend features JSON. Pass either 'remote:' or a remote-prefixed path."""
    name = remote_name(remote_or_path)
    if name is None:
        # Local backend features
        cp = _run(["backend", "features", "/"])
    else:
        cp = _run(["backend", "features", f"{name}:"])
    return json.loads(cp.stdout)


@dataclass
class LsfEntry:
    path: str
    size: int
    mtime: Optional[float]  # epoch seconds, None if not available


def lsf(path: str, extra_flags: Optional[List[str]] = None) -> List[LsfEntry]:
    """List files (recursive) with size + modtime.

    Format: 's|p|t' → size|path|RFC3339-modtime. Uses '|' separator, so paths
    containing '|' will break — uncommon in practice.
    """
    args = [
        "lsf",
        "--files-only",
        "--recursive",
        "--format", "spt",
        "--separator", "|",
        path,
    ]
    if extra_flags:
        args.extend(extra_flags)
    cp = _run(args)
    out: List[LsfEntry] = []
    for line in cp.stdout.splitlines():
        if not line:
            continue
        # 's|p|t' produces: size|path|modtime
        parts = line.split("|", 2)
        if len(parts) < 3:
            continue
        size_s, p, mtime_s = parts
        try:
            size = int(size_s)
        except ValueError:
            continue
        mtime = _parse_rfc3339(mtime_s) if mtime_s else None
        out.append(LsfEntry(path=p, size=size, mtime=mtime))
    return out


def _parse_rfc3339(s: str) -> Optional[float]:
    from datetime import datetime
    s = s.strip()
    if not s:
        return None
    # rclone emits e.g. "2026-04-30 22:24:48.000000000 +0000 UTC"
    # or RFC3339 "2026-04-30T22:24:48.000+00:00"
    fmts = [
        "%Y-%m-%dT%H:%M:%S.%f%z",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%d %H:%M:%S.%f %z %Z",
        "%Y-%m-%d %H:%M:%S %z %Z",
        "%Y-%m-%d %H:%M:%S.%f %z",
        "%Y-%m-%d %H:%M:%S %z",
    ]
    for fmt in fmts:
        try:
            return datetime.strptime(s, fmt).timestamp()
        except ValueError:
            continue
    # Strip nanoseconds: "2026-04-30 22:24:48.123456789 +0000 UTC" → trim to micro
    try:
        # split off trailing " UTC" if present
        s2 = s.rsplit(" UTC", 1)[0]
        # truncate fractional to 6 digits
        if "." in s2:
            head, rest = s2.split(".", 1)
            digits = ""
            i = 0
            while i < len(rest) and rest[i].isdigit():
                digits += rest[i]
                i += 1
            digits = digits[:6]
            s2 = f"{head}.{digits}{rest[i:]}"
        for fmt in fmts:
            try:
                return datetime.strptime(s2, fmt).timestamp()
            except ValueError:
                continue
    except Exception:
        pass
    return None


def hashsum(algo: str, path: str, download: bool = False) -> Iterator[Tuple[str, str]]:
    """Yield (hash, relpath) for all files under path.

    For backends that store the hash, no download. For others, --download forces fetch.
    """
    args = ["hashsum", algo, path]
    if download:
        args.append("--download")
    cp = _run(args)
    for line in cp.stdout.splitlines():
        if not line:
            continue
        # md5sum-style: "<hash>  <path>"
        # rclone may output "<hash>  <path>" with two spaces. Some backends may emit
        # "UNSUPPORTED" placeholder — skip those.
        parts = line.split("  ", 1)
        if len(parts) != 2:
            continue
        h, p = parts[0].strip(), parts[1]
        if not h or h.upper() in ("UNSUPPORTED", "ERROR"):
            continue
        yield h.lower(), p


def hashsum_streaming(
    algo: str, path: str, download: bool = False,
) -> Iterator[Tuple[str, str]]:
    """Like `hashsum`, but reads stdout line-by-line as rclone produces it.

    The non-streaming variant blocks until rclone exits and returns ALL lines
    at once. For large remote trees that's hours of work where any kill
    discards everything. This generator yields each (hash, path) as soon as
    rclone emits it, letting callers persist progress incrementally.

    Caller is expected to fully consume the generator OR catch the OSError
    raised by the cleanup if it doesn't (subprocess will be terminated).
    """
    args = [_bin(), "hashsum", algo, path]
    if download:
        args.append("--download")
    proc = subprocess.Popen(
        args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1,  # line-buffered
    )
    assert proc.stdout is not None
    try:
        for line in proc.stdout:
            line = line.rstrip("\n")
            if not line:
                continue
            parts = line.split("  ", 1)
            if len(parts) != 2:
                continue
            h, p = parts[0].strip(), parts[1]
            if not h or h.upper() in ("UNSUPPORTED", "ERROR"):
                continue
            yield h.lower(), p
        rc = proc.wait()
        if rc != 0:
            err = proc.stderr.read() if proc.stderr else ""
            raise RcloneError(
                f"rclone hashsum exited {rc}:\n{err}"
            )
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


def hashsum_file(algo: str, file_path: str, download: bool = False) -> Optional[str]:
    """Hash a single file via rclone (used for remote-side single-file rehash)."""
    args = ["hashsum", algo, file_path]
    if download:
        args.append("--download")
    cp = _run(args, check=False)
    if cp.returncode != 0:
        return None
    for line in cp.stdout.splitlines():
        parts = line.split("  ", 1)
        if len(parts) == 2 and parts[0].strip():
            h = parts[0].strip().lower()
            if h in ("unsupported", "error"):
                return None
            return h
    return None


_RC_POLL_INTERVAL = 0.7  # seconds between core/stats polls (≈1.4 Hz)


def _free_loopback_port() -> int:
    """Grab an ephemeral port the OS just told us is free. There is a small
    race between close() and rclone's bind(); the caller retries once."""
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def _copyto_rc(full: List[str], on_stats) -> Tuple[int, str]:
    """Run an rclone copy/copyto that has --rc enabled, polling its
    core/stats HTTP endpoint ~1.4Hz and forwarding (bytes, speed) to
    ``on_stats`` until the process exits. Returns (returncode, stderr)."""
    # --rc-addr is passed as a single "--rc-addr=host:port" token.
    addr = next((a.split("=", 1)[1] for a in full if a.startswith("--rc-addr=")), "")
    url = f"http://{addr}/core/stats"
    proc = subprocess.Popen(
        full, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True
    )
    stop = threading.Event()

    def poll() -> None:
        while not stop.wait(_RC_POLL_INTERVAL):
            try:
                req = urllib.request.Request(url, data=b"{}", method="POST")
                with urllib.request.urlopen(req, timeout=2) as r:
                    s = json.load(r)
            except Exception:
                continue  # server not up yet / transient — keep trying
            try:
                tr = s.get("transferring") or []
                if tr:
                    cur = tr[0]
                    spd = cur.get("speedAvg") or s.get("speed") or 0.0
                    on_stats(int(cur.get("bytes", 0)), float(spd))
                else:
                    on_stats(int(s.get("bytes", 0)),
                             float(s.get("speed") or 0.0))
            except Exception:
                pass

    th = threading.Thread(target=poll, name="rmig-rc-poll", daemon=True)
    th.start()
    try:
        _, err = proc.communicate()
    finally:
        stop.set()
        th.join(timeout=2)
    return proc.returncode, err or ""


def copyto(
    src: str,
    dst: str,
    algo: str,
    transfers: int = 8,
    extra: Optional[List[str]] = None,
    on_stats=None,
) -> None:
    """Copy a single object with `rclone copyto --checksum`.

    When ``on_stats`` is given it is called ~1.4×/s as
    ``on_stats(bytes_transferred: int, speed_bps: float)`` with live
    figures polled from rclone's ``core/stats`` remote-control endpoint
    (the maintainer-blessed path — ``--use-json-log --stats`` does not emit
    interim stats for a single-object copyto). The rc server binds an
    ephemeral 127.0.0.1 port with ``--rc-no-auth``: loopback-only and gone
    when the copy finishes.
    """
    args = ["copyto", "--checksum", "--transfers", str(transfers)]
    if extra:
        args.extend(extra)
    if on_stats is None:
        _run(args + [src, dst], capture=False)
        return

    last_err = ""
    for attempt in range(2):  # one retry for the close()/bind() port race
        port = _free_loopback_port()
        full = [
            _bin(), *args,
            "--rc", f"--rc-addr=127.0.0.1:{port}", "--rc-no-auth",
            src, dst,
        ]
        if _VERBOSE_HOOK is not None and _VERBOSE_HOOK.is_detail():
            import shlex
            _VERBOSE_HOOK.detail("    $ " + " ".join(shlex.quote(a) for a in full))
        rc, err = _copyto_rc(full, on_stats)
        if rc == 0:
            if _VERBOSE_HOOK is not None and _VERBOSE_HOOK.is_detail() and err.strip():
                for line in err.splitlines():
                    _VERBOSE_HOOK.detail(f"    | {line}")
            return
        last_err = err
        if "address already in use" not in err.lower():
            break
    raise RcloneError(f"rclone copyto failed:\n{last_err}")


def deletefile(path: str) -> None:
    _run(["deletefile", path], capture=False)


def rmdirs(path: str, leave_root: bool = True) -> None:
    args = ["rmdirs", path]
    if leave_root:
        args.append("--leave-root")
    _run(args, check=False, capture=False)


def mkdir(path: str) -> None:
    _run(["mkdir", path], check=False, capture=False)
