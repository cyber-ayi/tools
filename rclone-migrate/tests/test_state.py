from pathlib import Path

from rclone_migrate import state


def test_meta_set_get_clear(tmp_path: Path):
    conn = state.open_db(tmp_path)
    assert state.meta_get(conn, "x") is None
    state.meta_set(conn, "x", "1")
    assert state.meta_get(conn, "x") == "1"
    state.meta_set(conn, "x", "2")  # update
    assert state.meta_get(conn, "x") == "2"
    state.meta_clear(conn, "x")
    assert state.meta_get(conn, "x") is None
    conn.close()


def test_remote_hash_cache_roundtrip(tmp_path: Path):
    conn = state.open_db(tmp_path)
    e = state.RemoteCacheEntry(
        side="src", path="a.txt", algorithm="sha256",
        hash="deadbeef", size=10, modtime=100.0,
    )
    state.rhc_upsert(conn, [e], refreshed=1.0)
    loaded = state.rhc_load(conn, "src", "sha256")
    assert "a.txt" in loaded
    assert loaded["a.txt"].hash == "deadbeef"
    state.rhc_delete(conn, "src", ["a.txt"], "sha256")
    assert "a.txt" not in state.rhc_load(conn, "src", "sha256")
    conn.close()
