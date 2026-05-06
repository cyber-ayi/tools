"""Tests for audit context manager: events, file_events, orphan detection,
log file tee, fcntl lockfile."""
import fcntl
import json
import os
import sqlite3
from pathlib import Path

import pytest

from rclone_migrate import audit, state


def test_event_lifecycle(tmp_path: Path):
    with audit.run(tmp_path, op="check") as ev:
        ev.set_algo("sha256")
        ev.set_counts(src=10, dst=12, affected=0)
        ev.set_signature("deadbeef")
        ev.set_result("ok")

    conn = state.open_db(tmp_path)
    rows = state.query_events(conn)
    assert len(rows) == 1
    r = rows[0]
    assert r["op"] == "check"
    assert r["result"] == "ok"
    assert r["algo"] == "sha256"
    assert r["src_count"] == 10 and r["dst_count"] == 12
    assert r["signature"] == "deadbeef"
    assert r["log_path"] is not None
    assert r["ended_ts"] is not None
    log_path = tmp_path / r["log_path"]
    assert log_path.exists()
    text = log_path.read_text()
    assert "started" in text and "ended" in text
    assert "result=ok" in text
    conn.close()


def test_file_events_recorded(tmp_path: Path):
    with audit.run(tmp_path, op="check") as ev:
        ev.set_result("fail")
        ev.record_file("src", "missing1.bin", outcome="missing", hash="h1")
        ev.record_file("src", "missing2.bin", outcome="missing", hash="h2")

    conn = state.open_db(tmp_path)
    fes = state.query_file_events(conn, side="src")
    assert {f["path"] for f in fes} == {"missing1.bin", "missing2.bin"}
    assert all(f["outcome"] == "missing" for f in fes)
    assert all(f["op"] == "check" for f in fes)
    conn.close()


def test_orphan_detection(tmp_path: Path):
    """Crash mid-op → next run finds orphan and marks crashed."""
    # Manually insert an in-progress event (simulating a crashed process)
    conn = state.open_db(tmp_path)
    eid = state.event_start(conn, op="copy", log_path="runs/x.log",
                            pid=99999, hostname="test")
    conn.close()

    # Next run notices the orphan
    with audit.run(tmp_path, op="check") as ev:
        ev.set_result("ok")

    conn = state.open_db(tmp_path)
    rows = state.query_events(conn)
    # 2 events: the orphan (now 'crashed') + the new one
    by_id = {r["id"]: r for r in rows}
    assert by_id[eid]["result"] == "crashed"
    assert by_id[eid]["ended_ts"] is not None
    conn.close()


def test_exception_records_fail(tmp_path: Path):
    with pytest.raises(ValueError):
        with audit.run(tmp_path, op="check") as ev:
            raise ValueError("boom")

    conn = state.open_db(tmp_path)
    rows = state.query_events(conn)
    assert rows[0]["result"] == "fail"
    assert "ValueError" in (rows[0]["notes"] or "")
    conn.close()


def test_lock_contention_raises(tmp_path: Path):
    """A second concurrent audit.run() on the same state_dir must fail
    with LockContention rather than silently corrupt state."""
    fh = audit._acquire_job_lock(tmp_path, op="check")
    try:
        with pytest.raises(audit.LockContention) as exc:
            with audit.run(tmp_path, op="copy") as ev:
                pass  # never reached
        # The error message should identify the holder
        assert exc.value.holder_pid == os.getpid()
        assert exc.value.holder_op == "check"
        assert "another rmig run holds the job lock" in str(exc.value)
    finally:
        fh.close()  # release


def test_lock_released_on_normal_exit(tmp_path: Path):
    """After a successful audit.run, a follow-up run must succeed."""
    with audit.run(tmp_path, op="check") as ev:
        ev.set_result("ok")
    # Lock should be released — second run works
    with audit.run(tmp_path, op="copy") as ev:
        ev.set_result("ok")
    conn = state.open_db(tmp_path)
    rows = state.query_events(conn)
    assert len(rows) == 2
    conn.close()


def test_lock_released_on_exception(tmp_path: Path):
    """An exception inside audit.run must still release the lock."""
    with pytest.raises(ValueError):
        with audit.run(tmp_path, op="check") as ev:
            raise ValueError("boom")
    # Follow-up run succeeds → lock was released
    with audit.run(tmp_path, op="check") as ev:
        ev.set_result("ok")


def test_lock_skipped_when_acquire_lock_false(tmp_path: Path):
    """Read-only commands pass acquire_lock=False; concurrent audit.run
    with acquire_lock=False must coexist."""
    with audit.run(tmp_path, op="log", acquire_lock=False) as ev1:
        with audit.run(tmp_path, op="log", acquire_lock=False) as ev2:
            ev2.set_result("ok")
        ev1.set_result("ok")


def test_lock_contention_metadata_diagnostic(tmp_path: Path):
    """The lockfile holds JSON metadata for diagnostics."""
    fh = audit._acquire_job_lock(tmp_path, op="copy")
    try:
        body = (tmp_path / "job.lock").read_text().strip()
        info = json.loads(body)
        assert info["pid"] == os.getpid()
        assert info["op"] == "copy"
        assert "started" in info
    finally:
        fh.close()


def test_log_file_strips_ansi(tmp_path: Path, capsys):
    """Terminal output keeps colors; the persisted log file must not."""
    with audit.run(tmp_path, op="check") as ev:
        # Print an ANSI-decorated line (as Verbose would when color=True)
        print("\x1b[31mred error\x1b[0m and \x1b[32mgreen ok\x1b[0m")
        ev.set_result("ok")

    captured = capsys.readouterr()
    # Original stdout sees the escape codes
    assert "\x1b[31m" in captured.out

    # But the log file is plain text
    conn = state.open_db(tmp_path)
    rows = state.query_events(conn)
    log_text = (tmp_path / rows[0]["log_path"]).read_text()
    assert "\x1b[" not in log_text
    assert "red error and green ok" in log_text
    conn.close()


def test_stdout_teed_to_log(tmp_path: Path, capsys):
    with audit.run(tmp_path, op="check") as ev:
        print("hello from inside the audit run")
        ev.set_result("ok")

    # User still saw the output via the original stdout
    captured = capsys.readouterr()
    assert "hello from inside" in captured.out

    # And it's also in the log file
    conn = state.open_db(tmp_path)
    rows = state.query_events(conn)
    log_path = tmp_path / rows[0]["log_path"]
    text = log_path.read_text()
    assert "hello from inside" in text
    conn.close()
