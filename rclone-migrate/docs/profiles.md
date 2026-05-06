# Hash Profiles

A profile is a named hash-algorithm priority list. `rclone-migrate` consults
the profile (plus inline overrides) when negotiating a hash with both rclone
backends, then picks the strongest algorithm in the intersection of:

```
{ profile.priority } ∩ { algos rclone reports for src } ∩ { algos rclone reports for dst }
```

If profile-supplied priority and the common set don't overlap, rmig falls
back to the built-in `PREFERRED_ORDER` and finally to any common algorithm.

## How to set a profile

```toml
[defaults]
hash_profile = "dit"

[[jobs]]
name = "fuji-archive"
src  = "/Volumes/SDCard/DCIM"
dst  = "b2:photos/fuji"
hash_profile = "cloud-native"   # per-job override
```

Resolution order (first match wins):

1. `job.hash` (single algorithm; legacy escape hatch)
2. `defaults.hash`
3. `job.hash_priority` (explicit list, no profile lookup)
4. `defaults.hash_priority`
5. `job.hash_profile`
6. `defaults.hash_profile`
7. Built-in default `balanced`

## Profile file lookup chain

When you reference a profile name `X`, rmig searches:

1. **Inline** — `[profiles.X]` in the current job's TOML
2. **User** — `<state_dir>/profiles/X.toml`
3. **Bundled** — shipped with the package

Same name in a higher tier shadows lower. `rmig profiles list` shows the
resolved source for each.

## Built-in profiles

Four profiles ship with the package:

| Profile        | First algorithm | Use case                              | MHL ✓† |
|----------------|-----------------|---------------------------------------|--------|
| `balanced`     | `sha256`        | General-purpose; security-leaning     | ✗      |
| `dit`          | `xxh3`          | DIT/film/video; MHL-aligned; speed    | ✓      |
| `cloud-native` | `md5`           | Minimize compute; cloud backends      | ✗      |
| `forensic`     | `sha256` + md5  | Compliance / chain-of-custody         | ✗      |

† **strict**: every algo in `priority + multi_hash` must be in the MHL v2.0
set (`c4`/`md5`/`sha1`/`xxh3`/`xxh64`/`xxh128`). `cloud-native` and
`forensic` have head-of-list MHL algos (`md5`, `sha1`, ...) but include
non-MHL fallbacks too — usable with MHL **after** rmig filters the priority
list at emit time (planned in UoW 2). Only `dit` is unconditionally clean.

Inspect with:

```bash
rmig profiles list
rmig profiles show dit
```

## Comparison matrix

| Profile        | Priority head             | Speed   | Collision resistance     | MHL strict† | Cloud-native fit | Algorithms                                              |
|----------------|---------------------------|---------|--------------------------|-------------|------------------|---------------------------------------------------------|
| `balanced`     | `sha256` → `sha1` → `md5` | medium  | strong                   | ✗           | partial          | sha256, sha1, md5, sha512, blake3, xxh128, xxh3, crc32, ... |
| `dit`          | `xxh3` → `xxh128` → `xxh64` | very fast | weak (non-cryptographic) | ✓        | weak             | xxh3, xxh128, xxh64, sha1, md5, c4                      |
| `cloud-native` | `md5` → `sha1`            | minimal | medium                   | ✗ (head ✓)  | strong           | md5, sha1, dropbox, quickxor, whirlpool, sha256, ...    |
| `forensic`     | `sha256` + `md5`          | slow    | strong                   | ✗ (multi-hash ✓) | partial     | sha256, sha512, sha1, c4, md5 (+ md5 multi-hash)        |
| `backup` (sample) | `blake3` → `sha256`    | fast    | strong                   | ✗           | weak             | blake3, sha256, sha512, sha1, md5                       |
| `speed` (sample)  | `xxh3` → `xxh128`      | fastest | weak                     | ✗ (blake3 not MHL) | weak       | xxh3, xxh128, xxh64, blake3, md5                        |
| `compat` (sample) | `md5` → `sha1`         | fast    | weak                     | ✗ (crc32 not MHL) | strong         | md5, sha1, sha256, sha512, crc32                        |

Speed / Collision / MHL labels are qualitative orientation. See *Picking a
profile* below for the decision rule.

## Picking a profile

```
src or dst is a cloud backend?
├─ yes
│   ├─ minimize cost / no --download?           → cloud-native
│   ├─ B2 / OneDrive Personal?                  → cloud-native (auto sha1)
│   └─ also send to film/post pipeline (MHL)?   → dit
└─ no (local↔local or local↔SFTP)
    ├─ camera media → working storage / LTO?    → dit
    ├─ long-term encrypted backup archive?      → backup (sample)
    ├─ legal / compliance / audit trail?        → forensic
    ├─ one-shot bulk dump, max throughput?      → speed (sample)
    ├─ heterogeneous / old NAS, want safest?    → compat (sample)
    └─ none of the above / unsure?              → balanced
```

