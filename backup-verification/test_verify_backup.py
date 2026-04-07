#!/usr/bin/env python3
"""Unit tests for verify_backup.py"""

import hashlib
import os
import shutil
import sqlite3
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

# Import module under test
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from verify_backup import (
    CACHE_FILENAME,
    find_jpeg_sos,
    fmt_size,
    is_jpeg,
    load_cache_all,
    open_cache_db,
    sha256,
    sha256_dual,
    scan_dir,
    sync_cache,
    validate_cache,
)


# ---------------------------------------------------------------------------
# Helpers for building minimal JPEG files
# ---------------------------------------------------------------------------

def _make_jpeg(exif_payload=b'\x00' * 20, image_data=b'\xAB' * 100):
    """Build a minimal valid JPEG with controllable EXIF and image data.

    Structure: SOI | APP1(exif_payload)... | SOS | image_data | EOI

    If exif_payload exceeds the APP1 max size (65533 bytes), it is split
    across multiple APP1 markers automatically.
    """
    buf = bytearray()
    # SOI
    buf += b'\xff\xd8'
    # Split exif_payload into APP1 segments (max payload per marker = 65533)
    MAX_PAYLOAD = 65533  # 65535 - 2 (length field includes itself)
    offset = 0
    while offset < len(exif_payload):
        chunk = exif_payload[offset:offset + MAX_PAYLOAD]
        buf += b'\xff\xe1'
        seg_len = 2 + len(chunk)
        buf += struct.pack('>H', seg_len)
        buf += chunk
        offset += MAX_PAYLOAD
    if not exif_payload:
        # Write an empty APP1 if no payload
        buf += b'\xff\xe1'
        buf += struct.pack('>H', 2)
    # SOS marker (minimal: marker + length(2) + dummy header(1))
    buf += b'\xff\xda'
    buf += struct.pack('>H', 3)
    buf += b'\x00'
    # Image data (entropy-coded segment)
    buf += image_data
    # EOI
    buf += b'\xff\xd9'
    return bytes(buf)


def _write_file(directory, name, content):
    p = Path(directory) / name
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(str(p), 'wb') as f:
        f.write(content)
    return p


def _file_sha256(data):
    return hashlib.sha256(data).hexdigest()


def _path_key(p):
    """Convert a path string to the native str(Path(...)) form for cache key consistency."""
    return str(Path(p))


# ===========================================================================
# Test cases
# ===========================================================================

class TestIsJpeg(unittest.TestCase):

    def test_jpg_extension(self):
        self.assertTrue(is_jpeg("photo.JPG"))
        self.assertTrue(is_jpeg("photo.jpg"))
        self.assertTrue(is_jpeg(Path("dir/photo.jpeg")))

    def test_non_jpeg(self):
        self.assertFalse(is_jpeg("photo.RAF"))
        self.assertFalse(is_jpeg("video.MOV"))
        self.assertFalse(is_jpeg("file.txt"))


class TestFmtSize(unittest.TestCase):

    def test_bytes(self):
        self.assertEqual(fmt_size(0), "0.0 B")
        self.assertEqual(fmt_size(512), "512.0 B")

    def test_kb(self):
        self.assertIn("KB", fmt_size(2048))

    def test_gb(self):
        self.assertIn("GB", fmt_size(2 * 1024 ** 3))


