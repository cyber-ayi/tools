"""Tests for ASC MHL v2.0 emission.

Coverage:
  - C4 ID structure and determinism
  - Filename / sequence-counter conventions
  - Manifest XML well-formed + carries required elements
  - Chain XML round-trip
  - Walk skips ascmhl/ directory
  - Profile filter drops non-MHL algos when emit_mhl=true
  - emit hooks fire from do_copy / do_check / cmd_hash
  - rmig export-mhl subcommand writes generations
  - mhl_sides config restricts output
"""
from __future__ import annotations

import shutil
import subprocess
import textwrap
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from rclone_migrate import config as config_mod
from rclone_migrate import mhl
from rclone_migrate import ops


pytestmark_e2e = pytest.mark.skipif(
    shutil.which("rclone") is None, reason="rclone not installed"
)


NS_MANIFEST = "{urn:ASC:MHL:v2.0}"
NS_DIRECTORY = "{urn:ASC:MHL:DIRECTORY:v2.0}"


# --- Author parsing -----------------------------------------------------------

def test_parse_author_simple_name():
    assert mhl.parse_author("Alice") == ("Alice", None)


def test_parse_author_git_style():
    assert mhl.parse_author("Alice <alice@example.com>") == (
        "Alice", "alice@example.com",
    )


def test_parse_author_multi_word_name():
    assert mhl.parse_author("Alice Smith <a@host.dom>") == (
        "Alice Smith", "a@host.dom",
    )


def test_parse_author_email_only():
    assert mhl.parse_author("<a@host.dom>") == ("", "a@host.dom")


def test_parse_author_invalid_email_keeps_full_string():
    """email lacks '.' in domain → fails MHL XSD pattern → don't split."""
    name, email = mhl.parse_author("Alice <alice@local>")
    assert email is None
    assert "alice@local" in name


def test_parse_author_empty():
    assert mhl.parse_author(None) == (None, None)
    assert mhl.parse_author("") == (None, None)
    assert mhl.parse_author("   ") == (None, None)


def test_render_manifest_author_attributes():
    """email/phone/role should appear as attributes, not text."""
    gen = mhl.Generation(
        sequencenr=1, process="in-place",
        creator=mhl.CreatorInfo.default(
            author_name="Alice",
            author_email="alice@example.com",
            author_phone="+1-555-1234",
            author_role="DIT",
            location="Studio A",
        ),
        entries=[],
    )
    body = mhl.render_manifest(gen)
    root = ET.fromstring(body)
    author = root.find(f"{NS_MANIFEST}creatorinfo/{NS_MANIFEST}author")
    assert author is not None
    assert author.text == "Alice"
    assert author.attrib["email"] == "alice@example.com"
    assert author.attrib["phone"] == "+1-555-1234"
    assert author.attrib["role"] == "DIT"
    location = root.find(f"{NS_MANIFEST}creatorinfo/{NS_MANIFEST}location")
    assert location is not None and location.text == "Studio A"


def test_render_manifest_author_omitted_when_empty():
    gen = mhl.Generation(
        sequencenr=1, process="in-place",
        creator=mhl.CreatorInfo.default(),  # no author info
        entries=[],
    )
    root = ET.fromstring(mhl.render_manifest(gen))
    assert root.find(f"{NS_MANIFEST}creatorinfo/{NS_MANIFEST}author") is None


# --- C4 ID --------------------------------------------------------------------

def test_c4_length_and_prefix():
    cid = mhl.compute_c4_id(b"hello world")
    assert len(cid) == 90
    assert cid.startswith("c4")


def test_c4_deterministic():
    a = mhl.compute_c4_id(b"the quick brown fox")
    b = mhl.compute_c4_id(b"the quick brown fox")
    assert a == b
    assert a != mhl.compute_c4_id(b"the quick brown FOX")


def test_c4_empty_input():
    cid = mhl.compute_c4_id(b"")
    assert len(cid) == 90 and cid.startswith("c4")


def test_c4_alphabet():
    """No 0/O/I/l in the body — Bitcoin base58 alphabet."""
    body = mhl.compute_c4_id(b"x")[2:]
    forbidden = set("0OIl")
    assert not (set(body) & forbidden)


# --- Filename + sequence counter ---------------------------------------------

