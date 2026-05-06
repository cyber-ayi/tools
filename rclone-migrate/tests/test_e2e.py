"""End-to-end test using two local directories.

Exercises the full copy → check → delete flow with:
  - dst already containing one file under a different name (hash match)
  - src containing duplicate-content files (must all be deletable)
  - signature-based protection against src changing between check and delete
"""
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Optional

import pytest

from rclone_migrate import config as config_mod
from rclone_migrate import ops


pytestmark = pytest.mark.skipif(
    shutil.which("rclone") is None, reason="rclone not installed"
)


def _make_tree(root: Path, files: dict) -> None:
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content if isinstance(content, bytes) else content.encode())


def _write_config(
    path: Path, src: Path, dst: Path, state_dir: Path,
    *, hash: Optional[str] = None, require_within: str = "24h",
    require_confirm: bool = True,
) -> None:
    lines = [
        "[defaults]",
        f"state_dir = '{state_dir}'",
        "transfers = 2",
        "local_cache_in_root = true",
    ]
    if hash:
        lines.append(f"hash = '{hash}'")
    lines += [
        "[delete]",
        f"require_check_within = '{require_within}'",
        f"require_confirm = {str(require_confirm).lower()}",
        "remove_empty_src_dirs = true",
        "[[jobs]]",
        "name = 't'",
        f"src = '{src}'",
        f"dst = '{dst}'",
    ]
    path.write_text("\n".join(lines) + "\n")


def _list(p: Path) -> set:
    """Set of relative paths under p (excluding our cache file + WAL siblings)."""
    out = set()
    for dp, _dn, fn in os.walk(p):
        for n in fn:
            if n.startswith(".rmig-cache.db"):  # also catches -wal, -shm, -journal
                continue
            out.add(str((Path(dp) / n).relative_to(p)))
    return out


def test_full_flow_records_events(tmp_path: Path):
    """copy → check → delete each emit one event; deleted files appear in
    file_events as outcome='deleted'."""
    src = tmp_path / "src"; dst = tmp_path / "dst"; sd = tmp_path / "state"
    src.mkdir(); dst.mkdir(); sd.mkdir()
    _make_tree(src, {"a.txt": b"alpha", "b.txt": b"beta"})
    _make_tree(dst, {"x.txt": b"alpha"})

    cfg_path = tmp_path / "c.toml"
    _write_config(cfg_path, src, dst, sd, require_within="24h")
    cfg = config_mod.load(cfg_path)
    job = cfg.get_job("t")

    assert ops.do_copy(cfg, job, progress=False) == 0
    assert ops.do_check(cfg, job, progress=False) == 0
    assert ops.do_delete(cfg, job, confirm=True, progress=False) == 0

    from rclone_migrate import state
    conn = state.open_db(sd / "t")
    events = state.query_events(conn)
    ops_seen = {e["op"] for e in events}
    assert {"copy", "check", "delete"}.issubset(ops_seen)
    # All ended with result=ok
    for e in events:
        if e["op"] in ("copy", "check", "delete"):
            assert e["result"] == "ok", f"event {e}"

    # file_events: 2 deletes (a.txt, b.txt) + 1 copy (b.txt)
    deleted = state.query_file_events(conn, side="src")
    deleted_paths = {f["path"] for f in deleted if f["outcome"] == "deleted"}
    assert deleted_paths == {"a.txt", "b.txt"}
    copied = state.query_file_events(conn, side="dst")
    copied_paths = {f["path"] for f in copied if f["outcome"] == "copied"}
    assert copied_paths == {"b.txt"}
    conn.close()


def test_check_failure_records_missing_in_file_events(tmp_path: Path):
    src = tmp_path / "src"; dst = tmp_path / "dst"; sd = tmp_path / "state"
    src.mkdir(); dst.mkdir(); sd.mkdir()
    _make_tree(src, {"a.txt": b"a", "b.txt": b"b"})
    _make_tree(dst, {"x.txt": b"a"})
    _write_config(tmp_path / "c.toml", src, dst, sd)
    cfg = config_mod.load(tmp_path / "c.toml")
    job = cfg.get_job("t")
    assert ops.do_check(cfg, job, progress=False) == 1

    from rclone_migrate import state
    conn = state.open_db(sd / "t")
    miss = state.query_file_events(conn, side="src")
    assert any(f["path"] == "b.txt" and f["outcome"] == "missing"
               for f in miss)
    conn.close()


