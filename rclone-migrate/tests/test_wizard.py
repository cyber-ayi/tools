"""Tests for the config setup wizard."""
import io
from pathlib import Path
from unittest.mock import patch

import pytest

from rclone_migrate import config as config_mod
from rclone_migrate import verbose, wizard


def _silent_v():
    """Verbose at QUIET so wizard's info() calls don't pollute capsys."""
    return verbose.Verbose(level=verbose.QUIET, color=False, timestamps=False,
                           stream=io.StringIO(), err_stream=io.StringIO())


def test_slugify():
    assert wizard.slugify("Insta360 X5") == "insta360-x5"
    assert wizard.slugify("/Volumes/FUJIFILM") == "fujifilm"
    assert wizard.slugify("nikon-d850") == "nikon-d850"
    assert wizard.slugify("a__b__c") == "a-b-c"
    assert wizard.slugify("") == "job"


def test_detect_kind_local():
    assert wizard.detect_kind("/tmp/x") == wizard.KIND_LOCAL
    assert wizard.detect_kind("./relative") == wizard.KIND_LOCAL


def test_detect_kind_remote_falls_back_to_cloud(monkeypatch):
    """If `rclone config show` fails or returns nothing, default to cloud."""
    def fake_run(args, check=True, capture=True):
        class FakeCp:
            stdout = ""
            stderr = ""
            returncode = 1
        return FakeCp()
    monkeypatch.setattr(wizard.rclone_mod, "_run", fake_run)
    # Whatever the path, fallback to cloud
    assert wizard.detect_kind("unknownremote:/p") == wizard.KIND_CLOUD


def test_detect_kind_recognizes_sftp(monkeypatch):
    def fake_run(args, check=True, capture=True):
        class FakeCp:
            stdout = "type = sftp\nhost = thinkpad\nuser = ubuntu\n"
            stderr = ""
            returncode = 0
        return FakeCp()
    monkeypatch.setattr(wizard.rclone_mod, "_run", fake_run)
    assert wizard.detect_kind("thinkpad:/foo") == wizard.KIND_SSH


def test_detect_kind_recognizes_s3(monkeypatch):
    def fake_run(args, check=True, capture=True):
        class FakeCp:
            stdout = "type = s3\nprovider = AWS\n"
            stderr = ""
            returncode = 0
        return FakeCp()
    monkeypatch.setattr(wizard.rclone_mod, "_run", fake_run)
    assert wizard.detect_kind("s3bucket:/foo") == wizard.KIND_CLOUD


def test_derive_defaults_local_src_disables_cache_in_root():
    d = wizard.derive_defaults(wizard.KIND_LOCAL, wizard.KIND_SSH)
    assert d["local_cache_in_root"] is False
    assert d["transfers"] == 4


def test_derive_defaults_cloud_bumps_transfers():
    d = wizard.derive_defaults(wizard.KIND_LOCAL, wizard.KIND_CLOUD)
    assert d["transfers"] == 8


def test_derive_defaults_remote_to_local_keeps_cache_in_root():
    d = wizard.derive_defaults(wizard.KIND_SSH, wizard.KIND_LOCAL)
    assert d["local_cache_in_root"] is True


def test_render_toml_round_trips_through_config_loader(tmp_path: Path):
    opts = wizard.InitOptions(
        name="test-job",
        src="/Volumes/Test",
        dst="thinkpad:/path",
        src_kind=wizard.KIND_LOCAL,
        dst_kind=wizard.KIND_SSH,
    )
    text = wizard.render_toml(opts)
    p = tmp_path / "out.toml"
    p.write_text(text)
    cfg = config_mod.load(p)
    assert len(cfg.jobs) == 1
    j = cfg.jobs[0]
    assert j.name == "test-job"
    assert j.src == "/Volumes/Test"
    assert j.dst == "thinkpad:/path"


def test_render_includes_kind_comments():
    opts = wizard.InitOptions(
        name="t", src="/a", dst="b:/c",
        src_kind=wizard.KIND_LOCAL, dst_kind=wizard.KIND_CLOUD,
    )
    text = wizard.render_toml(opts)
    assert "src_kind" in text and "local" in text
    assert "dst_kind" in text and "remote-cloud" in text


