#!/usr/bin/env python3
"""Verify SD card files against NAS backup by comparing checksums.

Filenames may differ between source and destination, so matching is done
by file size first, then by SHA-256 checksum.

Comparison modes (--mode):
  full      - Full-file SHA-256 only (original behavior)
  smart     - Full hash first; on JPEG mismatch, fallback to image-data hash
              to detect metadata-only changes (default)
  data-only - For JPEG, compare image-data hash only (skip full hash)

Dest hash cache:
  - Stored as SQLite DB alongside the dest directory (.verify_cache.db)
  - Each entry stores path, sha256, data_sha256, size, mtime
  - On load, entries are validated: stale (size/mtime changed) or missing
    files are evicted; files not in cache are hashed fresh
  - Use --no-cache to skip cache entirely, --clear-cache to delete and rebuild

Usage:
    python verify_backup.py <src_dir> <dest_dir> [-w WORKERS] [-o REPORT]
    python verify_backup.py <src_dir> <dest_dir> --mode smart
    python verify_backup.py <src_dir> <dest_dir> --no-cache
    python verify_backup.py <src_dir> <dest_dir> --clear-cache
    python verify_backup.py <src_dir> <dest_dir> --strict

Example:
    python verify_backup.py "O:\\DCIM\\100_FUJI" "W:\\storage\\ingest\\...\\FUJIFILM X-T5"
    python verify_backup.py "O:\\DCIM\\100_FUJI" "W:\\storage\\ingest\\...\\FUJIFILM X-T5" -w 8 --mode smart
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sqlite3
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from threading import Lock
from typing import Dict, List, Optional, Tuple


CACHE_FILENAME = ".verify_cache.db"
JPEG_EXTENSIONS = (".jpg", ".jpeg")

CacheEntry = Dict[str, object]
FileInfo = Tuple[Path, int, float]  # (path, size, mtime)


def sha256(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def is_jpeg(path: str | Path) -> bool:
    return str(path).lower().endswith(JPEG_EXTENSIONS)


def find_jpeg_sos(path: str | Path) -> Optional[int]:
    """Return byte offset of JPEG SOS marker (0xFFDA), or None.

    Walks the JPEG marker structure properly, skipping marker payloads
    to avoid false matches inside embedded thumbnails.
    """
    with open(path, "rb") as f:
        if f.read(2) != b'\xff\xd8':
            return None
        while True:
            marker = f.read(2)
            if len(marker) < 2 or marker[0:1] != b'\xff':
                return None
            if marker == b'\xff\xda':
                return f.tell() - 2
            if marker[1:2] in (b'\x00', b'\x01') or (b'\xd0' <= marker[1:2] <= b'\xd7'):
                continue
            length_bytes = f.read(2)
            if len(length_bytes) < 2:
                return None
            length = (length_bytes[0] << 8) | length_bytes[1]
            f.seek(length - 2, 1)
    return None


def sha256_dual(path: str | Path) -> Tuple[str, Optional[str]]:
    """Single-pass dual hash: computes full-file SHA-256 and image-data SHA-256.

    For JPEG files, reads the file once and simultaneously computes both hashes.
    Returns (full_sha256, data_sha256). data_sha256 is None for non-JPEG or
    if SOS marker is not found.
    """
    sos_offset = find_jpeg_sos(path) if is_jpeg(path) else None

    h_full = hashlib.sha256()
    h_data = hashlib.sha256() if sos_offset is not None else None
    offset = 0

    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            h_full.update(chunk)
            if h_data is not None:
                chunk_end = offset + len(chunk)
                if chunk_end > sos_offset:
                    data_start = max(0, sos_offset - offset)
                    h_data.update(chunk[data_start:])
            offset += len(chunk)

    return h_full.hexdigest(), (h_data.hexdigest() if h_data is not None else None)


def fmt_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def scan_dir(directory: Path) -> List[FileInfo]:
    files: List[FileInfo] = []
    for p in sorted(directory.rglob("*")):
        if p.is_file() and p.name != CACHE_FILENAME:
            st = p.stat()
            files.append((p, st.st_size, st.st_mtime))
    return files


# --- SQLite hash cache ---

def cache_db_path(dest_dir: str | Path) -> Path:
    return Path(dest_dir) / CACHE_FILENAME


def open_cache_db(dest_dir: str | Path) -> sqlite3.Connection:
    db_path = cache_db_path(dest_dir)
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS hash_cache (
            path        TEXT PRIMARY KEY,
            sha256      TEXT NOT NULL,
            size        INTEGER NOT NULL,
            mtime       REAL NOT NULL,
            data_sha256 TEXT
        )
    """)
    try:
        conn.execute("ALTER TABLE hash_cache ADD COLUMN data_sha256 TEXT")
    except sqlite3.OperationalError:
        pass
    conn.commit()
    return conn


