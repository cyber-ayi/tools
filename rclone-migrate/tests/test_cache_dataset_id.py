"""Stage E: dataset-id keyed cache (mount-path independent) + auto-migrate."""
import io

import pytest

from rclone_migrate import cache, manifest, verbose


def _v():
    return verbose.Verbose(level=verbose.DETAIL, color=False, timestamps=False,
                           stream=io.StringIO(), err_stream=io.StringIO())


def test_is_sidecar():
    assert cache.is_sidecar(".rmig-dataset")
    assert cache.is_sidecar(".rmig-cache.db")
    assert cache.is_sidecar(".rmig-cache.db-wal")
    assert not cache.is_sidecar("VID_0001.insv")
    # macOS AppleDouble companions of our dotfiles (exFAT/SMB SD cards)
    assert cache.is_sidecar("._.rmig-dataset")
    assert cache.is_sidecar("._.rmig-cache.db")
    assert cache.is_sidecar("._.rmig-cache.db-wal")
    # but NOT AppleDouble of real media — that's user-data territory
    assert not cache.is_sidecar("._VID_0001.insv")


def test_appledouble_marker_excluded_from_manifest(tmp_path):
    root = tmp_path / "src"; root.mkdir()
    (root / "real.bin").write_bytes(b"x" * 16)
    (root / ".rmig-dataset").write_text("deadbeefcafebabe\n")
    (root / "._.rmig-dataset").write_bytes(b"\x00\x05\x16\x07")  # AppleDouble
    m = manifest._refresh_local(
        "src", str(root), "sha256",
        transfers=1, full=False, local_cache_in_root=False,
        fallback_dir=tmp_path / "fb", progress=False, v=_v(),
    )
    paths = {e.path for e in m.entries}
    assert "real.bin" in paths
    assert ".rmig-dataset" not in paths
    assert "._.rmig-dataset" not in paths       # the reported bug


def test_marker_created_and_db_keyed_by_id(tmp_path):
    root = tmp_path / "data"; root.mkdir()
    fb = tmp_path / "fb"
    db, dsid = cache.resolve_fallback_db(root, fb, _v())
    assert dsid and len(dsid) >= 16
    assert (root / ".rmig-dataset").read_text().strip() == dsid
    assert db.name == f"cache-{dsid}.db"
    # second call reads the existing marker → same id, stable
    db2, dsid2 = cache.resolve_fallback_db(root, fb, _v())
    assert dsid2 == dsid and db2 == db


def test_same_data_different_mount_path_reuses_cache(tmp_path):
    """The whole point: same physical dir reached via a different path
    (symlink ≈ a different mount) must resolve to the SAME db."""
    real = tmp_path / "nas_real"; real.mkdir()
    fb = tmp_path / "fb"
    db_a, id_a = cache.resolve_fallback_db(real, fb, _v())

    alias = tmp_path / "mnt_alias"
    alias.symlink_to(real)                      # alias path → same dir
    db_b, id_b = cache.resolve_fallback_db(alias, fb, _v())

    assert id_b == id_a                          # marker travels with data
    assert db_b == db_a                          # → same cache db, no orphan


def test_legacy_db_auto_migrated_and_kept(tmp_path):
    root = tmp_path / "d"; root.mkdir()
    fb = tmp_path / "fb"; fb.mkdir()
    legacy = cache._legacy_fallback_db(root, fb)
    conn = cache.open_db(legacy)
    cache.upsert_many(conn, [cache.CacheEntry("a.bin", "deadbeef",
                      "xxh3", 10, 1.0)], refreshed=1.0)
    conn.close()

    db, dsid = cache.resolve_fallback_db(root, fb, _v())
    assert db != legacy
    assert legacy.exists()                       # kept as rollback backup
    # migrated content is present (no re-hash needed)
    c = cache.open_db(db)
    assert "a.bin" in cache.load_for_algorithm(c, "xxh3")
    c.close()


