"""Smoke tests for CLI ergonomics: --version and list-jobs."""
import io
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from rclone_migrate import __version__
from rclone_migrate import cli


def test_safe_exit_keyboardinterrupt_is_clean(capsys):
    """Ctrl-C must yield exit 130 + a one-line message, not a traceback."""
    def boom():
        raise KeyboardInterrupt

    rc = cli._safe_exit(boom)
    assert rc == 130
    err = capsys.readouterr().err
    assert "Interrupted" in err
    assert "Traceback" not in err


def test_safe_exit_passes_through_normal_return():
    assert cli._safe_exit(lambda: 0) == 0
    assert cli._safe_exit(lambda: 7) == 7


def _write_config(path: Path) -> None:
    path.write_text(
        "[defaults]\nhash = 'SHA256'\n"
        "[[jobs]]\nname = 'a'\nsrc = '/tmp/sa'\ndst = '/tmp/da'\n"
        "[[jobs]]\nname = 'b'\nsrc = '/tmp/sb'\ndst = '/tmp/db'\nenabled = false\n"
    )


def test_version_top_level(capsys):
    with pytest.raises(SystemExit) as ex:
        cli.main(["--version"])
    assert ex.value.code == 0
    out = capsys.readouterr().out
    assert __version__ in out


def test_list_jobs(tmp_path: Path, capsys):
    cfg_path = tmp_path / "c.toml"
    _write_config(cfg_path)
    rc = cli.cmd_list_jobs(["-c", str(cfg_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "a" in out and "b" in out
    assert "SHA256" in out
    assert "yes" in out and "no" in out


def test_humanize_age():
    assert cli._humanize_age(None) == "—"
    assert cli._humanize_age(5) == "5s"
    assert cli._humanize_age(125) == "2m"
    assert cli._humanize_age(7200) == "2h"
    assert cli._humanize_age(2 * 86400) == "2d"
    assert cli._humanize_age(42 * 86400) == "42d"


def test_color_for_age_thresholds():
    from rclone_migrate import verbose as v
    assert cli._color_for_age(None) == v.RED
    assert cli._color_for_age(0) == v.GREEN
    assert cli._color_for_age(60 * 60) == v.GREEN          # 1h
    assert cli._color_for_age(86400 * 0.99) == v.GREEN     # <24h
    assert cli._color_for_age(86400 * 2) == v.YELLOW       # 2d
    assert cli._color_for_age(86400 * 6.99) == v.YELLOW
    assert cli._color_for_age(86400 * 30) == v.RED         # 30d


def test_color_for_status():
    from rclone_migrate import verbose as v
    assert cli._color_for_status("✓ ok") == v.GREEN
    assert cli._color_for_status("✗ 5 miss") == v.RED
    assert cli._color_for_status("⚠ crashed") == v.YELLOW
    assert cli._color_for_status("? never") == v.RED


def test_list_jobs_all_no_configs(tmp_path: Path, capsys):
    rc = cli.cmd_list_jobs(["--all", "--state-dir", str(tmp_path)])
    assert rc == 0
    assert "no *.toml" in capsys.readouterr().out


def test_list_jobs_all_scans_multiple_files(tmp_path: Path, capsys):
    """Two TOMLs in state_dir should both be discovered."""
    (tmp_path / "a.toml").write_text(
        "[[jobs]]\nname = 'a'\nsrc = '/a/s'\ndst = '/a/d'\n"
    )
    (tmp_path / "b.toml").write_text(
        "[[jobs]]\nname = 'b'\nsrc = '/b/s'\ndst = '/b/d'\n"
    )
    rc = cli.cmd_list_jobs(["--all", "--state-dir", str(tmp_path),
                            "--no-color", "--no-status"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "a" in out and "/a/s" in out and "/a/d" in out
    assert "b" in out and "/b/s" in out and "/b/d" in out


def test_list_jobs_all_shows_never_for_no_state(tmp_path: Path, capsys):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "fresh.toml").write_text(
        f"[defaults]\nstate_dir = '{state_dir}'\n"
        "[[jobs]]\nname='fresh'\nsrc='/x'\ndst='/y'\n"
    )
    rc = cli.cmd_list_jobs(["--all", "--state-dir", str(state_dir),
                            "--no-color"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "fresh" in out
    assert "never" in out


def test_list_jobs_all_reads_status_from_state_db(tmp_path: Path, capsys):
    """A state.db with a recent ok event should surface as ✓ ok."""
    from rclone_migrate import state as state_mod
    state_dir = tmp_path / "state"
    state_dir.mkdir()

    job_state = state_dir / "myjob"
    job_state.mkdir()
    conn = state_mod.open_db(job_state)
    eid = state_mod.event_start(conn, op="check", log_path="r/x.log",
                                pid=1, hostname="h")
    state_mod.event_finish(conn, eid, result="ok", algo="sha256",
                           src_count=10, dst_count=10, affected=0)
    conn.close()

    (state_dir / "myjob.toml").write_text(
        f"[defaults]\nstate_dir = '{state_dir}'\n"
        "[[jobs]]\nname='myjob'\nsrc='/x'\ndst='/y'\n"
    )
    rc = cli.cmd_list_jobs(["--all", "--state-dir", str(state_dir),
                            "--no-color"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "ok" in out
    # AGE column should show recent (seconds → "0s" or "1s"-ish, NOT "—")
    assert "—" not in out.split("ok")[1].split("\n")[0]


def test_list_jobs_all_failed_check_shows_miss_count(tmp_path: Path, capsys):
    from rclone_migrate import state as state_mod
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    job_state = state_dir / "broken"
    job_state.mkdir()
    conn = state_mod.open_db(job_state)
    eid = state_mod.event_start(conn, op="check", log_path="r/x.log",
                                pid=1, hostname="h")
    state_mod.event_finish(conn, eid, result="fail",
                           src_count=10, dst_count=7, affected=3)
    conn.close()
    (state_dir / "broken.toml").write_text(
        f"[defaults]\nstate_dir = '{state_dir}'\n"
        "[[jobs]]\nname='broken'\nsrc='/x'\ndst='/y'\n"
    )
    rc = cli.cmd_list_jobs(["--all", "--state-dir", str(state_dir),
                            "--no-color"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "3 miss" in out


def test_list_jobs_requires_c_or_all(capsys):
    """Without -c or --all, command should error."""
    with pytest.raises(SystemExit) as ex:
        cli.cmd_list_jobs([])
    assert ex.value.code == 2


def test_list_jobs_no_color_strips_ansi(tmp_path: Path, capsys):
    """--no-color guarantees no escape codes."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "x.toml").write_text(
        "[[jobs]]\nname='x'\nsrc='/a'\ndst='/b'\n"
    )
    cli.cmd_list_jobs(["--all", "--state-dir", str(state_dir),
                       "--no-color"])
    out = capsys.readouterr().out
    assert "\x1b[" not in out


def test_resolve_config_path_uses_explicit_c(tmp_path: Path):
    """When -c is given, that wins regardless of state_dir."""
    (tmp_path / "x.toml").write_text("[[jobs]]\nname='x'\nsrc='/a'\ndst='/b'\n")
    args = type("A", (), {"config": str(tmp_path / "x.toml"),
                          "state_dir": "/nonexistent",
                          "job": "x"})()
    p = cli._resolve_config_path(args)
    assert Path(p) == tmp_path / "x.toml"


def test_resolve_config_path_uses_convention(tmp_path: Path):
    """Without -c, the convention <state-dir>/<job>.toml is used."""
    (tmp_path / "myjob.toml").write_text(
        "[[jobs]]\nname='myjob'\nsrc='/a'\ndst='/b'\n"
    )
    args = type("A", (), {"config": None,
                          "state_dir": str(tmp_path),
                          "job": "myjob"})()
    p = cli._resolve_config_path(args)
    assert Path(p) == tmp_path / "myjob.toml"


def test_resolve_config_path_missing_errors(tmp_path: Path, capsys):
    args = type("A", (), {"config": None,
                          "state_dir": str(tmp_path),
                          "job": "absent"})()
    with pytest.raises(SystemExit) as ex:
        cli._resolve_config_path(args)
    assert ex.value.code == 2
    err = capsys.readouterr().err
    assert "no config found" in err
    assert str(tmp_path / "absent.toml") in err


def test_log_uses_convention_path(tmp_path: Path, capsys):
    """rmig log -j JOB without -c works when conventional toml exists."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "myjob.toml").write_text(
        f"[defaults]\nstate_dir = '{state_dir}'\n"
        "[[jobs]]\nname='myjob'\nsrc='/a'\ndst='/b'\n"
    )
    rc = cli.cmd_log(["-j", "myjob", "--state-dir", str(state_dir)])
    assert rc == 0  # No state.db yet → "nothing to show", not error


def test_unknown_subcommand_exits_2(capsys):
    with pytest.raises(SystemExit) as ex:
        cli.main(["nonexistent-cmd"])
    assert ex.value.code == 2


def test_help_exits_0(capsys):
    with pytest.raises(SystemExit) as ex:
        cli.main(["--help"])
    assert ex.value.code == 0


# --- rmig log / rmig file-status ---

def _setup_e2e(tmp_path: Path):
    """Create a tiny e2e: src=2 files, dst=1 matching, run check→fails."""
    import contextlib, io
    from rclone_migrate import config as config_mod, ops
    src = tmp_path / "src"; dst = tmp_path / "dst"; sd = tmp_path / "state"
    src.mkdir(); dst.mkdir(); sd.mkdir()
    (src / "a.txt").write_bytes(b"alpha")
    (src / "b.txt").write_bytes(b"beta")
    (dst / "x.txt").write_bytes(b"alpha")
    cfg_path = tmp_path / "c.toml"
    cfg_path.write_text(
        f"[defaults]\nstate_dir = '{sd}'\ntransfers = 2\n"
        f"local_cache_in_root = true\n"
        f"[delete]\nrequire_check_within = '24h'\nrequire_confirm = true\n"
        f"[[jobs]]\nname = 't'\nsrc = '{src}'\ndst = '{dst}'\n"
    )
    cfg = config_mod.load(cfg_path)
    job = cfg.get_job("t")
    # Swallow the MISSING report so it doesn't leak into capsys for the
    # tests that read CLI stdout afterwards.
    with contextlib.redirect_stdout(io.StringIO()), \
         contextlib.redirect_stderr(io.StringIO()):
        ops.do_check(cfg, job, progress=False)
    return cfg_path


def test_log_table_output(tmp_path: Path, capsys):
    cfg_path = _setup_e2e(tmp_path)
    rc = cli.cmd_log(["-c", str(cfg_path), "-j", "t"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "STARTED" in out and "OP" in out and "RESULT" in out
    assert "check" in out
    assert "fail" in out


def test_log_filter_op(tmp_path: Path, capsys):
    cfg_path = _setup_e2e(tmp_path)
    rc = cli.cmd_log(["-c", str(cfg_path), "-j", "t", "--op", "copy"])
    assert rc == 0
    assert "(no events match)" in capsys.readouterr().out


def test_file_status_backed_up(tmp_path: Path, capsys):
    cfg_path = _setup_e2e(tmp_path)
    rc = cli.cmd_file_status(["-c", str(cfg_path), "-j", "t", "a.txt"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "BACKED UP" in out
    assert "x.txt" in out


def test_file_status_missing(tmp_path: Path, capsys):
    cfg_path = _setup_e2e(tmp_path)
    rc = cli.cmd_file_status(["-c", str(cfg_path), "-j", "t", "b.txt"])
    assert rc == 1
    out = capsys.readouterr().out
    assert "MISSING" in out


def test_file_status_json(tmp_path: Path, capsys):
    import json
    cfg_path = _setup_e2e(tmp_path)
    cli.cmd_file_status(["-c", str(cfg_path), "-j", "t", "--json", "a.txt"])
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert parsed["status"] == "backed_up"
    assert parsed["path"] == "a.txt"
    assert len(parsed["matches"]) == 1


def test_file_status_missing_list(tmp_path: Path, capsys):
    cfg_path = _setup_e2e(tmp_path)
    rc = cli.cmd_file_status(["-c", str(cfg_path), "-j", "t", "--missing"])
    assert rc == 1
    out = capsys.readouterr().out
    assert "b.txt" in out
    assert "a.txt" not in out


def test_file_status_hash_lookup(tmp_path: Path, capsys):
    import hashlib
    cfg_path = _setup_e2e(tmp_path)
    h = hashlib.sha256(b"alpha").hexdigest()
    rc = cli.cmd_file_status(["-c", str(cfg_path), "-j", "t", "--hash", h])
    assert rc == 0
    out = capsys.readouterr().out
    # Both src "a.txt" and dst "x.txt" share hash
    assert "a.txt" in out
    assert "x.txt" in out
