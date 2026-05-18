"""Hash algorithm negotiation between two rclone endpoints."""
from __future__ import annotations

import hashlib
from typing import Iterable, List, Optional

from . import rclone


# Preference order: strongest first, with universally-supported fallbacks.
# Rclone uses lowercase names internally (matches `backend features --json` output).
PREFERRED_ORDER: List[str] = [
    "sha256",
    "sha1",
    "md5",
    "sha512",
    "blake3",
    # Backend-specific (only chosen if nothing else is shared)
    "dropbox",
    "quickxor",
    "whirlpool",
    "crc32",
    "xxh128",
    "xxh3",
]

# Hashes that Python's hashlib can compute locally without invoking rclone.
HASHLIB_SUPPORTED = {"md5", "sha1", "sha256", "sha512"}


class HashNegotiationError(RuntimeError):
    pass


def supported_hashes(path: str) -> List[str]:
    """Return rclone-reported hash list for the backend serving `path` (lowercase)."""
    feats = rclone.backend_features(path)
    return [h.lower() for h in feats.get("Hashes", [])]


def negotiate(
    src: str,
    dst: str,
    override: Optional[str] = None,
    *,
    priority: Optional[List[str]] = None,
) -> str:
    """Pick the best hash algorithm shared by both endpoints.

    `override` (case-insensitive) forces a specific algorithm; raises if either
    side doesn't list it. Returns rclone's lowercase hash name.

    `priority` overrides the built-in PREFERRED_ORDER for this call (e.g.
    sourced from a profile). Items missing from the common set are skipped;
    fall back to PREFERRED_ORDER → any-common if `priority` exhausts.
    """
    src_h = set(supported_hashes(src))
    dst_h = set(supported_hashes(dst))

    if override:
        algo = override.lower()
        if algo not in src_h:
            raise HashNegotiationError(
                f"src ({src}) does not natively support hash '{algo}'. "
                f"Supported: {sorted(src_h)}"
            )
        if algo not in dst_h:
            raise HashNegotiationError(
                f"dst ({dst}) does not natively support hash '{algo}'. "
                f"Supported: {sorted(dst_h)}"
            )
        return algo

    common = src_h & dst_h
    if not common:
        raise HashNegotiationError(
            f"no common hash between src ({sorted(src_h)}) and dst ({sorted(dst_h)})"
        )

    if priority:
        for cand in priority:
            c = cand.strip().lower()
            if c in common:
                return c
        # Caller-supplied priority exhausted with no match — fall through to
        # PREFERRED_ORDER so we still return a usable algo, rather than fail.
    for cand in PREFERRED_ORDER:
        if cand in common:
            return cand
    return sorted(common)[0]


def hash_file_local(
    path: str,
    algo: str,
    chunk_size: int = 1 << 20,
    progress_cb=None,
) -> str:
    """Compute hash of a local file using hashlib if possible, else rclone.

    ``progress_cb``, if given, is called with the byte count of each chunk
    read so callers can drive a live throughput meter even while a single
    large file is being hashed.
    """
    if algo in HASHLIB_SUPPORTED:
        h = hashlib.new(algo)
        with open(path, "rb") as f:
            while chunk := f.read(chunk_size):
                h.update(chunk)
                if progress_cb is not None:
                    progress_cb(len(chunk))
        return h.hexdigest()
    # Fallback to rclone for exotic algorithms
    result = rclone.hashsum_file(algo, path)
    if result is None:
        raise HashNegotiationError(
            f"failed to hash {path} with algorithm '{algo}'"
        )
    return result


def normalize(algo: str) -> str:
    """Normalize user-typed hash names to rclone's lowercase form."""
    a = algo.strip().lower()
    # Common aliases
    return {
        "sha-1": "sha1",
        "sha-256": "sha256",
        "sha-512": "sha512",
    }.get(a, a)
