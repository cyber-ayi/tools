"""Tests for the profile loader, validator, and lookup chain.

Coverage:
  - Bundled profiles load + validate
  - User-level profiles shadow bundled by name
  - Inline `[profiles.X]` shadows both
  - Invalid TOML / unknown algorithm / missing required field error cleanly
  - Config.resolve_priority picks correct list along all override chains
  - hashing.negotiate honors profile-supplied priority
"""
from __future__ import annotations

from pathlib import Path

import pytest

from rclone_migrate import config as config_mod
from rclone_migrate import hashing
from rclone_migrate import profiles as profiles_mod


# --- bundled inventory --------------------------------------------------------

def test_bundled_profiles_present():
    """All four bundled profiles must exist."""
    bundled = set(profiles_mod.list_bundled())
    assert {"balanced", "dit", "cloud-native", "forensic"} <= bundled


def test_bundled_balanced_loads():
    p = profiles_mod.load("balanced")
    assert p.name == "balanced"
    assert p.source == "bundled"
    assert "sha256" in p.priority
    assert p.priority[0] == "sha256"


def test_bundled_dit_is_mhl_compatible():
    p = profiles_mod.load("dit")
    mhl_set = {"c4", "md5", "sha1", "xxh64", "xxh3", "xxh128"}
    assert all(a in mhl_set for a in p.priority)
    assert all(a in mhl_set for a in p.multi_hash)
    # DIT prefers xxh family first
    assert p.priority[0].startswith("xxh")


def test_bundled_forensic_has_multi_hash():
    p = profiles_mod.load("forensic")
    assert p.multi_hash == ["md5"]
    assert p.priority[0] == "sha256"


# --- validation ---------------------------------------------------------------

def test_load_missing_priority(tmp_path: Path):
    pdir = tmp_path / "profiles"
    pdir.mkdir()
    (pdir / "broken.toml").write_text("description = 'no priority'\n")
    with pytest.raises(profiles_mod.ProfileError, match="missing required field 'priority'"):
        profiles_mod.load("broken", state_dir=tmp_path)


def test_load_unknown_algorithm(tmp_path: Path):
    pdir = tmp_path / "profiles"
    pdir.mkdir()
    (pdir / "weird.toml").write_text("priority = ['sha3', 'md5']\n")
    with pytest.raises(profiles_mod.ProfileError, match="unknown algorithm 'sha3'"):
        profiles_mod.load("weird", state_dir=tmp_path)


def test_load_priority_must_be_list(tmp_path: Path):
    pdir = tmp_path / "profiles"
    pdir.mkdir()
    (pdir / "bad.toml").write_text("priority = 'sha256'\n")
    with pytest.raises(profiles_mod.ProfileError, match="non-empty list"):
        profiles_mod.load("bad", state_dir=tmp_path)


def test_load_empty_priority(tmp_path: Path):
    pdir = tmp_path / "profiles"
    pdir.mkdir()
    (pdir / "empty.toml").write_text("priority = []\n")
    with pytest.raises(profiles_mod.ProfileError, match="non-empty list"):
        profiles_mod.load("empty", state_dir=tmp_path)


def test_load_bad_toml(tmp_path: Path):
    pdir = tmp_path / "profiles"
    pdir.mkdir()
    (pdir / "x.toml").write_text("priority = [")  # syntax error
    with pytest.raises(profiles_mod.ProfileError, match="TOML parse error"):
        profiles_mod.load("x", state_dir=tmp_path)


def test_load_bad_warnings_type(tmp_path: Path):
    pdir = tmp_path / "profiles"
    pdir.mkdir()
    (pdir / "w.toml").write_text("priority = ['md5']\nwarnings = 'oops'\n")
    with pytest.raises(profiles_mod.ProfileError, match="'warnings' must be a list"):
        profiles_mod.load("w", state_dir=tmp_path)


def test_load_normalizes_case(tmp_path: Path):
    pdir = tmp_path / "profiles"
    pdir.mkdir()
    (pdir / "u.toml").write_text("priority = ['SHA256', 'Md5']\n")
    p = profiles_mod.load("u", state_dir=tmp_path)
    assert p.priority == ["sha256", "md5"]


# --- lookup chain -------------------------------------------------------------

def test_user_shadows_bundled(tmp_path: Path):
    pdir = tmp_path / "profiles"
    pdir.mkdir()
    # User defines their own 'dit' that's all md5 — should shadow bundled
    (pdir / "dit.toml").write_text(
        "description = 'custom user dit'\n"
        "priority = ['md5']\n"
    )
    p = profiles_mod.load("dit", state_dir=tmp_path)
    assert p.priority == ["md5"]
    assert "user" in p.source
    assert p.description == "custom user dit"