def load_cache_all(conn: sqlite3.Connection) -> Dict[str, CacheEntry]:
    """Load all cache entries into a dict keyed by path."""
    rows = conn.execute(
        "SELECT path, sha256, size, mtime, data_sha256 FROM hash_cache"
    ).fetchall()
    return {
        row[0]: {
            "sha256": row[1], "size": row[2], "mtime": row[3],
            "data_sha256": row[4],
        }
        for row in rows
    }


def validate_cache(
    cache: Dict[str, CacheEntry],
    dest_files: List[FileInfo],
) -> Tuple[Dict[str, CacheEntry], List[str], List[str], List[str]]:
    """Validate cache against current dest files.

    Returns (valid, stale_keys, missing_keys, removed_keys).
    """
    current = {str(p): (size, mtime) for p, size, mtime in dest_files}

    valid: Dict[str, CacheEntry] = {}
    stale: List[str] = []
    removed: List[str] = []

    for key, entry in cache.items():
        if key not in current:
            removed.append(key)
            continue
        disk_size, disk_mtime = current[key]
        if entry["size"] == disk_size and abs(entry["mtime"] - disk_mtime) < 0.01:
            valid[key] = entry
        else:
            stale.append(key)

    missing = [k for k in current if k not in cache]
    return valid, stale, missing, removed


