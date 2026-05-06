from pathlib import Path

from rclone_migrate import cache


def test_open_and_upsert(tmp_path: Path):
    db = tmp_path / "c.db"
    conn = cache.open_db(db)
    e = cache.CacheEntry(path="a.txt", hash="abc", algorithm="md5",
                         size=10, mtime=100.0)
    cache.upsert_many(conn, [e], refreshed=200.0)
    loaded = cache.load_for_algorithm(conn, "md5")
    assert "a.txt" in loaded
    assert loaded["a.txt"].hash == "abc"
    conn.close()


def test_multi_algorithm_coexist(tmp_path: Path):
    """Same path, different algorithms — both rows kept."""
    db = tmp_path / "c.db"
    conn = cache.open_db(db)
    e1 = cache.CacheEntry("a.txt", "abc", "md5", 10, 100.0)
    e2 = cache.CacheEntry("a.txt", "def", "sha256", 10, 100.0)
    cache.upsert_many(conn, [e1, e2], refreshed=200.0)
    assert "a.txt" in cache.load_for_algorithm(conn, "md5")
    assert "a.txt" in cache.load_for_algorithm(conn, "sha256")
    conn.close()


def test_diff(tmp_path: Path):
    cached = {
        "stable": cache.CacheEntry("stable", "h1", "md5", 10, 100.0),
        "changed": cache.CacheEntry("changed", "h2", "md5", 10, 100.0),
        "gone": cache.CacheEntry("gone", "h3", "md5", 10, 100.0),
    }
    current = {
        "stable": (10, 100.0),
        "changed": (20, 200.0),
        "added": (5, 50.0),
    }
    d = cache.diff_against_filesystem(cached, current)
    assert "stable" in d.valid
    assert d.stale == ["changed"]
    assert d.new == ["added"]
    assert d.removed == ["gone"]


def test_full_invalidates_all(tmp_path: Path):
    cached = {"a": cache.CacheEntry("a", "h", "md5", 10, 100.0)}
    current = {"a": (10, 100.0)}
    d = cache.diff_against_filesystem(cached, current, full=True)
    assert not d.valid
    assert d.stale == ["a"]


def test_cache_path_in_writable_root(tmp_path: Path):
    p = cache.cache_path_for_root(tmp_path)
    assert p == tmp_path / cache.CACHE_FILENAME
