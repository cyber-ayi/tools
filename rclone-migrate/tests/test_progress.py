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


def test_worker_api_aggregates_and_multiline_mode():
    v = _v()
    m = progress.ProgressMeter(v, "[src] hash", total_files=3,
                               total_bytes=300)
    assert m._multiline is False
    w0 = m.worker_slot()
    assert m._multiline is True
    m.worker_start(w0, "a.bin", 100)
    m.worker_add(w0, 60)
    # second "thread": monkey a distinct ident by calling from this thread
    # again returns same slot (thread-stable); simulate a 2nd slot directly
    m._slot_of_thread[-1] = 1
    m._nslots = 2
    m.worker_start(1, "b.bin", 200)
    m.worker_add(1, 50)
    assert m._committed == 110          # aggregate = sum of worker_add
    lines = m._format_worker_lines()
    assert any("a.bin" in ln and "w0" in ln for ln in lines)
    assert any("b.bin" in ln and "w1" in ln for ln in lines)
    m.worker_done(w0)                    # streamed: no committed_size
    m.worker_done(1, committed_size=0)
    with m:
        pass
    out = v._stream.getvalue()
    assert "[src] hash done: 2 files" in out


def test_worker_done_failure_counts():
    v = _v()
    m = progress.ProgressMeter(v, "[src] hash", total_files=1)
    w = m.worker_slot()
    m.worker_start(w, "x", 10)
    m.worker_done(w, ok=False)
    with m:
        pass
    assert "1 failed" in v._err.getvalue()


def test_idle_slots_render_placeholder():
    v = _v()
    m = progress.ProgressMeter(v, "[s] hash")
    m.worker_slot()
    m._nslots = 3                        # 3 slots, only w0 active
    m.worker_start(0, "live.bin", 50)
    m.worker_add(0, 25)
    lines = m._format_worker_lines()
    assert len(lines) == 3
    assert "live.bin" in lines[0]
    assert "idle" in lines[1] and "idle" in lines[2]


def test_inflight_advances_processed_but_not_speed():
    v = _v()
    m = progress.ProgressMeter(v, "[copy]", total_files=2, total_bytes=1000,
                               cumulative=True)
    m._start_t = time.time() - 2.0
    m.file_done(committed_size=300)        # one file done → committed 300
    m.set_inflight(150)                    # 150 B into the next file
    assert m._processed() == 450           # bytes/% include inflight
    m._refresh_speed()
    # cumulative speed uses committed only (300/2s≈150), NOT 450 — inflight
    # must never inflate speed/ETA.
    assert 100 <= m._speed <= 200
    line = m._format_line()
    assert "450 B/1000 B" in line or "0.44 KiB/0.98 KiB" in line
    m.file_done(committed_size=300)        # next file done → inflight cleared
    assert m._inflight == 0
    assert m._processed() == 600


def test_inflight_zero_degrades_to_wallclock():
    """No .partial watcher ⇒ inflight stays 0 ⇒ identical to pre-Stage-C."""
    v = _v()
    m = progress.ProgressMeter(v, "[copy]", total_files=1, total_bytes=100,
                               cumulative=True)
    assert m._inflight == 0
    m.file_done(committed_size=100)
    with m:
        pass
    assert "[copy] done: 1 files, 100 B" in v._stream.getvalue()


def test_pct_has_two_decimals():
    v = _v()
    m = progress.ProgressMeter(v, "[copy]", total_files=15,
                               total_bytes=226 * 1024**3)
    m.add_processed(471 * 1024**2)        # 471 MiB of 226 GiB ≈ 0.20%
    line = m._format_line()
    assert "(0.20%)" in line              # not "(0%)"
    assert "(0%)" not in line


def test_windowed_copy_speed_from_inflight():
    """Stage H: a non-cumulative meter fed continuous inflight (the
    .partial watcher) yields a real speed + ETA mid-file, not '--'."""
    v = _v()
    m = progress.ProgressMeter(v, "[copy]", total_files=2,
                               total_bytes=10_000_000)  # windowed
    m._last_sample_t = time.time() - 1.0
    m.set_inflight(4_000_000)             # 4 MB in ~1s → ~4 MB/s (≥ floor)
    m._refresh_speed()
    assert m._speed > 512
    line = m._format_line()
    assert "MiB/s" in line and "ETA" in line
    assert "ETA --:--" not in line       # real ETA at a real rate


def test_eta_unknown_below_speed_floor():
    """Sub-floor / zero speed → 'ETA --:--', never a garbage huge number
    (the field bug: ETA 4096365853117046538234757:07:12)."""
    v = _v()
    m = progress.ProgressMeter(v, "[copy]", total_files=2,
                               total_bytes=10**9)
    m._last_sample_t = time.time() - 1.0
    m.set_inflight(50)                    # ~50 B/s, below 512 floor
    m._refresh_speed()
    assert "ETA --:--" in m._format_line()


def test_human_eta_caps_absurd():
    assert progress._human_eta(30 * 3600) == "30:00:00"     # real long ETA ok
    assert progress._human_eta(1e18) == "--:--"             # absurd → unknown
    assert progress._human_eta(float("inf")) == "--:--"


def test_speed_ema_not_dragged_by_interfile_dip():
    """A file-boundary dip (processed momentarily lower) must NOT collapse
    the EMA to ~0 — only positive samples update it."""
    v = _v()
    m = progress.ProgressMeter(v, "[copy]", total_files=3,
                               total_bytes=10**9)
    # establish a real rate
    m._last_sample_t = time.time() - 1.0
    m.set_inflight(5_000_000)
    m._refresh_speed()
    fast = m._speed
    assert fast > 1_000_000
    # simulate inter-file dip: processed drops (inflight reset, next file
    # not yet watched) — speed must hold, not crater toward 0
    m._inflight = 0
    m._last_sample_t = time.time() - 1.0
    m._refresh_speed()
    assert m._speed == fast              # unchanged (non-positive skipped)


def test_quiet_level_is_silent():
    v = _v(level=verbose.QUIET)
    m = progress.ProgressMeter(v, "[q]", total_files=1)
    assert m.live is False
    with m:
        m.add_processed(10)
        m.file_done()
    assert v._stream.getvalue() == ""
    assert v._err.getvalue() == ""
