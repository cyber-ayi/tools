"""Stage G: pre-clean .partial orphaned by an interrupted run."""
import io

from rclone_migrate import verbose
from rclone_migrate.manifest import Entry
from rclone_migrate.ops import _clean_stale_partials


class _Job:
    def __init__(self, dst):
        self.dst = dst
        self.src = "/whatever"


def _v():
    return verbose.Verbose(level=verbose.DETAIL, color=False,
                           timestamps=False, stream=io.StringIO(),
                           err_stream=io.StringIO())


def test_removes_stale_partial_for_to_copy_files(tmp_path):
    d = tmp_path / "dst"
    d.mkdir()
    (d / "VID_001.insv.b856018f.partial").write_bytes(b"x" * 10)  # stale
    (d / "VID_001.insv").unlink(missing_ok=True)
    (d / "OTHER.mov.deadbeef.partial").write_bytes(b"y")          # unrelated
    (d / "VID_002.insv").write_bytes(b"real")                     # real file

    to_copy = [Entry(path="VID_001.insv", hash="h", size=10)]
    n = _clean_stale_partials(_Job(str(d)), to_copy, _v())

    assert n == 1
    assert not (d / "VID_001.insv.b856018f.partial").exists()     # removed
    assert (d / "OTHER.mov.deadbeef.partial").exists()            # untouched
    assert (d / "VID_002.insv").exists()                          # untouched


def test_no_partial_nothing_removed(tmp_path):
    d = tmp_path / "dst"; d.mkdir()
    (d / "VID_001.insv").write_bytes(b"data")
    assert _clean_stale_partials(
        _Job(str(d)), [Entry("VID_001.insv", "h", 4)], _v()) == 0


def test_skips_remote_dst():
    # remote dst → rclone owns the partial; helper must no-op
    assert _clean_stale_partials(
        _Job("b2:bucket/path"), [Entry("a", "h", 1)], _v()) == 0


def test_prefix_boundary_not_overmatched(tmp_path):
    d = tmp_path / "dst"; d.mkdir()
    # a different file's partial that merely shares a name prefix
    (d / "VID_1.insv.aa.partial").write_bytes(b"x")
    (d / "VID_10.insv.bb.partial").write_bytes(b"y")
    to_copy = [Entry(path="VID_1.insv", hash="h", size=1)]
    _clean_stale_partials(_Job(str(d)), to_copy, _v())
    assert not (d / "VID_1.insv.aa.partial").exists()   # matched (dot boundary)
    assert (d / "VID_10.insv.bb.partial").exists()      # NOT over-matched
