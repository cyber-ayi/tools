"""Hash profile loader.

A 'profile' is a named hash algorithm priority list, optionally with
extra algorithms to compute alongside the primary, plus human-readable
description and warnings.

Lookup chain (first match wins):
  1. inline `[profiles.<name>]` table in the current config
  2. user-level <state_dir>/profiles/<name>.toml
  3. bundled <package>/profiles/<name>.toml
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib  # type: ignore


# Algorithms rmig recognizes. Profiles must use only these names.
# Union of rclone-supported hashes (lowercase) and MHL v2.0 algorithms.
KNOWN_ALGORITHMS = frozenset({
    "md5", "sha1", "sha256", "sha512",
    "blake3",
    "crc32",
    "xxh3", "xxh64", "xxh128",
    "c4",
    "dropbox", "quickxor", "whirlpool", "mailru",
})

DEFAULT_PROFILE = "balanced"


class ProfileError(RuntimeError):
    """Raised on profile load / validation failures, with file context."""


@dataclass
class Profile:
    name: str
    priority: List[str]
    description: str = ""
    multi_hash: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    source: str = "<unknown>"


def _bundled_dir() -> Path:
    return Path(__file__).parent / "profiles"


def _validate(name: str, raw: dict, source: str) -> Profile:
    if not isinstance(raw, dict):
        raise ProfileError(
            f"profile '{name}' from {source}: top-level must be a TOML table"
        )
    if "priority" not in raw:
        raise ProfileError(
            f"profile '{name}' from {source}: missing required field 'priority'"
        )

    pri_raw = raw["priority"]
    if not isinstance(pri_raw, list) or not pri_raw:
        raise ProfileError(
            f"profile '{name}' from {source}: 'priority' must be a non-empty list"
        )
    priority: List[str] = []
    for a in pri_raw:
        if not isinstance(a, str):
            raise ProfileError(
                f"profile '{name}' from {source}: priority entries must be "
                f"strings, got {a!r}"
            )
        an = a.strip().lower()
        if an not in KNOWN_ALGORITHMS:
            raise ProfileError(
                f"profile '{name}' from {source}: unknown algorithm '{a}' "
                f"in priority. Known: {', '.join(sorted(KNOWN_ALGORITHMS))}"
            )
        priority.append(an)

    multi_raw = raw.get("multi_hash", [])
    if not isinstance(multi_raw, list):
        raise ProfileError(
            f"profile '{name}' from {source}: 'multi_hash' must be a list"
        )
    multi_hash: List[str] = []
    for a in multi_raw:
        if not isinstance(a, str):
            raise ProfileError(
                f"profile '{name}' from {source}: multi_hash entries must "
                f"be strings, got {a!r}"
            )
        an = a.strip().lower()
        if an not in KNOWN_ALGORITHMS:
            raise ProfileError(
                f"profile '{name}' from {source}: unknown algorithm '{a}' "
                f"in multi_hash"
            )
        multi_hash.append(an)

    desc = raw.get("description", "")
    if not isinstance(desc, str):
        raise ProfileError(
            f"profile '{name}' from {source}: 'description' must be a string"
        )

    warnings_raw = raw.get("warnings", [])
    if not isinstance(warnings_raw, list) or not all(
        isinstance(w, str) for w in warnings_raw
    ):
        raise ProfileError(
            f"profile '{name}' from {source}: 'warnings' must be a list "
            f"of strings"
        )

    return Profile(
        name=name,
        priority=priority,
        description=desc,
        multi_hash=multi_hash,
        warnings=list(warnings_raw),
        source=source,
    )


def _load_toml(path: Path) -> dict:
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise ProfileError(f"TOML parse error in {path}: {e}")


def list_bundled() -> List[str]:
    d = _bundled_dir()
    if not d.is_dir():
        return []
    return sorted(p.stem for p in d.glob("*.toml"))


def list_user(state_dir: Path) -> List[str]:
    d = Path(state_dir) / "profiles"
    if not d.is_dir():
        return []
    return sorted(p.stem for p in d.glob("*.toml"))


def load(
    name: str,
    *,
    state_dir: Optional[Path] = None,
    inline: Optional[Dict[str, dict]] = None,
) -> Profile:
    """Resolve a profile by name through the inline → user → bundled chain.

    `inline`: dict of name → raw TOML table for `[profiles.<n>]` entries
              in the current config. Wins over user/bundled when name matches.
    `state_dir`: state-directory root; profiles searched at
                 <state_dir>/profiles/. If None, user-level lookup is skipped.
    """
    if inline and name in inline:
        return _validate(name, inline[name], source="inline")

    if state_dir is not None:
        user_path = Path(state_dir) / "profiles" / f"{name}.toml"
        if user_path.is_file():
            return _validate(
                name, _load_toml(user_path), source=f"user ({user_path})",
            )

    bundled_path = _bundled_dir() / f"{name}.toml"
    if bundled_path.is_file():
        return _validate(name, _load_toml(bundled_path), source="bundled")

    checked = []
    if inline:
        checked.append("inline")
    if state_dir is not None:
        checked.append(str(Path(state_dir) / "profiles"))
    checked.append(str(_bundled_dir()))
    raise ProfileError(
        f"profile '{name}' not found. Checked: {', '.join(checked)}"
    )


def list_all(
    *,
    state_dir: Optional[Path] = None,
    inline: Optional[Dict[str, dict]] = None,
) -> List[Profile]:
    """All known profiles after the lookup chain.

    User profiles shadow bundled profiles of the same name; inline
    profiles shadow both. One bad profile doesn't break the listing —
    its name is silently dropped.
    """
    seen: Dict[str, Profile] = {}
    for name in list_bundled():
        try:
            seen[name] = load(name)
        except ProfileError:
            continue
    if state_dir is not None:
        for name in list_user(state_dir):
            try:
                seen[name] = load(name, state_dir=state_dir)
            except ProfileError:
                continue
    if inline:
        for name in inline:
            try:
                seen[name] = load(name, state_dir=state_dir, inline=inline)
            except ProfileError:
                continue
    return sorted(seen.values(), key=lambda p: p.name)


def is_overridden(name: str, profile: Profile) -> bool:
    """True if a non-bundled source supplies this profile despite a bundled
    version also existing — useful for `rmig profiles list` annotation.
    """
    if profile.source == "bundled":
        return False
    return (_bundled_dir() / f"{name}.toml").is_file()
