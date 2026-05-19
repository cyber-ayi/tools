"""rclone subprocess wrapper."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
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


def copyto(src: str, dst: str, algo: str, transfers: int = 8,
           extra: Optional[List[str]] = None) -> None:
    """Copy a single object with `rclone copyto --checksum`.

    Note: rmig deliberately does NOT scrape rclone for intra-file copy
    progress. rclone's interim accounting (`--stats`/`--use-json-log` log
    lines, `rc core/stats`) is only populated when the transfer streams
    through the accounting reader — which a fast local→local copy (APFS
    clonefile, or plain full-bandwidth disk copy) bypasses, leaving it
    stuck at 0 B / -- the whole run. The copy meter instead uses a
    wall-clock model that credits each file's size on completion: always
    correct, no rclone cooperation required.
    """
    # Hardening (#236 part 2): retry transient SMB/network errors in place
    # instead of failing the file → full re-copy. NOT --inplace (collides
    # with Stage C/G .partial machinery; rsync engine is the resumable
    # -huge-file answer).
    args = [
        "copyto", "--checksum", "--transfers", str(transfers),
        "--retries", "3", "--low-level-retries", "10",
    ]
    if extra:
        args.extend(extra)
    _run(args + [src, dst], capture=False)


def have_rsync() -> bool:
    return shutil.which("rsync") is not None


def rsync_copyto(src: str, dst: str) -> None:
    """Resumable single-file copy (#236 part 1).

    `rsync --partial --inplace --append`:
      * `--partial` is REQUIRED. macOS ships **openrsync**, which removes
        a partially-transferred file on interruption *unless* --partial
        (`man rsync`: "--partial: Do not remove partially transferred
        files if openrsync is interrupted"). Without it every killed run
        discards its progress and only a fluke-surviving partial sticks
        — the "resumes only from the first break" bug (RCA).
      * `--append` extends a shorter dst by the missing tail; it implies
        --inplace (we also pass it explicitly for non-openrsync rsync).
        Together: a kill leaves the grown dst; the next run continues
        from the new length → true progressive resume across repeated
        interruptions.

    `--append` not `--append-verify`: rmig's post-copy xxh3 dst re-hash
    + MHL is the single integrity gate for both engines; openrsync's
    --append already whole-file-checksums and delta-heals a bad prefix
    at the end, so a corrupt tail can't silently pass — and rmig's xxh3
    is the authoritative DIT check regardless. Caller guarantees dst
    is local.
    """
    rb = shutil.which("rsync")
    if not rb:
        raise RcloneError("rsync not found in PATH")
    os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
    full = [rb, "--partial", "--inplace", "--append", "--times", src, dst]
    if _VERBOSE_HOOK is not None and _VERBOSE_HOOK.is_detail():
        import shlex
        _VERBOSE_HOOK.detail("    $ " + " ".join(shlex.quote(a) for a in full))
    cp = subprocess.run(full, capture_output=True, text=True)
    if cp.returncode != 0:
        raise RcloneError(
            f"rsync --append failed (exit {cp.returncode}):\n{cp.stderr}"
        )


def deletefile(path: str) -> None:
    _run(["deletefile", path], capture=False)


def rmdirs(path: str, leave_root: bool = True) -> None:
    args = ["rmdirs", path]
    if leave_root:
        args.append("--leave-root")
    _run(args, check=False, capture=False)


def mkdir(path: str) -> None:
    _run(["mkdir", path], check=False, capture=False)
