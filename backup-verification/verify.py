#!/usr/bin/env python3
"""Entrypoint script for batch backup verification.

Reads verify_config.json and runs verify_backup.py for each enabled job.

Usage:
    python verify.py                        # run all enabled jobs
    python verify.py --list                 # list all jobs
    python verify.py --only "FUJIFILM X-T5" # run specific job(s) by name
    python verify.py --config my_config.json
    python verify.py --workers 8            # override worker count
    python verify.py --mode smart           # set comparison mode
    python verify.py --strict               # treat metadata diffs as failures
    python verify.py --no-cache             # pass --no-cache to all jobs
    python verify.py --clear-cache          # pass --clear-cache to all jobs
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime


DEFAULT_CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "verify_config.json")
VERIFY_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "verify_backup.py")


def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


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


def main() -> None:
    parser = argparse.ArgumentParser(description="Batch backup verification entrypoint")
    parser.add_argument("-c", "--config", type=str, default=DEFAULT_CONFIG,
                        help="Config file path (default: verify_config.json)")
    parser.add_argument("-l", "--list", action="store_true",
                        help="List all configured jobs and exit")
    parser.add_argument("--only", type=str, nargs="+", metavar="NAME",
                        help="Run only jobs matching these names")
    parser.add_argument("-w", "--workers", type=int, default=None,
                        help="Override worker thread count")
    parser.add_argument("-m", "--mode", type=str, default=None,
                        choices=["full", "smart", "data-only"],
                        help="Override comparison mode for all jobs")
    parser.add_argument("--strict", action="store_true",
                        help="Treat metadata-only diffs as failures")
    parser.add_argument("--no-cache", action="store_true",
                        help="Pass --no-cache to all jobs")
    parser.add_argument("--clear-cache", action="store_true",
                        help="Pass --clear-cache to all jobs")
    args = parser.parse_args()

    if not os.path.isfile(args.config):
        print(f"Error: config not found: {args.config}")
        sys.exit(1)

    config = load_config(args.config)

    if args.list:
        list_jobs(config)
        sys.exit(0)

    jobs = config.get("jobs", [])
    if not jobs:
        print(f"No jobs configured in {args.config}")
        sys.exit(1)

    # Filter jobs
    if args.only:
        only_lower = [n.lower() for n in args.only]
        selected = [j for j in jobs if j.get("name", "").lower() in only_lower]
        if not selected:
            print(f"No jobs matched: {', '.join(args.only)}")
            print("Available jobs:")
            for j in jobs:
                print(f"  - {j.get('name', '(unnamed)')}")
            sys.exit(1)
    else:
        selected = [j for j in jobs if j.get("enabled", True)]

    if not selected:
        print("No enabled jobs to run.")
        sys.exit(0)

    workers = args.workers or config.get("workers", 4)
    output_dir = config.get("output_dir", "./reports")
    os.makedirs(output_dir, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    print("=" * 70)
    print("Batch Verification")
    print(f"Config   : {args.config}")
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

        cmd = [
            sys.executable, VERIFY_SCRIPT,
            src, dest,
            "-w", str(job_workers),
            "-o", report_file,
            "--mode", job_mode,
        ]
        if args.strict:
            cmd.append("--strict")
        if args.no_cache:
            cmd.append("--no-cache")
        if args.clear_cache:
            cmd.append("--clear-cache")

        ret = subprocess.call(cmd)
        results.append((name, "PASS" if ret == 0 else "FAIL", report_file))

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

    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
