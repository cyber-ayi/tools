"""Tests for the unified _refresh_remote path.

We don't have a real SSH/SFTP backend in CI, so we monkeypatch
`rclone.is_local`, `rclone.lsf`, `rclone.hashsum_streaming`, and
`rclone.hashsum_file` to simulate a remote without doing actual rclone calls.
The tests exercise:
  - Bulk streaming path persists each hash to remote_hash_cache as it
    arrives (so kill mid-stream preserves work).
  - Per-file path with size_filter persists in batches.
  - Cache hit on second run avoids re-hashing.
  - mtime change invalidates cache.
"""
from pathlib import Path
from unittest.mock import patch

import pytest

from rclone_migrate import config as config_mod
from rclone_migrate import manifest, rclone, state


def _write_cfg(path: Path, src: str, dst: str, sd: Path) -> None:
    path.write_text(
        f"[defaults]\nstate_dir = '{sd}'\nhash = 'SHA1'\n"
        f"transfers = 2\nlocal_cache_in_root = false\n"
        f"[delete]\nrequire_check_within = '24h'\nrequire_confirm = true\n"
        f"[[jobs]]\nname = 't'\nsrc = '{src}'\ndst = '{dst}'\n"
    )


def _make_remote_fakes(
    files: dict,           # rel → (size, mtime)
    hash_table: dict,      # rel → hash
):
    def fake_is_local(path: str) -> bool:
        return not path.startswith("fakeremote:")

    def fake_supported_hashes(path: str):
        return ["sha1"]

    def fake_lsf(path: str, extra_flags=None):
        return [
            rclone.LsfEntry(path=p, size=sz, mtime=mt)
            for p, (sz, mt) in files.items()
        ]

    def fake_hashsum_streaming(algo: str, path: str, download: bool = False):
        for rel, h in hash_table.items():
            yield h, rel

    def fake_hashsum_file(algo: str, file_path: str, download: bool = False):
        # file_path is e.g. "fakeremote:/root/foo.bin"
        # We strip "fakeremote:/root/" prefix to get the rel path
        for rel, h in hash_table.items():
            if file_path.endswith("/" + rel) or file_path.endswith(rel):
                return h
        return None

    return fake_is_local, fake_supported_hashes, fake_lsf, fake_hashsum_streaming, fake_hashsum_file


def test_bulk_streaming_persists_each_hash(tmp_path: Path):
    """Bulk hashing path writes to remote_hash_cache as it streams,
    so a hypothetical mid-stream kill preserves earlier hashes."""
    sd = tmp_path / "state"; sd.mkdir()
    cfg_path = tmp_path / "c.toml"
    _write_cfg(cfg_path, "fakeremote:/src", "fakeremote:/dst", sd)
    cfg = config_mod.load(cfg_path)
    job = cfg.get_job("t")

    files = {f"f{i}.bin": (100 + i, 1000.0 + i) for i in range(5)}
    hashes = {f"f{i}.bin": f"hash{i:040d}" for i in range(5)}
    is_local, supp, lsf, streaming, hashfile = _make_remote_fakes(files, hashes)

    with patch.object(rclone, "is_local", is_local), \
         patch("rclone_migrate.hashing.supported_hashes", supp), \
         patch.object(rclone, "lsf", lsf), \
         patch.object(rclone, "hashsum_streaming", streaming), \
         patch.object(rclone, "hashsum_file", hashfile):
        state_conn = state.open_db(sd / "t")
        m = manifest.refresh(
            "src", job, "sha1", state_conn, sd / "t",
            transfers=2, full=False, progress=False,
        )
        assert len(m.entries) == 5
        # All 5 should be in remote_hash_cache (proof of incremental persistence)
        cached = state.rhc_load(state_conn, "src", "sha1")
        assert len(cached) == 5
        assert cached["f3.bin"].hash == "hash" + ("3".rjust(40, "0"))
        state_conn.close()


def test_per_file_checkpoint_with_size_filter(tmp_path: Path):
    """Per-file path (size_filter set) persists each hash as it completes."""
    sd = tmp_path / "state"; sd.mkdir()
    cfg_path = tmp_path / "c.toml"
    _write_cfg(cfg_path, "fakeremote:/src", "fakeremote:/dst", sd)
    cfg = config_mod.load(cfg_path)
    job = cfg.get_job("t")

    files = {f"f{i}.bin": (100 + i, 1000.0 + i) for i in range(5)}
    hashes = {f"f{i}.bin": f"h{i}" for i in range(5)}
    is_local, supp, lsf, streaming, hashfile = _make_remote_fakes(files, hashes)

    with patch.object(rclone, "is_local", is_local), \
         patch("rclone_migrate.hashing.supported_hashes", supp), \
         patch.object(rclone, "lsf", lsf), \
         patch.object(rclone, "hashsum_streaming", streaming), \
         patch.object(rclone, "hashsum_file", hashfile):
        state_conn = state.open_db(sd / "t")
        m = manifest.refresh(
            "dst", job, "sha1", state_conn, sd / "t",
            transfers=2, full=False, progress=False,
            size_filter={101, 103},     # only f1.bin (size=101) and f3.bin (size=103)
        )
        # Only the 2 size-matching files should be in manifest
        paths = {e.path for e in m.entries}
        assert paths == {"f1.bin", "f3.bin"}
        # And only those 2 should be cached
        cached = state.rhc_load(state_conn, "dst", "sha1")
        assert set(cached) == {"f1.bin", "f3.bin"}
        state_conn.close()


