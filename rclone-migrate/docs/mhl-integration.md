# ASC MHL v2.0 integration

`rclone-migrate` can write [ASC MHL v2.0](https://github.com/ascmitc/mhl-specification)
generation files alongside its data — the same format used by Silverstack /
Hedge / ShotPut Pro and the official `ascmhl` Python tool. This makes the
migration's hash work portable into film/post-production chain-of-custody
workflows.

## Quickstart

```toml
# <state_dir>/<job>.toml
[defaults]
hash_profile = "dit"            # MHL-aligned algos (xxh3 / sha1 / md5 / c4)
emit_mhl     = true             # opt-in
mhl_author   = "Me <me@example.com>"

[[jobs]]
name = "fuji-archive"
src  = "/Volumes/SDCard/DCIM"
dst  = "/Volumes/NAS/photos/fuji"
```

Then:

```bash
rmig hash   -j fuji-archive --side both
rmig copy   -j fuji-archive
rmig check  -j fuji-archive
```

After each successful op, an MHL generation lands under the relevant data
root:

```
/Volumes/SDCard/DCIM/
  ascmhl/
    0001_DCIM_2026-05-06_103214Z.mhl    # written by `rmig hash --side src`
    0002_DCIM_2026-05-06_104518Z.mhl    # written by `rmig check`
    ascmhl_chain.xml
  Clips/...

/Volumes/NAS/photos/fuji/
  ascmhl/
    0001_fuji_2026-05-06_103217Z.mhl    # written by `rmig hash --side dst`
    0002_fuji_2026-05-06_104310Z.mhl    # written by `rmig copy`
    ascmhl_chain.xml
  Clips/...
```

## How emit maps to rmig ops

| rmig op    | side(s) emitted | `<process>` | `<action>` | When skipped |
|------------|-----------------|-------------|------------|--------------|
| `hash`     | requested side(s) | `in-place` | `original` | side is remote; no entries |
| `check`    | src             | `in-place` | `verified` | check fails; src is remote |
| `copy`     | dst             | `transfer` | `original` | nothing copied (no delta); dst is remote |
| `delete`   | (none)          | —           | —           | always — MHL has no delete semantic |
| `export-mhl` | requested side(s) | `in-place` | `original` | side is remote; no cache rows |

Only **successful** ops emit. A failed `check` does not write a generation —
the chain stays clean of "we tried but failed" entries.

`copy` writes a *delta*: only the files that were just copied this run.
Earlier dst files retain their attestation from previous generations
(generation 0001 has files A/B; generation 0002, after a partial re-ingest,
has only the newly-arrived files C/D).

`check` writes a *full manifest*: every src file just got verified against
dst, so the entire src state is re-attested.

## Configuration

| Field               | Scope          | Type       | Default | Meaning                                                                |
|---------------------|----------------|------------|---------|------------------------------------------------------------------------|
| `emit_mhl`          | defaults / job | bool       | `false` | opt-in toggle                                                          |
| `mhl_author`        | defaults / job | string     | unset   | git-style `"Name <email@host.dom>"` — name → element text, email → `email` attr |
| `mhl_author_phone`  | defaults / job | string     | unset   | `<author phone="...">` — rare                                          |
| `mhl_author_role`   | defaults / job | string     | unset   | `<author role="DIT">` — e.g. "DIT", "Editor"                           |
| `mhl_location`      | defaults / job | string     | unset   | `<creatorinfo><location>` — e.g. "Studio A, Burbank"                   |
| `mhl_comment`       | defaults / job | string     | unset   | `<creatorinfo><comment>`                                                |
| `mhl_sides`         | defaults / job | string list | unset   | restrict emission to a subset of {`src`, `dst`}                        |

Job-level fields override defaults exactly like other config keys.

### About the `mhl_author` parser

The MHL XSD splits author into a name (element text) plus optional `email`,
`phone`, `role` attributes. `mhl_author` accepts a single git-style string
and splits on the trailing `<...>`:

| Config string                          | Emitted XML                                     |
|----------------------------------------|--------------------------------------------------|
| `"Alice"`                              | `<author>Alice</author>`                         |
| `"Alice <alice@example.com>"`          | `<author email="alice@example.com">Alice</author>` |
| `"Alice <alice@local>"`                | `<author>Alice &lt;alice@local&gt;</author>`     |
| `""` / unset                           | `<author>` element omitted                       |

The third row shows the safety fallback: if the email part doesn't satisfy
the MHL XSD's required `[^@]+@[^\.]+\..+` pattern (notice no `.` in
`local`), the original string is preserved as the author name and no
`email` attribute is emitted — better than producing schema-invalid XML.
Use `mhl_author_phone` / `mhl_author_role` flat fields if you need to set
those attributes; they only apply when an author name is present.

