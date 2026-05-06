"""Tests for the Verbose helper module."""
import io
import re

import pytest

from rclone_migrate import verbose


def test_levels_filter_correctly():
    out = io.StringIO()
    err = io.StringIO()
    v = verbose.Verbose(level=verbose.NORMAL, color=False, timestamps=False,
                        stream=out, err_stream=err)
    v.info("info-line")
    v.detail("detail-line")    # suppressed at NORMAL
    v.debug("debug-line")      # suppressed
    v.warn("warn-line")
    v.error("error-line")
    out_text = out.getvalue()
    err_text = err.getvalue()
    assert "info-line" in out_text
    assert "detail-line" not in out_text
    assert "debug-line" not in out_text
    assert "WARN: warn-line" in err_text
    assert "ERROR: error-line" in err_text


def test_detail_level_includes_detail():
    out = io.StringIO()
    v = verbose.Verbose(level=verbose.DETAIL, color=False, timestamps=False,
                        stream=out, err_stream=io.StringIO())
    v.detail("d1")
    v.debug("d2")
    text = out.getvalue()
    assert "d1" in text
    assert "d2" not in text   # still suppressed at DETAIL


def test_debug_level_includes_all():
    out = io.StringIO()
    v = verbose.Verbose(level=verbose.DEBUG, color=False, timestamps=False,
                        stream=out, err_stream=io.StringIO())
    v.detail("d1"); v.debug("d2")
    assert "d1" in out.getvalue() and "d2" in out.getvalue()


def test_quiet_suppresses_info_but_not_warn():
    out = io.StringIO()
    err = io.StringIO()
    v = verbose.Verbose(level=verbose.QUIET, color=False, timestamps=False,
                        stream=out, err_stream=err)
    v.info("hidden")
    v.warn("must-show")
    assert out.getvalue() == ""
    assert "must-show" in err.getvalue()


def test_color_codes_when_color_on():
    out = io.StringIO()
    v = verbose.Verbose(level=verbose.NORMAL, color=True, timestamps=False,
                        stream=out, err_stream=io.StringIO())
    v.ok("good")
    text = out.getvalue()
    assert "\x1b[32m" in text   # GREEN
    assert "\x1b[0m" in text    # RESET


def test_no_color_when_color_off():
    out = io.StringIO()
    v = verbose.Verbose(level=verbose.NORMAL, color=False, timestamps=False,
                        stream=out, err_stream=io.StringIO())
    v.ok("good")
    text = out.getvalue()
    assert "\x1b[" not in text


def test_timestamps_prefix():
    out = io.StringIO()
    v = verbose.Verbose(level=verbose.NORMAL, color=False, timestamps=True,
                        stream=out, err_stream=io.StringIO())
    v.info("hello")
    text = out.getvalue()
    assert re.match(r"\[\d\d:\d\d:\d\d\] hello", text.strip())


def test_strip_ansi():
    s = "\x1b[31mred\x1b[0m and \x1b[32mgreen\x1b[0m"
    assert verbose.strip_ansi(s) == "red and green"


def test_phase_emits_at_detail_level():
    out = io.StringIO()
    v = verbose.Verbose(level=verbose.DETAIL, color=False, timestamps=False,
                        stream=out, err_stream=io.StringIO())
    with v.phase("test phase"):
        pass
    text = out.getvalue()
    assert "→ test phase starting" in text
    assert "← test phase done in " in text


def test_phase_silent_at_normal():
    out = io.StringIO()
    v = verbose.Verbose(level=verbose.NORMAL, color=False, timestamps=False,
                        stream=out, err_stream=io.StringIO())
    with v.phase("test phase"):
        pass
    assert out.getvalue() == ""


def test_phase_emits_on_exception():
    """The exit log line must fire even if the body raised."""
    out = io.StringIO()
    v = verbose.Verbose(level=verbose.DETAIL, color=False, timestamps=False,
                        stream=out, err_stream=io.StringIO())
    with pytest.raises(ValueError):
        with v.phase("explody"):
            raise ValueError("boom")
    assert "← explody done" in out.getvalue()


def test_color_auto_detects_isatty(monkeypatch):
    fake_tty = io.StringIO()
    fake_tty.isatty = lambda: True   # type: ignore
    v = verbose.Verbose(level=verbose.NORMAL, stream=fake_tty)
    assert v.color is True
    fake_not_tty = io.StringIO()
    fake_not_tty.isatty = lambda: False   # type: ignore
    v2 = verbose.Verbose(level=verbose.NORMAL, stream=fake_not_tty)
    assert v2.color is False


def test_default_returns_normal():
    d = verbose.default()
    assert d.level == verbose.NORMAL