def test_second_run_hits_cache(tmp_path: Path):
    """Re-running with unchanged remote state should not call hashsum_streaming."""
    sd = tmp_path / "state"; sd.mkdir()
    cfg_path = tmp_path / "c.toml"
    _write_cfg(cfg_path, "fakeremote:/src", "fakeremote:/dst", sd)
    cfg = config_mod.load(cfg_path)
    job = cfg.get_job("t")

    files = {"a.bin": (10, 1000.0), "b.bin": (20, 2000.0)}
    hashes = {"a.bin": "ha", "b.bin": "hb"}
    is_local, supp, lsf, streaming, hashfile = _make_remote_fakes(files, hashes)

    streaming_calls = []
    def counting_streaming(algo, path, download=False):
        streaming_calls.append((algo, path))
        return streaming(algo, path, download)

    with patch.object(rclone, "is_local", is_local), \
         patch("rclone_migrate.hashing.supported_hashes", supp), \
         patch.object(rclone, "lsf", lsf), \
         patch.object(rclone, "hashsum_streaming", counting_streaming), \
         patch.object(rclone, "hashsum_file", hashfile):
        state_conn = state.open_db(sd / "t")
        m1 = manifest.refresh("src", job, "sha1", state_conn, sd / "t",
                              transfers=2, progress=False)
        assert len(streaming_calls) == 1
        # Second run: cache fully valid → streaming not called again
        m2 = manifest.refresh("src", job, "sha1", state_conn, sd / "t",
                              transfers=2, progress=False)
        assert len(streaming_calls) == 1   # unchanged
        assert m2.stats.valid == 2
        assert m2.stats.rehashed == 0
        state_conn.close()


def test_mtime_change_invalidates_cache(tmp_path: Path):
    sd = tmp_path / "state"; sd.mkdir()
    cfg_path = tmp_path / "c.toml"
    _write_cfg(cfg_path, "fakeremote:/src", "fakeremote:/dst", sd)
    cfg = config_mod.load(cfg_path)
    job = cfg.get_job("t")

    files = {"a.bin": (10, 1000.0)}
    hashes = {"a.bin": "ha-old"}
    is_local, supp, _lsf_unused, streaming, hashfile = _make_remote_fakes(
        files, hashes
    )

    streaming_calls = [0]
    def counting_streaming(algo, path, download=False):
        streaming_calls[0] += 1
        return streaming(algo, path, download)

    def lsf_v1(path, extra_flags=None):
        return [rclone.LsfEntry(path="a.bin", size=10, mtime=1000.0)]
    def lsf_v2(path, extra_flags=None):
        return [rclone.LsfEntry(path="a.bin", size=10, mtime=2000.0)]  # newer

    state_conn = state.open_db(sd / "t")
    with patch.object(rclone, "is_local", is_local), \
         patch("rclone_migrate.hashing.supported_hashes", supp), \
         patch.object(rclone, "lsf", lsf_v1), \
         patch.object(rclone, "hashsum_streaming", counting_streaming), \
         patch.object(rclone, "hashsum_file", hashfile):
        manifest.refresh("src", job, "sha1", state_conn, sd / "t",
                         transfers=2, progress=False)
        assert streaming_calls[0] == 1

    # mtime changed; new hash returned by streaming
    new_hashes = {"a.bin": "ha-new"}
    _, _, _, streaming2, hashfile2 = _make_remote_fakes(
        {"a.bin": (10, 2000.0)}, new_hashes,
    )
    def counting_streaming2(algo, path, download=False):
        streaming_calls[0] += 1
        return streaming2(algo, path, download)

    with patch.object(rclone, "is_local", is_local), \
         patch("rclone_migrate.hashing.supported_hashes", supp), \
         patch.object(rclone, "lsf", lsf_v2), \
         patch.object(rclone, "hashsum_streaming", counting_streaming2), \
         patch.object(rclone, "hashsum_file", hashfile2):
        m2 = manifest.refresh("src", job, "sha1", state_conn, sd / "t",
                              transfers=2, progress=False)
        assert streaming_calls[0] == 2     # called again
        assert m2.entries[0].hash == "ha-new"
    state_conn.close()