## Customizing a built-in profile

```bash
rmig profiles init dit
# wrote /Users/me/.local/share/rclone-migrate/profiles/dit.toml
$EDITOR ~/.local/share/rclone-migrate/profiles/dit.toml
```

Your `dit.toml` now shadows the bundled `dit` for every job. Package
upgrades won't touch it. Delete the file to revert to bundled.

## Switching profiles — cache cost

The negotiated algorithm is a function of `priority ∩ src_supported ∩
dst_supported`. Switching profile can move the result: e.g. with a job
whose dst is SFTP exposing only `{md5, sha1}`:

| Profile        | Negotiated |
|----------------|------------|
| `balanced`     | `sha1`     |
| `dit`          | `sha1`     |
| `cloud-native` | `md5`      |
| `forensic`     | `sha1`     |

Hash caches (both local `<root>/.rmig-cache.db` and remote
`state.db.remote_hash_cache`) are keyed by `(path, algorithm)`. When a
profile switch lands on a different algorithm, **the new algorithm has
no cache rows** and the next run will re-hash every file at least once.

Practical implications:

- **Multi-algorithm coexistence is built in.** Cache rows for the old
  algo aren't deleted; switching back to a previously-used algo is fast
  (cache hits as before).
- **Plan the first run after a profile switch around the re-hash cost.**
  For an SD card → NAS migration of 100 GB at ~150 MB/s, expect ~10 min
  of additional hashing on the first run; subsequent runs are
  cache-served as usual.
- **`check_signature` is invalidated** by `do_copy` whenever the
  algorithm differs from the saved signature's algorithm. The error
  message now includes a hint when this is the cause.

If you want to switch *without* incurring the rehash, prime the new algo
in advance: `rmig hash --side both --full` once with the target profile,
then carry on.

## Profile file schema

```toml
description = "..."           # optional; one-line, shown in `rmig profiles list`
priority    = ["alg", ...]    # required; non-empty list; lowercased on load
multi_hash  = ["alg", ...]    # optional; algorithms to compute alongside primary
warnings    = ["..."]          # optional; printed to stderr when the profile is selected
```

Every algorithm name must be one of: `md5`, `sha1`, `sha256`, `sha512`,
`blake3`, `crc32`, `xxh3`, `xxh64`, `xxh128`, `c4`, `dropbox`, `quickxor`,
`whirlpool`, `mailru`. Names are case-insensitive.

## Sample profiles (not bundled)

Copy any of these into `<state_dir>/profiles/<name>.toml`. They live here
rather than in the package because they only make sense in narrow scenarios.

### `backup.toml` — encrypted-backup-style hashing

```toml
description = "Encryption-grade hashing; favors BLAKE3 + SHA-256 (Restic/Borg/Kopia style)."
priority = [
    "blake3", "sha256", "sha512",
    "sha1", "md5",
]
```

### `speed.toml` — pure throughput

```toml
description = "Bulk hashing; non-adversarial; xxh3 maxes out NVMe RAID."
priority = [
    "xxh3", "xxh128", "blake3",
    "xxh64", "md5", "sha1",
]
warnings = [
    "non-cryptographic hash; not suitable for adversarial / compliance use",
]
```

### `compat.toml` — lowest common denominator

```toml
description = "Heterogeneous environments; works with old NAS and rsync workflows."
priority = [
    "md5", "sha1", "sha256", "sha512", "crc32",
]
```

## Inline profile (config-only, no file)

For one-offs without creating a profile file:

```toml
[defaults]
hash_profile = "my-fast"

[profiles.my-fast]
description = "ad-hoc 'speed' for this config only"
priority    = ["xxh3", "blake3", "sha1"]

[[jobs]]
name = "..."
src  = "..."
dst  = "..."
```

`[profiles.X]` in the same TOML always wins over user/bundled `X`.

## Validation

```bash
rmig profiles validate                       # validate all reachable profiles
rmig profiles validate -c my-config.toml     # also include inline profiles
```

Validate runs the same checks as the runtime loader:
- `priority` exists and is a non-empty list of strings
- every algorithm name is recognized
- `multi_hash` (if present) is a list of valid algorithm names
- `warnings` (if present) is a list of strings
- TOML syntax is valid

## See also

- Issue tracker entry for **profile inheritance via `extends` field**: [Jarvie8176/tools#4](https://github.com/Jarvie8176/tools/issues/4) — currently you copy the whole bundled file via `rmig profiles init`.
