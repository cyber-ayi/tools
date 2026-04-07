#!/usr/bin/env python3
"""Unified entrypoint for backup verification.

Single verification:
    python verify.py <src_dir> <dest_dir> [options]

Batch verification (from config file):
    python verify.py -c verify_config.json [options]
    python verify.py -c verify_config.json --only "FUJIFILM X-T5"
    python verify.py -c verify_config.json --list

No arguments (uses default config if present):
    python verify.py
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime

from verify_backup import run_verify


DEFAULT_CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "verify_config.json")


def load_config(path: str) -> dict:
    """Load and parse JSON config file with friendly error handling.

    Paths in config should use forward slashes (e.g., "W:/path").
    They are automatically normalized for the current OS.
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            config = json.load(f)
    except json.JSONDecodeError as e:
        print(f"Error: Failed to parse {path}: {e}")
        print('Hint: Use forward slashes in paths (e.g., "W:/storage/photos")')
        print('      Backslashes in JSON require escaping ("W:\\\\path"), which is error-prone.')
        sys.exit(1)
    except FileNotFoundError:
        print(f"Error: config not found: {path}")
        sys.exit(1)

    # Normalize paths for current OS
    for job in config.get("jobs", []):
        for key in ("src", "dest"):
            if key in job:
                job[key] = os.path.normpath(job[key])

    if "output_dir" in config:
        config["output_dir"] = os.path.normpath(config["output_dir"])

    return config


def list_jobs(config: dict) -> None:
    jobs = config.get("jobs", [])
    if not jobs:
        print("No jobs configured.")
        return
    print(f"{'NAME':<30s} {'ENABLED':<8s} SRC -> DEST")
    print("-" * 80)
    for j in jobs:
        name = j.get("name", "(unnamed)")
        enabled = "Yes" if j.get("enabled", True) else "No"
        src = j.get("src", "?")
        dest = j.get("dest", "?")
        print(f"{name:<30s} {enabled:<8s} {src} -> {dest}")