def test_full_flag_ignores_cache(tmp_path: Path):
    sd = tmp_path / "state"; sd.mkdir()
    cfg_path = tmp_path / "c.toml"
    _write_cfg(cfg_path, "fakeremote:/src", "fakeremote:/dst", sd)
    cfg = config_mod.load(cfg_path)
    job = cfg.get_job("t")

    files = {"a.bin": (10, 1000.0)}
    hashes = {"a.bin": "ha"}
    is_local, supp, lsf, streaming, hashfile = _make_remote_fakes(files, hashes)
    calls = [0]
    def counting_streaming(algo, path, download=False):
        calls[0] += 1
        return streaming(algo, path, download)

    with patch.object(rclone, "is_local", is_local), \
         patch("rclone_migrate.hashing.supported_hashes", supp), \
         patch.object(rclone, "lsf", lsf), \
         patch.object(rclone, "hashsum_streaming", counting_streaming), \
         patch.object(rclone, "hashsum_file", hashfile):
        state_conn = state.open_db(sd / "t")
        manifest.refresh("src", job, "sha1", state_conn, sd / "t",
                         transfers=2, progress=False)
        assert calls[0] == 1
        # full=True bypasses valid cache
        manifest.refresh("src", job, "sha1", state_conn, sd / "t",
                         transfers=2, progress=False, full=True)
        assert calls[0] == 2
        state_conn.close()


def test_per_file_path_writes_from_threads(tmp_path: Path):
    """Regression: SQLite check_same_thread must be disabled, since
    ThreadPoolExecutor workers in the per-file path call rhc_upsert from
    non-main threads. The bug surfaces as silently empty cache after
    workers raise sqlite3.ProgrammingError, so we explicitly verify rows
    are written."""
    sd = tmp_path / "state"; sd.mkdir()
    cfg_path = tmp_path / "c.toml"
    _write_cfg(cfg_path, "fakeremote:/src", "fakeremote:/dst", sd)
    cfg = config_mod.load(cfg_path)
    job = cfg.get_job("t")

    # 60 files, all matching size_filter — exercises threadpool + checkpoint
    files = {f"file{i:03d}.bin": (1000, 1000.0 + i) for i in range(60)}
    hashes = {f"file{i:03d}.bin": f"hash{i:04d}" for i in range(60)}
    is_local, supp, lsf, streaming, hashfile = _make_remote_fakes(files, hashes)

    with patch.object(rclone, "is_local", is_local), \
         patch("rclone_migrate.hashing.supported_hashes", supp), \
         patch.object(rclone, "lsf", lsf), \
         patch.object(rclone, "hashsum_streaming", streaming), \
         patch.object(rclone, "hashsum_file", hashfile):
        state_conn = state.open_db(sd / "t")
        m = manifest.refresh(
            "dst", job, "sha1", state_conn, sd / "t",
            transfers=4, full=False, progress=False,
            size_filter={1000},
        )
        assert len(m.entries) == 60
        cached = state.rhc_load(state_conn, "dst", "sha1")
        assert len(cached) == 60        # all 60 must have been persisted
        state_conn.close()


def test_no_native_hash_requires_download_implicit(tmp_path: Path):
    """Backend without native algo support: download is set automatically
    by the dispatcher so the call doesn't error."""
    sd = tmp_path / "state"; sd.mkdir()
    cfg_path = tmp_path / "c.toml"
    _write_cfg(cfg_path, "fakeremote:/src", "fakeremote:/dst", sd)
    cfg = config_mod.load(cfg_path)
    job = cfg.get_job("t")

    files = {"a.bin": (10, 1000.0)}
    hashes = {"a.bin": "ha"}
    is_local, _supp, lsf, streaming, hashfile = _make_remote_fakes(files, hashes)

    # Pretend backend reports NO native hash for sha1
    def fake_supp(path):
        return ["md5"]   # no sha1

    download_seen = []
    def streaming_capture(algo, path, download=False):
        download_seen.append(download)
        return streaming(algo, path, download)

    with patch.object(rclone, "is_local", is_local), \
         patch("rclone_migrate.hashing.supported_hashes", fake_supp), \
         patch.object(rclone, "lsf", lsf), \
         patch.object(rclone, "hashsum_streaming", streaming_capture), \
         patch.object(rclone, "hashsum_file", hashfile):
        state_conn = state.open_db(sd / "t")
        manifest.refresh("src", job, "sha1", state_conn, sd / "t",
                         transfers=2, progress=False)
        # Dispatcher should have flipped download=True
        assert download_seen == [True]
        state_conn.close()