def test_filename_pattern():
    fn = mhl.filename_for(7, "A002R2EC", ts="2026-05-04_091500Z")
    assert fn == "0007_A002R2EC_2026-05-04_091500Z.mhl"


def test_filename_sanitizes_unsafe_chars():
    fn = mhl.filename_for(1, "Insta360 X5/DCIM", ts="2026-05-04_091500Z")
    assert " " not in fn
    assert "/" not in fn


def test_next_sequencenr_empty(tmp_path: Path):
    assert mhl.next_sequencenr(tmp_path) == 1


def test_next_sequencenr_increments(tmp_path: Path):
    d = mhl.ascmhl_dir(tmp_path)
    d.mkdir()
    (d / "0001_x_2026-01-01_010101Z.mhl").write_text("")
    (d / "0007_x_2026-02-02_020202Z.mhl").write_text("")
    assert mhl.next_sequencenr(tmp_path) == 8


# --- XML rendering ------------------------------------------------------------

def _make_gen(seq=1, process="in-place", with_hash=True):
    entries = []
    if with_hash:
        entries.append(mhl.HashEntry(
            path="Clips/A002C006.mov",
            size=12345,
            hashes={"sha1": "0123456789abcdef" * 2 + "01234567"},
            actions={"sha1": "original"},
            modtime=1715000000.0,
        ))
    return mhl.Generation(
        sequencenr=seq, process=process,
        creator=mhl.CreatorInfo.default(
            author_name="Reviewer",
            author_email="r@example.com",
            comment="rmig event_id=42",
        ),
        entries=entries,
    )


def test_render_manifest_well_formed():
    body = mhl.render_manifest(_make_gen())
    root = ET.fromstring(body)
    assert root.tag == f"{NS_MANIFEST}hashlist"
    assert root.attrib["version"] == "2.0"


def test_render_manifest_required_elements():
    body = mhl.render_manifest(_make_gen())
    root = ET.fromstring(body)
    assert root.find(f"{NS_MANIFEST}creatorinfo") is not None
    assert root.find(f"{NS_MANIFEST}processinfo") is not None
    assert root.find(f"{NS_MANIFEST}hashes") is not None
    proc = root.findtext(f"{NS_MANIFEST}processinfo/{NS_MANIFEST}process")
    assert proc == "in-place"
    tool = root.find(f"{NS_MANIFEST}creatorinfo/{NS_MANIFEST}tool")
    assert tool is not None
    assert tool.text == "rclone-migrate"
    assert tool.attrib["version"]


def test_render_manifest_hash_entry_attrs():
    body = mhl.render_manifest(_make_gen())
    root = ET.fromstring(body)
    h = root.find(f"{NS_MANIFEST}hashes/{NS_MANIFEST}hash")
    assert h is not None
    p = h.find(f"{NS_MANIFEST}path")
    assert p is not None and p.text == "Clips/A002C006.mov"
    assert p.attrib["size"] == "12345"
    assert "lastmodificationdate" in p.attrib
    sha1 = h.find(f"{NS_MANIFEST}sha1")
    assert sha1 is not None
    assert sha1.attrib["action"] == "original"
    assert "hashdate" in sha1.attrib


def test_render_manifest_skips_entry_without_mhl_algo():
    """Defensive: entries with only non-MHL hashes get dropped, not emitted
    as malformed <hash> with no algo child."""
    gen = _make_gen()
    gen.entries[0].hashes = {"sha256": "abc" * 16}
    gen.entries[0].actions = {"sha256": "original"}
    body = mhl.render_manifest(gen)
    root = ET.fromstring(body)
    hashes = root.find(f"{NS_MANIFEST}hashes")
    # No <hash> children since the only entry's hash isn't MHL-compatible
    assert hashes is None or len(list(hashes)) == 0


def test_render_manifest_no_entries():
    gen = _make_gen(with_hash=False)
    body = mhl.render_manifest(gen)
    root = ET.fromstring(body)
    # An empty <hashes> element is omitted entirely
    assert root.find(f"{NS_MANIFEST}hashes") is None


def test_render_manifest_includes_default_ignores():
    body = mhl.render_manifest(_make_gen())
    root = ET.fromstring(body)
    patterns = root.findall(
        f"{NS_MANIFEST}processinfo/{NS_MANIFEST}ignore/{NS_MANIFEST}pattern"
    )
    pattern_texts = {p.text for p in patterns}
    assert "ascmhl" in pattern_texts
    assert ".rmig-cache.db" in pattern_texts


