# tools

Backup verification & rclone migration tools — content-addressed,
EXIF-aware, with persisted SQLite checksum caches.

[![CI](https://github.com/Jarvie8176/tools/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/Jarvie8176/tools/actions/workflows/ci.yml)
[![CodeQL](https://github.com/Jarvie8176/tools/actions/workflows/codeql.yml/badge.svg?branch=main)](https://github.com/Jarvie8176/tools/actions/workflows/codeql.yml)
[![Security](https://github.com/Jarvie8176/tools/actions/workflows/security.yml/badge.svg?branch=main)](https://github.com/Jarvie8176/tools/actions/workflows/security.yml)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue.svg)](https://www.python.org/)

A small, opinionated personal toolchain for moving and verifying
file archives where the **content** matters more than the **path**:
file renames between source and destination should not break a
verification, and recomputing checksums for terabytes of data on
every run is not acceptable.

Both tools share a common ethos:

- **Content-addressed.** Match by hash, not by filename. A
  camera-style `IMG_0050.jpg` on the SD card and a
  `2026-03-28_18-24-59_IMG_0050.jpg` on the NAS are "the same file"
  iff their hashes agree.
- **Persisted SQLite caches.** Hashes are written to a SQLite cache
  beside the data, invalidated by `(size, mtime)`, and resumable
  across crashes / SIGKILL / mount drops.
- **Independent steps.** No giant atomic operation that has to roll
  back. Each step is idempotent on re-run.

## The toolchain

| Tool | What it does | Path |
|---|---|---|
| **backup-verification** | Verify SD-card contents against a NAS backup, with EXIF-aware comparison modes (`smart` / `full` / `data-only`) and multi-threaded SHA-256. | [`backup-verification/`](backup-verification/) |
| **rclone-migrate** | Safer alternative to `rclone move`: split into `copy → check → delete`, content-addressed matching, persisted hash manifests, audit log, and a check-signature gate that refuses delete if src changed mid-flight. | [`rclone-migrate/`](rclone-migrate/) |

Each sub-project has its own `README.md` with a full reference; this
top-level README is the umbrella entry point.

## Quick start

```bash
# Clone
git clone https://github.com/Jarvie8176/tools.git
cd tools

# Pick a tool
cd backup-verification && poetry install         # SD ↔ NAS verifier
# or
cd rclone-migrate && pip install -e '.[dev]'     # rclone migration
```

Then read the per-tool README for command-line reference and
configuration. `rclone-migrate` ships an interactive `rmig init`
wizard that generates a working TOML in one pass.

## Project conventions

These apply across both sub-projects.

- **Python**: 3.9 minimum. CI tests Python 3.9 – 3.13.
- **Lint**: `ruff` from repo root. Configured narrowly for high-signal
  bug rules (`E9`, `F63`, `F7`, `F82`) — see [`ruff.toml`](ruff.toml).
- **Tests**: `pytest` + `pytest-cov` with branch coverage, threshold
  enforced per sub-project.
- **CI**: every PR runs the matrix above plus CodeQL, `bandit`,
  `gitleaks`, `trivy fs`, `pip-audit`, `zizmor`, and `actionlint`.
  See [`.github/workflows/`](.github/workflows/).
- **Working with AI coding agents**: see
  [`AGENTS.md`](AGENTS.md) for the conventions agents are
  expected to follow.
- **Reporting a vulnerability**: see [`SECURITY.md`](SECURITY.md).

## License

Apache License 2.0 — see [`LICENSE`](LICENSE).

Copyright 2026 Jarvie8176.
