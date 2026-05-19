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


_SIZE_UNITS = {
    "b": 1,
    "kib": 1024, "mib": 1024**2, "gib": 1024**3, "tib": 1024**4,
    "kb": 1000, "mb": 1000**2, "gb": 1000**3, "tb": 1000**4,
    "k": 1024, "m": 1024**2, "g": 1024**3, "t": 1024**4,
}


def _parse_size(s) -> int:
    """Parse '10GiB', '500MiB', '10G', '0', 1234 → bytes. '0'/'' → 0
    (disabled). Binary units (KiB/GiB) and bare K/M/G are 1024-based;
    KB/MB/GB are 1000-based."""
    if s is None:
        return 0
    if isinstance(s, (int, float)):
        return int(s)
    s = str(s).strip().lower()
    if not s or s in ("0", "off", "none"):
        return 0
    for u in ("tib", "gib", "mib", "kib", "tb", "gb", "mb", "kb",
              "t", "g", "m", "k", "b"):
        if s.endswith(u):
            return int(float(s[: -len(u)]) * _SIZE_UNITS[u])
    return int(float(s))


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
    # Files at least this large, when dst is a local path/mount and rsync
    # is available, are transferred via `rsync --append` (resumable across
    # process death — rclone cannot resume a single file). Smaller files /
    # remote dst / no rsync → rclone. "0"/"off" disables (all rclone).
    resumable_min_size: str = "10GiB"
    # ASC MHL v2.0 emit: opt-in. When true, copy/check/hash ops write a
    # generation file under <root>/ascmhl/ for the relevant side(s).
    emit_mhl: bool = False
    # `mhl_author` accepts git-style "Name <email@host.dom>" syntax; the
    # email is split out into the schema's `email` attribute. When the
    # email part doesn't validate (e.g. no '.' in domain), the full
    # string is kept as the author name.
    mhl_author: Optional[str] = None
    mhl_author_phone: Optional[str] = None  # rare; <author phone="...">
    mhl_author_role: Optional[str] = None   # e.g. "DIT", "Editor"
    mhl_location: Optional[str] = None      # physical location, e.g. "Studio A"
    mhl_comment: Optional[str] = None
    mhl_sides: Optional[List[str]] = None  # None → smart default by op


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
    resumable_min_size: Optional[str] = None
    emit_mhl: Optional[bool] = None
    mhl_author: Optional[str] = None
    mhl_author_phone: Optional[str] = None
    mhl_author_role: Optional[str] = None
    mhl_location: Optional[str] = None
    mhl_comment: Optional[str] = None
    mhl_sides: Optional[List[str]] = None

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

    def resolved_resumable_min_size(self, defaults: Defaults) -> int:
        """Threshold in bytes; 0 = rsync engine disabled (all rclone)."""
        raw = (self.resumable_min_size if self.resumable_min_size is not None
               else defaults.resumable_min_size)
        return _parse_size(raw)

    def resolved_emit_mhl(self, defaults: Defaults) -> bool:
        return self.emit_mhl if self.emit_mhl is not None else defaults.emit_mhl

    def resolved_mhl_author(self, defaults: Defaults) -> Optional[str]:
        return self.mhl_author or defaults.mhl_author

    def resolved_mhl_author_phone(self, defaults: Defaults) -> Optional[str]:
        return self.mhl_author_phone or defaults.mhl_author_phone

    def resolved_mhl_author_role(self, defaults: Defaults) -> Optional[str]:
        return self.mhl_author_role or defaults.mhl_author_role

    def resolved_mhl_location(self, defaults: Defaults) -> Optional[str]:
        return self.mhl_location or defaults.mhl_location

    def resolved_mhl_comment(self, defaults: Defaults) -> Optional[str]:
        return self.mhl_comment or defaults.mhl_comment

    def resolved_mhl_sides(self, defaults: Defaults) -> Optional[List[str]]:
        return self.mhl_sides or defaults.mhl_sides


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

        When `emit_mhl` is in effect for this job, non-MHL-v2.0 algorithms
        are filtered out so the negotiated algo is guaranteed emittable.
        Empty result raises ValueError.
        """
        explicit = job.resolved_hash_priority(self.defaults)
        if explicit:
            priority = [a.strip().lower() for a in explicit]
        else:
            name = (
                job.resolved_hash_profile(self.defaults)
                or profiles_mod.DEFAULT_PROFILE
            )
            prof = profiles_mod.load(
                name,
                state_dir=self.state_dir_root(),
                inline=self.inline_profiles or None,
            )
            priority = list(prof.priority)
        if job.resolved_emit_mhl(self.defaults):
            from . import mhl
            filtered = [a for a in priority if a in mhl.MHL_ALGORITHMS]
            if not filtered:
                raise ValueError(
                    f"emit_mhl=true but no MHL v2.0 algorithms remain in "
                    f"priority. Source list: {priority}. "
                    f"MHL set: {sorted(mhl.MHL_ALGORITHMS)}. "
                    f"Pick an MHL-aligned profile (e.g. 'dit') or disable "
                    f"emit_mhl."
                )
            priority = filtered
        return priority

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
        resumable_min_size=str(d.get("resumable_min_size", Defaults.resumable_min_size)),
        emit_mhl=bool(d.get("emit_mhl", Defaults.emit_mhl)),
        mhl_author=d.get("mhl_author"),
        mhl_author_phone=d.get("mhl_author_phone"),
        mhl_author_role=d.get("mhl_author_role"),
        mhl_location=d.get("mhl_location"),
        mhl_comment=d.get("mhl_comment"),
        mhl_sides=_as_str_list(
            d.get("mhl_sides"), "mhl_sides", "[defaults]",
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
                resumable_min_size=jr.get("resumable_min_size"),
                emit_mhl=jr.get("emit_mhl"),
                mhl_author=jr.get("mhl_author"),
                mhl_author_phone=jr.get("mhl_author_phone"),
                mhl_author_role=jr.get("mhl_author_role"),
                mhl_location=jr.get("mhl_location"),
                mhl_comment=jr.get("mhl_comment"),
                mhl_sides=_as_str_list(
                    jr.get("mhl_sides"), "mhl_sides", ctx,
                ),
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