When `emit_mhl = true`, the resolved hash priority list is **filtered** to
the MHL v2.0 algorithm set (`{c4, md5, sha1, xxh3, xxh64, xxh128}`) so the
negotiated algorithm is guaranteed emittable. If the filter would leave the
priority empty, config loading errors out with a clear message — switch to
an MHL-aligned profile (e.g. `dit`) or include `sha1`/`md5` in your
`hash_priority`.

## What gets written

Each generation is one XML file per side. Structure (full example):

```xml
<?xml version="1.0" encoding="UTF-8"?>
<hashlist version="2.0" xmlns="urn:ASC:MHL:v2.0">
  <creatorinfo>
    <creationdate>2026-05-06T10:32:14+00:00</creationdate>
    <hostname>workstation.local</hostname>
    <tool version="0.8.1">rclone-migrate</tool>
    <author>Me &lt;me@example.com&gt;</author>
  </creatorinfo>
  <processinfo>
    <process>in-place</process>
    <ignore>
      <pattern>.DS_Store</pattern>
      <pattern>ascmhl</pattern>
      <pattern>ascmhl/</pattern>
      <pattern>.rmig-cache.db</pattern>
    </ignore>
  </processinfo>
  <hashes>
    <hash>
      <path size="141901480" lastmodificationdate="2026-05-04T12:53:20+00:00">Clips/A002C006.mov</path>
      <xxh3 action="original" hashdate="2026-05-06T10:32:14+00:00">a37bf8...</xxh3>
    </hash>
    ...
  </hashes>
</hashlist>
```

Plus one `ascmhl_chain.xml` per side, listing each generation's filename
and the C4 ID of the manifest bytes (tamper-evidence across the chain):

```xml
<?xml version="1.0" encoding="UTF-8"?>
<ascmhldirectory xmlns="urn:ASC:MHL:DIRECTORY:v2.0">
  <hashlist sequencenr="1">
    <path>0001_DCIM_2026-05-06_103214Z.mhl</path>
    <c4>c43qQABr...</c4>
  </hashlist>
  <hashlist sequencenr="2">
    <path>0002_DCIM_2026-05-06_104518Z.mhl</path>
    <c4>c41JS8am...</c4>
  </hashlist>
</ascmhldirectory>
```

## Verifying with the official tool

`rclone-migrate` only **writes** MHL files; it doesn't verify them.
Independent verification is exactly the value proposition — anyone with the
official tooling can re-verify rmig's output:

```bash
pip install ascmhl
ascmhl verify /Volumes/SDCard/DCIM
```

The `ascmhl` tool reads our `ascmhl/` folder, recomputes hashes, and
reports any mismatches.

## Manual export from existing state

When you've already run `rmig hash` / `rmig check` for some time and now
want to materialize MHL files retroactively, use:

```bash
rmig export-mhl -j JOB --side both
```

This reads the current cache, writes one `in-place` / `original` generation
per side. Good for adopting MHL on an existing job without re-hashing.

## Limitations (current)

| Limitation | Workaround |
|------------|------------|
| Remote sides are skipped (only local roots get `ascmhl/` written) | Rsync / `rclone copy` the `ascmhl/` folder up after emit, or run a local mirror first |
| `multi_hash` profile field is parsed but not yet computed | Filed as follow-up; for now, single primary algo per generation |
| `action="failed"` is never emitted (we don't compare against prior generations) | Use `ascmhl verify` for cross-generation diffing |
| C4 IDs are computed for chain integrity only, not exposed in the manifest body | Add `c4` to your priority list to make it the emitted algo |

## Algorithm choice

The MHL v2.0 schema enumerates these algorithms only:

```
c4    md5    sha1    xxh3    xxh64    xxh128
```

`rclone-migrate`'s `dit` profile is curated to land here every time
(xxh3 / xxh128 / xxh64 / sha1 / md5 / c4 in priority order). Other profiles
work too — `cloud-native`'s `md5`/`sha1` head is MHL-clean, and `forensic`
falls through to `sha1` when `sha256` isn't available — but the `dit`
profile is the most predictable choice when MHL output matters.

If the negotiated algorithm ends up outside this set (e.g. `sha256`
because both backends advertise it and you pinned `hash = "sha256"`), the
emit step warns and skips the relevant side rather than producing a
schema-invalid file.

## See also

- ASC MHL v2.0 spec: <https://github.com/ascmitc/mhl-specification>
- Reference Python implementation: <https://github.com/ascmitc/mhl>
- C4 ID specification: CINE / Cinema Content Creation Cloud
- `docs/profiles.md` — picking a hash profile
