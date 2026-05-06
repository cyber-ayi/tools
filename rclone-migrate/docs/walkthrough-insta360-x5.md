# Walkthrough: verifying an Insta360 X5 SD-card backup

A real-world `rmig-check` run end-to-end — including every blocker we hit and
the fix. Use this as a template when verifying SD-card → NAS-style backups
where the destination is much larger than the source and is reached over a
flaky filesystem mount.

## Goal

Confirm every file on the Insta360 X5 SD card has a content-identical copy on
the NAS:

| Side | Path                                                           | Size  | Files |
|------|----------------------------------------------------------------|-------|-------|
| src  | `/Volumes/Insta360 X5/DCIM/Camera01` (USB-3 exFAT)             | 117 GB |  62  |
| dst  | NAS via SSH-to-thinkpad → external USB-3 WD drive              | 1.2 TB | 218  |

Filenames are 1:1 in this case, but the tool doesn't rely on that — matching
is purely by content hash.

## Final working config

`~/.local/share/rclone-migrate/insta360-x5.toml`:

```toml
[defaults]
state_dir = "~/.local/share/rclone-migrate"
transfers = 4
local_cache_in_root = false        # SD card: no writes to source

[delete]
require_check_within = "24h"
require_confirm = true

[[jobs]]
name = "insta360-x5"
src  = "/Volumes/Insta360 X5/DCIM/Camera01"
dst  = "thinkpad:/mnt/usb-drive-wd/storage/ingest/Insta360 X5"
```

The dst is **deliberately the SFTP rclone remote**, not the fuse-t mount path
under `~/mnt/thinkpad-wd/...`. See run 4 below for why.

## Run

```bash
rmig-check -c ~/.local/share/rclone-migrate/insta360-x5.toml -j insta360-x5
```

Result:

```
[job=insta360-x5] hash algorithm = sha1
[src] cache @ ...: valid=0 stale=0 new=62 removed=0
[src] hashing 62 files with 4 threads...
[check] src size set: 60 unique sizes
[dst] remote candidates after size filter: 62 (skipped 162)
  [dst] remote-hashed 62/62
[check] src=62 dst=62 missing=0 (algo=sha1)
OK: signature = 9553b678e84adc62ba902d828e50d14398feecb18a98bf4a8c63e08b9c5087db
```

Wall-clock: ~5 min (src exFAT read at ~400 MB/s + 4-way parallel SSH
`sha1sum` on the thinkpad's local disk).

## What had to be fixed first (chronological log)

### Run 1: `local_cache_in_root = true` + dst on fuse-t mount

Config wrote `<root>/.rmig-cache.db` directly into the SD card and into the
NAS mount. SD card writes are bad practice (wear); NAS mount worked but...

**Failure**: After 50 of 62 dst files were hashed, fuse-t threw
`ConnectionResetError` on a 16 GB `.insv` file mid-read. The Python
`ThreadPoolExecutor.as_completed()` loop raised, never reaching the
`cache.upsert_many` call → all 50 already-computed dst hashes were lost.

**Fix in code**: `manifest.py::_refresh_local`
- Per-file `try/except` around `hash_file_local` → record failure, continue.
- Periodic incremental flush every 25 entries → partial work survives crashes.

**Config change**: `local_cache_in_root = false` — caches now under
`~/.local/share/rclone-migrate/<job>/local-cache/cache-<sha1(root)>.db`,
nothing written to SD card or NAS root.

### Run 2: state was in `/tmp` → wiped overnight

Initial config used `state_dir = "/tmp/rmig-insta/state"`. macOS purged `/tmp`
between runs (cross-day cleanup), so src cache was gone — would have rehashed
all 62 src files again.

**Config change**: state in `~/.local/share/rclone-migrate/`. `/tmp` is fine
for one-shot test rigs but never for persistent work.

### Run 3: fuse-t mount died entirely

The peer-reset from run 1 had taken down the whole fuse-t session. Mount
disappeared — `mount | grep thinkpad-wd` was empty. `rmig-check` correctly
errored out with `local root does not exist` and refused to proceed.

**Fix**: User remounted via `~/bin/mount-thinkpad.sh`.

### Run 4: dst hashing kept failing on big `.insv` files

With the resilience fixes from run 1 in place, runs 3-4 still left **11
files** in `MISSING` (out of 12 candidates ≥ 1 GB):

```
VID_*.insv: OSError(6, 'Device not configured')
VID_*.insv: ConnectionResetError(54, 'Connection reset by peer')
VID_*.insv: FileNotFoundError(2, 'No such file or directory')
```

But `ls -la` on those exact paths confirmed they all existed at dst with
correct sizes. Pattern: every failure was on a file > 1 GB; `transfers = 4`
meant four large reads concurrently saturated/destabilised the fuse-t kernel
extension.

**Diagnosis**: fuse-t isn't reliable enough to read multiple multi-GB files
in parallel from the python process. The dst fundamentally **already is** an
rclone remote (`thinkpad:` SFTP); the mount is just a read-convenience layer
we don't need.

**Fix in code**: `manifest.py::_refresh_remote_live` — when `size_filter` is
set (verify-mode), don't `rclone hashsum REMOTE` (which would SSH-run
sha1sum on the entire 1.2 TB tree). Instead `lsf` first, filter by size,
then call `rclone hashsum_file` per surviving candidate in a thread pool.
4 parallel SSH connections, each remotely runs `sha1sum` on one file → no
file bytes traverse the network.

**Config change**: `dst = "thinkpad:/mnt/usb-drive-wd/storage/ingest/Insta360 X5"`
(SFTP remote path, not the fuse-t mount path).

