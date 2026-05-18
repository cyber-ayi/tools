"""Stage D: fail-fast reachability probe for dead/stale local roots."""
import io
import os
import time

import pytest

from rclone_migrate import manifest, verbose


def _v():
    return verbose.Verbose(level=verbose.DETAIL, color=False, timestamps=False,
                           stream=io.StringIO(), err_stream=io.StringIO())


def test_probe_passes_for_live_dir(tmp_path):
    # Should not raise and should be fast.
    t0 = time.time()
    manifest._probe_reachable(tmp_path, "dst", _v())
    assert time.time() - t0 < 1.0


def test_probe_raises_on_hung_stat(tmp_path, monkeypatch):
    monkeypatch.setenv("RMIG_ROOT_PROBE_TIMEOUT", "0.3")
    real_stat = os.stat

    def slow_stat(p, *a, **k):
        time.sleep(3.0)            # simulate a wedged smbfs syscall
        return real_stat(p, *a, **k)

    monkeypatch.setattr(manifest.os, "stat", slow_stat)
    t0 = time.time()
    with pytest.raises(manifest.UnreachableRootError) as ei:
        manifest._probe_reachable(tmp_path, "dst", _v())
    dt = time.time() - t0
    assert 0.25 < dt < 2.0          # bounded by timeout, not by the 3s stat
    assert "did not respond within" in str(ei.value)
    assert "stale/dead network mount" in str(ei.value)


def test_probe_disabled_when_timeout_zero(tmp_path, monkeypatch):
    monkeypatch.setenv("RMIG_ROOT_PROBE_TIMEOUT", "0")

    def slow_stat(p, *a, **k):
        time.sleep(3.0)
        raise AssertionError("stat should not be waited on when disabled")

    monkeypatch.setattr(manifest.os, "stat", slow_stat)
    manifest._probe_reachable(tmp_path, "src", _v())   # returns immediately


def test_missing_root_still_raises_filenotfound(tmp_path):
    """Probe must not mask the existing fast ENOENT path."""
    missing = tmp_path / "nope"
    with pytest.raises(FileNotFoundError):
        manifest._refresh_local(
            "dst", str(missing), "sha256",
            transfers=1, full=False, local_cache_in_root=False,
            fallback_dir=tmp_path / "fb", progress=False, v=_v(),
        )


def test_cli_safe_exit_maps_unreachable_to_4(capsys):
    from rclone_migrate import cli

    def boom():
        raise manifest.UnreachableRootError("dst root did not respond ...")

    rc = cli._safe_exit(boom)
    assert rc == 4
    err = capsys.readouterr().err
    assert "ERROR:" in err and "did not respond" in err
    assert "Traceback" not in err