def test_full_flow(tmp_path: Path):
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    state = tmp_path / "state"
    src.mkdir(); dst.mkdir(); state.mkdir()

    _make_tree(src, {
        "a.txt": b"a",
        "sub/dup.txt": b"a",         # same content as a.txt — should be dedup'd
        "b.txt": b"b",
    })
    _make_tree(dst, {
        "already.txt": b"a",         # different name, same content as src "a"
    })

    cfg_path = tmp_path / "c.toml"
    _write_config(cfg_path, src, dst, state)
    cfg = config_mod.load(cfg_path)
    job = cfg.get_job("t")

    # 1. copy
    rc = ops.do_copy(cfg, job, progress=False)
    assert rc == 0
    # b.txt should be the only thing copied (a's content is already at dst)
    files_in_dst = _list(dst)
    assert "already.txt" in files_in_dst
    assert "b.txt" in files_in_dst
    assert len(files_in_dst) == 2     # nothing else snuck in

    # 2. check — must pass
    rc = ops.do_check(cfg, job, progress=False)
    assert rc == 0

    # 3. delete — without confirm: dry-run, src untouched
    rc = ops.do_delete(cfg, job, confirm=False, progress=False)
    assert rc == 0
    assert _list(src) == {"a.txt", "sub/dup.txt", "b.txt"}

    # 4. delete --confirm: all 3 src files (incl the duplicate) gone
    rc = ops.do_delete(cfg, job, confirm=True, progress=False)
    assert rc == 0
    assert _list(src) == set()
    assert _list(dst) == {"already.txt", "b.txt"}


def test_signature_blocks_modified_src(tmp_path: Path):
    src = tmp_path / "src"; dst = tmp_path / "dst"; state = tmp_path / "state"
    src.mkdir(); dst.mkdir(); state.mkdir()
    _make_tree(src, {"a.txt": b"a"})
    _make_tree(dst, {"x.txt": b"a"})       # already there

    cfg_path = tmp_path / "c.toml"
    _write_config(cfg_path, src, dst, state)
    cfg = config_mod.load(cfg_path)
    job = cfg.get_job("t")

    assert ops.do_check(cfg, job, progress=False) == 0
    # tamper: add a new src file after check
    (src / "new.txt").write_bytes(b"new content")
    # delete must refuse
    rc = ops.do_delete(cfg, job, confirm=True, progress=False)
    assert rc == 2
    assert (src / "a.txt").exists()
    assert (src / "new.txt").exists()


def test_check_fails_when_dst_missing_hash(tmp_path: Path):
    src = tmp_path / "src"; dst = tmp_path / "dst"; state = tmp_path / "state"
    src.mkdir(); dst.mkdir(); state.mkdir()
    _make_tree(src, {"a.txt": b"a", "b.txt": b"b"})
    _make_tree(dst, {"x.txt": b"a"})       # b is missing at dst

    cfg_path = tmp_path / "c.toml"
    _write_config(cfg_path, src, dst, state)
    cfg = config_mod.load(cfg_path)
    job = cfg.get_job("t")

    rc = ops.do_check(cfg, job, progress=False)
    assert rc == 1


def test_delete_refuses_without_check(tmp_path: Path):
    src = tmp_path / "src"; dst = tmp_path / "dst"; state = tmp_path / "state"
    src.mkdir(); dst.mkdir(); state.mkdir()
    _make_tree(src, {"a.txt": b"a"})
    _make_tree(dst, {"a.txt": b"a"})
    _write_config(tmp_path / "c.toml", src, dst, state)
    cfg = config_mod.load(tmp_path / "c.toml")
    job = cfg.get_job("t")
    # No check has been run yet
    rc = ops.do_delete(cfg, job, confirm=True, progress=False)
    assert rc == 2
    assert (src / "a.txt").exists()


def test_check_within_timeout(tmp_path: Path):
    src = tmp_path / "src"; dst = tmp_path / "dst"; state = tmp_path / "state"
    src.mkdir(); dst.mkdir(); state.mkdir()
    _make_tree(src, {"a.txt": b"a"})
    _make_tree(dst, {"a.txt": b"a"})
    _write_config(tmp_path / "c.toml", src, dst, state, require_within="500ms")
    cfg = config_mod.load(tmp_path / "c.toml")
    job = cfg.get_job("t")

    assert ops.do_check(cfg, job, progress=False) == 0
    time.sleep(1.0)
    rc = ops.do_delete(cfg, job, confirm=True, progress=False)
    assert rc == 2
    assert (src / "a.txt").exists()


def test_cache_persists_across_runs(tmp_path: Path):
    """Second hash run after no changes should hit cache for everything."""
    src = tmp_path / "src"; dst = tmp_path / "dst"; state = tmp_path / "state"
    src.mkdir(); dst.mkdir(); state.mkdir()
    _make_tree(src, {f"f{i}.txt": f"content {i}".encode() for i in range(5)})

    _write_config(tmp_path / "c.toml", src, dst, state)
    cfg = config_mod.load(tmp_path / "c.toml")
    job = cfg.get_job("t")

    from rclone_migrate import manifest
    from rclone_migrate.ops import _open_state, negotiate_algo
    conn, state_dir = _open_state(cfg, job)
    algo = negotiate_algo(job, cfg.defaults.hash)

    m1 = manifest.refresh("src", job, algo, conn, state_dir, progress=False,
                          local_cache_in_root=True)
    assert m1.stats.rehashed == 5
    assert m1.stats.valid == 0

    m2 = manifest.refresh("src", job, algo, conn, state_dir, progress=False,
                          local_cache_in_root=True)
    assert m2.stats.valid == 5
    assert m2.stats.rehashed == 0
    conn.close()


