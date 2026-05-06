"""TOML config loader."""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib  # type: ignore


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


@dataclass
class Defaults:
    hash: Optional[str] = None
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
    transfers: Optional[int] = None
    checkers: Optional[int] = None
    download: Optional[bool] = None
    local_cache_in_root: Optional[bool] = None

    def resolved_hash(self, defaults: Defaults) -> Optional[str]:
        return self.hash or defaults.hash

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

    def get_job(self, name: str) -> Job:
        for j in self.jobs:
            if j.name == name:
                return j
        raise KeyError(f"job not found: {name}")

    def state_dir_for(self, job: Job) -> Path:
        return Path(_expand(self.defaults.state_dir)) / job.name


def load(path: str | Path) -> Config:
    with open(path, "rb") as f:
        raw = tomllib.load(f)

    d = raw.get("defaults", {})
    defaults = Defaults(
        hash=d.get("hash"),
        state_dir=d.get("state_dir", Defaults.state_dir),
        transfers=d.get("transfers", Defaults.transfers),
        checkers=d.get("checkers", Defaults.checkers),
        download=d.get("download", Defaults.download),
        local_cache_in_root=d.get("local_cache_in_root", Defaults.local_cache_in_root),
    )

    de = raw.get("delete", {})
    delete = DeleteOpts(
        require_check_within_s=_parse_duration(de.get("require_check_within", "24h")),
        remove_empty_src_dirs=de.get("remove_empty_src_dirs", True),
        require_confirm=de.get("require_confirm", True),
    )

    jobs = []
    for jr in raw.get("jobs", []):
        if "name" not in jr or "src" not in jr or "dst" not in jr:
            raise ValueError(f"job missing required fields: {jr}")
        jobs.append(
            Job(
                name=jr["name"],
                src=jr["src"],
                dst=jr["dst"],
                enabled=jr.get("enabled", True),
                hash=jr.get("hash"),
                transfers=jr.get("transfers"),
                checkers=jr.get("checkers"),
                download=jr.get("download"),
                local_cache_in_root=jr.get("local_cache_in_root"),
            )
        )

    return Config(defaults=defaults, delete=delete, jobs=jobs)