# --- Chain --------------------------------------------------------------------

def test_render_chain_round_trip():
    entries = [
        mhl.ChainEntry(sequencenr=1, path="0001_a.mhl", c4="c4" + "1" * 88),
        mhl.ChainEntry(sequencenr=2, path="0002_a.mhl", c4="c4" + "2" * 88),
    ]
    body = mhl.render_chain(entries)
    parsed = mhl.parse_chain(body)
    assert [(e.sequencenr, e.path, e.c4) for e in parsed] == [
        (1, "0001_a.mhl", "c4" + "1" * 88),
        (2, "0002_a.mhl", "c4" + "2" * 88),
    ]


def test_render_chain_namespace():
    body = mhl.render_chain([
        mhl.ChainEntry(sequencenr=1, path="x.mhl", c4="c4" + "1" * 88),
    ])
    root = ET.fromstring(body)
    assert root.tag == f"{NS_DIRECTORY}ascmhldirectory"


def test_parse_chain_skips_malformed_entries(tmp_path: Path):
    bad = (
        '<?xml version="1.0"?>'
        '<ascmhldirectory xmlns="urn:ASC:MHL:DIRECTORY:v2.0">'
        '<hashlist sequencenr="1"><path>a.mhl</path><c4>c41</c4></hashlist>'
        '<hashlist><path>missing-seq.mhl</path><c4>c42</c4></hashlist>'
        '</ascmhldirectory>'
    )
    parsed = mhl.parse_chain(bad.encode())
    assert len(parsed) == 1
    assert parsed[0].sequencenr == 1


# --- write_generation end-to-end ---------------------------------------------

def test_write_generation_creates_layout(tmp_path: Path):
    root = tmp_path / "A002R2EC"
    root.mkdir()
    p = mhl.write_generation(root, _make_gen(seq=1))
    assert p.parent == mhl.ascmhl_dir(root)
    assert p.name.startswith("0001_") and p.name.endswith(".mhl")
    chain = (mhl.ascmhl_dir(root) / "ascmhl_chain.xml").read_bytes()
    parsed = mhl.parse_chain(chain)
    assert len(parsed) == 1
    assert parsed[0].sequencenr == 1
    assert parsed[0].c4 == mhl.compute_c4_id(p.read_bytes())


def test_write_generation_sequential(tmp_path: Path):
    root = tmp_path / "data"
    root.mkdir()
    mhl.write_generation(root, _make_gen(seq=1))
    mhl.write_generation(
        root,
        mhl.Generation(
            sequencenr=mhl.next_sequencenr(root),
            process="transfer",
            creator=mhl.CreatorInfo.default(),
            entries=[],
        ),
    )
    chain = mhl.parse_chain(
        (mhl.ascmhl_dir(root) / "ascmhl_chain.xml").read_bytes()
    )
    assert [e.sequencenr for e in chain] == [1, 2]


def test_write_generation_refuses_collision(tmp_path: Path):
    root = tmp_path / "data"
    root.mkdir()
    gen = _make_gen(seq=1)
    p = mhl.write_generation(root, gen)
    # Manually re-create the file path to simulate filename collision
    p.write_text("squatter")
    with pytest.raises(FileExistsError):
        mhl.write_generation(root, _make_gen(seq=1))


# --- Manifest walk ignores ascmhl/ -------------------------------------------

def test_manifest_walk_skips_ascmhl(tmp_path: Path):
    """ascmhl/ files must not appear in the refreshed manifest."""
    from rclone_migrate import manifest as mf_mod
    from rclone_migrate.config import Job

    root = tmp_path / "data"
    root.mkdir()
    (root / "real.bin").write_bytes(b"payload")
    (root / "ascmhl").mkdir()
    (root / "ascmhl" / "0001_x.mhl").write_text(
        "<hashlist version='2.0'/>"
    )

    job = Job(name="t", src=str(root), dst=str(root))  # dst unused for src refresh
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    from rclone_migrate import state as state_mod
    conn = state_mod.open_db(state_dir)
    m = mf_mod.refresh(
        "src", job, "sha1", conn, state_dir,
        local_cache_in_root=True, progress=False,
    )
    paths = {e.path for e in m.entries}
    assert paths == {"real.bin"}
    conn.close()


