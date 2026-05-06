"""Tests for the file-level traceability query API."""
from pathlib import Path

from rclone_migrate import config as config_mod
from rclone_migrate import ops, query


def _make_tree(root: Path, files: dict) -> None:
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content if isinstance(content, bytes) else content.encode())


def _write_config(path: Path, src: Path, dst: Path, state_dir: Path) -> None:
    path.write_text(
        f"[defaults]\n"
        f"state_dir = '{state_dir}'\n"
        f"transfers = 2\n"
        f"local_cache_in_root = true\n"
        f"[delete]\nrequire_check_within = '24h'\nrequire_confirm = true\n"
        f"[[jobs]]\nname = 't'\nsrc = '{src}'\ndst = '{dst}'\n"
    )


def _setup(tmp_path: Path, src_files: dict, dst_files: dict):
    src = tmp_path / "src"; dst = tmp_path / "dst"; sd = tmp_path / "state"
    src.mkdir(); dst.mkdir(); sd.mkdir()
    _make_tree(src, src_files)
    _make_tree(dst, dst_files)
    cfg_path = tmp_path / "c.toml"
    _write_config(cfg_path, src, dst, sd)
    cfg = config_mod.load(cfg_path)
    job = cfg.get_job("t")
    return cfg, job, src, dst


def test_file_status_backed_up(tmp_path: Path):
    cfg, job, _, _ = _setup(
        tmp_path,
        {"a.txt": b"alpha"},
        {"renamed_a.txt": b"alpha"},      # different name, same content
    )
    assert ops.do_check(cfg, job, progress=False) == 0

    st = query.file_status(cfg, job, side="src", path="a.txt")
    assert st.found_in_cache
    assert st.status == "backed_up"
    assert len(st.matches) == 1
    assert st.matches[0].path == "renamed_a.txt"


def test_file_status_missing(tmp_path: Path):
    cfg, job, _, _ = _setup(
        tmp_path,
        {"a.txt": b"alpha", "missing.txt": b"unique"},
        {"renamed_a.txt": b"alpha"},
    )
    # Don't run check (would fail anyway); just hash both sides via refresh
    from rclone_migrate import manifest, state as state_mod
    from rclone_migrate.ops import _open_state, negotiate_algo
    conn, state_dir = _open_state(cfg, job)
    algo = negotiate_algo(job, cfg)
    state_mod.meta_set(conn, "hash_algorithm", algo)
    manifest.refresh("src", job, algo, conn, state_dir, progress=False)
    manifest.refresh("dst", job, algo, conn, state_dir, progress=False)
    conn.close()

    st = query.file_status(cfg, job, side="src", path="missing.txt")
    assert st.status == "missing"
    assert st.matches == []


def test_file_status_unknown(tmp_path: Path):
    cfg, job, _, _ = _setup(tmp_path, {"a.txt": b"a"}, {"x.txt": b"a"})
    # No hash run at all
    st = query.file_status(cfg, job, side="src", path="a.txt")
    assert st.status == "unknown"
    assert not st.found_in_cache


def test_file_status_includes_event_history(tmp_path: Path):
    """After a failing check, file_events should record the missing path,
    and file_status should surface that history."""
    cfg, job, _, _ = _setup(
        tmp_path, {"a.txt": b"a", "b.txt": b"b"}, {"x.txt": b"a"},
    )
    rc = ops.do_check(cfg, job, progress=False)
    assert rc == 1

    st = query.file_status(cfg, job, side="src", path="b.txt")
    assert st.status == "missing"
    assert any(e["op"] == "check" and e["outcome"] == "missing"
               for e in st.events)


def test_list_status_filter_missing(tmp_path: Path):
    cfg, job, _, _ = _setup(
        tmp_path,
        {"a.txt": b"a", "b.txt": b"b", "c.txt": b"c"},
        {"x.txt": b"a"},
    )
    from rclone_migrate import manifest, state as state_mod
    from rclone_migrate.ops import _open_state, negotiate_algo
    conn, state_dir = _open_state(cfg, job)
    algo = negotiate_algo(job, cfg)
    state_mod.meta_set(conn, "hash_algorithm", algo)
    manifest.refresh("src", job, algo, conn, state_dir, progress=False)
    manifest.refresh("dst", job, algo, conn, state_dir, progress=False)
    conn.close()

    missing = query.list_status(cfg, job, side="src", filter_kind="missing")
    paths = {s.path for s in missing}
    assert paths == {"b.txt", "c.txt"}

    backed = query.list_status(cfg, job, side="src", filter_kind="backed_up")
    assert {s.path for s in backed} == {"a.txt"}


def test_find_by_hash(tmp_path: Path):
    cfg, job, _, _ = _setup(
        tmp_path,
        {"a.txt": b"a", "dup_a.txt": b"a"},
        {"renamed_a.txt": b"a"},
    )
    from rclone_migrate import manifest, state as state_mod
    from rclone_migrate.ops import _open_state, negotiate_algo
    conn, state_dir = _open_state(cfg, job)
    algo = negotiate_algo(job, cfg)
    state_mod.meta_set(conn, "hash_algorithm", algo)
    src_mf = manifest.refresh("src", job, algo, conn, state_dir, progress=False)
    manifest.refresh("dst", job, algo, conn, state_dir, progress=False)
    conn.close()

    # Get the actual hash for content "a"
    a_hash = next(e.hash for e in src_mf.entries if e.path == "a.txt")
    matches = query.find_by_hash(cfg, job, hash=a_hash)
    paths = {(m.side, m.path) for m in matches}
    assert ("src", "a.txt") in paths
    assert ("src", "dup_a.txt") in paths
    assert ("dst", "renamed_a.txt") in paths
