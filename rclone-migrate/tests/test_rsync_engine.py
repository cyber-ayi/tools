"""#236: preflight rclone/rsync engine selection + retry hardening + rsync
--append resume. e2e uses on-disk temp files only (never the SD card)."""
import io
import os
import subprocess
from unittest import mock

import pytest

from rclone_migrate import rclone, verbose
from rclone_migrate.config import Defaults, Job, _parse_size
from rclone_migrate.manifest import Entry
from rclone_migrate.ops import _select_engine


def _v():
    return verbose.Verbose(level=verbose.NORMAL, color=False,
                           timestamps=False, stream=io.StringIO(),
                           err_stream=io.StringIO())


# ---- size parsing / config resolution ----

def test_parse_size():
    assert _parse_size("10GiB") == 10 * 1024**3
    assert _parse_size("500MiB") == 500 * 1024**2
    assert _parse_size("10G") == 10 * 1024**3        # bare = binary
    assert _parse_size("1GB") == 1000**3             # decimal
    assert _parse_size("1.5GiB") == int(1.5 * 1024**3)
    assert _parse_size("0") == 0 and _parse_size("off") == 0
    assert _parse_size(None) == 0
    assert _parse_size(2048) == 2048


def test_resolved_resumable_min_size_default_and_override():
    d = Defaults()                                   # default "10GiB"
    j = Job(name="j", src="/s", dst="/d")
    assert j.resolved_resumable_min_size(d) == 10 * 1024**3
    j2 = Job(name="j", src="/s", dst="/d", resumable_min_size="2GiB")
    assert j2.resolved_resumable_min_size(d) == 2 * 1024**3
    d2 = Defaults(resumable_min_size="0")
    assert Job(name="j", src="/s", dst="/d").resolved_resumable_min_size(d2) == 0


# ---- engine selection matrix ----

def _job(dst="/Volumes/nas/x", rms=None):
    return Job(name="j", src="/sd/DCIM", dst=dst, resumable_min_size=rms)


def _cfg():
    class C:  # minimal cfg with .defaults
        defaults = Defaults()  # resumable_min_size "10GiB"
    return C()


def test_engine_big_local_with_rsync_picks_rsync(monkeypatch):
    monkeypatch.setattr(rclone, "have_rsync", lambda: True)
    to_copy = [Entry("big.insv", "h", 20 * 1024**3),
               Entry("small.lrv", "h", 100 * 1024**2)]
    eng = _select_engine(to_copy, _job(), _cfg(), True, _v())
    assert eng["big.insv"] == "rsync"
    assert eng["small.lrv"] == "rclone"      # below 10 GiB


def test_engine_remote_dst_always_rclone(monkeypatch):
    monkeypatch.setattr(rclone, "have_rsync", lambda: True)
    eng = _select_engine([Entry("big.insv", "h", 50 * 1024**3)],
                         _job(dst="b2:bucket/x"), _cfg(), True, _v())
    assert eng["big.insv"] == "rclone"       # rsync-append needs local dst


def test_engine_no_rsync_binary_falls_back(monkeypatch):
    monkeypatch.setattr(rclone, "have_rsync", lambda: False)
    v = _v()
    eng = _select_engine([Entry("big.insv", "h", 50 * 1024**3)],
                         _job(), _cfg(), True, v)
    assert eng["big.insv"] == "rclone"
    assert "rsync not found" in v._err.getvalue()


def test_engine_disabled_all_rclone(monkeypatch):
    monkeypatch.setattr(rclone, "have_rsync", lambda: True)
    eng = _select_engine([Entry("big.insv", "h", 50 * 1024**3)],
                         _job(), _cfg(), False, _v())     # use_rsync=False
    assert eng["big.insv"] == "rclone"


# ---- rclone retry hardening (part 2) ----

def test_copyto_passes_retry_flags(monkeypatch):
    seen = {}
    monkeypatch.setattr(rclone, "_run",
                        lambda a, **k: seen.setdefault("args", a))
    rclone.copyto("s", "d", "xxh3")
    assert "--retries" in seen["args"] and "--low-level-retries" in seen["args"]
    assert "--inplace" not in seen["args"]   # deliberately NOT inplace


# ---- rsync_copyto ----

def test_rsync_copyto_missing_binary(monkeypatch):
    monkeypatch.setattr(rclone.shutil, "which", lambda _: None)
    with pytest.raises(rclone.RcloneError, match="rsync not found"):
        rclone.rsync_copyto("s", "d")


def test_rsync_copyto_argv(monkeypatch, tmp_path):
    monkeypatch.setattr(rclone.shutil, "which", lambda _: "/usr/bin/rsync")
    captured = {}

    def fake_run(argv, **kw):
        captured["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(rclone.subprocess, "run", fake_run)
    rclone.rsync_copyto(str(tmp_path / "s"), str(tmp_path / "sub" / "d"))
    # --partial is the RCA fix: openrsync deletes a partial on interrupt
    # without it → only a fluke partial survives ("resume only from the
    # first break"). --inplace explicit (--append implies it on openrsync).
    assert captured["argv"][1:] == ["--partial", "--inplace", "--append",
                                    "--times", str(tmp_path / "s"),
                                    str(tmp_path / "sub" / "d")]
    assert "--partial" in captured["argv"]   # guard the RCA regression


@pytest.mark.skipif(__import__("shutil").which("rsync") is None,
                    reason="rsync not installed")
def test_rsync_append_actually_resumes(tmp_path):
    """e2e (on-disk temp only): a truncated dst is *resumed* by
    rsync --append, not restarted, and ends byte-identical to src."""
    src = tmp_path / "src.bin"
    src.write_bytes(os.urandom(5_000_000))
    dst = tmp_path / "out" / "src.bin"
    dst.parent.mkdir()
    # simulate an interrupted transfer: dst has only the first 2 MB
    dst.write_bytes(src.read_bytes()[:2_000_000])
    before = dst.stat().st_size
    rclone.rsync_copyto(str(src), str(dst))
    assert before == 2_000_000
    assert dst.read_bytes() == src.read_bytes()      # completed via append