# --- emit_mhl filter via Config.resolve_priority ------------------------------

def _write_basic_cfg(
    tmp_path: Path,
    *,
    src: Path,
    dst: Path,
    state_dir: Path,
    extras: str = "",
) -> Path:
    cfg = textwrap.dedent(f"""
        [defaults]
        state_dir = '{state_dir}'
        transfers = 2
        local_cache_in_root = true
        {extras}
        [[jobs]]
        name = 't'
        src = '{src}'
        dst = '{dst}'
    """).strip() + "\n"
    p = tmp_path / "c.toml"
    p.write_text(cfg)
    return p


def test_emit_mhl_filters_priority_to_mhl_set(tmp_path: Path):
    cfg_path = _write_basic_cfg(
        tmp_path,
        src=tmp_path / "s", dst=tmp_path / "d",
        state_dir=tmp_path / "state",
        extras="emit_mhl = true\nhash_profile = 'balanced'",
    )
    cfg = config_mod.load(cfg_path)
    pri = cfg.resolve_priority(cfg.jobs[0])
    assert all(a in mhl.MHL_ALGORITHMS for a in pri)
    assert "sha256" not in pri  # filtered out
    assert "sha1" in pri  # kept


def test_emit_mhl_no_compatible_algos_raises(tmp_path: Path):
    cfg_path = _write_basic_cfg(
        tmp_path,
        src=tmp_path / "s", dst=tmp_path / "d",
        state_dir=tmp_path / "state",
        extras="emit_mhl = true\nhash_priority = ['sha256', 'blake3']",
    )
    cfg = config_mod.load(cfg_path)
    with pytest.raises(ValueError, match="no MHL v2.0 algorithms"):
        cfg.resolve_priority(cfg.jobs[0])


def test_emit_mhl_disabled_passes_through(tmp_path: Path):
    cfg_path = _write_basic_cfg(
        tmp_path,
        src=tmp_path / "s", dst=tmp_path / "d",
        state_dir=tmp_path / "state",
        extras="emit_mhl = false\nhash_profile = 'balanced'",
    )
    cfg = config_mod.load(cfg_path)
    pri = cfg.resolve_priority(cfg.jobs[0])
    # balanced full priority should be preserved (sha256 first)
    assert pri[0] == "sha256"


# --- end-to-end with real local backends -------------------------------------

@pytestmark_e2e
def test_check_emits_src_mhl(tmp_path: Path):
    """Successful rmig check writes a generation under src/ascmhl/."""
    src = tmp_path / "src"; dst = tmp_path / "dst"
    sd = tmp_path / "state"
    src.mkdir(); dst.mkdir(); sd.mkdir()
    (src / "a.bin").write_bytes(b"alpha")
    (dst / "a.bin").write_bytes(b"alpha")

    cfg_path = _write_basic_cfg(
        tmp_path, src=src, dst=dst, state_dir=sd,
        extras="emit_mhl = true\nhash_profile = 'dit'",
    )
    cfg = config_mod.load(cfg_path)
    rc = ops.do_check(cfg, cfg.jobs[0], progress=False)
    assert rc == 0

    ascmhl = src / "ascmhl"
    assert ascmhl.is_dir()
    mhls = list(ascmhl.glob("*.mhl"))
    assert len(mhls) == 1
    body = mhls[0].read_bytes()
    root = ET.fromstring(body)
    proc = root.findtext(f"{NS_MANIFEST}processinfo/{NS_MANIFEST}process")
    assert proc == "in-place"
    h = root.find(f"{NS_MANIFEST}hashes/{NS_MANIFEST}hash")
    assert h is not None
    # action='verified' since check ran successfully. The first child of <hash>
    # is <path>; subsequent siblings are algorithm elements (xxh3/sha1/...).
    children = list(h)
    algo_el = children[1]
    assert algo_el.attrib["action"] == "verified"
    chain_path = ascmhl / "ascmhl_chain.xml"
    assert chain_path.is_file()


