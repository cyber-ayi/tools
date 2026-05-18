# rclone-migrate

Safer alternative to `rclone move`: **copy → check → delete** as three independent
steps, with **content-addressed matching** (filenames may differ between src and
dst — match by hash) and **persisted checksums** (cache survives across runs).

Sister tool of [`backup-verification/`](../backup-verification/) — same SQLite
caching pattern, same hash-based matching idea, different goal (migration vs.
verification).

## Install

One-liner:

```bash
curl -fsSL https://raw.githubusercontent.com/Jarvie8176/tools/main/rclone-migrate/scripts/install.sh | bash
```

Audited form (recommended — read the script before running it):

```bash
curl -fsSL https://raw.githubusercontent.com/Jarvie8176/tools/main/rclone-migrate/scripts/install.sh -o install.sh
less install.sh
bash install.sh
```

The installer:

1. Verifies you have Python ≥ 3.9 on `PATH`
2. Installs [`pipx`](https://pipx.pypa.io/) into your user dir if missing
3. Downloads the wheel + `SHA256SUMS` from the latest GitHub Release
4. Verifies the wheel's SHA-256
5. Installs `rmig` (and its `rmig-*` subcommands) via `pipx`

Pin a specific version:

```bash
bash install.sh --version 0.7.2
```

Uninstall (preserves user state in `${XDG_DATA_HOME:-~/.local/share}/rclone-migrate/`):

```bash
curl -fsSL https://raw.githubusercontent.com/Jarvie8176/tools/main/rclone-migrate/scripts/uninstall.sh | bash
```

### Verify the signature (optional)

Every release artifact is signed via [Sigstore](https://www.sigstore.dev/)
keylessly, bound to the build workflow's OIDC identity. To verify a wheel
came from this repo's release workflow, download **both** the wheel and its
`.sigstore.json` bundle from the [GitHub Release page][releases]:

```bash
VERSION=0.7.2   # the release you want to verify
TAG="rclone-migrate-v${VERSION}"
BASE="https://github.com/Jarvie8176/tools/releases/download/${TAG}"

curl -fsSL -O "${BASE}/rclone_migrate-${VERSION}-py3-none-any.whl"
curl -fsSL -O "${BASE}/rclone_migrate-${VERSION}-py3-none-any.whl.sigstore.json"

pip install sigstore
python -m sigstore verify identity \
    --bundle "rclone_migrate-${VERSION}-py3-none-any.whl.sigstore.json" \
    --cert-identity "https://github.com/Jarvie8176/tools/.github/workflows/release-rclone-migrate.yml@refs/tags/${TAG}" \
    --cert-oidc-issuer 'https://token.actions.githubusercontent.com' \
    "rclone_migrate-${VERSION}-py3-none-any.whl"
```

A successful run prints `OK: rclone_migrate-...whl` and exits 0. A non-zero
exit means the wheel is not genuinely from this repo's release workflow —
**do not install it**.

[releases]: https://github.com/Jarvie8176/tools/releases

### Requirements

- macOS or Linux. Windows is not covered by the installer; install
  manually with `pipx install rclone-migrate-<X.Y.Z>.whl` from a
  GitHub Release wheel.
- Python ≥ 3.9
- [`rclone`](https://rclone.org/downloads/) on `PATH`

## When to use

You want to move files from src → dst, but:

- Some files may already exist at dst under different names (don't re-copy).
- You want a verifiable "all good at dst" gate before deleting at src.
- You don't want to re-hash gigabytes every run; checksums should persist.
- Both ends may be local or rclone remotes (S3, B2, GDrive, ...).

## Three commands

```
rmig-hash    refresh per-side hash manifests (independent step; auto-run by the others)
rmig-copy    copy each src file whose hash isn't already at dst (deduplicated)
rmig-check   verify every src file's hash exists at dst; record a signature on success
rmig-delete  delete src files whose hash is at dst — refuses unless check_signature
             still matches and the last check is recent enough
```

A single dispatcher `rmig {hash|copy|check|delete}` is also installed.

## Quickstart

### Set up a new job (`rmig init`)

Interactive wizard — prompts for the missing pieces, generates the TOML, optionally runs an immediate `rmig-check`:

```bash
rmig init
# Source path or rclone remote: /Volumes/Insta360 X5/DCIM/Camera01
# Destination path: thinkpad:/mnt/usb-drive-wd/storage/ingest/Insta360 X5
# Job name [insta360-x5-dcim-camera01]: insta360-x5
# src kind [local] (local/remote-ssh/remote-cloud): local
# dst kind [remote-ssh] (...): remote-ssh
# Write TOML to [~/.local/share/rclone-migrate/insta360-x5.toml]: ↵
# (probe rclone backends, show negotiated hash)
# Generated TOML: ...
# Write this file? [Y/n]: y
# Run `rmig-check` now? [y/N]: y
```

Or fully scripted (e.g. cron / Ansible):

```bash
rmig init -y \
  --name insta360-x5 \
  --src "/Volumes/Insta360 X5/DCIM/Camera01" \
  --dst "thinkpad:/mnt/usb-drive-wd/storage/ingest/Insta360 X5" \
  --src-kind local \
  --dst-kind remote-ssh \
  --run-check
```

The wizard auto-detects `src_kind`/`dst_kind` from the path (filesystem path → `local`; `remote:`-style with `type=sftp` in rclone config → `remote-ssh`; everything else remote → `remote-cloud`) and picks defaults accordingly:

- `local` src → `local_cache_in_root = false` (keep cache off SD card)
- `remote-cloud` either side → bumps `transfers` to 8

Re-runs are idempotent: editing the TOML by hand later is fine, the wizard never modifies an existing config file unless you say yes to overwrite.

## Daily Usage

```bash
# Install
cd rclone-migrate
pip install -e .                  # or: pip install -e '.[dev]'  for tests

# Config
cp config.example.toml my.toml
$EDITOR my.toml                   # set [[jobs]] entries with src/dst

# Run, in order
rmig-hash   -c my.toml -j fuji-archive --side both
rmig-copy   -c my.toml -j fuji-archive
rmig-check  -c my.toml -j fuji-archive
rmig-delete -c my.toml -j fuji-archive          # dry-run by default
rmig-delete -c my.toml -j fuji-archive --confirm
```

## Configuration

```toml
[defaults]
# hash = "MD5"                    # uncomment to force; else auto-negotiated
state_dir  = "~/.local/share/rclone-migrate"
transfers  = 8
checkers   = 16
download   = false                # true → download remote bytes when the
                                  # negotiated hash isn't natively supported
local_cache_in_root = true        # cache at <root>/.rmig-cache.db (false →
                                  # centralized under state_dir)

[delete]
require_check_within = "24h"      # delete must be within this of last check
remove_empty_src_dirs = true
require_confirm = true            # without --confirm, delete is dry-run

[[jobs]]
name    = "fuji-archive"
src     = "/Volumes/SDCard/DCIM"
dst     = "b2:photos/fuji"
enabled = true

[[jobs]]
name    = "audio-archive"
src     = "/Users/me/backup/audio"
dst     = "/Volumes/NAS/audio"
hash    = "SHA256"                # local↔local: stronger hash worth it
transfers = 4
```

Per-job fields override `[defaults]`. Duration syntax: `500ms`, `30s`, `5m`,
`24h`, `7d`.

## How matching works

`rclone copy` and `rclone check` match by **path**. This tool matches by
**hash** — so a src file `IMG_0050.jpg` and a dst file
`2026-03-28_18-24-59_IMG_0050.jpg` are considered "same content" iff their
hashes match.

- **Copy**: src files are deduplicated by hash (multiple src files with the
  same content → one copy at dst). `rclone copyto --checksum` handles per-file
  verification on the wire.
- **Check**: every src hash must appear in dst's hash set. (`rclone check`
  isn't used — it pairs by path and would report all renames as missing.)
- **Delete**: every src file whose hash is in dst's hash set is removed,
  including all duplicate-content copies.

## Hash algorithm — automatic

On every run, the tool probes both backends via `rclone backend features`,
takes the intersection of supported hashes, and picks the highest-priority
shared algorithm. The default priority is the `balanced` profile:

```
SHA256 > SHA1 > MD5 > SHA512 > BLAKE3 > backend-specific (Dropbox, QuickXor, ...)
```

Common outcomes:

| src ↔ dst                          | algo  |
|------------------------------------|-------|
| local ↔ local                      | sha256|
| local ↔ S3 / GCS / Azure / GDrive  | md5   |
| local ↔ B2                         | sha1  |
| local ↔ OneDrive (business)        | sha1 or sha256 |
| local ↔ Dropbox                    | dropbox content_hash |

To force a single algorithm: `[defaults] hash = "MD5"` or per-job
`hash = "..."`. Forcing an algo that a backend doesn't natively expose
will error unless you also set `download = true` (which fetches bytes to
compute it — slow + expensive).

### Profiles — picking a different priority order

Set `hash_profile = "<name>"` to swap the priority list. Built-in profiles:

| Profile        | First algo | Use case                                |
|----------------|------------|-----------------------------------------|
| `balanced`     | sha256     | General-purpose; default                |
| `dit`          | xxh3       | DIT/film/video; MHL-aligned             |
| `cloud-native` | md5        | Minimize re-hashing across cloud        |
| `forensic`     | sha256 + md5 multi-hash | Compliance / chain-of-custody |

```toml
[defaults]
hash_profile = "dit"             # global

[[jobs]]
name = "fuji-archive"
hash_profile = "cloud-native"    # per-job override
```

Inspect / customize:

```bash
rmig profiles list               # all profiles + sources
rmig profiles show dit           # one profile's content
rmig profiles init dit           # copy bundled to user dir for editing
rmig profiles validate           # check every reachable profile
```

User profiles live at `<state_dir>/profiles/<name>.toml` and shadow bundled
ones of the same name. Inline `[profiles.<name>]` in the job TOML wins over
both. See [docs/profiles.md](docs/profiles.md) for full schema, three more
sample profiles (`backup`/`speed`/`compat`), and a comparison matrix.

## ASC MHL v2.0 output (opt-in)

Set `emit_mhl = true` in `[defaults]` or per-job to make rmig write
[ASC MHL v2.0](https://github.com/ascmitc/mhl-specification) generation
files alongside data — the same format used by Silverstack / Hedge /
ShotPut Pro. Each successful `hash` / `check` / `copy` writes one
generation under `<root>/ascmhl/`:

```toml
[defaults]
hash_profile = "dit"            # MHL-aligned algorithms (xxh family)
emit_mhl     = true
mhl_author   = "Me <me@example.com>"
```

```bash
rmig export-mhl -j JOB           # also: explicitly emit from current cache
```

When emit is on, the priority list is filtered to MHL-compatible algos
(`c4`/`md5`/`sha1`/`xxh3`/`xxh64`/`xxh128`) so the negotiated algorithm is
guaranteed emittable. Independent verification with the official tool:

```bash
pip install ascmhl && ascmhl verify <root>
```

See [docs/mhl-integration.md](docs/mhl-integration.md) for op→generation
mapping, XML structure, and current limitations.

### Use the rclone remote, not its fuse mount

If your destination is an rclone-mounted directory (e.g. `~/mnt/nas/...` from
`rclone mount nas:/...`), **don't put the mount path in the config**. Use the
underlying rclone remote path instead:

```toml
# WRONG: hashing reads bytes through fuse-t for every file → flaky on multi-GB files
dst = "/Users/me/mnt/thinkpad-wd/storage/ingest/X"

# RIGHT: hashing happens on the remote (e.g. SFTP backend SSH-runs sha1sum)
dst = "thinkpad:/mnt/usb-drive-wd/storage/ingest/X"
```

The remote path lets the tool detect "remote with native hash support" and
ask the backend to compute hashes server-side — for SFTP, that means
SSH-running `sha1sum`/`md5sum` on the remote machine, transferring zero
file bytes. fuse-t (and other FUSE-based mounts) repeatedly fail under
parallel reads of multi-GB files; we learned this the hard way (see
[walkthrough](docs/walkthrough-insta360-x5.md#run-4)).

### S3 multipart caveat

S3's ETag is the MD5 of single-part objects only — for multipart uploads it's
`MD5(MD5(part1) + MD5(part2) + ...)-N`, which is **not** the file's MD5. rclone
works around this by writing `x-amz-meta-md5chksum` metadata when **rclone
itself** uploads the object. Objects uploaded by `awscli`/`s3cmd` lack this
metadata and will appear as missing-hash. Workarounds: re-upload via rclone, or
choose `download = true`, or pick SHA1 if the bucket has it elsewhere.

## Where checksums live

The design splits "data" from "state":

| What                           | Where                                              |
|--------------------------------|----------------------------------------------------|
| Local-side hash cache          | `<root>/.rmig-cache.db` (travels with data)        |
| Remote-side hash               | Not cached. Read live from backend metadata via `rclone hashsum REMOTE` (which doesn't download bytes). |
| Remote w/o native hash support | `<state_dir>/<job>/state.db` table `remote_hash_cache` (best-effort, only if `download = true`) |
| Job state (signature, timestamps) | `<state_dir>/<job>/state.db` table `meta`        |

Why no caching for remotes with native hash? The backend already stores the
hash — caching it locally just risks staleness if the remote is changed
externally. The backend is the source of truth.

If `<root>` isn't writable (read-only mount, etc.), the local cache silently
falls back to `<state_dir>/local-cache/cache-<sha1(root)>.db`.

The cache schema includes an `algorithm` column, so switching the negotiated
hash doesn't blow away accumulated work — both algorithms can coexist.

## The check_signature gate

`rmig-check`, on success, records a SHA-256 of the entire src manifest into
`state.db` as `check_signature`. `rmig-delete` recomputes the current src
signature on startup and **refuses to delete unless it matches**. Any change
to src between check and delete (file added, modified, removed) flips the
signature and aborts the deletion.

This is strictly stronger than mtime comparison and catches:

- new file added to src
- existing src file modified
- src file removed (content might still need to be deleted from somewhere else)

A timeout is also enforced: `[delete] require_check_within = "24h"` means the
last successful check must be within 24h.

## Resumability

Every step is idempotent:

- `rmig-copy` re-run: any file already at dst (hash match) is skipped.
- `rmig-check` is read-only on data; safe to re-run.
- `rmig-delete` after partial failure: re-run continues; only files still
  present at src are touched.

After a copy, `check_signature` is automatically cleared (src may have been
read while copying — re-run check before delete).

## What it doesn't do

- **Two-way sync.** This is a one-shot migration tool. After delete, src is
  empty; for ongoing sync use `rclone sync`.
- **Atomic transactions.** The three steps are partially-failure-tolerant
  (idempotent re-runs) but not transactional in the database sense.
- **Automatic conflict resolution.** If dst has a file at the same relative
  path as the src file you want to copy but with different content, the copy
  will overwrite. (Use `rclone copy --immutable` semantics if you need
  protection — not currently exposed.)

## Testing

```
pip install -e '.[dev]'
pytest -v
```

The test suite covers:
- TOML parsing + duration syntax + job overrides
- Cache CRUD + size/mtime invalidation + multi-algorithm coexistence
- Hash algorithm negotiation across mocked backend feature sets
- End-to-end on two local directories: copy/check/delete with deduplication,
  signature-based tamper detection, dry-run, timeout, cache hit on re-run

26 tests pass on Python 3.9+ (uses `tomli` backport when stdlib `tomllib`
isn't available).

## Concurrency & crash recovery

### Per-job exclusive lock (fcntl)

Mutating ops (`hash`, `copy`, `check`, `delete`) acquire an exclusive
`fcntl.flock` on `<state_dir>/<job>/job.lock` at startup. A second
concurrent run gets a clean refusal:

```
$ rmig-check -c CFG -j t
REFUSE: another rmig run holds the job lock (pid=12345, op=copy,
started=2026-05-04T05:24:44); refusing.
```

The lock is auto-released by the OS when the holder exits — including
SIGKILL. Read-only commands (`log`, `file-status`, `list-jobs`) skip the
lock entirely.

### Checkpointed remote hashing

Every remote-side hash result is written to `state.db.remote_hash_cache`
**incrementally** (in batches of 25, or as each rclone subprocess
returns). A SIGKILL that loses the Python process keeps everything
already on disk; a re-run picks up from where it stopped:

```
[src] remote cache: valid=42 stale=0 new=20 removed=0
                                  ↑ first run produced these 42, second resumes
[src] remote-hashing 20 files (4 threads)
```

Two paths share the same logic, picked by whether `size_filter` is set:

| size_filter | Path | Use case | rclone command |
|---|---|---|---|
| None | bulk streaming | full src manifest | `rclone hashsum REMOTE` (one process, lines parsed as they arrive) |
| set | per-file parallel | dst with `dst >> src` | `rclone hashsum_file REMOTE/path` × N (4-way thread pool) |

Both write to `remote_hash_cache(side, path, hash, size, modtime)` with
size+mtime invalidation. Backends without native hash support (e.g.
GDrive paths missing md5) automatically use `--download` to pull bytes
for hashing — same caching applies.

## Audit log & per-file traceability

Every `rmig copy / check / delete` invocation writes an event row to
`<state_dir>/<job>/state.db` (table `events`) and tees its stdout to
`<state_dir>/<job>/runs/<ISO-ts>-<op>-<pid>.log`. Per-file outcomes that
are auditable changes or anomalies (copies, deletes, missing-on-check,
failures) go into `file_events` with a foreign key to the parent event.

### Inspect operation history

```bash
rmig log -c CFG -j JOB                 # last 20 ops
rmig log -c CFG -j JOB --last 100      # more
rmig log -c CFG -j JOB --op check --result fail
rmig log -c CFG -j JOB --json | jq '.[]'
```

Output (tabular):

```
   ID  STARTED              OP          RESULT    ALGO     COUNTS                   LOG
142    2026-05-04 05:24:44  check       ok        sha1     src=62 dst=62 aff=0      runs/...check-79613.log
141    2026-05-04 05:00:14  check       fail      sha1     src=62 dst=51 aff=11     runs/...check-79202.log
140    2026-05-04 04:22:40  check       crashed   sha256                            runs/...check-79613.log
```

If a previous run was killed mid-op, the next `rmig` invocation detects
the orphan, marks it `result='crashed'`, and prints a stderr warning.

### Per-file query

```bash
rmig file-status -c CFG -j JOB <relpath>           # human-readable
rmig file-status -c CFG -j JOB <relpath> --json    # script-friendly
rmig file-status -c CFG -j JOB --src foo.bin       # src side (default)
rmig file-status -c CFG -j JOB --dst bar.bin       # dst side
rmig file-status -c CFG -j JOB --hash <hexhash>    # reverse lookup
rmig file-status -c CFG -j JOB --missing           # all currently-missing src files
rmig file-status -c CFG -j JOB --orphan            # all dst files w/ no src match
```

Single-file output:

```
PATH          IMG_20260501_101600_00_004.dng  (src side)
FOUND         yes
ALGORITHM     sha1
SIZE          141,901,480 B
HASH          59daf2289a546191cd27c1d39f82d31438c886d6
LAST HASHED   2026-05-04 05:06:16

MATCHES ON DST (1):
  IMG_20260501_101600_00_004.dng
    size=141901480  last_hashed=2026-05-04 05:24:42

STATUS        ✓ BACKED UP

EVENT HISTORY (1):
  event#142   2026-05-04 05:24:44  check        matched
```

### When dst is a remote with no local cache

`rmig file-status` falls back to **inference from the latest passing
check**: a file present in the src cache that is NOT recorded as
`missing` in the most recent `result='ok'` check is reported as
`backed_up` (with a warning that the conclusion is inferred, not
directly verified against current dst contents). This works because
matched-OK files are intentionally NOT recorded in `file_events` —
absence implies match.

### Storage layout

```
<state_dir>/<job>/
  state.db                         # meta + events + file_events + remote_hash_cache
  state.db-wal, state.db-shm
  runs/                            # per-invocation stdout transcripts
    2026-05-04T05-24-44-check-79613.log
    2026-05-04T04-22-40-check-79613.log
    ...
  local-cache/                     # used when local_cache_in_root=false
    cache-<sha1(root)>.db          # per-side hash cache
```

### What's recorded vs not

| Operation | Per-file rows in file_events |
|---|---|
| `check` | one row per **missing** src file (matches are implicit) |
| `copy`  | one row per successful copy (`outcome='copied'`) + one per failure |
| `delete`| one row per successful delete (`outcome='deleted'`) + one per failure |
| `hash`  | none (`hash_cache.refreshed` already records when each file was hashed) |

### Direct SQL access

```bash
sqlite3 ~/.local/share/rclone-migrate/insta360-x5/state.db \
  "SELECT id, datetime(started_ts,'unixepoch','localtime'), op, result, algo
   FROM events ORDER BY started_ts DESC LIMIT 10"
```

## Real-world walkthrough

For a fully-worked example — including all the dead-ends and the fixes they
provoked — see [docs/walkthrough-insta360-x5.md](docs/walkthrough-insta360-x5.md).
That run motivated several of the optimizations now in the codebase:

- `size_filter` parameter on `manifest.refresh` (avoid hashing dst files whose
  size can't possibly match a src file)
- incremental cache flush every 25 hashed files (survive mid-run mount drops)
- per-file SFTP path in `_refresh_remote_live` (parallel SSH `sha?sum`,
  zero file bytes over the wire)
- `refresh_both` applies the filter to every operation (`copy`/`check`/
  `delete` all benefit when dst >> src)
- `REFUSE` message on `rmig-delete` hints at hash-algorithm mismatch when
  the saved signature uses a different algorithm than the current run

CLI ergonomics that came out of the post-walkthrough audit:

```bash
rmig --version                 # 0.7.2
rmig list-jobs -c CONFIG       # tabular dump of all jobs
```

## Layout

```
rclone-migrate/
  pyproject.toml
  config.example.toml
  rclone_migrate/
    cli.py           # 4 entry points + dispatcher
    config.py        # TOML → dataclass
    rclone.py        # subprocess wrapper
    hashing.py       # algorithm negotiation
    cache.py         # <root>/.rmig-cache.db
    state.py         # central state.db
    manifest.py      # unifying abstraction (local / remote-live / remote-cached)
    ops.py           # copy / check / delete glue
  tests/
    test_config.py
    test_cache.py
    test_state.py
    test_hashing.py
    test_e2e.py
```
