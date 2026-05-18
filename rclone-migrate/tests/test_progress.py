"""Tests for the ProgressMeter live/periodic throughput helper."""
import hashlib
import io
import time

import pytest

from rclone_migrate import hashing, progress, verbose


def _v(level=verbose.NORMAL):
    return verbose.Verbose(
        level=level, color=False, timestamps=False,
        stream=io.StringIO(), err_stream=io.StringIO(),
    )


def test_human_helpers():
    assert progress._human_bytes(0) == "0 B"
    assert progress._human_bytes(1536) == "1.50 KiB"
    assert progress._human_bytes(5 * 1024**3) == "5.00 GiB"
    assert progress._human_rate(0) == "--"
    assert progress._human_rate(1024**2).endswith("MiB/s")
    assert progress._human_eta(0) == "00:00"
    assert progress._human_eta(3661) == "1:01:01"
    assert progress._human_eta(float("inf")) == "--:--"


def test_not_live_under_pytest_but_summary_emitted():
    # pytest captures stderr → sys.__stderr__ is not a TTY → no live thread.
    v = _v()
    m = progress.ProgressMeter(v, "[t]", total_files=2, total_bytes=200)
    assert m.live is False
    with m:
        m.add_processed(100)
        m.file_done()
        m.add_processed(100)
        m.file_done()
    out = v._stream.getvalue()
    assert "[t] done: 2 files" in out
    assert "200 B" in out


def test_periodic_fallback_rate_limited():
    v = _v()
    m = progress.ProgressMeter(v, "[p]", total_files=3, total_bytes=300,
                               interval=0.05)
    with m:
        m.add_processed(100); m.file_done()      # first periodic line
        m.add_processed(100); m.file_done()      # within interval → suppressed
        time.sleep(0.06)
        m.add_processed(100); m.file_done()      # interval elapsed → emitted
    body = v._stream.getvalue()
    # 2 periodic "[p]  ... files" lines + 1 summary line
    assert body.count("[p]") >= 2
    assert "done: 3 files" in body


def test_periodic_disabled_when_periodic_false():
    v = _v()
    m = progress.ProgressMeter(v, "[c]", total_files=2, total_bytes=2,
                               periodic=False, cumulative=True)
    with m:
        m.file_done(committed_size=1)
        m.file_done(committed_size=1)
    body = v._stream.getvalue()
    # No interim periodic lines — only the final summary.
    assert body.strip().count("\n") == 0
    assert "[c] done: 2 files" in body


def test_cumulative_speed_is_committed_over_elapsed():
    v = _v()
    m = progress.ProgressMeter(v, "[cu]", total_bytes=1000, cumulative=True)
    m._start_t = time.time() - 2.0       # pretend 2s elapsed
    m.add_processed(1000)
    m._refresh_speed()
    assert 400 <= m._speed <= 600        # ~1000B / 2s = 500 B/s


def test_failures_counted_in_summary():
    v = _v()
    m = progress.ProgressMeter(v, "[f]", total_files=2)
    with m:
        m.file_done()
        m.file_done(ok=False)
    body = v._err.getvalue()             # warn() routes to err stream
    assert "1 failed" in body


def test_hash_file_local_progress_cb_sums_to_size(tmp_path):
    data = b"x" * (3 * (1 << 20) + 123)   # 3 chunks + tail
    f = tmp_path / "blob.bin"
    f.write_bytes(data)
    seen = []
    h = hashing.hash_file_local(str(f), "sha256", progress_cb=seen.append)
    assert h == hashlib.sha256(data).hexdigest()
    assert sum(seen) == len(data)
    assert len(seen) == 4   # 1MiB,1MiB,1MiB,tail


def test_non_streamable_algo_still_reports_bytes(tmp_path, monkeypatch):
    """Regression: algos with no in-process streaming impl (crc32, blake3,
    …) shell out to a per-file `rclone hashsum` with no progress_cb. The
    local refresh must still credit file sizes so the meter doesn't sit at
    '0 B / -- ' the entire run."""
    from rclone_migrate import hashing, manifest

    assert not hashing.can_stream_local("crc32")  # premise of this test

    root = tmp_path / "src"
    root.mkdir()
    for i in range(3):
        (root / f"f{i}.lrv").write_bytes(b"D" * 4096)

    # Simulate the rclone-subprocess fallback: returns a digest WITHOUT
    # calling progress_cb.
    def fake_hash(path, algo, chunk_size=1 << 20, progress_cb=None):
        return "deadbeef"

    monkeypatch.setattr(hashing, "hash_file_local", fake_hash)

    v = _v()
    manifest._refresh_local(
        "src", str(root), "crc32",
        transfers=2, full=False, local_cache_in_root=False,
        fallback_dir=tmp_path / "fb", progress=True, v=v,
    )
    out = v._stream.getvalue()
    # The summary's byte figure is processed bytes — only non-zero if the
    # file sizes were credited (the fix). 3×4096 = 12 KiB.
    assert "hash done: 3 files, 12.00 KiB in" in out


def test_interrupted_summary_says_interrupted(tmp_path):
    """A KeyboardInterrupt through the `with meter` block must report
    'interrupted' (not a misleading 'done')."""
    v = _v()
    m = progress.ProgressMeter(v, "[copy]", total_files=10, total_bytes=1000)
    with pytest.raises(KeyboardInterrupt):
        with m:
            m.file_done(committed_size=100)
            m.file_done(committed_size=100)
            raise KeyboardInterrupt
    err = v._err.getvalue()          # interrupted summary goes via warn()
    assert "[copy] interrupted: 2/10 files" in err
    assert "done:" not in v._stream.getvalue()


def test_quiet_level_is_silent():
    v = _v(level=verbose.QUIET)
    m = progress.ProgressMeter(v, "[q]", total_files=1)
    assert m.live is False
    with m:
        m.add_processed(10)
        m.file_done()
    assert v._stream.getvalue() == ""
    assert v._err.getvalue() == ""
