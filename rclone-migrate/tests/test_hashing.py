import hashlib
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