def test_readonly_root_falls_back_to_legacy(tmp_path, monkeypatch):
    root = tmp_path / "ro"; root.mkdir()
    fb = tmp_path / "fb"
    # simulate marker write failing (read-only / perm)
    monkeypatch.setattr(cache.Path, "write_text",
                        lambda *a, **k: (_ for _ in ()).throw(OSError()))
    db, dsid = cache.resolve_fallback_db(root, fb, _v())
    assert dsid is None                          # signalled fallback
    assert db == cache._legacy_fallback_db(root, fb)  # unchanged behaviour


def test_marker_excluded_from_manifest(tmp_path):
    root = tmp_path / "src"; root.mkdir()
    (root / "real.bin").write_bytes(b"x" * 32)
    fb = tmp_path / "fb"
    m = manifest._refresh_local(
        "src", str(root), "sha256",
        transfers=1, full=False, local_cache_in_root=False,
        fallback_dir=fb, progress=False, v=_v(),
    )
    paths = {e.path for e in m.entries}
    assert "real.bin" in paths
    assert ".rmig-dataset" in [p.name for p in [root / ".rmig-dataset"]] \
        and (root / ".rmig-dataset").exists()    # marker was created
    assert ".rmig-dataset" not in paths          # …but NOT in the manifest


def test_readonly_resolve_no_marker_does_not_create(tmp_path):
    """create=False (file-status): must NOT write the marker or migrate."""
    root = tmp_path / "d"; root.mkdir()
    fb = tmp_path / "fb"
    db, dsid = cache.resolve_fallback_db(root, fb, create=False)
    assert dsid is None
    assert db == cache._legacy_fallback_db(root, fb)
    assert not (root / ".rmig-dataset").exists()      # not created


def test_readonly_resolve_marker_present_points_at_data(tmp_path):
    """Marker exists, id-db not yet materialised, legacy holds the data:
    read-only resolve must point at legacy (no copy) — same data the
    mutating path would read."""
    root = tmp_path / "d"; root.mkdir()
    fb = tmp_path / "fb"; fb.mkdir()
    (root / ".rmig-dataset").write_text("feedfacecafebeef\n")
    legacy = cache._legacy_fallback_db(root, fb)
    cache.open_db(legacy).close()                      # legacy exists
    db, dsid = cache.resolve_fallback_db(root, fb, create=False)
    assert dsid == "feedfacecafebeef"
    assert db == legacy                                # not the id-db copy
    assert not (fb / f"cache-{dsid}.db").exists()      # no migration done


def test_readonly_resolve_matches_mutating_after_migration(tmp_path):
    """Once the mutating path has migrated, read-only resolves to the
    same id-db (file-status now sees current hashes)."""
    root = tmp_path / "d"; root.mkdir()
    fb = tmp_path / "fb"; fb.mkdir()
    cache.open_db(cache._legacy_fallback_db(root, fb)).close()
    rw_db, dsid = cache.resolve_fallback_db(root, fb, _v())   # create=True → migrates
    ro_db, ro_id = cache.resolve_fallback_db(root, fb, create=False)
    assert ro_db == rw_db and ro_id == dsid


def test_partial_files_excluded_from_manifest(tmp_path):
    """rclone `.partial` temps (often huge leftovers) must never be hashed
    or land in the manifest — refresh runs before Stage G cleanup."""
    root = tmp_path / "dst"; root.mkdir()
    (root / "VID_001.insv").write_bytes(b"real" * 8)
    (root / "VID_002.insv.b856018f.partial").write_bytes(b"x" * 4096)
    (root / "VID_003.insv.partial").write_bytes(b"y" * 16)
    m = manifest._refresh_local(
        "dst", str(root), "sha256",
        transfers=1, full=False, local_cache_in_root=False,
        fallback_dir=tmp_path / "fb", progress=False, v=_v(),
    )
    paths = {e.path for e in m.entries}
    assert "VID_001.insv" in paths
    assert not any(p.endswith(".partial") for p in paths)


def test_meta_dataset_id_roundtrip(tmp_path):
    db = tmp_path / "x.db"
    c = cache.open_db(db)
    assert cache.meta_get(c, "dataset_id") is None
    cache.meta_set(c, "dataset_id", "abc123")
    assert cache.meta_get(c, "dataset_id") == "abc123"
    c.close()