def test_size_filter_skips_unrelated_dst_files(tmp_path: Path):
    """Verify-mode optimization: dst files whose size isn't in src's set are
    not hashed at all. Critical when dst >> src (NAS archive vs SD card).

    Strategy: put a 'big' file at dst with a unique size that doesn't match
    any src file. With size_filter applied, that file is excluded from the
    dst manifest and doesn't get hashed.
    """
    src = tmp_path / "src"; dst = tmp_path / "dst"; state = tmp_path / "state"
    src.mkdir(); dst.mkdir(); state.mkdir()
    _make_tree(src, {"a.txt": b"a", "b.txt": b"bb"})
    _make_tree(dst, {
        "matches_a.txt": b"a",                # size 1, matches src "a.txt"
        "matches_b.txt": b"bb",               # size 2, matches src "b.txt"
        "huge_unrelated.bin": b"x" * 10_000,  # size 10000, not in src size set
    })
    _write_config(tmp_path / "c.toml", src, dst, state)
    cfg = config_mod.load(tmp_path / "c.toml")
    job = cfg.get_job("t")

    from rclone_migrate import manifest
    from rclone_migrate.ops import _open_state, negotiate_algo
    conn, state_dir = _open_state(cfg, job)
    algo = negotiate_algo(job, cfg.defaults.hash)

    # Pretend src has only sizes {1, 2}; dst should skip the 10000-byte file
    src_size_set = {1, 2}
    dst_mf = manifest.refresh(
        "dst", job, algo, conn, state_dir, progress=False,
        local_cache_in_root=True, size_filter=src_size_set,
    )
    paths = {e.path for e in dst_mf.entries}
    assert "matches_a.txt" in paths
    assert "matches_b.txt" in paths
    assert "huge_unrelated.bin" not in paths   # filtered out, never hashed
    conn.close()


def test_size_filter_propagates_through_refresh_both(tmp_path: Path):
    """ops.refresh_both with default filter_dst_by_src_size=True must compute
    src first, then constrain dst hashing to src's size set."""
    src = tmp_path / "src"; dst = tmp_path / "dst"; state = tmp_path / "state"
    src.mkdir(); dst.mkdir(); state.mkdir()
    _make_tree(src, {"a.txt": b"a"})
    _make_tree(dst, {
        "x.txt": b"a",                        # size match
        "unrelated.bin": b"x" * 5_000,        # excluded
    })
    _write_config(tmp_path / "c.toml", src, dst, state)
    cfg = config_mod.load(tmp_path / "c.toml")
    job = cfg.get_job("t")

    from rclone_migrate.ops import refresh_both
    src_mf, dst_mf, _algo, conn, _ = refresh_both(cfg, job, progress=False)
    paths = {e.path for e in dst_mf.entries}
    assert "x.txt" in paths
    assert "unrelated.bin" not in paths
    conn.close()


def test_size_filter_disabled_by_flag(tmp_path: Path):
    """Set filter_dst_by_src_size=False — dst manifest should include all files."""
    src = tmp_path / "src"; dst = tmp_path / "dst"; state = tmp_path / "state"
    src.mkdir(); dst.mkdir(); state.mkdir()
    _make_tree(src, {"a.txt": b"a"})
    _make_tree(dst, {"x.txt": b"a", "unrelated.bin": b"x" * 5_000})
    _write_config(tmp_path / "c.toml", src, dst, state)
    cfg = config_mod.load(tmp_path / "c.toml")
    job = cfg.get_job("t")

    from rclone_migrate.ops import refresh_both
    src_mf, dst_mf, _algo, conn, _ = refresh_both(
        cfg, job, progress=False, filter_dst_by_src_size=False,
    )
    paths = {e.path for e in dst_mf.entries}
    assert paths == {"x.txt", "unrelated.bin"}
    conn.close()


def test_cache_invalidates_on_mtime_change(tmp_path: Path):
    src = tmp_path / "src"; dst = tmp_path / "dst"; state = tmp_path / "state"
    src.mkdir(); dst.mkdir(); state.mkdir()
    f = src / "a.txt"
    f.write_bytes(b"original")
    _write_config(tmp_path / "c.toml", src, dst, state)
    cfg = config_mod.load(tmp_path / "c.toml")
    job = cfg.get_job("t")

    from rclone_migrate import manifest
    from rclone_migrate.ops import _open_state, negotiate_algo
    conn, state_dir = _open_state(cfg, job)
    algo = negotiate_algo(job, cfg.defaults.hash)

    manifest.refresh("src", job, algo, conn, state_dir, progress=False,
                     local_cache_in_root=True)
    # Modify file (different size + mtime)
    time.sleep(0.05)
    f.write_bytes(b"modified content")
    m2 = manifest.refresh("src", job, algo, conn, state_dir, progress=False,
                          local_cache_in_root=True)
    assert m2.stats.stale + m2.stats.new == 1
    assert m2.stats.rehashed == 1
    conn.close()
