"""rclone.copyto rc-monitoring path: parsing, error handling, fast path.

No real rclone here — Popen and urllib are mocked so we exercise the
core/stats JSON parsing and the on_stats forwarding deterministically.
"""
import io
import json
import time
from unittest.mock import patch

import pytest

from rclone_migrate import rclone


class _FakeProc:
    """A Popen stand-in whose communicate() blocks long enough for the
    poller thread to fire at least once."""

    def __init__(self, returncode=0, stderr="", run_for=0.05):
        self.returncode = returncode
        self._stderr = stderr
        self._run_for = run_for

    def communicate(self):
        time.sleep(self._run_for)
        return "", self._stderr

    def poll(self):
        return self.returncode


def _stats_payload(bytes_=42, speed=1234.0, with_transferring=True):
    s = {"bytes": bytes_, "speed": speed}
    if with_transferring:
        s["transferring"] = [
            {"name": "f.bin", "bytes": bytes_, "size": 100,
             "speedAvg": speed, "eta": 7, "percentage": 42}
        ]
    return io.BytesIO(json.dumps(s).encode())


def test_fast_path_when_no_on_stats():
    """on_stats=None must not touch rc; it goes through _run."""
    with patch.object(rclone, "_run") as run:
        rclone.copyto("a", "b", "sha256")
    args = run.call_args[0][0]
    assert "--rc" not in args
    assert args[:2] == ["copyto", "--checksum"]


def test_on_stats_receives_parsed_core_stats(monkeypatch):
    monkeypatch.setattr(rclone, "_RC_POLL_INTERVAL", 0.01)
    monkeypatch.setattr(rclone, "_bin", lambda: "rclone")
    monkeypatch.setattr(rclone, "_free_loopback_port", lambda: 5599)
    monkeypatch.setattr(rclone.subprocess, "Popen",
                        lambda *a, **k: _FakeProc(returncode=0))
    monkeypatch.setattr(rclone.urllib.request, "urlopen",
                        lambda *a, **k: _stats_payload(bytes_=42, speed=999.0))

    seen = []
    rclone.copyto("src", "dst", "sha256", on_stats=lambda b, s: seen.append((b, s)))
    assert seen, "poller never forwarded a sample"
    assert seen[-1] == (42, 999.0)


def test_prefers_per_file_speedavg_over_global(monkeypatch):
    monkeypatch.setattr(rclone, "_RC_POLL_INTERVAL", 0.01)
    monkeypatch.setattr(rclone, "_bin", lambda: "rclone")
    monkeypatch.setattr(rclone, "_free_loopback_port", lambda: 5599)
    monkeypatch.setattr(rclone.subprocess, "Popen",
                        lambda *a, **k: _FakeProc())

    def payload(*a, **k):
        return io.BytesIO(json.dumps({
            "bytes": 1, "speed": 10.0,
            "transferring": [{"bytes": 88, "speedAvg": 777.0}],
        }).encode())

    monkeypatch.setattr(rclone.urllib.request, "urlopen", payload)
    seen = []
    rclone.copyto("s", "d", "sha256", on_stats=lambda b, s: seen.append((b, s)))
    assert seen[-1] == (88, 777.0)


def test_nonzero_exit_raises_rclone_error(monkeypatch):
    monkeypatch.setattr(rclone, "_RC_POLL_INTERVAL", 0.01)
    monkeypatch.setattr(rclone, "_bin", lambda: "rclone")
    monkeypatch.setattr(rclone, "_free_loopback_port", lambda: 5599)
    monkeypatch.setattr(rclone.subprocess, "Popen",
                        lambda *a, **k: _FakeProc(returncode=3,
                                                  stderr="boom"))
    monkeypatch.setattr(rclone.urllib.request, "urlopen",
                        lambda *a, **k: (_ for _ in ()).throw(OSError()))

    with pytest.raises(rclone.RcloneError, match="boom"):
        rclone.copyto("s", "d", "sha256", on_stats=lambda b, s: None)


def test_poller_survives_urlopen_errors(monkeypatch):
    """Connection-refused during rc startup must not crash the copy."""
    monkeypatch.setattr(rclone, "_RC_POLL_INTERVAL", 0.01)
    monkeypatch.setattr(rclone, "_bin", lambda: "rclone")
    monkeypatch.setattr(rclone, "_free_loopback_port", lambda: 5599)
    monkeypatch.setattr(rclone.subprocess, "Popen",
                        lambda *a, **k: _FakeProc(returncode=0))
    monkeypatch.setattr(rclone.urllib.request, "urlopen",
                        lambda *a, **k: (_ for _ in ()).throw(
                            ConnectionRefusedError()))
    # Should complete without raising despite every poll failing.
    rclone.copyto("s", "d", "sha256", on_stats=lambda b, s: None)
