import hashlib
import shutil
from pathlib import Path
from unittest import mock

import pytest

from rclone_migrate import hashing


def test_normalize():
    assert hashing.normalize("MD5") == "md5"
    assert hashing.normalize("SHA-256") == "sha256"
    assert hashing.normalize("sha1") == "sha1"


def test_hash_file_local_md5(tmp_path: Path):
    f = tmp_path / "a.txt"
    f.write_bytes(b"hello world")
    expected = hashlib.md5(b"hello world").hexdigest()
    assert hashing.hash_file_local(str(f), "md5") == expected


def test_hash_file_local_sha256(tmp_path: Path):
    f = tmp_path / "a.txt"
    f.write_bytes(b"hello world")
    expected = hashlib.sha256(b"hello world").hexdigest()
    assert hashing.hash_file_local(str(f), "sha256") == expected


xxhash = pytest.importorskip("xxhash")


def test_can_stream_local_truth_table():
    for a in ("md5", "sha1", "sha256", "sha512", "xxh3", "xxh128", "xxh64"):
        assert hashing.can_stream_local(a) is True
    for a in ("crc32", "blake3", "quickxor", "whirlpool"):
        assert hashing.can_stream_local(a) is False


def test_hash_file_local_xxhash_streams_and_matches_oneshot(tmp_path: Path):
    data = b"".join(bytes([i % 256]) for i in range(300_000)) + b"end"
    f = tmp_path / "x.bin"
    f.write_bytes(data)
    cases = {
        "xxh3": xxhash.xxh3_64(data).hexdigest(),
        "xxh128": xxhash.xxh3_128(data).hexdigest(),
        "xxh64": xxhash.xxh64(data).hexdigest(),
    }
    for algo, expected in cases.items():
        seen = []
        got = hashing.hash_file_local(
            str(f), algo, chunk_size=64 * 1024, progress_cb=seen.append
        )
        assert got == expected, algo
        assert sum(seen) == len(data)        # progress_cb fired per chunk
        assert len(seen) >= 4


@pytest.mark.skipif(shutil.which("rclone") is None, reason="rclone not installed")
def test_xxhash_digest_byte_identical_to_rclone(tmp_path: Path):
    """Critical: a manifest hashed in-process must match a side hashed by
    rclone, or check/copy would see false 'missing' files."""
    import subprocess

    f = tmp_path / "blob.bin"
    f.write_bytes(b"rclone-parity-check" * 9999)
    for algo in ("xxh3", "xxh128"):
        out = subprocess.run(
            ["rclone", "hashsum", algo, str(f)],
            capture_output=True, text=True, check=True,
        ).stdout.split()[0]
        assert hashing.hash_file_local(str(f), algo) == out, algo


def test_negotiate_picks_strongest_common(monkeypatch):
    def fake(path):
        return {
            "/local/src": ["md5", "sha1", "sha256", "sha512"],
            "remote:dst": ["md5"],
        }[path]
    monkeypatch.setattr(hashing, "supported_hashes", fake)
    assert hashing.negotiate("/local/src", "remote:dst") == "md5"


def test_negotiate_local_to_local_picks_sha256(monkeypatch):
    def fake(path):
        return ["md5", "sha1", "sha256", "sha512", "blake3"]
    monkeypatch.setattr(hashing, "supported_hashes", fake)
    assert hashing.negotiate("/a", "/b") == "sha256"


def test_negotiate_b2_picks_sha1(monkeypatch):
    def fake(path):
        return {
            "/local/src": ["md5", "sha1", "sha256", "sha512"],
            "b2:bucket":  ["sha1"],
        }[path]
    monkeypatch.setattr(hashing, "supported_hashes", fake)
    assert hashing.negotiate("/local/src", "b2:bucket") == "sha1"


def test_negotiate_override_must_be_supported(monkeypatch):
    def fake(path):
        return ["md5"]
    monkeypatch.setattr(hashing, "supported_hashes", fake)
    with pytest.raises(hashing.HashNegotiationError):
        hashing.negotiate("a", "b", override="sha256")


def test_negotiate_override_works(monkeypatch):
    def fake(path):
        return ["md5", "sha1", "sha256"]
    monkeypatch.setattr(hashing, "supported_hashes", fake)
    assert hashing.negotiate("a", "b", override="MD5") == "md5"


def test_negotiate_no_common(monkeypatch):
    def fake(path):
        return {"a": ["md5"], "b": ["sha256"]}[path]
    monkeypatch.setattr(hashing, "supported_hashes", fake)
    with pytest.raises(hashing.HashNegotiationError):
        hashing.negotiate("a", "b")
