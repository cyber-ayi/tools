"""Guard: xxhash is a CORE dependency (Stage F).

Deliberately NOT pytest.importorskip — if xxhash is ever dropped back to
an optional extra, importing it here fails and CI goes red, instead of the
field-observed silent degradation (xxh3 → slow per-file `rclone hashsum`,
no intra-file progress).
"""
import xxhash  # noqa: F401  (hard import on purpose)

from rclone_migrate import hashing


def test_xxhash_importable_as_core_dep():
    assert xxhash.VERSION  # present without any optional extra


def test_xxhash_family_is_streamable():
    # can_stream_local True ⇒ hash_file_local streams with progress_cb
    # ⇒ the multi-line worker bytes/% actually advance.
    for algo in ("xxh3", "xxh128", "xxh64"):
        assert hashing.can_stream_local(algo) is True, algo


def test_dit_default_algo_streams():
    """The `dit` profile's primary algo (xxh3) must stream, else the
    whole camera-original workflow degrades."""
    assert hashing.can_stream_local("xxh3") is True