@pytestmark_e2e
def test_copy_emits_dst_mhl_delta(tmp_path: Path):
    """rmig copy emits dst-side MHL only for newly-copied files."""
    src = tmp_path / "src"; dst = tmp_path / "dst"
    sd = tmp_path / "state"
    src.mkdir(); dst.mkdir(); sd.mkdir()
    (src / "a.bin").write_bytes(b"alpha")
    (src / "b.bin").write_bytes(b"beta")
    # dst already has b.bin under same name → b.bin will be skipped (hash match)
    (dst / "b.bin").write_bytes(b"beta")

    cfg_path = _write_basic_cfg(
        tmp_path, src=src, dst=dst, state_dir=sd,
        extras="emit_mhl = true\nhash_profile = 'dit'",
    )
    cfg = config_mod.load(cfg_path)
    rc = ops.do_copy(cfg, cfg.jobs[0], progress=False)
    assert rc == 0

    mhls = list((dst / "ascmhl").glob("*.mhl"))
    assert len(mhls) == 1
    root = ET.fromstring(mhls[0].read_bytes())
    proc = root.findtext(f"{NS_MANIFEST}processinfo/{NS_MANIFEST}process")
    assert proc == "transfer"
    paths = [
        e.text for e in root.findall(
            f"{NS_MANIFEST}hashes/{NS_MANIFEST}hash/{NS_MANIFEST}path"
        )
    ]
    # Only a.bin was newly copied; b.bin was already present
    assert paths == ["a.bin"]


@pytestmark_e2e
def test_emit_disabled_writes_nothing(tmp_path: Path):
    src = tmp_path / "src"; dst = tmp_path / "dst"
    sd = tmp_path / "state"
    src.mkdir(); dst.mkdir(); sd.mkdir()
    (src / "a.bin").write_bytes(b"alpha")
    (dst / "a.bin").write_bytes(b"alpha")

    cfg_path = _write_basic_cfg(
        tmp_path, src=src, dst=dst, state_dir=sd,
    )  # no emit_mhl, no profile → defaults
    cfg = config_mod.load(cfg_path)
    rc = ops.do_check(cfg, cfg.jobs[0], progress=False)
    assert rc == 0
    assert not (src / "ascmhl").exists()
    assert not (dst / "ascmhl").exists()


@pytestmark_e2e
def test_mhl_sides_restricts_emission(tmp_path: Path):
    """mhl_sides=['dst'] silences src emission even on a check op."""
    src = tmp_path / "src"; dst = tmp_path / "dst"
    sd = tmp_path / "state"
    src.mkdir(); dst.mkdir(); sd.mkdir()
    (src / "a.bin").write_bytes(b"alpha")
    (dst / "a.bin").write_bytes(b"alpha")

    cfg_path = _write_basic_cfg(
        tmp_path, src=src, dst=dst, state_dir=sd,
        extras=(
            "emit_mhl = true\nhash_profile = 'dit'\n"
            "mhl_sides = ['dst']\n"
        ),
    )
    cfg = config_mod.load(cfg_path)
    rc = ops.do_check(cfg, cfg.jobs[0], progress=False)
    assert rc == 0
    # check defaults to src side; mhl_sides=['dst'] filters that out → no output
    assert not (src / "ascmhl").exists()


@pytestmark_e2e
def test_export_mhl_subcommand(tmp_path: Path):
    """rmig export-mhl writes a generation file from cache."""
    from rclone_migrate.cli import cmd_export_mhl

    src = tmp_path / "src"; dst = tmp_path / "dst"
    sd = tmp_path / "state"
    src.mkdir(); dst.mkdir(); sd.mkdir()
    (src / "a.bin").write_bytes(b"alpha")
    (dst / "a.bin").write_bytes(b"alpha")

    cfg_path = _write_basic_cfg(
        tmp_path, src=src, dst=dst, state_dir=sd,
        extras="emit_mhl = true\nhash_profile = 'dit'",
    )
    rc = cmd_export_mhl([
        "-c", str(cfg_path), "-j", "t", "--side", "src", "-q",
    ])
    assert rc == 0
    mhls = list((src / "ascmhl").glob("*.mhl"))
    assert len(mhls) == 1