def test_render_explicit_hash_emits_setting():
    opts = wizard.InitOptions(
        name="t", src="/a", dst="b:/c",
        src_kind=wizard.KIND_LOCAL, dst_kind=wizard.KIND_CLOUD,
        hash="MD5",
    )
    text = wizard.render_toml(opts)
    assert 'hash = "MD5"' in text


def test_render_no_hash_emits_commented_default():
    opts = wizard.InitOptions(
        name="t", src="/a", dst="b:/c",
        src_kind=wizard.KIND_LOCAL, dst_kind=wizard.KIND_CLOUD,
    )
    text = wizard.render_toml(opts)
    assert "auto-negotiated" in text


def test_collect_non_interactive_requires_src():
    v = _silent_v()
    opts = wizard.InitOptions(interactive=False)
    with pytest.raises(ValueError):
        wizard.collect(opts, v)


def test_collect_non_interactive_with_all_flags(tmp_path: Path, monkeypatch):
    v = _silent_v()
    # Mock rclone for kind detection
    def fake_run(args, check=True, capture=True):
        class FakeCp:
            stdout = "type = sftp\n"
            stderr = ""; returncode = 0
        return FakeCp()
    monkeypatch.setattr(wizard.rclone_mod, "_run", fake_run)

    opts = wizard.InitOptions(
        name="explicit-name",
        src="/my/src",
        dst="thinkpad:/dst",
        src_kind=wizard.KIND_LOCAL,
        dst_kind=wizard.KIND_SSH,
        write=str(tmp_path / "out.toml"),
        interactive=False,
        probe=False,
    )
    out = wizard.collect(opts, v)
    assert out.name == "explicit-name"


def test_collect_non_interactive_auto_fills_name_and_kinds(tmp_path: Path, monkeypatch):
    v = _silent_v()
    def fake_run(args, check=True, capture=True):
        class FakeCp:
            stdout = "type = sftp\n"
            stderr = ""; returncode = 0
        return FakeCp()
    monkeypatch.setattr(wizard.rclone_mod, "_run", fake_run)

    opts = wizard.InitOptions(
        src="/Volumes/SD Card",
        dst="thinkpad:/x",
        write=str(tmp_path / "out.toml"),
        interactive=False,
        probe=False,
    )
    out = wizard.collect(opts, v)
    assert out.name == "sd-card"
    assert out.src_kind == wizard.KIND_LOCAL
    assert out.dst_kind == wizard.KIND_SSH


def test_write_toml_creates_file(tmp_path: Path):
    v = _silent_v()
    opts = wizard.InitOptions(
        name="t", src="/a", dst="b:/c",
        src_kind=wizard.KIND_LOCAL, dst_kind=wizard.KIND_CLOUD,
        write=str(tmp_path / "out.toml"),
        interactive=False,
        probe=False,
    )
    p = wizard.write_toml(opts, v)
    assert p.exists()
    assert "name = \"t\"" in p.read_text()


def test_write_toml_overwrite_in_non_interactive(tmp_path: Path):
    v = _silent_v()
    out = tmp_path / "out.toml"
    out.write_text("# old content")
    opts = wizard.InitOptions(
        name="t", src="/a", dst="b:/c",
        src_kind=wizard.KIND_LOCAL, dst_kind=wizard.KIND_CLOUD,
        write=str(out), interactive=False, probe=False,
    )
    wizard.write_toml(opts, v)
    assert "old content" not in out.read_text()
    assert "name = \"t\"" in out.read_text()


def test_run_init_end_to_end_non_interactive(tmp_path: Path, monkeypatch, capsys):
    """All flags supplied, non-interactive, no probe → writes a parseable TOML."""
    v = verbose.Verbose(level=verbose.NORMAL, color=False, timestamps=False,
                        stream=io.StringIO(), err_stream=io.StringIO())
    out = tmp_path / "j.toml"
    opts = wizard.InitOptions(
        name="auto", src="/some/src", dst="remote:/dst",
        src_kind=wizard.KIND_LOCAL, dst_kind=wizard.KIND_CLOUD,
        write=str(out), interactive=False, probe=False,
    )
    rc = wizard.run_init(opts, v)
    assert rc == 0
    # Loadable
    cfg = config_mod.load(out)
    assert cfg.jobs[0].name == "auto"
    assert cfg.jobs[0].src == "/some/src"
    assert cfg.jobs[0].dst == "remote:/dst"
