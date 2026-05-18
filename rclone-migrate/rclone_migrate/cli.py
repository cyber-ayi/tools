"""CLI entry points.

Single dispatcher `rmig <hash|copy|check|delete>` plus 4 standalone scripts
(`rmig-hash`, `rmig-copy`, `rmig-check`, `rmig-delete`) wired through
pyproject.toml [project.scripts].
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import List, Optional

from . import __version__
from . import audit as audit_mod
from . import config as config_mod
from . import ops
from . import profiles as profiles_mod
from . import query as query_mod
from . import state as state_mod
from . import verbose as verbose_mod
from . import wizard as wizard_mod


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "-c", "--config",
        help="Path to TOML config file. If omitted, defaults to "
             "<state-dir>/<job>.toml (the convention used by `rmig init`).",
    )
    p.add_argument(
        "-j", "--job", required=True,
        help="Job name (must match [[jobs]] entry)",
    )
    p.add_argument(
        "--state-dir", default="~/.local/share/rclone-migrate",
        help="Default state directory used to resolve <job>.toml when "
             "-c is omitted (default: ~/.local/share/rclone-migrate)",
    )
    p.add_argument(
        "-q", "--quiet", action="store_true",
        help="Suppress progress output (level=quiet)",
    )
    p.add_argument(
        "-v", "--verbose", action="count", default=0,
        help="-v: per-file details, rclone subprocess argv, phase timings, "
             "subprocess stderr always shown. -vv: SQL queries + cache flush events.",
    )
    p.add_argument(
        "--no-color", action="store_true",
        help="Disable ANSI color output (auto-disabled when stdout is not a TTY)",
    )
    p.add_argument(
        "--timestamps", action="store_true",
        help="Prefix every output line with HH:MM:SS (auto at -v or higher)",
    )
    p.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}",
    )


def _make_verbose(args: argparse.Namespace) -> verbose_mod.Verbose:
    """Construct a Verbose object reflecting the user's CLI flags AND
    register it as the rclone subprocess-argv logger."""
    if args.quiet:
        level = verbose_mod.QUIET
    else:
        level = verbose_mod.NORMAL + args.verbose   # 1 + 0/1/2
    color = False if args.no_color else None        # None → auto-detect TTY
    timestamps = True if args.timestamps else None  # None → auto from level
    v = verbose_mod.Verbose(level=level, color=color, timestamps=timestamps)
    # rclone subprocess wrapper consults this for -v argv logging
    from . import rclone as rclone_mod
    rclone_mod.set_verbose(v)
    return v


def _resolve_config_path(args: argparse.Namespace) -> str:
    """Return an absolute config path: explicit -c if given, else
    <state_dir>/<job>.toml by convention."""
    if args.config:
        return os.path.expanduser(args.config)
    state_dir = os.path.expanduser(getattr(args, "state_dir", "~/.local/share/rclone-migrate"))
    candidate = os.path.join(state_dir, f"{args.job}.toml")
    if not os.path.exists(candidate):
        print(
            f"no config found: tried -c (omitted) and convention "
            f"{candidate}\n"
            f"hint: pass -c CFG explicitly, or use `rmig init` which "
            f"writes the conventional path",
            file=sys.stderr,
        )
        sys.exit(2)
    return candidate


def _load(args: argparse.Namespace):
    cfg_path = _resolve_config_path(args)
    cfg = config_mod.load(cfg_path)
    args.config = cfg_path  # let downstream code read the resolved path
    try:
        job = cfg.get_job(args.job)
    except KeyError:
        print(f"job '{args.job}' not found in {cfg_path}", file=sys.stderr)
        print(f"available: {[j.name for j in cfg.jobs]}", file=sys.stderr)
        sys.exit(2)
    if not job.enabled:
        print(f"job '{args.job}' is disabled", file=sys.stderr)
        sys.exit(2)
    return cfg, job


def cmd_hash(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(
        prog="rmig-hash",
        description="Refresh hash manifest for src/dst (independent step).",
    )
    _add_common(p)
    p.add_argument("--side", choices=["src", "dst", "both"], default="both")
    p.add_argument("--full", action="store_true",
                   help="Ignore size+mtime and re-hash everything")
    args = p.parse_args(argv)
    cfg, job = _load(args)
    v = _make_verbose(args)
    progress = not args.quiet

    state_dir = cfg.state_dir_for(job)
    state_dir.mkdir(parents=True, exist_ok=True)
    with audit_mod.run(state_dir, op="hash") as ev:
        from . import manifest, state
        from .ops import (
            _allowed_sides, _open_state, emit_mhl_generation, negotiate_algo,
        )
        algo = negotiate_algo(job, cfg)
        ev.set_algo(algo)
        total = 0
        manifests: dict = {}

        if args.side in ("src", "both"):
            conn, _sd = _open_state(cfg, job)
            state.meta_set(conn, "hash_algorithm", algo)
            m = manifest.refresh(
                "src", job, algo, conn, state_dir,
                transfers=job.resolved_transfers(cfg.defaults),
                download=job.resolved_download(cfg.defaults),
                full=args.full,
                local_cache_in_root=job.resolved_local_cache_in_root(cfg.defaults),
                progress=progress,
                v=v,
            )
            if progress:
                print(f"[src] {len(m.entries)} files, algo={algo}, stats={m.stats}")
            ev.set_counts(src=len(m.entries))
            total += len(m.entries)
            manifests["src"] = m
            conn.close()

        if args.side in ("dst", "both"):
            conn, _sd = _open_state(cfg, job)
            m = manifest.refresh(
                "dst", job, algo, conn, state_dir,
                transfers=job.resolved_transfers(cfg.defaults),
                download=job.resolved_download(cfg.defaults),
                full=args.full,
                local_cache_in_root=job.resolved_local_cache_in_root(cfg.defaults),
                progress=progress,
                v=v,
            )
            if progress:
                print(f"[dst] {len(m.entries)} files, algo={algo}, stats={m.stats}")
            ev.set_counts(dst=len(m.entries))
            total += len(m.entries)
            manifests["dst"] = m
            conn.close()
        ev.set_counts(affected=total)
        ev.set_result("ok")

        # MHL emit (opt-in). hash op records each side as an in-place
        # attestation of the current full manifest.
        if job.resolved_emit_mhl(cfg.defaults):
            for side in _allowed_sides(job, cfg, list(manifests.keys())):
                emit_mhl_generation(
                    cfg, job, side,
                    entries=manifests[side].entries, algorithm=algo,
                    process="in-place", action="original", v=v,
                )
    return 0


def cmd_copy(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(
        prog="rmig-copy",
        description="Copy src files whose hash is missing from dst.",
    )
    _add_common(p)
    p.add_argument("--no-refresh", action="store_true",
                   help="(Reserved.) Currently always refreshes manifests.")
    p.add_argument("--full", action="store_true",
                   help="Force full re-hash before copying")
    p.add_argument("--dry-run", action="store_true",
                   help="Print what would be copied; do nothing")
    args = p.parse_args(argv)
    cfg, job = _load(args)
    v = _make_verbose(args)
    return ops.do_copy(
        cfg, job, no_refresh=args.no_refresh, full=args.full,
        dry_run=args.dry_run, progress=not args.quiet, v=v,
    )


def cmd_check(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(
        prog="rmig-check",
        description="Verify every src hash exists at dst.",
    )
    _add_common(p)
    p.add_argument("--rehash-all", action="store_true",
                   help="Force full re-hash before checking")
    args = p.parse_args(argv)
    cfg, job = _load(args)
    v = _make_verbose(args)
    return ops.do_check(cfg, job, rehash_all=args.rehash_all,
                        progress=not args.quiet, v=v)


def cmd_log(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(
        prog="rmig log",
        description="Show recent operation events (audit trail).",
    )
    p.add_argument("-c", "--config",
                   help="TOML config (defaults to <state-dir>/<job>.toml)")
    p.add_argument("-j", "--job", required=True)
    p.add_argument("--state-dir", default="~/.local/share/rclone-migrate")
    p.add_argument("--last", type=int, default=20,
                   help="Number of events to show (default 20)")
    p.add_argument("--op", choices=["hash", "copy", "copy-dry",
                                    "check", "delete", "delete-dry"],
                   help="Filter by operation type")
    p.add_argument("--result", choices=["ok", "fail", "refused", "partial",
                                        "crashed"],
                   help="Filter by result")
    p.add_argument("--json", action="store_true",
                   help="Emit JSON instead of a table")
    p.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}",
    )
    args = p.parse_args(argv)
    cfg_path = _resolve_config_path(args)
    cfg = config_mod.load(cfg_path)
    try:
        job = cfg.get_job(args.job)
    except KeyError:
        print(f"job '{args.job}' not found", file=sys.stderr)
        return 2

    state_dir = cfg.state_dir_for(job)
    if not (state_dir / "state.db").exists():
        print(f"no state.db at {state_dir}; nothing to show")
        return 0

    conn = state_mod.open_db(state_dir)
    rows = state_mod.query_events(
        conn, op=args.op, result=args.result, limit=args.last,
    )
    conn.close()

    if args.json:
        import json as _json
        print(_json.dumps(rows, indent=2, default=str))
        return 0

    if not rows:
        print("(no events match)")
        return 0

    from datetime import datetime
    print(
        f"{'ID':>5}  {'STARTED':<19}  {'OP':<11}  {'RESULT':<8}  "
        f"{'ALGO':<7}  {'COUNTS':<22}  LOG"
    )
    print("-" * 110)
    for r in rows:
        ts = datetime.fromtimestamp(r["started_ts"]).strftime("%Y-%m-%d %H:%M:%S")
        counts_parts = []
        if r["src_count"] is not None:
            counts_parts.append(f"src={r['src_count']}")
        if r["dst_count"] is not None:
            counts_parts.append(f"dst={r['dst_count']}")
        if r["affected"] is not None:
            counts_parts.append(f"aff={r['affected']}")
        counts = " ".join(counts_parts)
        algo = r["algo"] or ""
        result = r["result"] or "(running)"
        print(
            f"{r['id']:>5}  {ts:<19}  {r['op']:<11}  {result:<8}  "
            f"{algo:<7}  {counts:<22}  {r['log_path'] or ''}"
        )
    return 0


def cmd_file_status(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(
        prog="rmig file-status",
        description="Show backup status for one or many files.",
    )
    p.add_argument("-c", "--config",
                   help="TOML config (defaults to <state-dir>/<job>.toml)")
    p.add_argument("-j", "--job", required=True)
    p.add_argument("--state-dir", default="~/.local/share/rclone-migrate")
    p.add_argument("path", nargs="?",
                   help="Relative path (default side: src)")
    p.add_argument("--src", help="Look up a path on src side")
    p.add_argument("--dst", help="Look up a path on dst side")
    p.add_argument("--hash", help="Reverse-lookup all files with this hash")
    p.add_argument("--all", action="store_true",
                   help="List status of every src file")
    p.add_argument("--missing", action="store_true",
                   help="List src files whose hash is not at dst")
    p.add_argument("--orphan", action="store_true",
                   help="List dst files whose hash is not at src")
    p.add_argument("--json", action="store_true",
                   help="Emit JSON instead of human-readable text")
    p.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}",
    )
    args = p.parse_args(argv)
    cfg_path = _resolve_config_path(args)
    cfg = config_mod.load(cfg_path)
    try:
        job = cfg.get_job(args.job)
    except KeyError:
        print(f"job '{args.job}' not found", file=sys.stderr)
        return 2

    # --hash reverse lookup
    if args.hash:
        matches = query_mod.find_by_hash(cfg, job, hash=args.hash)
        if args.json:
            import json as _json
            print(_json.dumps(
                [{"side": m.side, "path": m.path, "hash": m.hash,
                  "size": m.size, "last_hashed": m.last_hashed}
                 for m in matches], indent=2,
            ))
        else:
            if not matches:
                print(f"(no files match hash {args.hash})")
                return 0
            for m in matches:
                print(f"  {m.side:<3}  {m.path}  size={m.size}")
        return 0

    # --all / --missing / --orphan list mode
    if args.all or args.missing or args.orphan:
        if args.orphan:
            results = query_mod.list_status(cfg, job, side="dst",
                                            filter_kind="orphan")
        elif args.missing:
            results = query_mod.list_status(cfg, job, side="src",
                                            filter_kind="missing")
        else:
            results = query_mod.list_status(cfg, job, side="src")
        if args.json:
            import json as _json
            print(_json.dumps(
                [query_mod.status_to_dict(s) for s in results], indent=2,
            ))
            return 0
        for s in results:
            mark = {"backed_up": "✓", "missing": "✗",
                    "orphan": "?", "unknown": "?"}.get(s.status, "?")
            print(f"  {mark}  {s.status:<10}  {s.path}")
        return 0 if all(s.status != "missing" for s in results) else 1

    # Single-file lookup
    side = "dst" if args.dst else "src"
    path = args.dst or args.src or args.path
    if not path:
        p.error("supply a path (positional, --src, --dst, --hash, --all, "
                "--missing, or --orphan)")

    st = query_mod.file_status(cfg, job, side=side, path=path)
    if args.json:
        import json as _json
        print(_json.dumps(query_mod.status_to_dict(st), indent=2, default=str))
        return 0

    from datetime import datetime
    def _fmt_ts(t):
        return datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M:%S") if t else "-"

    print(f"PATH          {st.path}  ({st.side} side)")
    print(f"FOUND         {'yes' if st.found_in_cache else 'no'}")
    if st.algorithm:
        print(f"ALGORITHM     {st.algorithm}")
    if st.size is not None:
        print(f"SIZE          {st.size:,} B")
    if st.hash:
        print(f"HASH          {st.hash}")
    if st.last_hashed is not None:
        print(f"LAST HASHED   {_fmt_ts(st.last_hashed)}")

    if st.matches:
        print(f"\nMATCHES ON {('dst' if st.side == 'src' else 'src').upper()} ({len(st.matches)}):")
        for m in st.matches:
            print(f"  {m.path}")
            print(f"    size={m.size}  last_hashed={_fmt_ts(m.last_hashed)}")
    else:
        print(f"\nMATCHES       (none)")

    status_label = {
        "backed_up": "✓ BACKED UP",
        "missing":   "✗ MISSING",
        "orphan":    "? ORPHAN (dst has no matching src)",
        "unknown":   "? UNKNOWN (not in cache)",
    }.get(st.status, st.status)
    print(f"\nSTATUS        {status_label}")

    if st.events:
        print(f"\nEVENT HISTORY ({len(st.events)}):")
        for e in st.events[:20]:
            ts = _fmt_ts(e["started_ts"])
            print(f"  event#{e['event_id']:<4}  {ts}  {e['op']:<11}  "
                  f"{e['outcome']:<10}")
        if len(st.events) > 20:
            print(f"  ... and {len(st.events) - 20} more")

    if st.warnings:
        print()
        for w in st.warnings:
            print(f"WARN: {w}")

    return 0 if st.status in ("backed_up",) else 1


def cmd_init(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(
        prog="rmig init",
        description="Set up a new rclone-migrate job (interactive or by flags).",
    )
    p.add_argument("--name", help="Job name (defaults to slug of src basename)")
    p.add_argument("--src", help="Source path or rclone remote")
    p.add_argument("--dst", help="Destination path or rclone remote")
    p.add_argument("--src-kind", choices=wizard_mod.KINDS,
                   help="Storage medium for src; auto-detected if omitted")
    p.add_argument("--dst-kind", choices=wizard_mod.KINDS,
                   help="Storage medium for dst; auto-detected if omitted")
    p.add_argument("--hash", help="Force hash algorithm (otherwise auto-negotiate)")
    p.add_argument("--write", help="Output TOML path "
                                   "(default: <state_dir>/<name>.toml)")
    p.add_argument("--state-dir", default="~/.local/share/rclone-migrate",
                   help="State directory (default: ~/.local/share/rclone-migrate)")
    p.add_argument("--no-probe", action="store_true",
                   help="Skip rclone backend probing (faster but less safe)")
    p.add_argument("-y", "--yes", action="store_true",
                   help="Non-interactive: never prompt; require all flags")
    p.add_argument("--run-check", action="store_true",
                   help="After writing, exec `rmig-check` immediately")
    p.add_argument("-v", "--verbose", action="count", default=0)
    p.add_argument("--no-color", action="store_true")
    p.add_argument("--timestamps", action="store_true")
    p.add_argument("-q", "--quiet", action="store_true")
    p.add_argument("--version", action="version",
                   version=f"%(prog)s {__version__}")
    args = p.parse_args(argv)

    v = _make_verbose(args)
    opts = wizard_mod.InitOptions(
        name=args.name or "",
        src=args.src or "",
        dst=args.dst or "",
        src_kind=args.src_kind or "",
        dst_kind=args.dst_kind or "",
        hash=args.hash,
        write=args.write or "",
        state_dir=args.state_dir,
        probe=not args.no_probe,
        interactive=not args.yes,
        run_check=args.run_check,
    )
    try:
        return wizard_mod.run_init(opts, v)
    except ValueError as e:
        v.error(str(e))
        return 2


def cmd_list_jobs(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(
        prog="rmig list-jobs",
        description="List all jobs defined in the config (or across all "
                    "configs in state_dir with --all). Status / AGE columns "
                    "reflect the last recorded check; run `rmig-check` to "
                    "refresh.",
    )
    p.add_argument("-c", "--config",
                   help="Single TOML to inspect (mutually exclusive with --all)")
    p.add_argument("--all", action="store_true",
                   help="Scan <state-dir>/*.toml for every configured job")
    p.add_argument("--state-dir", default="~/.local/share/rclone-migrate",
                   help="State directory used when --all is set "
                        "(default: ~/.local/share/rclone-migrate)")
    p.add_argument("--no-color", action="store_true",
                   help="Disable ANSI colors (auto-disabled on non-TTY)")
    p.add_argument("--no-status", action="store_true",
                   help="Skip per-job state.db reads (just list src→dst)")
    p.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}",
    )
    args = p.parse_args(argv)

    if not args.all and not args.config:
        p.error("either -c CFG or --all is required")

    if args.all:
        sd = Path(os.path.expanduser(args.state_dir))
        configs = sorted(sd.glob("*.toml"))
        if not configs:
            print(f"(no *.toml found under {sd})")
            return 0
    else:
        configs = [Path(os.path.expanduser(args.config))]

    rows = []
    for cfg_path in configs:
        try:
            cfg = config_mod.load(cfg_path)
        except Exception as e:
            rows.append({
                "config": cfg_path.stem, "job": "(load error)",
                "enabled": False, "status": f"⚠ {e}", "age": None,
                "src": "—", "dst": "—",
            })
            continue
        for j in cfg.jobs:
            row = {
                "config": cfg_path.stem,
                "job": j.name,
                "enabled": j.enabled,
                "src": j.src,
                "dst": j.dst,
                "hash": j.resolved_hash(cfg.defaults) or "(auto)",
                "status": "? never",
                "age": None,
            }
            if not args.no_status:
                _enrich_with_state(row, cfg, j)
            rows.append(row)

    _print_jobs_table(rows, color=_should_color(args))
    return 0


# --- helpers for cmd_list_jobs ---

def _should_color(args) -> bool:
    if getattr(args, "no_color", False):
        return False
    try:
        return sys.stdout.isatty()
    except Exception:
        return False


def _enrich_with_state(row: dict, cfg, job) -> None:
    """Populate row with status/age from state.db (if it exists)."""
    state_dir = cfg.state_dir_for(job)
    db_path = state_dir / "state.db"
    if not db_path.exists():
        return
    try:
        conn = state_mod.open_db(state_dir)
    except Exception:
        return
    try:
        last = state_mod.query_events(conn, op="check", limit=1)
        if not last:
            return
        e = last[0]
        import time as _time
        age = _time.time() - float(e["started_ts"])
        row["age"] = age
        if e["result"] == "ok":
            row["status"] = "✓ ok"
        elif e["result"] == "fail":
            n = e.get("affected") or 0
            row["status"] = f"✗ {n} miss"
        elif e["result"] == "crashed":
            row["status"] = "⚠ crashed"
        elif e["result"] == "refused":
            row["status"] = "⚠ refused"
        else:
            row["status"] = f"? {e['result']}"
    finally:
        conn.close()


def _humanize_age(seconds: Optional[float]) -> str:
    if seconds is None:
        return "—"
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds / 60)}m"
    if seconds < 86400:
        return f"{int(seconds / 3600)}h"
    return f"{int(seconds / 86400)}d"


def _color_for_age(seconds: Optional[float]) -> Optional[str]:
    """Pick ANSI color for AGE value; None for never."""
    if seconds is None:
        return verbose_mod.RED
    if seconds < 86400:           # <24h
        return verbose_mod.GREEN
    if seconds < 7 * 86400:       # 1–7d
        return verbose_mod.YELLOW
    return verbose_mod.RED        # >7d


def _color_for_status(status: str) -> Optional[str]:
    s = status.lstrip()
    if s.startswith("✓"):
        return verbose_mod.GREEN
    if s.startswith("✗"):
        return verbose_mod.RED
    if s.startswith("⚠"):
        return verbose_mod.YELLOW
    if s.startswith("?"):
        return verbose_mod.RED
    return None


def _print_jobs_table(rows: list, color: bool) -> None:
    if not rows:
        print("(no jobs)")
        return

    def _wrap(s: str, c: Optional[str]) -> str:
        if not color or not c:
            return s
        return f"{c}{s}{verbose_mod.RESET}"

    cfg_w = max(6, max(len(r["config"]) for r in rows))
    job_w = max(3, max(len(r["job"]) for r in rows))
    status_w = max(8, max(len(r["status"]) for r in rows))

    headers = ["CONFIG", "JOB", "STATUS", "AGE", "ENABLED", "HASH", "SRC -> DST"]
    print(
        f"{'CONFIG':<{cfg_w}}  {'JOB':<{job_w}}  "
        f"{'STATUS':<{status_w}}  {'AGE':<7}  {'ENABLED':<7}  "
        f"{'HASH':<8}  SRC -> DST"
    )
    sep_len = cfg_w + job_w + status_w + 7 + 7 + 8 + 30
    print("-" * sep_len)

    any_stale = False
    for r in rows:
        age_s = _humanize_age(r["age"])
        if r["age"] is not None and r["age"] >= 86400:
            any_stale = True
        if r["age"] is None and "never" in r["status"]:
            any_stale = True
        status_str = _wrap(f"{r['status']:<{status_w}}", _color_for_status(r["status"]))
        age_str = _wrap(f"{age_s:<7}", _color_for_age(r["age"]))
        enabled = "yes" if r["enabled"] else "no"
        hash_disp = r.get("hash", "(auto)")
        print(
            f"{r['config']:<{cfg_w}}  {r['job']:<{job_w}}  "
            f"{status_str}  {age_str}  {enabled:<7}  "
            f"{hash_disp:<8}  {r['src']} -> {r['dst']}"
        )

    print()
    note = (
        "Status / AGE reflect the last recorded check. "
        "Run `rmig-check -j JOB` to refresh."
    )
    if any_stale and color:
        print(_wrap(f"  {note}", verbose_mod.GRAY))
    else:
        print(f"  {note}")


def cmd_export_mhl(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(
        prog="rmig export-mhl",
        description="Write an ASC MHL v2.0 generation file for a side (or both) "
                    "from the current cache, without running copy/check/hash.",
    )
    _add_common(p)
    p.add_argument("--side", choices=["src", "dst", "both"], default="both")
    p.add_argument("--full", action="store_true",
                   help="Re-hash everything before emitting (default: trust cache)")
    args = p.parse_args(argv)
    cfg, job = _load(args)
    v = _make_verbose(args)
    progress = not args.quiet

    state_dir = cfg.state_dir_for(job)
    state_dir.mkdir(parents=True, exist_ok=True)
    with audit_mod.run(state_dir, op="export-mhl") as ev:
        from . import manifest as mf_mod
        from .ops import (
            _allowed_sides, _open_state, emit_mhl_generation, negotiate_algo,
        )
        algo = negotiate_algo(job, cfg)
        ev.set_algo(algo)
        if algo not in __import__(
            "rclone_migrate.mhl", fromlist=["MHL_ALGORITHMS"]
        ).MHL_ALGORITHMS:
            v.error(
                f"negotiated algo '{algo}' is not in MHL v2.0 set. "
                f"Set emit_mhl=true (which forces an MHL-compatible profile) "
                f"or pick a profile like 'dit'."
            )
            ev.set_result("fail")
            return 2

        sides = ("src", "dst") if args.side == "both" else (args.side,)
        emitted = 0
        for side in _allowed_sides(job, cfg, list(sides)):
            conn, _sd = _open_state(cfg, job)
            m = mf_mod.refresh(
                side, job, algo, conn, state_dir,
                transfers=job.resolved_transfers(cfg.defaults),
                download=job.resolved_download(cfg.defaults),
                full=args.full,
                local_cache_in_root=job.resolved_local_cache_in_root(cfg.defaults),
                progress=progress, v=v,
            )
            conn.close()
            p_out = emit_mhl_generation(
                cfg, job, side,
                entries=m.entries, algorithm=algo,
                process="in-place", action="original", v=v,
            )
            if p_out is not None:
                emitted += 1
        ev.set_counts(affected=emitted)
        ev.set_result("ok" if emitted else "fail")
        if not emitted:
            v.warn("no MHL generation written. See preceding warnings.")
            return 1
    return 0


def cmd_profiles(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(
        prog="rmig profiles",
        description="Inspect and manage hash profiles.",
    )
    sub = p.add_subparsers(dest="action", required=True)

    p_list = sub.add_parser("list", help="List all known profiles with sources")
    p_list.add_argument("-c", "--config",
                        help="Include [profiles.*] inline tables from this TOML")
    p_list.add_argument("--state-dir", default="~/.local/share/rclone-migrate")

    p_show = sub.add_parser("show", help="Show a profile's resolved content")
    p_show.add_argument("name")
    p_show.add_argument("-c", "--config",
                        help="Include [profiles.*] inline tables from this TOML")
    p_show.add_argument("--state-dir", default="~/.local/share/rclone-migrate")

    p_init = sub.add_parser(
        "init",
        help="Copy a bundled profile to <state-dir>/profiles/ for customization",
    )
    p_init.add_argument("name", help="Bundled profile name (e.g. dit)")
    p_init.add_argument("--state-dir", default="~/.local/share/rclone-migrate")
    p_init.add_argument("--force", action="store_true",
                        help="Overwrite an existing user profile of the same name")

    p_val = sub.add_parser("validate", help="Validate every reachable profile")
    p_val.add_argument("-c", "--config",
                       help="Include [profiles.*] inline tables from this TOML")
    p_val.add_argument("--state-dir", default="~/.local/share/rclone-migrate")

    args = p.parse_args(argv)
    if args.action == "list":
        return _profiles_list(args)
    if args.action == "show":
        return _profiles_show(args)
    if args.action == "init":
        return _profiles_init(args)
    if args.action == "validate":
        return _profiles_validate(args)
    return 2


def _load_inline_profiles(cfg_path: Optional[str]):
    """Read [profiles.*] tables out of a config file, returning the inline
    dict (or empty dict on miss / error). Used by `rmig profiles` subcommands
    that don't otherwise need a fully-validated Config.
    """
    if not cfg_path:
        return {}
    try:
        cfg = config_mod.load(cfg_path)
    except Exception as e:
        print(f"WARN: could not load -c {cfg_path}: {e}", file=sys.stderr)
        return {}
    return cfg.inline_profiles


def _profiles_list(args) -> int:
    state_dir = Path(os.path.expanduser(args.state_dir))
    inline = _load_inline_profiles(args.config)
    profs = profiles_mod.list_all(state_dir=state_dir, inline=inline or None)
    if not profs:
        print("(no profiles found)")
        return 0
    bundled = set(profiles_mod.list_bundled())
    name_w = max(4, max(len(p.name) for p in profs))
    src_w = max(6, max(len(p.source) for p in profs))
    print(f"{'NAME':<{name_w}}  {'SOURCE':<{src_w}}  MHL  DESCRIPTION")
    print("-" * (name_w + src_w + 30))
    for prof in profs:
        mhl_ok = _profile_mhl_compatible(prof)
        ann = ""
        if prof.source != "bundled" and prof.name in bundled:
            ann = "  [overrides bundled]"
        desc = prof.description or "(no description)"
        print(f"{prof.name:<{name_w}}  {prof.source:<{src_w}}  "
              f"{'✓' if mhl_ok else '✗':<3}  {desc}{ann}")
    return 0


def _profiles_show(args) -> int:
    state_dir = Path(os.path.expanduser(args.state_dir))
    inline = _load_inline_profiles(args.config)
    try:
        prof = profiles_mod.load(
            args.name, state_dir=state_dir, inline=inline or None,
        )
    except profiles_mod.ProfileError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    print(f"name        {prof.name}")
    print(f"source      {prof.source}")
    print(f"description {prof.description or '(none)'}")
    print(f"priority    {prof.priority}")
    if prof.multi_hash:
        print(f"multi_hash  {prof.multi_hash}")
    if prof.warnings:
        print("warnings:")
        for w in prof.warnings:
            print(f"  - {w}")
    print(f"mhl_compatible  {_profile_mhl_compatible(prof)}")
    return 0


def _profiles_init(args) -> int:
    bundled_dir = Path(profiles_mod.__file__).parent / "profiles"
    src = bundled_dir / f"{args.name}.toml"
    if not src.is_file():
        print(
            f"ERROR: no bundled profile named '{args.name}'.\n"
            f"Bundled: {', '.join(profiles_mod.list_bundled()) or '(none)'}",
            file=sys.stderr,
        )
        return 1
    state_dir = Path(os.path.expanduser(args.state_dir))
    dst_dir = state_dir / "profiles"
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / f"{args.name}.toml"
    if dst.exists() and not args.force:
        print(
            f"ERROR: {dst} already exists. Use --force to overwrite.",
            file=sys.stderr,
        )
        return 1
    import shutil
    shutil.copyfile(src, dst)
    print(f"wrote {dst}")
    print(f"edit it to customize; rmig will load this in preference to bundled.")
    return 0


def _profiles_validate(args) -> int:
    state_dir = Path(os.path.expanduser(args.state_dir))
    inline = _load_inline_profiles(args.config)
    failures = 0
    sources = []
    for name in profiles_mod.list_bundled():
        sources.append((name, "bundled"))
    for name in profiles_mod.list_user(state_dir):
        sources.append((name, f"user ({state_dir / 'profiles' / (name + '.toml')})"))
    for name in inline or {}:
        sources.append((name, "inline"))
    if not sources:
        print("(no profiles to validate)")
        return 0
    for name, source in sources:
        try:
            if source == "inline":
                profiles_mod.load(name, state_dir=state_dir, inline=inline)
            elif source == "bundled":
                profiles_mod.load(name)  # bundled-only path
            else:
                profiles_mod.load(name, state_dir=state_dir)
            print(f"  ok    {name:<20} ({source})")
        except profiles_mod.ProfileError as e:
            print(f"  FAIL  {name:<20} ({source}): {e}")
            failures += 1
    return 1 if failures else 0


def _profile_mhl_compatible(prof) -> bool:
    """True iff every algo in priority + multi_hash is in the MHL v2.0 set."""
    mhl_set = {"c4", "md5", "sha1", "xxh64", "xxh3", "xxh128"}
    return all(a in mhl_set for a in (prof.priority + prof.multi_hash))


def cmd_delete(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(
        prog="rmig-delete",
        description="Delete src files whose hash exists at dst (after check).",
    )
    _add_common(p)
    p.add_argument("--confirm", action="store_true",
                   help="Actually delete (default: dry-run)")
    args = p.parse_args(argv)
    cfg, job = _load(args)
    v = _make_verbose(args)
    return ops.do_delete(cfg, job, confirm=args.confirm,
                         progress=not args.quiet, v=v)


# Entry-point shims (return ints from argparse-driven funcs, but setuptools
# expects functions that may return None; sys.exit makes both work).

def _safe_exit(fn) -> int:
    """Translate LockContention into a clean exit-3 message, and Ctrl-C into
    a clean exit-130, instead of a traceback. Other exceptions propagate."""
    try:
        return fn()
    except audit_mod.LockContention as e:
        print(f"REFUSE: {e}", file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        # rclone children share the tty process group and have already
        # exited on the same SIGINT, removing their own .partial files.
        # Completed files are committed/recorded — rerun resumes.
        print("\nInterrupted — rerun to resume (completed files are kept).",
              file=sys.stderr)
        return 130


def hash_cmd() -> None:  sys.exit(_safe_exit(cmd_hash))
def copy_cmd() -> None:  sys.exit(_safe_exit(cmd_copy))
def check_cmd() -> None: sys.exit(_safe_exit(cmd_check))
def delete_cmd() -> None: sys.exit(_safe_exit(cmd_delete))
def log_cmd() -> None: sys.exit(cmd_log())
def file_status_cmd() -> None: sys.exit(cmd_file_status())
def init_cmd() -> None: sys.exit(cmd_init())


def main(argv: Optional[List[str]] = None) -> None:
    if argv is None:
        argv = sys.argv[1:]
    if argv and argv[0] in ("--version", "-V"):
        print(f"rmig {__version__}")
        sys.exit(0)
    if not argv or argv[0] in ("-h", "--help"):
        print(
            "usage: rmig {init|hash|copy|check|delete|list-jobs|log|"
            "file-status|profiles|export-mhl} [options]\n"
            "       rmig --version",
            file=sys.stderr,
        )
        sys.exit(0 if argv and argv[0] in ("-h", "--help") else 2)
    sub, rest = argv[0], argv[1:]
    table = {
        "hash": cmd_hash, "copy": cmd_copy, "check": cmd_check,
        "delete": cmd_delete, "list-jobs": cmd_list_jobs,
        "log": cmd_log, "file-status": cmd_file_status,
        "init": cmd_init, "profiles": cmd_profiles,
        "export-mhl": cmd_export_mhl,
    }
    if sub not in table:
        print(f"unknown subcommand: {sub}", file=sys.stderr)
        sys.exit(2)
    fn = table[sub]
    sys.exit(_safe_exit(lambda: fn(rest)))


if __name__ == "__main__":
    main()