### Hash algorithm

The first three runs negotiated **SHA-256** (both sides reported as local fs;
local supports any algorithm). After switching to SFTP, negotiation was:

```
src hashes: blake3, crc32, dropbox, ..., md5, sha1, sha256, sha512  (local — all)
dst hashes: md5, sha1                                                (sftp — only)
intersection ∩ preference order → sha1
```

`thinkpad:` SFTP backend reports only md5/sha1 because rclone determined at
config time that `sha256sum` is not on the remote `$PATH`. SHA-1 is more than
adequate for content-addressed verification (not an adversarial setting).

The cache schema's `algorithm` column meant the existing src SHA-256 cache was
preserved, and SHA-1 entries were added alongside — switching algorithms
didn't blow away accumulated work. Final src cache:

```
sha1   | 62 | 116.75 GB
sha256 | 62 | 116.75 GB
```

## Generalised lessons

| Symptom                                              | Take-away                                                           |
|------------------------------------------------------|---------------------------------------------------------------------|
| ThreadPoolExecutor blew up → all in-flight work gone | Always flush incrementally; never put commit at the end of a long parallel section. |
| Fuse-style network mount throws OSError on big reads | The mount is a UX convenience; for hashing use the underlying rclone remote directly. |
| dst >> src and dst hashing dominated runtime        | Apply src.size_set as a filter on dst; sibling `backup-verification` does the same. |
| `/tmp` is *not* persistent on macOS                  | State that should survive logout / reboot belongs in `~/.local/share/...`. |
| Local SD card: don't write caches to it             | Set `local_cache_in_root = false`; central cache by sha1(root).     |
| Hash negotiation rejected SHA-256 on SFTP            | rclone's SFTP backend hashes via remote-side binaries; if `sha256sum` isn't on `$PATH`, drop to sha1. |

## Code changes that came out of this verification

All in [`rclone_migrate/manifest.py`](../rclone_migrate/manifest.py):

1. `Manifest.refresh()` and the three `_refresh_*` strategies grew an
   optional `size_filter: Set[int]` parameter, threaded through from
   `ops.do_check` (computed as `src.size_set` after src refresh).
2. `_refresh_local`: per-file try/except, incremental cache flush every
   `FLUSH_EVERY = 25` entries, end-of-run failure summary.
3. `_refresh_remote_live`: split into bulk path (no filter) and per-file
   path (with filter). Per-file path uses `rclone.hashsum_file` in a
   `ThreadPoolExecutor`, with the same warn-and-continue semantics.

`ops.do_check` was updated to refresh src first, snapshot
`{e.size for e in src.entries}`, then refresh dst with that as the filter.

26/26 unit + e2e tests still pass.

## After verification: audit log + per-file query

The check writes an event to `state.db`:

```bash
$ rmig log -c ~/.local/share/rclone-migrate/insta360-x5.toml -j insta360-x5 --last 3

   ID  STARTED              OP      RESULT  ALGO   COUNTS                   LOG
   12  2026-05-04 13:30:00  check   ok      sha1   src=62 dst=62 aff=0      runs/...check-...log
   11  2026-05-04 05:24:44  check   ok      sha1   src=62 dst=62 aff=0      runs/...check-...log
   10  2026-05-04 05:00:14  check   fail    sha1   src=62 dst=51 aff=11     runs/...check-...log
```

Drill into the failed run:

```bash
$ sqlite3 ~/.local/share/rclone-migrate/insta360-x5/state.db \
    "SELECT path FROM file_events WHERE event_id=10 AND outcome='missing'"
VID_20260501_135336_00_019.insv
VID_20260501_140427_00_020.insv
... (11 paths)
```

Per-file lookup:

```bash
$ rmig file-status -c ~/.local/share/rclone-migrate/insta360-x5.toml -j insta360-x5 \
    IMG_20260501_101600_00_004.dng

PATH          IMG_20260501_101600_00_004.dng  (src side)
FOUND         yes
ALGORITHM     sha1
SIZE          141,901,480 B
HASH          59daf2289a546191cd27c1d39f82d31438c886d6
LAST HASHED   2026-05-04 05:06:16

MATCHES       (none)
STATUS        ✓ BACKED UP

WARN: no local match found, but latest check passed and this file was
not flagged missing — inferred backed_up (remote side has no local cache
to confirm directly)
```

The "inferred" caveat appears because `dst = thinkpad:` (SFTP) has no
local hash cache; the tool reads the latest successful check event and
notes that this file wasn't in its `missing` list.

## Reproducing this verification

```bash
# 0. Mount the thinkpad if you want to browse it (not required for rmig)
~/bin/mount-thinkpad.sh

# 1. Insert SD card; confirm src path
ls "/Volumes/Insta360 X5/DCIM/Camera01" | head

# 2. Confirm SFTP remote works
rclone backend features thinkpad: | python3 -c \
  'import sys, json; print(json.load(sys.stdin)["Hashes"])'
# expected: ['md5', 'sha1']

# 3. Run check
rmig-check -c ~/.local/share/rclone-migrate/insta360-x5.toml -j insta360-x5

# 4. (Optional) safe delete from SD card
rmig-delete -c ~/.local/share/rclone-migrate/insta360-x5.toml -j insta360-x5
# inspect dry-run output, then:
rmig-delete -c ~/.local/share/rclone-migrate/insta360-x5.toml -j insta360-x5 --confirm
```

If the SD card is unchanged from the verified state, step 4's first invocation
(no `--confirm`) lists exactly the 62 files; if anything has been added or
modified since the check, the signature gate will refuse.