class TestFindJpegSos(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    def test_valid_jpeg(self):
        data = _make_jpeg()
        p = _write_file(self.tmpdir, "test.jpg", data)
        offset = find_jpeg_sos(str(p))
        self.assertIsNotNone(offset)
        with open(str(p), 'rb') as f:
            f.seek(offset)
            self.assertEqual(f.read(2), b'\xff\xda')

    def test_not_jpeg(self):
        p = _write_file(self.tmpdir, "test.txt", b'Hello world')
        self.assertIsNone(find_jpeg_sos(str(p)))

    def test_truncated_file(self):
        p = _write_file(self.tmpdir, "bad.jpg", b'\xff\xd8\xff')
        self.assertIsNone(find_jpeg_sos(str(p)))

    def test_no_sos_marker(self):
        data = b'\xff\xd8\xff\xe1\x00\x04\x00\x00'
        p = _write_file(self.tmpdir, "no_sos.jpg", data)
        self.assertIsNone(find_jpeg_sos(str(p)))

    def test_multiple_app_markers(self):
        """SOS should be found even after multiple APP markers."""
        data = _make_jpeg(exif_payload=b'\x00' * 200)
        p = _write_file(self.tmpdir, "multi.jpg", data)
        offset = find_jpeg_sos(str(p))
        self.assertIsNotNone(offset)
        with open(str(p), 'rb') as f:
            f.seek(offset)
            self.assertEqual(f.read(2), b'\xff\xda')


class TestSha256(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    def test_basic(self):
        data = b'test content for hashing'
        p = _write_file(self.tmpdir, "file.bin", data)
        expected = _file_sha256(data)
        self.assertEqual(sha256(str(p)), expected)

    def test_empty_file(self):
        p = _write_file(self.tmpdir, "empty.bin", b'')
        expected = _file_sha256(b'')
        self.assertEqual(sha256(str(p)), expected)


class TestSha256Dual(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    def test_non_jpeg_returns_none_data(self):
        data = b'not a jpeg'
        p = _write_file(self.tmpdir, "file.bin", data)
        full, data_hash = sha256_dual(str(p))
        self.assertEqual(full, _file_sha256(data))
        self.assertIsNone(data_hash)

    def test_jpeg_dual_hash(self):
        exif = b'\x01\x02\x03\x04' * 5
        img = b'\xDE\xAD\xBE\xEF' * 50
        jpeg_data = _make_jpeg(exif_payload=exif, image_data=img)
        p = _write_file(self.tmpdir, "photo.jpg", jpeg_data)

        full, data_hash = sha256_dual(str(p))

        # Full hash should match entire file
        self.assertEqual(full, _file_sha256(jpeg_data))

        # Data hash should match from SOS offset to EOF
        sos_offset = find_jpeg_sos(str(p))
        expected_data_hash = _file_sha256(jpeg_data[sos_offset:])
        self.assertEqual(data_hash, expected_data_hash)

    def test_metadata_change_only(self):
        """Two JPEGs with different EXIF but same image data should have
        different full hashes but identical data hashes."""
        img = b'\xCA\xFE' * 100
        jpeg_a = _make_jpeg(exif_payload=b'\x00' * 20, image_data=img)
        jpeg_b = _make_jpeg(exif_payload=b'\xFF' * 20, image_data=img)
        pa = _write_file(self.tmpdir, "a.jpg", jpeg_a)
        pb = _write_file(self.tmpdir, "b.jpg", jpeg_b)

        full_a, data_a = sha256_dual(str(pa))
        full_b, data_b = sha256_dual(str(pb))

        self.assertNotEqual(full_a, full_b)
        self.assertEqual(data_a, data_b)

    def test_image_data_change(self):
        """Two JPEGs with same EXIF but different image data should have
        different data hashes."""
        exif = b'\x00' * 20
        jpeg_a = _make_jpeg(exif_payload=exif, image_data=b'\x01' * 100)
        jpeg_b = _make_jpeg(exif_payload=exif, image_data=b'\x02' * 100)
        pa = _write_file(self.tmpdir, "a.jpg", jpeg_a)
        pb = _write_file(self.tmpdir, "b.jpg", jpeg_b)

        _, data_a = sha256_dual(str(pa))
        _, data_b = sha256_dual(str(pb))

        self.assertNotEqual(data_a, data_b)

    def test_large_file_crossing_chunk_boundary(self):
        """Ensure dual hash works when SOS offset falls mid-chunk."""
        # Create EXIF payload larger than 1 MB to force SOS into second chunk.
        # _make_jpeg splits large payloads across multiple APP1 markers.
        large_exif = b'\x00' * (1024 * 1024 + 500)
        img = b'\xAB' * 200
        jpeg_data = _make_jpeg(exif_payload=large_exif, image_data=img)
        p = _write_file(self.tmpdir, "big.jpg", jpeg_data)

        full, data_hash = sha256_dual(str(p))
        self.assertEqual(full, _file_sha256(jpeg_data))

        sos_offset = find_jpeg_sos(str(p))
        self.assertIsNotNone(sos_offset)
        self.assertGreater(sos_offset, 1 << 20)  # past first chunk
        expected_data = _file_sha256(jpeg_data[sos_offset:])
        self.assertEqual(data_hash, expected_data)


class TestScanDir(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    def test_finds_files(self):
        _write_file(self.tmpdir, "a.jpg", b'\x00')
        _write_file(self.tmpdir, "sub/b.txt", b'\x01\x02')
        files = scan_dir(Path(self.tmpdir))
        names = [p.name for p, _, _ in files]
        self.assertIn("a.jpg", names)
        self.assertIn("b.txt", names)
        self.assertEqual(len(files), 2)

    def test_excludes_cache_file(self):
        _write_file(self.tmpdir, CACHE_FILENAME, b'db data')
        _write_file(self.tmpdir, "real.jpg", b'\x00')
        files = scan_dir(Path(self.tmpdir))
        names = [p.name for p, _, _ in files]
        self.assertNotIn(CACHE_FILENAME, names)
        self.assertEqual(len(files), 1)

    def test_empty_dir(self):
        files = scan_dir(Path(self.tmpdir))
        self.assertEqual(files, [])


class TestCacheDB(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    def test_open_creates_table(self):
        conn = open_cache_db(self.tmpdir)
        cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = [row[0] for row in cur.fetchall()]
        self.assertIn("hash_cache", tables)
        conn.close()

    def test_open_idempotent(self):
        conn1 = open_cache_db(self.tmpdir)
        conn1.close()
        conn2 = open_cache_db(self.tmpdir)
        conn2.close()

    def test_migration_adds_data_sha256(self):
        db_path = Path(self.tmpdir) / CACHE_FILENAME
        conn = sqlite3.connect(str(db_path))
        conn.execute("""
            CREATE TABLE hash_cache (
                path TEXT PRIMARY KEY, sha256 TEXT NOT NULL,
                size INTEGER NOT NULL, mtime REAL NOT NULL
            )
        """)
        conn.execute(
            "INSERT INTO hash_cache VALUES (?, ?, ?, ?)",
            ("/old/file.jpg", "abc123", 1000, 1234.0)
        )
        conn.commit()
        conn.close()

        conn = open_cache_db(self.tmpdir)
        rows = load_cache_all(conn)
        self.assertIn("/old/file.jpg", rows)
        self.assertIsNone(rows["/old/file.jpg"]["data_sha256"])
        conn.close()


class TestValidateCache(unittest.TestCase):
    """Tests for validate_cache().

    Uses real temp-dir paths via _path_key() to ensure Windows/Unix
    compatibility (str(Path(...)) produces \\ on Windows, / on Unix).
    """

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    def _key(self, name):
        """Return native path string for a file in tmpdir."""
        return str(Path(self.tmpdir) / name)

    def test_all_valid(self):
        k = self._key("a.jpg")
        cache = {
            k: {"sha256": "aaa", "size": 100, "mtime": 1000.0, "data_sha256": None},
        }
        dest_files = [(Path(self.tmpdir) / "a.jpg", 100, 1000.0)]
        valid, stale, missing, removed = validate_cache(cache, dest_files)
        self.assertEqual(len(valid), 1)
        self.assertEqual(stale, [])
        self.assertEqual(missing, [])
        self.assertEqual(removed, [])

    def test_stale_size_changed(self):
        k = self._key("a.jpg")
        cache = {
            k: {"sha256": "aaa", "size": 100, "mtime": 1000.0, "data_sha256": None},
        }
        dest_files = [(Path(self.tmpdir) / "a.jpg", 200, 1000.0)]
        valid, stale, missing, removed = validate_cache(cache, dest_files)
        self.assertEqual(len(valid), 0)
        self.assertEqual(stale, [k])

    def test_stale_mtime_changed(self):
        k = self._key("a.jpg")
        cache = {
            k: {"sha256": "aaa", "size": 100, "mtime": 1000.0, "data_sha256": None},
        }
        dest_files = [(Path(self.tmpdir) / "a.jpg", 100, 2000.0)]
        valid, stale, missing, removed = validate_cache(cache, dest_files)
        self.assertEqual(len(valid), 0)
        self.assertEqual(stale, [k])

    def test_missing_new_file(self):
        k = self._key("new.jpg")
        cache = {}
        dest_files = [(Path(self.tmpdir) / "new.jpg", 100, 1000.0)]
        valid, stale, missing, removed = validate_cache(cache, dest_files)
        self.assertEqual(missing, [k])

    def test_removed_file(self):
        k = self._key("gone.jpg")
        cache = {
            k: {"sha256": "aaa", "size": 100, "mtime": 1000.0, "data_sha256": None},
        }
        dest_files = []
        valid, stale, missing, removed = validate_cache(cache, dest_files)
        self.assertEqual(removed, [k])

    def test_mtime_tolerance(self):
        k = self._key("a.jpg")
        cache = {
            k: {"sha256": "aaa", "size": 100, "mtime": 1000.005, "data_sha256": None},
        }
        dest_files = [(Path(self.tmpdir) / "a.jpg", 100, 1000.001)]
        valid, stale, missing, removed = validate_cache(cache, dest_files)
        self.assertEqual(len(valid), 1)


class TestSyncCache(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.conn = open_cache_db(self.tmpdir)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmpdir)

    def test_insert_new(self):
        new = {
            "/a.jpg": {"sha256": "aaa", "size": 100, "mtime": 1000.0, "data_sha256": "ddd"},
        }
        sync_cache(self.conn, {}, new, [])
        rows = load_cache_all(self.conn)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows["/a.jpg"]["sha256"], "aaa")
        self.assertEqual(rows["/a.jpg"]["data_sha256"], "ddd")

    def test_removes_stale(self):
        self.conn.execute(
            "INSERT INTO hash_cache VALUES (?, ?, ?, ?, ?)",
            ("/old.jpg", "old_hash", 50, 500.0, None)
        )
        self.conn.commit()

        sync_cache(self.conn, {}, {}, ["/old.jpg"])
        rows = load_cache_all(self.conn)
        self.assertEqual(len(rows), 0)

    def test_keeps_valid_and_new(self):
        self.conn.execute(
            "INSERT INTO hash_cache VALUES (?, ?, ?, ?, ?)",
            ("/keep.jpg", "keep_hash", 100, 1000.0, None)
        )
        self.conn.commit()

        valid = {"/keep.jpg": {"sha256": "keep_hash", "size": 100, "mtime": 1000.0, "data_sha256": None}}
        new = {"/new.jpg": {"sha256": "new_hash", "size": 200, "mtime": 2000.0, "data_sha256": "nd"}}
        sync_cache(self.conn, valid, new, [])

        rows = load_cache_all(self.conn)
        self.assertEqual(len(rows), 2)
        self.assertIn("/keep.jpg", rows)
        self.assertIn("/new.jpg", rows)


class TestEndToEnd(unittest.TestCase):
    """Integration tests running verify_backup.py as a subprocess."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.src = os.path.join(self.tmpdir, "src")
        self.dest = os.path.join(self.tmpdir, "dest")
        os.makedirs(self.src)
        os.makedirs(self.dest)
        self.report = os.path.join(self.tmpdir, "report.txt")
        self.script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "verify_backup.py")

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    def _run(self, mode="smart", extra_args=None):
        cmd = [
            sys.executable, self.script,
            self.src, self.dest,
            "-w", "1",
            "-o", self.report,
            "--mode", mode,
            "--no-cache",
        ]
        if extra_args:
            cmd.extend(extra_args)
        result = subprocess.run(cmd, capture_output=True, text=True)
        report_text = ""
        if os.path.exists(self.report):
            with open(self.report, "r") as f:
                report_text = f.read()
        return result.returncode, result.stdout + result.stderr, report_text

    def test_identical_files(self):
        data = b'identical content'
        _write_file(self.src, "file.txt", data)
        _write_file(self.dest, "file.txt", data)
        rc, output, report = self._run(mode="full")
        self.assertEqual(rc, 0)
        self.assertIn("Matched (OK)       : 1", report)

    def test_identical_jpeg(self):
        jpeg = _make_jpeg()
        _write_file(self.src, "photo.jpg", jpeg)
        _write_file(self.dest, "renamed.jpg", jpeg)
        rc, output, report = self._run(mode="smart")
        self.assertEqual(rc, 0)
        self.assertIn("Matched (OK)       : 1", report)

    def test_missing_file(self):
        _write_file(self.src, "only_src.txt", b'data')
        rc, output, report = self._run(mode="full")
        self.assertEqual(rc, 1)
        self.assertIn("Missing in dest    : 1", report)

    def test_full_mismatch(self):
        _write_file(self.src, "file.txt", b'aaa')
        _write_file(self.dest, "file.txt", b'bbb')
        rc, output, report = self._run(mode="full")
        self.assertEqual(rc, 1)
        self.assertIn("Checksum mismatch  : 1", report)

    def test_smart_metadata_diff(self):
        img = b'\xCA\xFE' * 100
        jpeg_src = _make_jpeg(exif_payload=b'\x01' * 20, image_data=img)
        jpeg_dest = _make_jpeg(exif_payload=b'\x02' * 20, image_data=img)
        self.assertEqual(len(jpeg_src), len(jpeg_dest))
        _write_file(self.src, "photo.jpg", jpeg_src)
        _write_file(self.dest, "photo.jpg", jpeg_dest)

        rc, output, report = self._run(mode="smart")
        self.assertEqual(rc, 0)
        self.assertIn("Metadata diff      : 1", report)
        self.assertIn("METADATA DIFFERENCES", report)

    def test_smart_metadata_diff_strict(self):
        img = b'\xCA\xFE' * 100
        jpeg_src = _make_jpeg(exif_payload=b'\x01' * 20, image_data=img)
        jpeg_dest = _make_jpeg(exif_payload=b'\x02' * 20, image_data=img)
        _write_file(self.src, "photo.jpg", jpeg_src)
        _write_file(self.dest, "photo.jpg", jpeg_dest)

        rc, output, report = self._run(mode="smart", extra_args=["--strict"])
        self.assertEqual(rc, 1)

    def test_smart_real_corruption(self):
        jpeg_src = _make_jpeg(exif_payload=b'\x00' * 20, image_data=b'\xAA' * 100)
        jpeg_dest = _make_jpeg(exif_payload=b'\x00' * 20, image_data=b'\xBB' * 100)
        _write_file(self.src, "photo.jpg", jpeg_src)
        _write_file(self.dest, "photo.jpg", jpeg_dest)

        rc, output, report = self._run(mode="smart")
        self.assertEqual(rc, 1)
        self.assertIn("Checksum mismatch  : 1", report)

    def test_data_only_mode(self):
        img = b'\xCA\xFE' * 100
        jpeg_src = _make_jpeg(exif_payload=b'\x01' * 20, image_data=img)
        jpeg_dest = _make_jpeg(exif_payload=b'\x02' * 20, image_data=img)
        _write_file(self.src, "photo.jpg", jpeg_src)
        _write_file(self.dest, "photo.jpg", jpeg_dest)

        rc, output, report = self._run(mode="data-only")
        self.assertEqual(rc, 0)
        self.assertIn("Matched (OK)       : 1", report)

    def test_full_mode_flags_metadata_as_mismatch(self):
        img = b'\xCA\xFE' * 100
        jpeg_src = _make_jpeg(exif_payload=b'\x01' * 20, image_data=img)
        jpeg_dest = _make_jpeg(exif_payload=b'\x02' * 20, image_data=img)
        _write_file(self.src, "photo.jpg", jpeg_src)
        _write_file(self.dest, "photo.jpg", jpeg_dest)

        rc, output, report = self._run(mode="full")
        self.assertEqual(rc, 1)
        self.assertIn("Checksum mismatch  : 1", report)

    def test_renamed_file_matched(self):
        data = b'unique content here'
        _write_file(self.src, "DSCF0001.JPG", data)
        _write_file(self.dest, "2026-01-01_DSCF0001.JPG", data)
        rc, output, report = self._run(mode="full")
        self.assertEqual(rc, 0)
        self.assertIn("Matched (OK)       : 1", report)

    def test_multiple_files_mixed_results(self):
        # File 1: identical (4 bytes)
        _write_file(self.src, "ok.txt", b'good')
        _write_file(self.dest, "ok.txt", b'good')
        # File 2: missing — use a unique size so it can't match anything in dest
        _write_file(self.src, "gone.txt", b'this file is lost and has no match')
        # File 3: JPEG metadata diff
        img = b'\xCC' * 50
        _write_file(self.src, "meta.jpg", _make_jpeg(b'\x01' * 20, img))
        _write_file(self.dest, "meta.jpg", _make_jpeg(b'\x02' * 20, img))

        rc, output, report = self._run(mode="smart")
        self.assertEqual(rc, 1)  # missing file causes failure
        self.assertIn("Matched (OK)       : 1", report)
        self.assertIn("Metadata diff      : 1", report)
        self.assertIn("Missing in dest    : 1", report)

    def test_cache_persists(self):
        jpeg = _make_jpeg()
        _write_file(self.src, "photo.jpg", jpeg)
        _write_file(self.dest, "photo.jpg", jpeg)

        cmd = [
            sys.executable, self.script,
            self.src, self.dest,
            "-w", "1", "-o", self.report, "--mode", "smart",
        ]
        subprocess.run(cmd, capture_output=True)

        cache_file = Path(self.dest) / CACHE_FILENAME
        self.assertTrue(cache_file.exists())

        conn = sqlite3.connect(str(cache_file))
        rows = conn.execute("SELECT COUNT(*) FROM hash_cache").fetchone()
        self.assertGreaterEqual(rows[0], 1)
        conn.close()

    def test_clear_cache(self):
        jpeg = _make_jpeg()
        _write_file(self.src, "photo.jpg", jpeg)
        _write_file(self.dest, "photo.jpg", jpeg)

        cmd = [
            sys.executable, self.script,
            self.src, self.dest,
            "-w", "1", "-o", self.report, "--mode", "smart",
        ]
        subprocess.run(cmd, capture_output=True)

        cache_file = Path(self.dest) / CACHE_FILENAME
        self.assertTrue(cache_file.exists())

        cmd2 = [
            sys.executable, self.script,
            self.src, self.dest,
            "-w", "1", "-o", self.report, "--mode", "smart", "--clear-cache",
        ]
        result = subprocess.run(cmd2, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
