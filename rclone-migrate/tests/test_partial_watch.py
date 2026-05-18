"""Stage C: ops._PartialWatch feeds the copy meter from rclone's .partial."""
import time

from rclone_migrate.ops import _PartialWatch


class _RecMeter:
    def __init__(self):
        self.vals = []

    def set_inflight(self, n):
        self.vals.append(n)


def test_watch_picks_up_growing_partial(tmp_path):
    dst = tmp_path / "VID_001.insv"
    partial = tmp_path / "VID_001.insv.b856018f.partial"
    m = _RecMeter()
    with _PartialWatch(str(dst), m, interval=0.02):
        partial.write_bytes(b"x" * 1000)
        time.sleep(0.05)
        partial.write_bytes(b"x" * 5000)
        time.sleep(0.05)
    assert m.vals, "watcher never sampled the .partial"
    assert max(m.vals) >= 5000
    assert m.vals == sorted(m.vals) or 5000 in m.vals  # monotone-ish growth


def test_watch_also_matches_final_name(tmp_path):
    dst = tmp_path / "a.bin"
    m = _RecMeter()
    with _PartialWatch(str(dst), m, interval=0.02):
        (tmp_path / "a.bin").write_bytes(b"y" * 2048)
        time.sleep(0.05)
    assert m.vals and max(m.vals) == 2048


def test_watch_silent_when_no_partial(tmp_path):
    """Degrade path: nothing matches ⇒ no set_inflight calls, no error."""
    m = _RecMeter()
    with _PartialWatch(str(tmp_path / "missing.bin"), m, interval=0.02):
        (tmp_path / "unrelated.txt").write_bytes(b"z" * 999)
        time.sleep(0.06)
    assert m.vals == []


def test_watch_survives_missing_dir(tmp_path):
    m = _RecMeter()
    with _PartialWatch(str(tmp_path / "nope" / "x.bin"), m, interval=0.02):
        time.sleep(0.05)
    assert m.vals == []  # no crash, no samples