def test_inline_shadows_user_and_bundled(tmp_path: Path):
    pdir = tmp_path / "profiles"
    pdir.mkdir()
    (pdir / "dit.toml").write_text("priority = ['sha1']\n")
    inline = {"dit": {"priority": ["sha512"]}}
    p = profiles_mod.load("dit", state_dir=tmp_path, inline=inline)
    assert p.priority == ["sha512"]
    assert p.source == "inline"


def test_load_unknown_name(tmp_path: Path):
    with pytest.raises(profiles_mod.ProfileError, match="not found"):
        profiles_mod.load("does-not-exist", state_dir=tmp_path)


def test_list_all_dedup_user_over_bundled(tmp_path: Path):
    pdir = tmp_path / "profiles"
    pdir.mkdir()
    (pdir / "dit.toml").write_text("priority = ['md5']\n")
    profs = profiles_mod.list_all(state_dir=tmp_path)
    by_name = {p.name: p for p in profs}
    assert by_name["dit"].priority == ["md5"]  # user wins
    # other bundled still listed
    assert "balanced" in by_name
    assert by_name["balanced"].source == "bundled"


def test_list_all_includes_inline(tmp_path: Path):
    inline = {"my-fast": {"priority": ["xxh3", "blake3"]}}
    profs = profiles_mod.list_all(state_dir=tmp_path, inline=inline)
    names = [p.name for p in profs]
    assert "my-fast" in names
    mf = next(p for p in profs if p.name == "my-fast")
    assert mf.source == "inline"


def test_list_all_silently_drops_broken(tmp_path: Path):
    pdir = tmp_path / "profiles"
    pdir.mkdir()
    (pdir / "broken.toml").write_text("priority = ['nonsense-algo']\n")
    profs = profiles_mod.list_all(state_dir=tmp_path)
    assert all(p.name != "broken" for p in profs)
    # bundled still loadable
    assert any(p.name == "balanced" for p in profs)


def test_is_overridden(tmp_path: Path):
    pdir = tmp_path / "profiles"
    pdir.mkdir()
    (pdir / "dit.toml").write_text("priority = ['md5']\n")
    user_dit = profiles_mod.load("dit", state_dir=tmp_path)
    assert profiles_mod.is_overridden("dit", user_dit) is True
    bundled_balanced = profiles_mod.load("balanced")
    assert profiles_mod.is_overridden("balanced", bundled_balanced) is False


# --- config integration -------------------------------------------------------