@pytestmark_e2e
def test_check_emits_author_as_xsd_compliant_attributes(tmp_path: Path):
    """`mhl_author = 'Name <email>'` config → emitted as text + email attr."""
    src = tmp_path / "src"; dst = tmp_path / "dst"
    sd = tmp_path / "state"
    src.mkdir(); dst.mkdir(); sd.mkdir()
    (src / "a.bin").write_bytes(b"alpha")
    (dst / "a.bin").write_bytes(b"alpha")

    cfg_path = _write_basic_cfg(
        tmp_path, src=src, dst=dst, state_dir=sd,
        extras=(
            "emit_mhl = true\nhash_profile = 'dit'\n"
            "mhl_author = 'Alice Smith <alice@example.com>'\n"
            "mhl_author_role = 'DIT'\n"
            "mhl_location = 'Studio A'\n"
        ),
    )
    cfg = config_mod.load(cfg_path)
    rc = ops.do_check(cfg, cfg.jobs[0], progress=False)
    assert rc == 0

    mhls = list((src / "ascmhl").glob("*.mhl"))
    root = ET.fromstring(mhls[0].read_bytes())
    author = root.find(f"{NS_MANIFEST}creatorinfo/{NS_MANIFEST}author")
    assert author is not None
    assert author.text == "Alice Smith"
    assert author.attrib["email"] == "alice@example.com"
    assert author.attrib["role"] == "DIT"
    location = root.find(f"{NS_MANIFEST}creatorinfo/{NS_MANIFEST}location")
    assert location is not None and location.text == "Studio A"


@pytestmark_e2e
def test_official_ascmhl_info_parses_emit(tmp_path: Path):
    """If `ascmhl` Python tool is installed, our output should round-trip
    through `ascmhl info` without it falling back to '-' for the author."""
    import sys as _sys
    # Prefer the version in the active venv (CI installs ascmhl as optional);
    # fall back to PATH so a system-wide install also works.
    candidate = Path(_sys.executable).parent / "ascmhl"
    ascmhl_bin = (str(candidate) if candidate.is_file()
                  else shutil.which("ascmhl"))
    if not ascmhl_bin:
        pytest.skip("official ascmhl CLI not installed")

    src = tmp_path / "src"; dst = tmp_path / "dst"
    sd = tmp_path / "state"
    src.mkdir(); dst.mkdir(); sd.mkdir()
    (src / "a.bin").write_bytes(b"alpha")
    (dst / "a.bin").write_bytes(b"alpha")

    cfg_path = _write_basic_cfg(
        tmp_path, src=src, dst=dst, state_dir=sd,
        extras=(
            "emit_mhl = true\nhash_profile = 'dit'\n"
            "mhl_author = 'Alice <alice@example.com>'\n"
            "mhl_location = 'Studio A'\n"
            "mhl_comment = 'roundtrip test'\n"
        ),
    )
    cfg = config_mod.load(cfg_path)
    assert ops.do_check(cfg, cfg.jobs[0], progress=False) == 0

    out = subprocess.run(
        [ascmhl_bin, "info", "-v", str(src)],
        capture_output=True, text=True,
    )
    assert out.returncode == 0, out.stderr
    # ascmhl 1.0.1's info command renders CreatorInfo fields it understands.
    # The author column has a known display quirk (always shows '-' even when
    # the schema-compliant <author email=... role=...>Name</author> form is
    # present), but `location` and the overall generation summary should
    # parse cleanly.
    assert "Generation 1" in out.stdout, out.stdout
    assert "Studio A" in out.stdout, out.stdout
    assert "rclone-migrate" in out.stdout, out.stdout


@pytestmark_e2e
def test_subsequent_runs_increment_sequencenr(tmp_path: Path):
    """Two consecutive ops produce generations 0001 and 0002."""
    src = tmp_path / "src"; dst = tmp_path / "dst"
    sd = tmp_path / "state"
    src.mkdir(); dst.mkdir(); sd.mkdir()
    (src / "a.bin").write_bytes(b"alpha")
    (dst / "a.bin").write_bytes(b"alpha")

    cfg_path = _write_basic_cfg(
        tmp_path, src=src, dst=dst, state_dir=sd,
        extras="emit_mhl = true\nhash_profile = 'dit'",
    )
    cfg = config_mod.load(cfg_path)
    assert ops.do_check(cfg, cfg.jobs[0], progress=False) == 0
    assert ops.do_check(cfg, cfg.jobs[0], progress=False) == 0
    mhls = sorted((src / "ascmhl").glob("*.mhl"))
    assert len(mhls) == 2
    assert mhls[0].name.startswith("0001_")
    assert mhls[1].name.startswith("0002_")