def run_batch(args, config: dict) -> int:
    """Run batch verification from config file. Returns exit code."""
    jobs = config.get("jobs", [])
    if not jobs:
        print("No jobs configured in config file")
        return 1

    # Filter jobs
    if args.only:
        only_lower = [n.lower() for n in args.only]
        selected = [j for j in jobs if j.get("name", "").lower() in only_lower]
        if not selected:
            print(f"No jobs matched: {', '.join(args.only)}")
            print("Available jobs:")
            for j in jobs:
                print(f"  - {j.get('name', '(unnamed)')}")
            return 1
    else:
        selected = [j for j in jobs if j.get("enabled", True)]

    if not selected:
        print("No enabled jobs to run.")
        return 0

    default_workers = min(os.cpu_count() or 4, 16)
    workers = args.workers or config.get("workers", default_workers)
    output_dir = config.get("output_dir", "./reports")
    os.makedirs(output_dir, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    print("=" * 70)
    print("Batch Verification")
    print(f"Jobs     : {len(selected)} selected")
    print(f"Workers  : {workers}")
    print(f"Reports  : {os.path.abspath(output_dir)}")
    print("=" * 70)

    t_start = time.time()
    results: list[tuple[str, str, str]] = []

    for i, job in enumerate(selected, 1):
        name = job.get("name", f"job_{i}")
        src = job.get("src", "")
        dest = job.get("dest", "")
        job_workers = job.get("workers", workers)

        safe_name = "".join(c if c.isalnum() or c in "-_ " else "_" for c in name).strip()
        report_file = os.path.join(output_dir, f"{safe_name}_{timestamp}.txt")

        print(f"\n{'#' * 70}")
        print(f"# [{i}/{len(selected)}] {name}")
        print(f"#   src  : {src}")
        print(f"#   dest : {dest}")
        print("#" * 70)

        if not os.path.isdir(src):
            print(f"  SKIP: source not found: {src}")
            results.append((name, "SKIP", "source not found"))
            continue
        if not os.path.isdir(dest):
            print(f"  SKIP: dest not found: {dest}")
            results.append((name, "SKIP", "dest not found"))
            continue

        # Resolve mode: CLI override > per-job > global config > default
        job_mode = args.mode or job.get("mode") or config.get("mode", "smart")

        rc = run_verify(
            src, dest,
            workers=job_workers,
            mode=job_mode,
            strict=args.strict,
            no_cache=args.no_cache,
            clear_cache=args.clear_cache,
            output=report_file,
            verbose=args.verbose,
            dry_run=args.dry_run,
        )
        results.append((name, "PASS" if rc == 0 else "FAIL", report_file))

    elapsed = time.time() - t_start

    # Summary
    print(f"\n{'=' * 70}")
    print(f"Batch Summary  ({elapsed:.1f}s)")
    print("=" * 70)
    print(f"{'JOB':<30s} {'STATUS':<6s} REPORT")
    print("-" * 70)
    for name, status, detail in results:
        print(f"{name:<30s} {status:<6s} {detail}")

    failed = sum(1 for _, s, _ in results if s == "FAIL")
    skipped = sum(1 for _, s, _ in results if s == "SKIP")
    passed = sum(1 for _, s, _ in results if s == "PASS")
    print("-" * 70)
    print(f"Passed: {passed}  Failed: {failed}  Skipped: {skipped}")

    return 1 if failed else 0


def main() -> None:
    default_workers = min(os.cpu_count() or 4, 16)

    parser = argparse.ArgumentParser(
        description="Backup verification tool",
        usage="""%(prog)s <src_dir> <dest_dir> [options]
       %(prog)s -c CONFIG [options]
       %(prog)s                          (uses default config)""",
    )

    # Positional args for single mode (optional)
    parser.add_argument("src_dir", nargs="?", default=None,
                        help="Source directory (SD card)")
    parser.add_argument("dest_dir", nargs="?", default=None,
                        help="Destination directory (NAS backup)")

    # Batch mode args
    parser.add_argument("-c", "--config", type=str, default=None,
                        help="Config file for batch verification")
    parser.add_argument("-l", "--list", action="store_true",
                        help="List all configured jobs and exit")
    parser.add_argument("--only", type=str, nargs="+", metavar="NAME",
                        help="Run only jobs matching these names (batch mode)")

    # Common args
    parser.add_argument("-w", "--workers", type=int, default=None,
                        help="Override worker thread count (default: auto, max 16)")
    parser.add_argument("-m", "--mode", type=str, default=None,
                        choices=["full", "smart", "data-only"],
                        help="Comparison mode (default: smart)")
    parser.add_argument("--strict", action="store_true",
                        help="Treat metadata-only diffs as failures")
    parser.add_argument("--no-cache", action="store_true",
                        help="Skip hash cache entirely")
    parser.add_argument("--clear-cache", action="store_true",
                        help="Delete existing cache and rebuild from scratch")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Verbose output (show every file)")
    parser.add_argument("-n", "--dry-run", action="store_true",
                        help="Scan only, show cache hit rate, do not hash")
    parser.add_argument("-o", "--output", type=str, default=None,
                        help="Report file path (single mode only)")
    args = parser.parse_args()

    # Determine mode: single vs batch
    has_positional = args.src_dir is not None
    has_config = args.config is not None

    if has_positional and has_config:
        parser.error("Cannot specify both positional args (src dest) and --config")

    if args.list:
        # --list requires --config or default config
        config_path = args.config or DEFAULT_CONFIG
        if not os.path.isfile(config_path):
            print(f"Error: config not found: {config_path}")
            sys.exit(1)
        config = load_config(config_path)
        list_jobs(config)
        sys.exit(0)

    if has_config:
        # Batch mode
        config = load_config(args.config)
        rc = run_batch(args, config)
        sys.exit(rc)

    if has_positional:
        if args.dest_dir is None:
            parser.error("dest_dir is required in single mode")

        # Single mode
        rc = run_verify(
            args.src_dir,
            args.dest_dir,
            workers=args.workers or default_workers,
            mode=args.mode or "smart",
            strict=args.strict,
            no_cache=args.no_cache,
            clear_cache=args.clear_cache,
            output=args.output,
            verbose=args.verbose,
            dry_run=args.dry_run,
        )
        sys.exit(rc)

    # No args at all — try default config
    if os.path.isfile(DEFAULT_CONFIG):
        config = load_config(DEFAULT_CONFIG)
        rc = run_batch(args, config)
        sys.exit(rc)

    parser.print_help()
    sys.exit(1)


if __name__ == "__main__":
    main()