def _write_cfg(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "c.toml"
    p.write_text(body)
    return p


def test_config_default_profile_is_balanced(tmp_path: Path):
    p = _write_cfg(
        tmp_path,
        "[[jobs]]\nname='j'\nsrc='/s'\ndst='/d'\n",
    )
    cfg = config_mod.load(p)
    cfg.defaults.state_dir = str(tmp_path)
    pri = cfg.resolve_priority(cfg.jobs[0])
    assert pri[0] == "sha256"  # balanced default


def test_config_hash_profile_global(tmp_path: Path):
    p = _write_cfg(
        tmp_path,
        "[defaults]\nhash_profile = 'dit'\nstate_dir = '" + str(tmp_path) + "'\n"
        "[[jobs]]\nname='j'\nsrc='/s'\ndst='/d'\n",
    )
    cfg = config_mod.load(p)
    pri = cfg.resolve_priority(cfg.jobs[0])
    assert pri[0].startswith("xxh")


def test_config_hash_profile_per_job_override(tmp_path: Path):
    p = _write_cfg(
        tmp_path,
        "[defaults]\nhash_profile = 'balanced'\nstate_dir = '" + str(tmp_path) + "'\n"
        "[[jobs]]\nname='j'\nsrc='/s'\ndst='/d'\nhash_profile = 'cloud-native'\n",
    )
    cfg = config_mod.load(p)
    pri = cfg.resolve_priority(cfg.jobs[0])
    assert pri[0] == "md5"  # cloud-native first


def test_config_hash_priority_inline_list_wins_over_profile(tmp_path: Path):
    p = _write_cfg(
        tmp_path,
        "[defaults]\nhash_profile = 'dit'\nhash_priority = ['md5','sha1']\n"
        "state_dir = '" + str(tmp_path) + "'\n"
        "[[jobs]]\nname='j'\nsrc='/s'\ndst='/d'\n",
    )
    cfg = config_mod.load(p)
    assert cfg.resolve_priority(cfg.jobs[0]) == ["md5", "sha1"]


def test_config_inline_profile_table(tmp_path: Path):
    p = _write_cfg(
        tmp_path,
        "[defaults]\nhash_profile = 'speed-custom'\n"
        "state_dir = '" + str(tmp_path) + "'\n"
        "[profiles.speed-custom]\n"
        "priority = ['xxh3', 'blake3']\n"
        "[[jobs]]\nname='j'\nsrc='/s'\ndst='/d'\n",
    )
    cfg = config_mod.load(p)
    pri = cfg.resolve_priority(cfg.jobs[0])
    assert pri == ["xxh3", "blake3"]


def test_config_hash_priority_invalid_type(tmp_path: Path):
    p = _write_cfg(
        tmp_path,
        "[defaults]\nhash_priority = 'sha256'\n"  # should be a list
        "[[jobs]]\nname='j'\nsrc='/s'\ndst='/d'\n",
    )
    with pytest.raises(ValueError, match="hash_priority"):
        config_mod.load(p)


def test_config_inline_profiles_must_be_table(tmp_path: Path):
    p = _write_cfg(
        tmp_path,
        "profiles = 'oops'\n"
        "[[jobs]]\nname='j'\nsrc='/s'\ndst='/d'\n",
    )
    with pytest.raises(ValueError, match=r"\[profiles\]"):
        config_mod.load(p)


# --- negotiate integration ----------------------------------------------------

def test_negotiate_with_priority_picks_first_in_common(monkeypatch):
    def fake(path):
        return {
            "/a": ["md5", "sha1", "sha256"],
            "/b": ["md5", "sha1", "sha256"],
        }[path]
    monkeypatch.setattr(hashing, "supported_hashes", fake)
    # Profile-supplied priority puts md5 first
    assert hashing.negotiate("/a", "/b", priority=["md5", "sha256"]) == "md5"
    # Different priority → different pick
    assert hashing.negotiate("/a", "/b", priority=["sha1"]) == "sha1"


def test_negotiate_priority_falls_back_to_default(monkeypatch):
    """If profile priority has no match in common, fall back to PREFERRED_ORDER."""
    def fake(path):
        return ["md5", "sha1"]   # neither side has anything exotic
    monkeypatch.setattr(hashing, "supported_hashes", fake)
    # Profile asks for blake3/sha256 — neither is in common; fall back to
    # PREFERRED_ORDER → md5 (since sha256 isn't common, sha1 is, then md5).
    # PREFERRED_ORDER first match: sha1.
    assert hashing.negotiate("/a", "/b",
                             priority=["blake3", "sha256"]) == "sha1"


def test_negotiate_priority_skips_unsupported(monkeypatch):
    def fake(path):
        return {
            "/a": ["md5", "sha1", "sha256"],
            "b:": ["sha1"],   # B2-like
        }[path]
    monkeypatch.setattr(hashing, "supported_hashes", fake)
    # Profile prefers sha256 first, but b: only has sha1 → falls to sha1
    assert hashing.negotiate("/a", "b:", priority=["sha256", "sha1"]) == "sha1"


# --- ops.negotiate_algo wiring -----------------------------------------------

def test_negotiate_algo_uses_profile(monkeypatch, tmp_path: Path):
    from rclone_migrate import ops

    def fake(path):
        return ["md5", "sha1", "sha256"]
    monkeypatch.setattr(hashing, "supported_hashes", fake)

    p = _write_cfg(
        tmp_path,
        "[defaults]\nhash_profile = 'cloud-native'\n"
        "state_dir = '" + str(tmp_path) + "'\n"
        "[[jobs]]\nname='j'\nsrc='/s'\ndst='/d'\n",
    )
    cfg = config_mod.load(p)
    assert ops.negotiate_algo(cfg.jobs[0], cfg) == "md5"


def test_negotiate_algo_legacy_hash_field_short_circuits(monkeypatch, tmp_path: Path):
    """Setting `hash = "sha256"` must override profile resolution."""
    from rclone_migrate import ops

    def fake(path):
        return ["md5", "sha1", "sha256"]
    monkeypatch.setattr(hashing, "supported_hashes", fake)

    p = _write_cfg(
        tmp_path,
        "[defaults]\nhash = 'sha256'\nhash_profile = 'cloud-native'\n"
        "state_dir = '" + str(tmp_path) + "'\n"
        "[[jobs]]\nname='j'\nsrc='/s'\ndst='/d'\n",
    )
    cfg = config_mod.load(p)
    assert ops.negotiate_algo(cfg.jobs[0], cfg) == "sha256"
