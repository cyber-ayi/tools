"""TOML config loader."""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib  # type: ignore

from . import profiles as profiles_mod


def _expand(p: str) -> str:
    return os.path.expanduser(os.path.expandvars(p))


def _parse_duration(s: str) -> float:
    """Parse '24h', '30m', '15s', '500ms' to seconds (float)."""
    s = s.strip().lower()
    if s.endswith("ms"):
        return float(s[:-2]) / 1000
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    if s and s[-1] in units:
        return float(s[:-1]) * units[s[-1]]
    return float(s)


def _as_str_list(value, key: str, ctx: str) -> Optional[List[str]]:
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
        raise ValueError(f"{ctx}: '{key}' must be a list of strings")
    return list(value)


@dataclass
class Defaults:
    hash: Optional[str] = None
    hash_profile: Optional[str] = None
    hash_priority: Optional[List[str]] = None
    state_dir: str = "~/.local/share/rclone-migrate"
    transfers: int = 8
    checkers: int = 16
    download: bool = False
    local_cache_in_root: bool = True


@dataclass
class DeleteOpts:
    require_check_within_s: float = 86400.0
    remove_empty_src_dirs: bool = True
    require_confirm: bool = True


@dataclass
class Job:
    name: str
    src: str
    dst: str
    enabled: bool = True
    hash: Optional[str] = None
    hash_profile: Optional[str] = None
    hash_priority: Optional[List[str]] = None
    transfers: Optional[int] = None
    checkers: Optional[int] = None
    download: Optional[bool] = None
    local_cache_in_root: Optional[bool] = None

    def resolved_hash(self, defaults: Defaults) -> Optional[str]:
        return self.hash or defaults.hash

    def resolved_hash_profile(self, defaults: Defaults) -> Optional[str]:
        return self.hash_profile or defaults.hash_profile

    def resolved_hash_priority(self, defaults: Defaults) -> Optional[List[str]]:
        return self.hash_priority or defaults.hash_priority

    def resolved_transfers(self, defaults: Defaults) -> int:
        return self.transfers if self.transfers is not None else defaults.transfers

    def resolved_checkers(self, defaults: Defaults) -> int:
        return self.checkers if self.checkers is not None else defaults.checkers

    def resolved_download(self, defaults: Defaults) -> bool:
        return self.download if self.download is not None else defaults.download

    def resolved_local_cache_in_root(self, defaults: Defaults) -> bool:
        return (
            self.local_cache_in_root
            if self.local_cache_in_root is not None
            else defaults.local_cache_in_root
        )


@dataclass
class Config:
    defaults: Defaults = field(default_factory=Defaults)
    delete: DeleteOpts = field(default_factory=DeleteOpts)
    jobs: List[Job] = field(default_factory=list)
    inline_profiles: Dict[str, dict] = field(default_factory=dict)

    def get_job(self, name: str) -> Job:
        for j in self.jobs:
            if j.name == name:
                return j
        raise KeyError(f"job not found: {name}")

    def state_dir_for(self, job: Job) -> Path:
        return Path(_expand(self.defaults.state_dir)) / job.name

    def state_dir_root(self) -> Path:
        return Path(_expand(self.defaults.state_dir))

    def resolve_priority(self, job: Job) -> List[str]:
        """Compute the algorithm priority list for `job`.

        Resolution order (first hit wins):
          1. job.hash_priority / defaults.hash_priority (inline list)
          2. job.hash_profile / defaults.hash_profile (named profile)
          3. bundled DEFAULT_PROFILE
        Caller is responsible for honoring `job.hash` / `defaults.hash`
        (single-algo override) ahead of this — see `negotiate_algo`.
        """
        explicit = job.resolved_hash_priority(self.defaults)
        if explicit:
            return [a.strip().lower() for a in explicit]
        name = (
            job.resolved_hash_profile(self.defaults)
            or profiles_mod.DEFAULT_PROFILE
        )
        prof = profiles_mod.load(
            name,
            state_dir=self.state_dir_root(),
            inline=self.inline_profiles or None,
        )
        return list(prof.priority)

    def resolve_profile(self, job: Job) -> Optional[profiles_mod.Profile]:
        """Return the named profile object for `job` (None if hash_priority
        wins, or if `hash` single-algo override is in effect — caller
        decides). Used for surfacing warnings / multi_hash / source.
        """
        if job.resolved_hash_priority(self.defaults):
            return None
        name = (
            job.resolved_hash_profile(self.defaults)
            or profiles_mod.DEFAULT_PROFILE
        )
        return profiles_mod.load(
            name,
            state_dir=self.state_dir_root(),
            inline=self.inline_profiles or None,
        )


def load(path: str | Path) -> Config:
    with open(path, "rb") as f:
        raw = tomllib.load(f)

    d = raw.get("defaults", {})
    defaults = Defaults(
        hash=d.get("hash"),
        hash_profile=d.get("hash_profile"),
        hash_priority=_as_str_list(
            d.get("hash_priority"), "hash_priority", "[defaults]",
        ),
        state_dir=d.get("state_dir", Defaults.state_dir),
        transfers=d.get("transfers", Defaults.transfers),
        checkers=d.get("checkers", Defaults.checkers),
        download=d.get("download", Defaults.download),
        local_cache_in_root=d.get(
            "local_cache_in_root", Defaults.local_cache_in_root,
        ),
    )

    de = raw.get("delete", {})
    delete = DeleteOpts(
        require_check_within_s=_parse_duration(
            de.get("require_check_within", "24h"),
        ),
        remove_empty_src_dirs=de.get("remove_empty_src_dirs", True),
        require_confirm=de.get("require_confirm", True),
    )

    jobs = []
    for jr in raw.get("jobs", []):
        if "name" not in jr or "src" not in jr or "dst" not in jr:
            raise ValueError(f"job missing required fields: {jr}")
        ctx = f"[[jobs]] {jr.get('name', '?')}"
        jobs.append(
            Job(
                name=jr["name"],
                src=jr["src"],
                dst=jr["dst"],
                enabled=jr.get("enabled", True),
                hash=jr.get("hash"),
                hash_profile=jr.get("hash_profile"),
                hash_priority=_as_str_list(
                    jr.get("hash_priority"), "hash_priority", ctx,
                ),
                transfers=jr.get("transfers"),
                checkers=jr.get("checkers"),
                download=jr.get("download"),
                local_cache_in_root=jr.get("local_cache_in_root"),
            )
        )

    inline_profiles = {}
    raw_profiles = raw.get("profiles", {})
    if not isinstance(raw_profiles, dict):
        raise ValueError("[profiles] must be a TOML table of named tables")
    for pname, ptable in raw_profiles.items():
        if not isinstance(ptable, dict):
            raise ValueError(
                f"[profiles.{pname}] must be a TOML table"
            )
        inline_profiles[pname] = ptable

    return Config(
        defaults=defaults, delete=delete, jobs=jobs,
        inline_profiles=inline_profiles,
    )