def sync_cache(
    conn: sqlite3.Connection,
    valid: Dict[str, CacheEntry],
    new_entries: Dict[str, CacheEntry],
    removed_keys: List[str],
) -> None:
    """Update the DB: delete removed/stale rows, upsert new entries."""
    keep_keys = set(valid.keys()) | set(new_entries.keys())
    existing = {row[0] for row in conn.execute("SELECT path FROM hash_cache").fetchall()}

    to_delete = existing - keep_keys
    if to_delete:
        conn.executemany("DELETE FROM hash_cache WHERE path = ?",
                         [(k,) for k in to_delete])

    if new_entries:
        conn.executemany(
            "INSERT OR REPLACE INTO hash_cache "
            "(path, sha256, size, mtime, data_sha256) VALUES (?, ?, ?, ?, ?)",
            [(k, v["sha256"], v["size"], v["mtime"], v.get("data_sha256"))
             for k, v in new_entries.items()]
        )

    conn.commit()


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify SD card backup checksums")
    parser.add_argument("src_dir", help="Source directory (SD card)")
    parser.add_argument("dest_dir", help="Destination directory (NAS backup)")
    parser.add_argument("-w", "--workers", type=int, default=4,
                        help="Number of hash threads (default: 4)")
    parser.add_argument("-o", "--output", type=str, default=None,
                        help="Save report to file (default: auto-generated name)")
    parser.add_argument("-m", "--mode", type=str, default="smart",
                        choices=["full", "smart", "data-only"],
                        help="Comparison mode (default: smart)")
    parser.add_argument("--strict", action="store_true",
                        help="Treat metadata-only diffs as failures")
    parser.add_argument("--no-cache", action="store_true",
                        help="Skip hash cache entirely")
    parser.add_argument("--clear-cache", action="store_true",
                        help="Delete existing cache and rebuild from scratch")
    args = parser.parse_args()

    src_dir = Path(args.src_dir)
    dest_dir = Path(args.dest_dir)
    mode = args.mode

    if not src_dir.is_dir():
        print(f"Error: source directory not found: {src_dir}")
        sys.exit(1)
    if not dest_dir.is_dir():
        print(f"Error: destination directory not found: {dest_dir}")
        sys.exit(1)

    if args.clear_cache:
        cp = cache_db_path(dest_dir)
        if cp.exists():
            os.remove(cp)
            print(f"Cache cleared: {cp}")

    t_start = time.time()

    # --- Scan ---
    print(f"Scanning source: {src_dir}")
    src_files = scan_dir(src_dir)
    src_total_size = sum(s for _, s, _ in src_files)
    print(f"  Found {len(src_files)} files ({fmt_size(src_total_size)})")

    print(f"Scanning destination: {dest_dir}")
    dest_files = scan_dir(dest_dir)
    dest_total_size = sum(s for _, s, _ in dest_files)
    print(f"  Found {len(dest_files)} files ({fmt_size(dest_total_size)})")

    # Group dest files by size
    dest_by_size: Dict[int, List[Path]] = defaultdict(list)
    for p, size, mtime in dest_files:
        dest_by_size[size].append(p)

    # --- Load and validate cache ---
    use_cache = not args.no_cache
    cache_hits = cache_stale = cache_new = cache_removed = 0
    valid: Dict[str, CacheEntry] = {}
    removed_keys: List[str] = []

    if use_cache:
        conn = open_cache_db(dest_dir)
        raw_cache = load_cache_all(conn)
        valid, stale_keys, missing_keys, removed_keys = validate_cache(raw_cache, dest_files)
        cache_hits = len(valid)
        cache_stale = len(stale_keys)
        cache_new = len(missing_keys)
        cache_removed = len(removed_keys)

        print(f"\nCache: {cache_db_path(dest_dir)}")
        print(f"  Valid (reusable) : {cache_hits}")
        print(f"  Stale (rehash)   : {cache_stale}")
        print(f"  New (not cached) : {cache_new}")
        print(f"  Removed (pruned) : {cache_removed}")
    else:
        conn = None
        print("\nCache: disabled")

    # --- Pre-compute dest checksums ---
    src_sizes = {s for _, s, _ in src_files}
    dest_to_hash: List[FileInfo] = []
    dest_checksums: Dict[Path, str] = {}
    dest_data_checksums: Dict[Path, str] = {}

    for p, size, mtime in dest_files:
        if size not in src_sizes:
            continue
        key = str(p)
        if key in valid:
            dest_checksums[p] = valid[key]["sha256"]
            if valid[key].get("data_sha256"):
                dest_data_checksums[p] = valid[key]["data_sha256"]
        else:
            dest_to_hash.append((p, size, mtime))

    if dest_to_hash:
        print(f"\nHashing {len(dest_to_hash)} dest files with {args.workers} threads...")
    else:
        print("\nAll dest candidates served from cache.")

    dest_hash_lock = Lock()
    done_count = [0]
    new_cache_entries: Dict[str, CacheEntry] = {}

    def hash_dest(path: Path, size: int, mtime: float) -> str:
        if mode in ("smart", "data-only") and is_jpeg(path):
            h, dh = sha256_dual(path)
        else:
            h, dh = sha256(path), None

        with dest_hash_lock:
            dest_checksums[path] = h
            if dh:
                dest_data_checksums[path] = dh
            new_cache_entries[str(path)] = {
                "sha256": h, "size": size, "mtime": mtime, "data_sha256": dh,
            }
            done_count[0] += 1
            if done_count[0] % 10 == 0 or done_count[0] == len(dest_to_hash):
                print(f"  Dest hashed: {done_count[0]}/{len(dest_to_hash)}")
        return h

    if dest_to_hash:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = [pool.submit(hash_dest, p, s, m) for p, s, m in dest_to_hash]
            for f in as_completed(futures):
                f.result()

    # --- Save updated cache ---
    if use_cache and conn:
        sync_cache(conn, valid, new_cache_entries, removed_keys)
        print(f"Cache saved: {len(valid) + len(new_cache_entries)} entries")
        conn.close()

    # --- Hash source files in parallel and match ---
    print(f"\nHashing {len(src_files)} source files and verifying... (mode={mode})")

    matched: List[Tuple] = []
    metadata_diff: List[Tuple] = []
    missing: List[Path] = []
    corrupted: List[Tuple] = []
    results_lock = Lock()
    src_done = [0]

    def verify_one(src_path: Path, src_size: int) -> None:
        rel = src_path.relative_to(src_dir)
        candidates = dest_by_size.get(src_size, [])

        if not candidates:
            with results_lock:
                missing.append(rel)
                src_done[0] += 1
                print(f"  [{src_done[0]}/{len(src_files)}] MISSING  {rel}")
            return

        # Single-pass: compute full hash + data hash in one read
        need_dual = mode in ("smart", "data-only") and is_jpeg(src_path)
        if need_dual:
            src_hash, src_data_hash = sha256_dual(src_path)
        else:
            src_hash, src_data_hash = sha256(src_path), None

        # --- data-only mode: match by image-data hash only ---
        if mode == "data-only" and is_jpeg(src_path):
            use_hash = src_data_hash if src_data_hash is not None else src_hash
            for dest_path in candidates:
                ddh = dest_data_checksums.get(dest_path) if src_data_hash is not None else dest_checksums.get(dest_path)
                if ddh and ddh == use_hash:
                    dest_rel = dest_path.relative_to(dest_dir)
                    with results_lock:
                        matched.append((rel, dest_rel, use_hash))
                        src_done[0] += 1
                        print(f"  [{src_done[0]}/{len(src_files)}] OK       {rel} -> {dest_rel} (data-only)")
                    return

            with results_lock:
                corrupted.append((rel, use_hash))
                src_done[0] += 1
                print(f"  [{src_done[0]}/{len(src_files)}] MISMATCH {rel} (image data differs)")
            return

        # --- full / smart mode: try exact full-file match ---
        for dest_path in candidates:
            if (dh := dest_checksums.get(dest_path)) and dh == src_hash:
                dest_rel = dest_path.relative_to(dest_dir)
                with results_lock:
                    matched.append((rel, dest_rel, src_hash))
                    src_done[0] += 1
                    print(f"  [{src_done[0]}/{len(src_files)}] OK       {rel} -> {dest_rel}")
                return

        # --- smart fallback: image-data hash (already computed above) ---
        if src_data_hash is not None:
            for dest_path in candidates:
                if (ddh := dest_data_checksums.get(dest_path)) and ddh == src_data_hash:
                    dest_rel = dest_path.relative_to(dest_dir)
                    dest_full = dest_checksums.get(dest_path, "?")
                    with results_lock:
                        metadata_diff.append((rel, dest_rel, src_hash, dest_full, src_data_hash))
                        src_done[0] += 1
                        print(f"  [{src_done[0]}/{len(src_files)}] METADIFF {rel} -> {dest_rel} (EXIF differs, image data OK)")
                    return

        # No match at all
        with results_lock:
            corrupted.append((rel, src_hash))
            src_done[0] += 1
            print(f"  [{src_done[0]}/{len(src_files)}] MISMATCH {rel} (checksum differs)")

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(verify_one, p, s) for p, s, _ in src_files]
        for f in as_completed(futures):
            f.result()

    elapsed = time.time() - t_start

    # --- Build report ---
    lines = [
        "=" * 70,
        "Backup Verification Report",
        f"Date     : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"Source   : {src_dir}",
        f"Dest     : {dest_dir}",
        f"Mode     : {mode}",
        f"Strict   : {args.strict}",
        f"Elapsed  : {elapsed:.1f}s",
        f"Threads  : {args.workers}",
    ]
    if use_cache:
        lines.append(f"Cache    : {cache_hits} valid, {cache_stale} stale, {cache_new} new, {cache_removed} removed")
    else:
        lines.append("Cache    : disabled")
    lines += [
        "=" * 70,
        "",
        f"Total source files : {len(src_files)}  ({fmt_size(src_total_size)})",
        f"Matched (OK)       : {len(matched)}",
        f"Metadata diff      : {len(metadata_diff)}",
        f"Missing in dest    : {len(missing)}",
        f"Checksum mismatch  : {len(corrupted)}",
        "",
    ]

    if matched:
        lines += ["-" * 70, "MATCHED FILES:", "-" * 70]
        for src_rel, dest_rel, h in sorted(matched):
            lines.append(f"  {src_rel} -> {dest_rel}")
            lines.append(f"    SHA-256: {h}")
        lines.append("")

    if metadata_diff:
        lines += ["-" * 70, "METADATA DIFFERENCES (image data identical, EXIF modified):", "-" * 70]
        for src_rel, dest_rel, src_full, dest_full, data_h in sorted(metadata_diff):
            lines.append(f"  {src_rel} -> {dest_rel}")
            lines.append(f"    Full SHA-256 (src) : {src_full}")
            lines.append(f"    Full SHA-256 (dest): {dest_full}")
            lines.append(f"    Data SHA-256 (both): {data_h}")
        lines.append("")

    if missing:
        lines += ["-" * 70, "MISSING FILES (not found in destination):", "-" * 70]
        for f in sorted(missing):
            lines.append(f"  {f}")
        lines.append("")

    if corrupted:
        lines += ["-" * 70, "CHECKSUM MISMATCHES (possible corruption):", "-" * 70]
        for f, h in sorted(corrupted):
            lines.append(f"  {f}")
            lines.append(f"    SHA-256 (source): {h}")
        lines.append("")

    has_failure = bool(missing or corrupted)
    if args.strict and metadata_diff:
        has_failure = True

    if not has_failure and not metadata_diff:
        lines.append("All files verified successfully.")
    elif not has_failure:
        lines.append(f"All image data verified. {len(metadata_diff)} file(s) have metadata-only differences.")
    else:
        lines.append("VERIFICATION FAILED -- see details above.")

    report = "\n".join(lines)
    print(f"\n{report}")

    report_name = args.output or f"verify_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
    with open(report_name, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"\nReport saved to: {os.path.abspath(report_name)}")

    sys.exit(1 if has_failure else 0)


if __name__ == "__main__":
    main()
